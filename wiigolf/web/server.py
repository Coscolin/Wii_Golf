"""
Servidor web local para la Wii Balance Board (una o dos tablas).

- Un hilo orquestador busca tablas cada 3 s y arranca un hilo lector por cada una
  (~100 Hz). Con UNA tabla todo funciona como siempre. Con DOS (una por pie) el
  modo pasa solo a "dual": cada informe de la tabla izquierda genera una muestra
  fusionada con la última lectura de la derecha (≤ 10 ms), con carga y CoP por
  pie y CoP global (ver dual.py). Si una tabla se apaga se vuelve a una tabla.
- Se aplica la tara (por tabla), se graba a CSV si hay una grabación activa y se
  alimenta la captura automática de swings.
- Micrófono y webcam (opcionales) capturan en continuo con el mismo reloj que
  la tabla; al guardar un swing se recorta su tramo (WAV + fotogramas JPEG) y
  se busca el golpe a la bola en el audio para marcar el impacto.
- El navegador recibe las muestras por WebSocket (~50 Hz) y controla todo por
  una API REST: grabación, tara, ajustes, análisis, swing de referencia,
  exportar / importar, emparejado Bluetooth y colocación de las dos tablas.
- Cada swing son varios archivos en data/: <n>.csv (tabla), <n>.json (meta y
  eventos, incluida la colocación de las tablas), <n>.wav (audio),
  <n>.frames.zip (vídeo) y out/<n>.png (figura).

Arranque:  python scripts/04_web.py [--host 0.0.0.0] [--port 8000] [--sim [N]]

Captura automática: con el modo armado se guarda un buffer de los últimos
segundos; cuando el % de peso en el pie trail cae ≥ AUTO_DROP_PCT puntos en
menos de AUTO_WINDOW_S (el downswing), se espera AUTO_POST_S y se guarda
data/auto_<fecha>.csv con el tramo [disparo - AUTO_PRE_S, disparo + AUTO_POST_S],
que se analiza al vuelo.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import media

# Emparejado Bluetooth de la tabla: una implementación por sistema con la misma API
if sys.platform == "win32":
    from .. import bt_win as bt
elif sys.platform == "darwin":
    from .. import bt_mac as bt
else:
    bt = None
from ..analysis import DUAL_HEADER, SINGLE_HEADER, SwingAnalysis, analyze, load_csv, plot
from ..balance_board import MIN_LOAD_KG, SENSORS, BalanceBoard
from ..dual import (BOARD_PHYS_DEPTH_CM, BOARD_PHYS_WIDTH_CM, BUTTON_SIDES, HALF_X_CM, HALF_Y_CM,
                    Layout, combine, spread_foot_load)


def _base_dir() -> Path:
    """Carpeta de trabajo (data/, out/): la raíz del repo al ejecutar los scripts,
    la carpeta del .exe si va empaquetado con PyInstaller. WIIGOLF_HOME la fuerza."""
    env = os.environ.get("WIIGOLF_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


ROOT = _base_dir()
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
STATIC_DIR = Path(__file__).parent / "static"
SETTINGS_FILE = DATA_DIR / "settings.json"
REFERENCE_FILE = DATA_DIR / "reference.json"

CSV_HEADER = SINGLE_HEADER
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
IMPORT_ENTRY_RE = re.compile(r"^([A-Za-z0-9_-]{1,60})\.(csv|json|wav|frames\.zip|png)$")

STANDING_KG = 20.0       # por debajo, no hay nadie sobre la tabla
BUFFER_S = 10.0          # segundos que guarda el buffer circular de la tabla
MEDIA_BUFFER_S = 20.0    # segundos de audio/vídeo en memoria
AUTO_DROP_PCT = 25.0     # caída de trail% que dispara la captura automática
AUTO_WINDOW_S = 0.5      # ... medida en esta ventana
AUTO_PRE_S = 3.0         # segundos guardados antes del disparo
AUTO_POST_S = 1.5        # segundos guardados después del disparo
AUTO_REFRACTORY_S = 2.5  # tiempo mínimo entre capturas
AUTO_FORCE_MIN = 1.12    # y un pico de fuerza vertical >= 112 % del peso corporal...
AUTO_FORCE_WINDOW_S = 0.4  # ...en los últimos 0,4 s (el rebote tras el swing no lo tiene)

MAX_BOARDS = 2           # una por pie
SCAN_S = 3.0             # cada cuánto se buscan tablas nuevas
BOARD_TIMEOUT_S = 2.0    # sin informes durante este tiempo = la tabla se ha apagado
STALE_S = 0.5            # (dual) la otra tabla lleva demasiado sin datos: cuenta como 0 kg

DEFAULT_SETTINGS = {"handed": "right", "flip_x": False, "flip_y": False,
                    "audio": False, "video": False, "audio_device": None, "camera": 0,
                    "body_weight_kg": None,   # fijado por el asistente; referencia del % de fuerza
                    # dos tablas: hueco entre bordes interiores (None = sin indicar), lado hacia
                    # el que apunta el botón de encendido y qué tabla está a la izquierda
                    "dual_gap_cm": None, "dual_button": "left", "dual_left_key": None}


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return {**DEFAULT_SETTINGS, **{k: data[k] for k in DEFAULT_SETTINGS if k in data}}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def apply_tare(kg_raw: dict, tare: dict | None) -> dict:
    tare = tare or {}
    return {s: max(0.0, kg_raw[s] - tare.get(s, 0.0)) for s in SENSORS}


def make_sample(t: float, kg: dict, raw: dict) -> dict:
    """Muestra de UNA tabla. `kg` ya tarado; `raw` = {clave de la tabla: lectura sin tarar}."""
    total = sum(kg.values())
    if total >= MIN_LOAD_KG:
        cx = ((kg["TR"] + kg["BR"]) - (kg["TL"] + kg["BL"])) / total
        cy = ((kg["TL"] + kg["TR"]) - (kg["BL"] + kg["BR"])) / total
    else:
        cx = cy = 0.0
    return {"mode": "single", "t": t, "kg": kg, "raw": raw, "total": total, "cop_x": cx, "cop_y": cy}


def make_dual_sample(t: float, kg_l: dict, kg_r: dict, raw: dict, layout: Layout,
                     t_l: float, t_r: float | None, stale: str | None) -> dict:
    """Muestra fusionada de DOS tablas (kg ya tarados). CoP en cm, sistema del golfista."""
    c = combine(kg_l, kg_r, layout)
    return {"mode": "dual", "t": t, "total": c["total"], "cop_x_cm": c["cop_x_cm"], "cop_y_cm": c["cop_y_cm"],
            "L": {"kg": kg_l, "t": t_l}, "R": {"kg": kg_r, "t": t_r},
            "feet": {"L": c["L"], "R": c["R"]}, "raw": raw, "stale": stale}


def trail_pct(sample: dict, settings: dict) -> float | None:
    total = sample["total"]
    if total < MIN_LOAD_KG:
        return None
    if sample.get("mode") == "dual":
        side = "R" if settings["handed"] == "right" else "L"   # flip_x no se aplica en dual
        return sample["feet"][side]["kg"] / total * 100.0
    kg = sample["kg"]
    right, left = kg["TR"] + kg["BR"], kg["TL"] + kg["BL"]
    if settings["flip_x"]:
        right, left = left, right
    trail = right if settings["handed"] == "right" else left
    return trail / total * 100.0


def csv_header(mode: str) -> list[str]:
    return DUAL_HEADER if mode == "dual" else SINGLE_HEADER


def csv_row(sample: dict, t0: float) -> list[str]:
    if sample.get("mode") == "dual":
        L, R = sample["L"]["kg"], sample["R"]["kg"]
        t_r = sample["R"]["t"]
        return ([f"{sample['t'] - t0:.4f}"] + [f"{L[s]:.2f}" for s in SENSORS] + [f"{R[s]:.2f}" for s in SENSORS]
                + [f"{sample['total']:.2f}", f"{sample['cop_x_cm']:.3f}", f"{sample['cop_y_cm']:.3f}",
                   f"{sample['L']['t'] - t0:.4f}", "" if t_r is None else f"{t_r - t0:.4f}"])
    kg = sample["kg"]
    return [f"{sample['t'] - t0:.4f}", f"{kg['TR']:.2f}", f"{kg['BR']:.2f}",
            f"{kg['TL']:.2f}", f"{kg['BL']:.2f}", f"{sample['total']:.2f}",
            f"{sample['cop_x']:.4f}", f"{sample['cop_y']:.4f}"]


def write_csv(path: Path, samples: list[dict]) -> str:
    """Escribe el CSV con el modo de la última muestra (las de otro modo se descartan).
    Devuelve el modo escrito."""
    mode = samples[-1].get("mode", "single") if samples else "single"
    rows = [s for s in samples if s.get("mode", "single") == mode]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(csv_header(mode))
        if rows:
            t0 = rows[0]["t"]
            w.writerows(csv_row(s, t0) for s in rows)
    return mode


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError("Demasiados archivos con ese nombre")


def next_name() -> str:
    nums = []
    for p in DATA_DIR.glob("swing_*.csv"):
        m = re.fullmatch(r"swing_(\d+)", p.stem)
        if m:
            nums.append(int(m.group(1)))
    return f"swing_{(max(nums) + 1) if nums else 1:03d}"


def sanitize_name(name: str | None) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_")[:60]
    return name or next_name()


def json_clean(o):
    """NaN/inf -> None y escalares numpy -> Python, para poder serializar."""
    if isinstance(o, dict):
        return {k: json_clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [json_clean(v) for v in o]
    if hasattr(o, "item"):
        o = o.item()
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Archivos de un swing, meta y referencia
# --------------------------------------------------------------------------- #
def swing_paths(name: str) -> dict:
    return {"csv": DATA_DIR / f"{name}.csv", "json": DATA_DIR / f"{name}.json",
            "wav": DATA_DIR / f"{name}.wav", "frames": DATA_DIR / f"{name}.frames.zip",
            "png": OUT_DIR / f"{name}.png"}


def load_meta(name: str) -> dict:
    try:
        return json.loads(swing_paths(name)["json"].read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_meta(name: str, meta: dict) -> None:
    swing_paths(name)["json"].write_text(json.dumps(json_clean(meta), indent=1, ensure_ascii=False),
                                         encoding="utf-8")


def get_reference() -> str | None:
    try:
        name = json.loads(REFERENCE_FILE.read_text(encoding="utf-8")).get("name")
    except Exception:
        return None
    return name if name and NAME_RE.match(name) and swing_paths(name)["csv"].exists() else None


def set_reference(name: str | None) -> None:
    if name:
        REFERENCE_FILE.write_text(json.dumps({"name": name}), encoding="utf-8")
    elif REFERENCE_FILE.exists():
        REFERENCE_FILE.unlink()


def list_swings() -> list[dict]:
    ref = get_reference()
    items = []
    for p in sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        paths = swing_paths(p.stem)
        meta = load_meta(p.stem)
        ev = meta.get("events") or {}
        items.append({
            "name": p.stem, "size": p.stat().st_size, "mtime": p.stat().st_mtime,
            "analyzed": paths["png"].exists(),
            "png": f"/out/{p.stem}.png?v={int(paths['png'].stat().st_mtime)}" if paths["png"].exists() else None,
            "audio": paths["wav"].exists(), "video": paths["frames"].exists(),
            "duration": meta.get("duration"), "source": meta.get("source"),
            "impact_source": ev.get("impact_source"), "is_reference": p.stem == ref,
            "mode": (meta.get("boards") or {}).get("mode"),
        })
    return items


# --------------------------------------------------------------------------- #
# Grabación
# --------------------------------------------------------------------------- #
class Recorder:
    """Graba muestras a CSV. El modo (una / dos tablas) queda fijado al empezar; si a
    mitad cambia (se apaga o se enciende una tabla) esas muestras se descartan."""

    def __init__(self, path: Path, mode: str = "single"):
        self.path = path
        self.mode = mode
        self._f = path.open("w", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        self._w.writerow(csv_header(mode))
        self.t0: float | None = None
        self.last_t: float | None = None
        self.n = 0
        self.dropped = 0
        self.started = time.time()

    def write(self, sample: dict) -> None:
        if sample.get("mode", "single") != self.mode:
            self.dropped += 1
            return
        if self.t0 is None:
            self.t0 = sample["t"]
        self.last_t = sample["t"]
        self._w.writerow(csv_row(sample, self.t0))
        self.n += 1

    def close(self) -> dict:
        self._f.close()
        dur = (self.last_t - self.t0) if (self.t0 is not None and self.last_t is not None) else 0.0
        return {"name": self.path.stem, "samples": self.n, "duration": round(dur, 2),
                "t0": self.t0, "t_end": self.last_t, "mode": self.mode, "dropped": self.dropped}


# --------------------------------------------------------------------------- #
# Tabla simulada (para desarrollar sin hardware)
# --------------------------------------------------------------------------- #
class SimState:
    """Reloj y presencia compartidos por las tablas simuladas (para que los dos pies
    vayan sincronizados y el asistente pueda 'subir' y 'bajar' al golfista)."""

    def __init__(self, bw_kg: float = 80.0):
        self.bw = bw_kg
        self.t0 = time.perf_counter()
        self.present = True   # False = nadie sobre la tabla (para probar el asistente)

    def set_present(self, present: bool) -> None:
        if present and not self.present:
            self.t0 = time.perf_counter()   # al subirse empieza en el address (quieto 2,5 s)
        self.present = present


class SimBoard:
    """Reproduce en bucle un swing de diestro (address, top, downswing, finish).

    `foot` None = una sola tabla bajo ambos pies (comportamiento clásico).
    `foot` "L"/"R" = la tabla de ese pie en modo dual (giradas 90°): la carga del pie y
    su CoP punta-talón / lateral siguen un perfil realista (el trail rueda a la punta
    en el top, el lead hunde el talón en el impacto y pasa a la punta en el finish)."""

    # (t, trail %, fuerza % peso, y_norm)  y_norm: -1 talón .. +1 punta   (una tabla)
    KEYS = [
        (0.0, 52, 100, -0.1), (2.5, 52, 100, -0.1), (3.3, 72, 95, 0.15),
        (3.55, 40, 130, -0.2), (3.7, 22, 92, -0.3), (4.7, 12, 100, -0.2),
        (6.5, 12, 100, -0.2), (8.0, 52, 100, -0.1),
    ]
    # (t, trail %, fuerza %, y_trail, y_lead, x_trail, x_lead) en cm (+ punta / + derecha)
    FOOT_KEYS = [
        (0.0, 52, 100, -1, -1, 0, 0), (2.5, 52, 100, -1, -1, 0, 0), (3.3, 72, 95, 6, -3, 2, -1),
        (3.55, 40, 130, 2, -6, 1, -2), (3.7, 22, 92, -2, -4, 0, -1), (4.7, 12, 100, -4, 4, -1, 1),
        (6.5, 12, 100, -4, 6, -1, 2), (8.0, 52, 100, -1, -1, 0, 0),
    ]
    PERIOD = 8.0

    def __init__(self, state: SimState | None = None, foot: str | None = None, layout_fn=None):
        self.state = state or SimState()
        self.foot = foot
        self.layout_fn = layout_fn or (lambda: Layout(gap_cm=0.0))
        self._next = time.perf_counter()

    @property
    def present(self) -> bool:
        return self.state.present

    def set_present(self, present: bool) -> None:
        self.state.set_present(present)

    def _interp(self, u: float) -> tuple:
        keys = self.FOOT_KEYS if self.foot else self.KEYS
        for k0, k1 in zip(keys, keys[1:]):
            if k0[0] <= u < k1[0]:
                w = (u - k0[0]) / (k1[0] - k0[0])
                w = w * w * (3 - 2 * w)
                return tuple(a + (b - a) * w for a, b in zip(k0[1:], k1[1:]))
        return tuple(keys[-1][1:])

    def read_raw(self) -> tuple[float, dict]:
        self._next += 0.01
        delay = self._next - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        t = time.perf_counter()
        if not self.state.present:
            return t, {k: max(0.0, random.gauss(0, 0.05)) for k in SENSORS}
        vals = self._interp((t - self.state.t0) % self.PERIOD)
        bw = self.state.bw
        if self.foot is None:
            trail, force, y = vals
            total = force / 100.0 * bw
            right = total * trail / 100.0
            left = total - right
            ft = 0.5 + y / 2.0
            kg = {"TR": right * ft, "BR": right * (1 - ft), "TL": left * ft, "BL": left * (1 - ft)}
        else:
            trail, force, y_tr, y_ld, x_tr, x_ld = vals
            total = force / 100.0 * bw
            kg_trail = total * trail / 100.0
            if self.foot == "R":   # diestro simulado: trail = tabla derecha
                kg = spread_foot_load(kg_trail, x_tr, y_tr, self.layout_fn())
            else:
                kg = spread_foot_load(total - kg_trail, x_ld, y_ld, self.layout_fn())
        return t, {k: max(0.0, v + random.gauss(0, 0.15)) for k, v in kg.items()}


# --------------------------------------------------------------------------- #
# Micrófono y webcam
# --------------------------------------------------------------------------- #
class MediaManager:
    """Arranca/para el micrófono y la webcam según los ajustes y recorta clips."""

    def __init__(self):
        self.audio: media.AudioRecorder | None = None
        self.video: media.VideoRecorder | None = None
        self.audio_error: str | None = None
        self.video_error: str | None = None
        self.lock = threading.Lock()

    def apply(self, settings: dict) -> None:
        with self.lock:
            want = bool(settings.get("audio"))
            dev = settings.get("audio_device")
            if want and (self.audio is None or not self.audio.running or self.audio.device != dev):
                if self.audio:
                    self.audio.stop()
                self.audio = media.AudioRecorder(buffer_s=MEDIA_BUFFER_S, device=dev)
                try:
                    self.audio.start()
                    self.audio_error = None
                except Exception as exc:
                    self.audio_error = str(exc)
                    self.audio = None
            elif not want and self.audio is not None:
                self.audio.stop()
                self.audio = None
                self.audio_error = None

            want = bool(settings.get("video"))
            cam = int(settings.get("camera") or 0)
            if want and (self.video is None or not self.video.running or self.video.device != cam):
                if self.video:
                    self.video.stop()
                self.video = media.VideoRecorder(buffer_s=MEDIA_BUFFER_S, device=cam)
                try:
                    self.video.start()
                    self.video_error = None
                except Exception as exc:
                    self.video_error = str(exc)
                    self.video = None
            elif not want and self.video is not None:
                self.video.stop()
                self.video = None
                self.video_error = None

    def stop(self) -> None:
        with self.lock:
            if self.audio:
                self.audio.stop()
            if self.video:
                self.video.stop()
            self.audio = self.video = None

    def status(self) -> dict:
        a, v = self.audio, self.video
        return {
            "audio": {"available": media.AUDIO_AVAILABLE, "running": bool(a and a.running),
                      "device": a.device_name if a else None, "level": round(a.level, 4) if a else 0.0,
                      "error": self.audio_error or (a.error if a else None) or media.AUDIO_ERROR},
            "video": {"available": media.VIDEO_AVAILABLE, "running": bool(v and v.running),
                      "fps": round(v.fps_measured, 1) if v else 0.0, "size": list(v.size) if v else None,
                      "error": self.video_error or (v.error if v else None) or media.VIDEO_ERROR},
        }

    def snapshot(self) -> bytes | None:
        return self.video.snapshot() if self.video else None

    def save_clip(self, name: str, t0: float, t_end: float) -> tuple[dict | None, dict | None]:
        paths = swing_paths(name)
        audio_info = video_info = None
        if self.audio and self.audio.running:
            try:
                audio_info = self.audio.save(paths["wav"], t0, t_end, t0)
            except Exception as exc:
                audio_info = {"error": str(exc)}
        if self.video and self.video.running:
            try:
                video_info = self.video.save(paths["frames"], t0, t_end, t0)
            except Exception as exc:
                video_info = {"error": str(exc)}
        return audio_info, video_info


# --------------------------------------------------------------------------- #
# Hilos lectores: uno por tabla + el orquestador
# --------------------------------------------------------------------------- #
class _BoardWorker(threading.Thread):
    """Abre, inicializa, calibra y lee UNA tabla; entrega cada lectura al BoardReader.
    Termina (y el orquestador lo relanza) si la tabla falla o deja de enviar datos."""

    def __init__(self, reader: "BoardReader", info: dict, sim: SimBoard | None):
        super().__init__(daemon=True, name=f"board-{str(info['key'])[-12:]}")
        self.reader = reader
        self.info = info
        self.key = info["key"]
        self.sim = sim
        self.blink = False
        self.stop_event = threading.Event()

    def run(self) -> None:
        err = None
        try:
            if self.sim is not None:
                self._run_sim()
            else:
                self._run_real()
        except Exception as exc:
            err = str(exc)
        finally:
            self.reader._worker_ended(self.key, err)

    def _run_sim(self) -> None:
        self.reader._worker_ready(self.key)
        n, t_hz = 0, time.perf_counter()
        while not self.stop_event.is_set():
            self.blink = False
            t, kg = self.sim.read_raw()
            self.reader._ingest_board(self.key, t, kg)
            n += 1
            if t - t_hz >= 1.0:
                self.reader._set_hz(self.key, n / (t - t_hz))
                n, t_hz = 0, t

    def _run_real(self) -> None:
        label = self.info.get("serial") or self.info.get("product") or "HID"
        board = BalanceBoard(path=self.info["path"])
        try:
            board.open()
        except Exception as exc:
            raise RuntimeError(f"No se pudo abrir la tabla {label} ({exc}). Si acabas de emparejarla, espera "
                               "unos segundos; si sigue, apágala y enciéndela con su botón.") from exc
        try:
            board.init()
            board.calibrate()
            print(f"  [tabla] conectada: {self.info.get('product') or 'HID'} serie={self.info.get('serial') or '?'}",
                  flush=True)
            self.reader._worker_ready(self.key)
            last = time.perf_counter()
            n, t_hz = 0, last
            while not self.stop_event.is_set():
                if self.blink:
                    self.blink = False
                    try:
                        board.identify(times=3, period_s=0.15)
                    except Exception:
                        pass
                    last = time.perf_counter()
                r = board.read(timeout_ms=200)
                now = time.perf_counter()
                if r is None:
                    if now - last > BOARD_TIMEOUT_S:
                        raise RuntimeError(f"La tabla {label} dejó de enviar datos (¿se ha apagado?)")
                    continue
                last = now
                self.reader._ingest_board(self.key, r.t, r.kg)
                n += 1
                if now - t_hz >= 1.0:
                    self.reader._set_hz(self.key, n / (now - t_hz))
                    n, t_hz = 0, now
        finally:
            board.close()


class BoardReader(threading.Thread):
    """Orquestador: busca tablas, mantiene un _BoardWorker por tabla (máx. 2), fusiona
    las lecturas en muestras, aplica la tara, graba y detecta swings."""

    def __init__(self, sim: int, settings: dict):
        super().__init__(daemon=True, name="board-reader")
        self.sim = int(sim or 0)
        self.settings = settings
        self.layout = Layout.from_settings(settings)
        self.stop_event = threading.Event()
        self._retry = threading.Event()
        self.lock = threading.Lock()
        self.latest: dict | None = None
        self.error: str | None = None
        # por tabla: {"key","serial","product","path","connected","hz","latest": (t, kg, raw)|None, "order"}
        self.boards: dict[str, dict] = {}
        self.workers: dict[str, _BoardWorker] = {}
        self._order = 0
        self.mode = "single"
        self.left_key: str | None = None     # tabla que marca el reloj (la izquierda en dual, la única en single)
        self.right_key: str | None = None
        self._last_t: float | None = None
        self.tare: dict[str, dict] = {}
        self.tared: set[str] = set()
        self.buffer: deque = deque(maxlen=int(BUFFER_S * 100))
        self.recorder: Recorder | None = None
        self.auto_armed = False
        self.auto_count = 0
        self._auto_trigger_t: float | None = None
        self._auto_last = -1e9
        self._bw_est: float | None = None   # peso corporal estimado (mediana del buffer)
        self._bw_counter = 0
        self.events: queue.Queue = queue.Queue()
        self._sim_state: SimState | None = None
        self._printed: dict[str, str] = {}   # último motivo impreso por tabla (no repetir)

    # ---- bucle del orquestador ------------------------------------------ #
    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._scan()
            except Exception as exc:
                self._print_once("scan", str(exc))
            self._retry.wait(SCAN_S)
            self._retry.clear()
        for w in list(self.workers.values()):
            w.stop_event.set()

    def retry_now(self) -> None:
        self._retry.set()

    def _print_once(self, key: str, msg: str) -> None:
        if self._printed.get(key) != msg:
            self._printed[key] = msg
            print(f"  [tabla] {msg}", flush=True)

    def _scan(self) -> None:
        if self.sim:
            if not self.workers:
                self._sim_state = SimState()
                if self.sim >= 2:
                    infos = [({"key": "SIM-L", "serial": "SIM-L", "product": "Tabla simulada (izquierda)", "path": None}, "L"),
                             ({"key": "SIM-R", "serial": "SIM-R", "product": "Tabla simulada (derecha)", "path": None}, "R")]
                else:
                    infos = [({"key": "SIM", "serial": "SIM", "product": "Tabla simulada", "path": None}, None)]
                for info, foot in infos:
                    self._start_worker(info, SimBoard(self._sim_state, foot, lambda: self.layout))
            return
        with self.lock:
            alive = [k for k, w in self.workers.items() if w.is_alive()]
        if len(alive) >= MAX_BOARDS:
            return
        found = BalanceBoard.enumerate_boards()   # fuera del lock: puede tardar
        if not found and not alive:
            msg = ("Tabla no encontrada. Pulsa su botón de encendido; si no aparece, "
                   "usa 'Emparejar' en el panel de la tabla.")
            with self.lock:
                self.error = msg
            self._print_once("scan", msg)
            return
        new = [b for b in found if b["key"] not in alive]
        for b in new[: MAX_BOARDS - len(alive)]:
            self._start_worker(b, None)
        if len(found) > MAX_BOARDS:
            self._print_once("scan", f"hay {len(found)} tablas; se usan las dos primeras")

    def _start_worker(self, info: dict, sim: SimBoard | None) -> None:
        with self.lock:
            self.boards[info["key"]] = {"key": info["key"], "serial": info.get("serial"), "product": info.get("product"),
                                        "path": info.get("path"), "connected": False, "hz": 0.0, "latest": None,
                                        "order": self._order}
            self._order += 1
            w = _BoardWorker(self, info, sim)
            self.workers[info["key"]] = w
        w.start()

    def _worker_ready(self, key: str) -> None:
        with self.lock:
            b = self.boards.get(key)
            if b is None:
                return
            b["connected"] = True
            self.error = None
            self._update_mode()
        self._printed.pop(key, None)

    def _worker_ended(self, key: str, err: str | None) -> None:
        with self.lock:
            self.workers.pop(key, None)
            self.boards.pop(key, None)
            self.tare.pop(key, None)
            self.tared.discard(key)
            self._update_mode()
            if not self.boards:
                self.error = err or "Tabla desconectada"
        if err:
            self._print_once(key, err)

    def _set_hz(self, key: str, hz: float) -> None:
        with self.lock:
            if key in self.boards:
                self.boards[key]["hz"] = hz

    def _update_mode(self) -> None:
        """(con el lock) Recalcula modo y asignación izquierda/derecha; al cambiar, vacía el
        buffer (no puede mezclar formas de muestra) y avisa al navegador al momento."""
        connected = sorted((b for b in self.boards.values() if b["connected"]), key=lambda b: b["order"])
        keys = [b["key"] for b in connected]
        if len(keys) >= 2:
            mode = "dual"
            pref = self.settings.get("dual_left_key")
            left = pref if pref in keys else keys[0]
            if pref != left:   # la primera vez: se recuerda para que L/R sea estable entre arranques
                self.settings["dual_left_key"] = left
                try:
                    save_settings(self.settings)
                except Exception:
                    pass
            right = next(k for k in keys if k != left)
        else:
            mode, left, right = "single", (keys[0] if keys else None), None
        if (mode, left, right) != (self.mode, self.left_key, self.right_key):
            self.mode, self.left_key, self.right_key = mode, left, right
            self.buffer.clear()
            self.latest = None
            self._last_t = None
            self._bw_est, self._bw_counter = None, 0
            self._auto_trigger_t = None
            self.events.put({"type": "mode", "mode": mode})
            print(f"  [tabla] modo: {'dos tablas (una por pie)' if mode == 'dual' else 'una tabla'}"
                  + (f"  izquierda={left} derecha={right}" if mode == "dual" else ""), flush=True)

    # ---- entrada de lecturas -------------------------------------------- #
    def _ingest_board(self, key: str, t: float, kg_raw: dict) -> None:
        with self.lock:
            b = self.boards.get(key)
            if b is None or not b["connected"]:
                return
            kg = apply_tare(kg_raw, self.tare.get(key))
            b["latest"] = (t, kg, kg_raw)
            if key != self.left_key:
                return   # el reloj lo marca la tabla izquierda (o la única); la otra se retiene
            if self._last_t is not None and t <= self._last_t:
                t = self._last_t + 1e-4   # t monótono aunque los hilos entreguen fuera de orden
            self._last_t = t
            if self.mode == "dual":
                other = self.boards.get(self.right_key)
                lo = other["latest"] if other else None
                stale = lo is None or (t - lo[0]) > STALE_S
                kg_r = {s: 0.0 for s in SENSORS} if stale else lo[1]
                raw = {key: kg_raw}
                if not stale:
                    raw[self.right_key] = lo[2]
                s = make_dual_sample(t, kg, kg_r, raw, self.layout, t, None if stale else lo[0],
                                     self.right_key if stale else None)
            else:
                s = make_sample(t, kg, {key: kg_raw})
            self.latest = s
            self.buffer.append(s)
            if self.recorder:
                self.recorder.write(s)
            self._bw_counter += 1
            if self._bw_counter >= 100:   # una vez por segundo
                self._bw_counter = 0
                totals = sorted(x["total"] for x in self.buffer if x["total"] >= STANDING_KG)
                self._bw_est = totals[len(totals) // 2] if len(totals) >= 100 else None
            self._auto_step(s)

    # ---- captura automática (se llama con el lock cogido) --------------- #
    def _auto_step(self, s: dict) -> None:
        if not self.auto_armed:
            return
        t = s["t"]
        if self._auto_trigger_t is not None:
            if t >= self._auto_trigger_t + AUTO_POST_S:
                self._auto_save()
            return
        if s.get("stale"):
            return   # una tabla sin datos hunde el trail% y dispararía una captura falsa
        if s["total"] < STANDING_KG or t - self._auto_last < AUTO_REFRACTORY_S:
            return
        tp = trail_pct(s, self.settings)
        bw = self._bw_est
        if tp is None or bw is None:
            return
        mx, fmax = None, 0.0
        for prev in reversed(self.buffer):
            age = t - prev["t"]
            if age > AUTO_WINDOW_S:
                break
            if prev["total"] < STANDING_KG or prev.get("stale"):
                return  # no ha estado de pie (con las dos tablas) durante toda la ventana
            v = trail_pct(prev, self.settings)
            if v is not None and (mx is None or v > mx):
                mx = v
            if age <= AUTO_FORCE_WINDOW_S and prev["total"] > fmax:
                fmax = prev["total"]
        if mx is not None and mx - tp >= AUTO_DROP_PCT and fmax >= AUTO_FORCE_MIN * bw:
            self._auto_trigger_t = t
            self._auto_last = t
            self.events.put({"type": "auto_trigger"})

    def _auto_save(self) -> None:
        t_trig = self._auto_trigger_t
        self._auto_trigger_t = None
        samples = [x for x in self.buffer if x["t"] >= t_trig - AUTO_PRE_S]
        if not samples:
            return
        name = "auto_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = unique_path(DATA_DIR / f"{name}.csv")
        mode = write_csv(path, samples)
        self.auto_count += 1
        self.events.put({"type": "auto_saved", "name": path.stem,
                         "t0": samples[0]["t"], "t_end": samples[-1]["t"], "boards": self._boards_meta(mode)})

    def _boards_meta(self, mode: str) -> dict:
        """(con el lock) Qué tablas y colocación se usaron: se congela en la meta del swing."""
        return {"mode": mode, "left_key": self.left_key, "right_key": self.right_key,
                "layout": self.layout.to_dict()}

    # ---- API usada por el servidor -------------------------------------- #
    def set_tare(self) -> dict:
        with self.lock:
            if self.latest is None:
                raise RuntimeError("Sin datos de la tabla para tarar")
            t_now = self.latest["t"]
            recent = [x for x in self.buffer if t_now - x["t"] <= 0.5]
            done = {}
            for k, b in self.boards.items():
                if not b["connected"]:
                    continue
                vals = [x["raw"][k] for x in recent if k in x["raw"]]
                if not vals:
                    continue
                self.tare[k] = {s: sum(v[s] for v in vals) / len(vals) for s in SENSORS}
                self.tared.add(k)
                done[k] = dict(self.tare[k])
            if not done:
                raise RuntimeError("Sin datos de la tabla para tarar")
            return done

    def clear_tare(self) -> None:
        with self.lock:
            self.tare = {}
            self.tared = set()

    def start_recording(self, name: str | None) -> str:
        with self.lock:
            if self.recorder:
                raise RuntimeError("Ya hay una grabación en curso")
            path = unique_path(DATA_DIR / f"{sanitize_name(name)}.csv")
            self.recorder = Recorder(path, self.mode)
            return path.stem

    def stop_recording(self) -> dict:
        with self.lock:
            if not self.recorder:
                raise RuntimeError("No hay ninguna grabación en curso")
            info = self.recorder.close()
            self.recorder = None
            info["boards"] = self._boards_meta(info["mode"])
            return info

    def set_auto(self, armed: bool) -> None:
        with self.lock:
            self.auto_armed = armed
            self._auto_trigger_t = None

    def set_layout(self) -> None:
        with self.lock:
            self.layout = Layout.from_settings(self.settings)

    def swap_boards(self) -> None:
        """Intercambia qué tabla es la izquierda y cuál la derecha (dos tablas)."""
        with self.lock:
            if self.mode != "dual":
                raise RuntimeError("Hacen falta las dos tablas conectadas para intercambiarlas")
            if self.recorder or self.auto_armed:
                raise RuntimeError("No se pueden intercambiar las tablas durante una grabación "
                                   "o con la captura automática armada (sal de la práctica)")
            self.settings["dual_left_key"] = self.right_key
            save_settings(self.settings)
            self._update_mode()

    def identify(self, key: str) -> None:
        """Parpadea el LED de esa tabla (entre lecturas, desde su propio hilo)."""
        with self.lock:
            w = self.workers.get(key)
            if w is None or not w.is_alive():
                raise RuntimeError("Esa tabla no está conectada")
            w.blink = True

    def set_sim_present(self, present: bool) -> bool:
        if self._sim_state is None:
            return False
        self._sim_state.set_present(present)
        return True

    def status(self) -> dict:
        with self.lock:
            rec = None
            if self.recorder:
                rec = {"name": self.recorder.path.stem, "samples": self.recorder.n,
                       "seconds": round(time.time() - self.recorder.started, 1)}
            connected = [b for b in self.boards.values() if b["connected"]]
            clock = self.boards.get(self.left_key) if self.left_key else None
            trail_side = "R" if self.settings.get("handed") == "right" else "L"
            lst = []
            for b in sorted(self.boards.values(), key=lambda b: b["order"]):
                side = "L" if b["key"] == self.left_key else ("R" if b["key"] == self.right_key else None)
                foot = None
                if side and self.mode == "dual":
                    foot = "trail" if side == trail_side else "lead"
                stale = False
                if self.mode == "dual" and side == "R":
                    lt = b["latest"][0] if b["latest"] else None
                    stale = lt is None or ((self._last_t or 0.0) - lt) > STALE_S
                lst.append({"key": b["key"], "serial": b["serial"], "product": b["product"], "side": side,
                            "foot": foot, "connected": b["connected"], "hz": round(b["hz"], 1),
                            "tared": b["key"] in self.tared, "stale": stale})
            return {
                "connected": bool(connected), "sim": bool(self.sim), "sim_boards": self.sim,
                "hz": round(clock["hz"], 1) if clock else 0.0,
                "error": self.error if not connected else None,
                "tared": bool(connected) and all(b["key"] in self.tared for b in connected),
                "recording": rec,
                "auto": {"armed": self.auto_armed, "count": self.auto_count},
                "settings": dict(self.settings), "next_name": next_name(),
                "boards": {"mode": self.mode, "count": len(connected), "list": lst,
                           "layout": self.layout.to_dict(),
                           "needs_gap": self.mode == "dual" and self.settings.get("dual_gap_cm") is None},
            }


# --------------------------------------------------------------------------- #
# Análisis, reproducción, exportar / importar
# --------------------------------------------------------------------------- #
_analysis_lock = threading.Lock()


def _layout_for(meta: dict, settings: dict) -> Layout:
    """Colocación de las tablas para analizar un swing: la congelada en su meta (si se
    mueven las tablas después, los swings viejos no cambian) o, si no la hay, la actual."""
    lay = (meta.get("boards") or {}).get("layout")
    return Layout.from_dict(lay) if lay else Layout.from_settings(settings)


def _kw(settings: dict, layout: Layout | None = None) -> dict:
    return dict(handed=settings["handed"], flip_x=settings["flip_x"], flip_y=settings["flip_y"], layout=layout)


def _settings_bw(settings: dict, bw: float | None) -> float | None:
    """Peso corporal de referencia: el pedido, si no el fijado en los ajustes (asistente)."""
    if bw:
        return bw
    v = settings.get("body_weight_kg")
    return float(v) if v and v >= 20 else None


def load_analysis(name: str, settings: dict, bw: float | None = None) -> SwingAnalysis:
    """Analiza un swing usando como marcas los eventos guardados (manual o audio)."""
    bw = _settings_bw(settings, bw)
    cap = load_csv(swing_paths(name)["csv"])
    meta = load_meta(name)
    ev = meta.get("events") or {}
    t_top = ev.get("t_top") if ev.get("top_source") == "manual" else None
    t_impact = ev.get("t_impact") if ev.get("impact_source") in ("manual", "audio") else None
    return analyze(cap, bw_kg=bw, t_top=t_top, t_impact=t_impact, **_kw(settings, _layout_for(meta, settings)))


def run_analysis(name: str, settings: dict, bw: float | None = None, t_top: float | None = None,
                 t_impact: float | None = None, reset_events: bool = False,
                 use_current_layout: bool = False) -> dict:
    paths = swing_paths(name)
    if not paths["csv"].exists():
        raise FileNotFoundError(f"No existe {paths['csv'].name}")
    bw = _settings_bw(settings, bw)
    meta = load_meta(name)
    ev = {} if reset_events else dict(meta.get("events") or {})
    if t_top is not None:
        ev["t_top"], ev["top_source"] = t_top, "manual"
    if t_impact is not None:
        ev["t_impact"], ev["impact_source"] = t_impact, "manual"
    man_top = ev.get("t_top") if ev.get("top_source") == "manual" else None
    man_imp = ev.get("t_impact") if ev.get("impact_source") == "manual" else None

    with _analysis_lock:
        cap = load_csv(paths["csv"])
        boards = dict(meta.get("boards") or {})
        if cap.dual and (use_current_layout or not boards.get("layout")):
            # se congela la colocación con la que se analiza (importados, sintéticos o corrección)
            boards.update(mode="dual", layout=Layout.from_settings(settings).to_dict())
            meta["boards"] = boards
        layout = _layout_for(meta, settings)
        an = analyze(cap, bw_kg=bw, t_top=man_top, t_impact=man_imp, **_kw(settings, layout))
        impact_source = "manual" if man_imp is not None else ("heuristic" if an.i_impact is not None else None)

        # Impacto por audio (si no hay marca manual): pico del golpe cerca del top
        if man_imp is None and paths["wav"].exists() and meta.get("audio"):
            try:
                fs, x = media.load_wav(paths["wav"])
                a0 = float(meta["audio"]["t_start"])
                win = (an.t_top - 0.2, an.t_top + 1.2) if an.t_top is not None else None
                det = media.detect_impact(x, fs, a0, win)
                meta["impact_audio"] = json_clean(det)
                if det.get("t") is not None:
                    an = analyze(cap, bw_kg=bw, t_top=man_top, t_impact=det["t"], **_kw(settings, layout))
                    impact_source = "audio"
            except Exception as exc:
                meta["impact_audio"] = {"t": None, "error": str(exc)}

        ev.update({"t_top": an.t_top, "t_impact": an.t_impact, "impact_source": impact_source,
                   "top_source": "manual" if man_top is not None else ("heuristic" if an.i_top is not None else None)})
        meta["events"] = json_clean(ev)
        meta["name"] = name
        save_meta(name, meta)

        ref_name = get_reference()
        ref_an, ref_block, deltas = None, None, None
        if ref_name and ref_name != name:
            try:
                ref_an = load_analysis(ref_name, settings)
                rm, m = json_clean(ref_an.metrics()), json_clean(an.metrics())
                ref_block = {"name": ref_name, "metrics": rm}
                deltas = {k: round(m[k] - rm[k], 1) for k in m
                          if isinstance(m.get(k), (int, float)) and isinstance(rm.get(k), (int, float))}
            except Exception as exc:
                ref_block = {"name": ref_name, "error": str(exc)}
        plot(an, out_path=paths["png"], reference=ref_an)

    notes = list(an.notes)
    dropped = (meta.get("recording") or {}).get("dropped")
    if dropped:
        notes.append(f"{dropped} muestras descartadas: una tabla se conectó o desconectó durante la grabación.")
    return {
        "name": name, "metrics": json_clean(an.metrics()), "notes": notes, "events": meta["events"],
        "impact_audio": meta.get("impact_audio"), "source": meta.get("source"),
        "png": f"/out/{name}.png?v={int(paths['png'].stat().st_mtime)}",
        "samples": int(cap.t.size), "duration": round(float(cap.t[-1] - cap.t[0]), 2), "hz": round(cap.fs, 1),
        "media": {"audio": paths["wav"].exists(), "video": paths["frames"].exists()},
        "reference": ref_block, "deltas": deltas, "path": _path_summary(an),
        "mode": "dual" if cap.dual else "single", "boards": meta.get("boards"),
    }


def _path_summary(an: SwingAnalysis, step: int = 4) -> dict:
    """Trazo del CoP (cm) del tramo de pie, diezmado, con las marcas de top e impacto.
    Con dos tablas incluye además el trazo de cada pie (en su propia tabla) y la colocación."""
    s, e = an.segment
    t, x, y = [], [], []
    feet = {name: {"x": [], "y": []} for name in (an.feet or {})}
    for i in range(s, e, step):
        xi, yi = float(an.cop_x_cm[i]), float(an.cop_y_cm[i])
        if math.isnan(xi) or math.isnan(yi):
            continue
        t.append(round(float(an.t[i]), 3))
        x.append(round(xi, 2))
        y.append(round(yi, 2))
        for name, foot in (an.feet or {}).items():
            fx, fy = float(foot["x_cm"][i]), float(foot["y_cm"][i])
            feet[name]["x"].append(None if math.isnan(fx) else round(fx, 2))
            feet[name]["y"].append(None if math.isnan(fy) else round(fy, 2))
    marks = {}
    for key, i in (("top", an.i_top), ("impact", an.i_impact)):
        if i is not None and not math.isnan(float(an.cop_x_cm[i])):
            marks[key] = [round(float(an.cop_x_cm[i]), 2), round(float(an.cop_y_cm[i]), 2)]
    out = {"t": t, "x": x, "y": y, "marks": marks, "mode": "dual" if an.dual else "single"}
    if an.dual:
        out["feet"] = feet
        out["layout"] = an.layout.to_dict()
    return out


def finalize_swing(name: str, t0: float, t_end: float, source: str, settings: dict,
                   media_mgr: MediaManager | None, boards: dict | None = None,
                   recording: dict | None = None) -> dict:
    """Tras guardar el CSV: recorta audio/vídeo, escribe la meta y analiza."""
    audio_info = video_info = None
    if media_mgr is not None:
        audio_info, video_info = media_mgr.save_clip(name, t0, t_end)
    meta = load_meta(name)
    meta.update({"name": name, "created": meta.get("created") or now_iso(), "source": source,
                 "duration": round(t_end - t0, 3), "audio": audio_info, "video": video_info,
                 "settings": {k: settings.get(k) for k in ("handed", "flip_x", "flip_y", "dual_gap_cm", "dual_button")}})
    if boards:
        meta["boards"] = boards
    if recording:
        meta["recording"] = {k: recording.get(k) for k in ("samples", "dropped", "mode")}
    save_meta(name, meta)
    return run_analysis(name, settings)


def _series(an: SwingAnalysis) -> dict:
    def arr(a):
        return [None if (isinstance(v, float) and math.isnan(v)) else round(float(v), 4) for v in a]
    out = {"t": arr(an.t), "trail_pct": arr(an.trail_pct), "cop_x_cm": arr(an.cop_x_cm),
           "cop_y_cm": arr(an.cop_y_cm), "force_pct": arr(an.force_pct), "segment": list(an.segment),
           "mode": "dual" if an.dual else "single"}
    if an.dual:
        out["layout"] = an.layout.to_dict()
        out["feet"] = {name: {"pct": arr(f["pct"]), "x_cm": arr(f["x_cm"]), "y_cm": arr(f["y_cm"]),
                              "force_pct": arr(f["force_pct"])} for name, f in an.feet.items()}
    return out


def track_payload(name: str, settings: dict) -> dict:
    """Series temporales + eventos + índice de vídeo para el reproductor del navegador."""
    an = load_analysis(name, settings)
    meta = load_meta(name)
    paths = swing_paths(name)
    payload = {"name": name, **_series(an), "bw_kg": json_clean(an.bw_kg),
               "events": meta.get("events") or {"t_top": an.t_top, "t_impact": an.t_impact},
               "handed": an.handed, "trail_is_pos_x": an.trail_is_pos_x,
               "half_x_cm": HALF_X_CM, "half_y_cm": HALF_Y_CM,
               "audio": meta.get("audio"), "impact_audio": meta.get("impact_audio"), "video": None,
               "reference": None}
    if paths["frames"].exists():
        idx = media.read_frames_index(paths["frames"])
        payload["video"] = {"n": len(idx["t"]), "t": idx["t"], "width": idx["width"], "height": idx["height"]}
    ref = get_reference()
    if ref and ref != name:
        try:
            ran = load_analysis(ref, settings)
            payload["reference"] = {"name": ref, **_series(ran),
                                    "events": load_meta(ref).get("events") or {"t_top": ran.t_top, "t_impact": ran.t_impact}}
        except Exception as exc:
            payload["reference"] = {"name": ref, "error": str(exc)}
    return json_clean(payload)


def export_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in swing_paths(name).values():
            if p.exists():
                z.write(p, p.name)
    return buf.getvalue()


def import_zip(data: bytes) -> list[str]:
    imported = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        groups: dict[str, list] = {}
        for info in z.infolist():
            if info.is_dir():
                continue
            m = IMPORT_ENTRY_RE.match(Path(info.filename).name)
            if m:
                groups.setdefault(m.group(1), []).append((m.group(2), info))
        for base, items in groups.items():
            if not any(ext == "csv" for ext, _ in items):
                continue
            new = base
            if swing_paths(new)["csv"].exists():
                new = unique_path(DATA_DIR / f"{base}.csv").stem
            for ext, info in items:
                dest = (OUT_DIR if ext == "png" else DATA_DIR) / f"{new}.{ext}"
                dest.write_bytes(z.read(info))
            meta = load_meta(new)
            meta.update({"name": new, "source": "import", "imported_from": base, "imported_at": now_iso()})
            meta.setdefault("created", now_iso())
            save_meta(new, meta)
            imported.append(new)
    if not imported:
        raise ValueError("El zip no contiene ningún swing (hace falta al menos <nombre>.csv)")
    return imported


# --------------------------------------------------------------------------- #
# Aplicación
# --------------------------------------------------------------------------- #
class RecordStart(BaseModel):
    name: str | None = None


class RecordStop(BaseModel):
    analyze: bool = True


class AutoBody(BaseModel):
    armed: bool


class SettingsBody(BaseModel):
    handed: str = "right"
    flip_x: bool = False
    flip_y: bool = False
    audio: bool = False
    video: bool = False
    audio_device: int | None = None
    camera: int = 0
    body_weight_kg: float | None = None
    dual_gap_cm: float | None = None
    dual_button: str = "left"
    dual_left_key: str | None = None


class SimPresentBody(BaseModel):
    present: bool


class AnalyzeBody(BaseModel):
    bw: float | None = None
    top: float | None = None
    impact: float | None = None
    reset: bool = False
    use_current_layout: bool = False   # dos tablas: reanalizar con la colocación actual


class BoardKeyBody(BaseModel):
    key: str


class PairBody(BaseModel):
    seconds: float = 10.0
    forget_first: bool = False


class ForgetBody(BaseModel):
    address: str


def create_app(sim: int | bool = 0) -> FastAPI:
    DATA_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
    settings = load_settings()
    reader = BoardReader(int(sim), settings)
    media_mgr = MediaManager()
    clients: set[WebSocket] = set()
    bt_lock = threading.Lock()

    def full_status() -> dict:
        return {**reader.status(), "media": media_mgr.status(), "reference": get_reference(),
                "platform": sys.platform}

    def bt_subprocess(flag: str, timeout: float) -> subprocess.CompletedProcess:
        """macOS: IOBluetooth necesita el run loop del hilo principal y el permiso de
        Bluetooth puede matar al proceso que lo pide, así que el estado y el emparejado
        corren en un proceso aparte (`WiiGolf --bt-status` / `--pair`)."""
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, flag]
        else:
            cmd = [sys.executable, "-m", "wiigolf.app", flag]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))

    def status_subprocess() -> dict:
        try:
            p = bt_subprocess("--bt-status", 30)
            line = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
            if line:
                return json.loads(line[-1])
            return {"available": False, "radio": None, "boards": [],
                    "error": (p.stderr.strip().splitlines() or [f"salida {p.returncode}"])[-1]}
        except Exception as exc:
            return {"available": False, "radio": None, "boards": [], "error": str(exc)}

    def pair_subprocess(seconds: float) -> dict:
        try:
            # búsqueda + hasta 4 intentos de emparejado + espera del HID (ver bt_mac)
            p = bt_subprocess("--pair", seconds + 200)
        except subprocess.TimeoutExpired:
            return {"ok": False, "board": None, "log": [], "message": "El emparejado no ha respondido a tiempo"}
        lines = [ln for ln in (p.stdout + p.stderr).splitlines() if ln.strip()]
        last = lines[-1] if lines else ""
        ok = p.returncode == 0 and last.startswith("OK:")
        msg = last.split(":", 1)[1].strip() if ":" in last else (last or f"salida {p.returncode}")
        return {"ok": ok, "board": None, "log": lines[:-1], "message": msg}

    async def send_all(msg: dict) -> None:
        dead = []
        for ws in list(clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)

    async def handle_auto_saved(ev: dict) -> None:
        try:
            res = await asyncio.to_thread(finalize_swing, ev["name"], ev["t0"], ev["t_end"], "auto",
                                          dict(reader.settings), media_mgr, ev.get("boards"))
            await send_all({"type": "auto_saved", "name": ev["name"], "analysis": res})
        except Exception as exc:
            await send_all({"type": "auto_saved", "name": ev["name"], "error": str(exc)})

    def sample_message(s: dict) -> dict:
        if s.get("mode") == "dual":
            f = s["feet"]
            return {"type": "s", "mode": "dual", "t": round(s["t"], 4), "tot": round(s["total"], 2),
                    "xcm": round(s["cop_x_cm"], 2), "ycm": round(s["cop_y_cm"], 2),
                    "L": {"kg": round(f["L"]["kg"], 2), "x": round(f["L"]["x_cm"], 2), "y": round(f["L"]["y_cm"], 2)},
                    "R": {"kg": round(f["R"]["kg"], 2), "x": round(f["R"]["x_cm"], 2), "y": round(f["R"]["y_cm"], 2)},
                    "stale": bool(s.get("stale"))}
        kg = s["kg"]
        return {"type": "s", "mode": "single", "t": round(s["t"], 4),
                "tr": round(kg["TR"], 2), "br": round(kg["BR"], 2),
                "tl": round(kg["TL"], 2), "bl": round(kg["BL"], 2),
                "tot": round(s["total"], 2),
                "x": round(s["cop_x"], 4), "y": round(s["cop_y"], 4)}

    async def broadcaster() -> None:
        last_t = None
        last_status = 0.0
        while True:
            await asyncio.sleep(0.02)
            while True:
                try:
                    ev = reader.events.get_nowait()
                except queue.Empty:
                    break
                if ev["type"] == "auto_saved":
                    asyncio.create_task(handle_auto_saved(ev))
                elif ev["type"] == "mode":
                    last_status = time.time()
                    await send_all({"type": "status", **full_status()})
                else:
                    await send_all(ev)
            with reader.lock:
                s = reader.latest
            if s is not None and s["t"] != last_t:
                last_t = s["t"]
                await send_all(sample_message(s))
            if time.time() - last_status >= 1.0:
                last_status = time.time()
                await send_all({"type": "status", **full_status()})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        reader.start()
        asyncio.get_running_loop().run_in_executor(None, media_mgr.apply, dict(settings))
        task = asyncio.create_task(broadcaster())
        try:
            yield
        finally:
            task.cancel()
            reader.stop_event.set()
            reader.retry_now()
            media_mgr.stop()
            reader.join(timeout=2.0)

    app = FastAPI(title="Wii Golf", lifespan=lifespan)
    app.mount("/out", StaticFiles(directory=OUT_DIR), name="out")

    def check_name(name: str) -> str:
        if not NAME_RE.match(name):
            raise HTTPException(400, "Nombre no válido")
        return name

    # ---- página y websocket ---------------------------------------------- #
    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/manual")
    async def manual():
        return FileResponse(STATIC_DIR / "manual.html", headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"type": "status", **full_status()})
        clients.add(websocket)
        try:
            while True:
                await websocket.receive_text()  # solo keepalive
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(websocket)

    # ---- estado, tara, grabación, auto, ajustes -------------------------- #
    @app.get("/api/status")
    async def api_status():
        return {**full_status(), "audio_inputs": media.list_audio_inputs()}

    @app.post("/api/tare")
    async def api_tare():
        try:
            offsets = reader.set_tare()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"tare": offsets, "status": full_status()}

    @app.post("/api/tare/clear")
    async def api_tare_clear():
        reader.clear_tare()
        return {"status": full_status()}

    @app.post("/api/record/start")
    async def api_record_start(body: RecordStart):
        try:
            name = reader.start_recording(body.name)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"name": name, "status": full_status()}

    @app.post("/api/record/stop")
    async def api_record_stop(body: RecordStop):
        try:
            info = reader.stop_recording()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        out: dict = {"recording": info, "status": full_status()}
        if info["samples"] > 0:
            try:
                if body.analyze:
                    out["analysis"] = await asyncio.to_thread(finalize_swing, info["name"], info["t0"],
                                                              info["t_end"], "manual", dict(reader.settings), media_mgr,
                                                              info.get("boards"), info)
                else:
                    await asyncio.to_thread(media_mgr.save_clip, info["name"], info["t0"], info["t_end"])
            except Exception as exc:
                out["analysis_error"] = str(exc)
        return out

    @app.post("/api/auto")
    async def api_auto(body: AutoBody):
        reader.set_auto(body.armed)
        return {"status": full_status()}

    @app.post("/api/settings")
    async def api_settings(body: SettingsBody):
        if body.handed not in ("right", "left"):
            raise HTTPException(400, "handed debe ser right o left")
        if body.dual_button not in BUTTON_SIDES:
            raise HTTPException(400, "dual_button debe ser left o right")
        gap = None if body.dual_gap_cm is None else max(0.0, min(100.0, float(body.dual_gap_cm)))
        with reader.lock:
            bw = body.body_weight_kg if (body.body_weight_kg or 0) >= 20 else None
            # dual_left_key solo lo cambia el servidor (intercambiar): se conserva el actual
            reader.settings.update(handed=body.handed, flip_x=body.flip_x, flip_y=body.flip_y,
                                   audio=body.audio, video=body.video, audio_device=body.audio_device,
                                   camera=body.camera, body_weight_kg=bw,
                                   dual_gap_cm=gap, dual_button=body.dual_button)
            reader.layout = Layout.from_settings(reader.settings)
            save_settings(reader.settings)
            snapshot = dict(reader.settings)
        await asyncio.to_thread(media_mgr.apply, snapshot)
        return {"status": full_status()}

    @app.post("/api/sim/present")
    async def api_sim_present(body: SimPresentBody):
        """Solo en --sim: simula que alguien se sube (true) o se baja (false) de la tabla."""
        if not reader.sim or not reader.set_sim_present(body.present):
            raise HTTPException(409, "Solo disponible con la tabla simulada (--sim)")
        return {"present": body.present}

    @app.get("/api/camera/snapshot.jpg")
    async def api_camera_snapshot():
        jpg = media_mgr.snapshot()
        if not jpg:
            raise HTTPException(404, "Sin imagen de la cámara")
        return Response(content=jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---- dos tablas --------------------------------------------------------- #
    @app.post("/api/boards/swap")
    async def api_boards_swap():
        try:
            reader.swap_boards()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"status": full_status()}

    @app.post("/api/boards/identify")
    async def api_boards_identify(body: BoardKeyBody):
        try:
            reader.identify(body.key)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True}

    # ---- swings ------------------------------------------------------------ #
    @app.get("/api/swings")
    async def api_swings():
        return list_swings()

    @app.post("/api/analyze/{name}")
    async def api_analyze(name: str, body: AnalyzeBody | None = None):
        check_name(name)
        body = body or AnalyzeBody()
        try:
            return await asyncio.to_thread(run_analysis, name, dict(reader.settings), body.bw, body.top,
                                           body.impact, body.reset, body.use_current_layout)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise HTTPException(500, f"Error en el análisis: {exc}")

    @app.get("/api/swings/{name}/track")
    async def api_track(name: str):
        check_name(name)
        if not swing_paths(name)["csv"].exists():
            raise HTTPException(404, "No existe")
        try:
            return await asyncio.to_thread(track_payload, name, dict(reader.settings))
        except Exception as exc:
            raise HTTPException(500, f"Error preparando la reproducción: {exc}")

    @app.get("/api/swings/{name}/csv")
    async def api_swing_csv(name: str):
        check_name(name)
        path = swing_paths(name)["csv"]
        if not path.exists():
            raise HTTPException(404, "No existe")
        return FileResponse(path, filename=path.name, media_type="text/csv")

    @app.get("/api/swings/{name}/audio")
    async def api_swing_audio(name: str):
        check_name(name)
        path = swing_paths(name)["wav"]
        if not path.exists():
            raise HTTPException(404, "Sin audio")
        return FileResponse(path, filename=path.name, media_type="audio/wav")

    @app.get("/api/swings/{name}/frames")
    async def api_swing_frames(name: str):
        check_name(name)
        path = swing_paths(name)["frames"]
        if not path.exists():
            raise HTTPException(404, "Sin vídeo")
        return media.read_frames_index(path)

    @app.get("/api/swings/{name}/frame/{i}")
    async def api_swing_frame(name: str, i: int):
        check_name(name)
        path = swing_paths(name)["frames"]
        if not path.exists():
            raise HTTPException(404, "Sin vídeo")
        try:
            jpg = await asyncio.to_thread(media.read_frame, path, i)
        except KeyError:
            raise HTTPException(404, "Fotograma inexistente")
        return Response(content=jpg, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/swings/{name}/export")
    async def api_swing_export(name: str):
        check_name(name)
        if not swing_paths(name)["csv"].exists():
            raise HTTPException(404, "No existe")
        data = await asyncio.to_thread(export_zip, name)
        return Response(content=data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.wiigolf.zip"'})

    @app.post("/api/swings/import")
    async def api_swing_import(file: UploadFile = File(...)):
        data = await file.read()
        try:
            names = await asyncio.to_thread(import_zip, data)
        except (zipfile.BadZipFile, ValueError) as exc:
            raise HTTPException(400, str(exc))
        results = []
        for n in names:
            try:
                results.append(await asyncio.to_thread(run_analysis, n, dict(reader.settings)))
            except Exception as exc:
                results.append({"name": n, "error": str(exc)})
        return {"imported": names, "analyses": results}

    @app.delete("/api/swings/{name}")
    async def api_swing_delete(name: str):
        check_name(name)
        removed = [p.name for p in swing_paths(name).values() if p.exists()]
        if not removed:
            raise HTTPException(404, "No existe")
        for p in swing_paths(name).values():
            if p.exists():
                p.unlink()
        if get_reference() is None:
            set_reference(None)
        return {"removed": removed}

    # ---- referencia -------------------------------------------------------- #
    @app.get("/api/reference")
    async def api_reference():
        return {"name": get_reference()}

    @app.post("/api/reference/{name}")
    async def api_reference_set(name: str):
        check_name(name)
        if not swing_paths(name)["csv"].exists():
            raise HTTPException(404, "No existe")
        set_reference(name)
        return {"name": name}

    @app.delete("/api/reference")
    async def api_reference_clear():
        set_reference(None)
        return {"name": None}

    # ---- tabla / bluetooth -------------------------------------------------- #
    @app.post("/api/board/reconnect")
    async def api_board_reconnect():
        reader.retry_now()
        return {"status": full_status()}

    @app.get("/api/bluetooth/status")
    async def api_bt_status():
        if bt is None:
            return {"available": False, "error": f"Sin soporte de emparejado en {sys.platform}",
                    "radio": None, "boards": [], "platform": sys.platform}
        fn = status_subprocess if sys.platform == "darwin" else bt.status
        return {**(await asyncio.to_thread(fn)), "platform": sys.platform}

    @app.post("/api/bluetooth/pair")
    async def api_bt_pair(body: PairBody):
        if bt is None:
            raise HTTPException(501, f"Sin soporte de emparejado en {sys.platform}")
        if not bt_lock.acquire(blocking=False):
            raise HTTPException(409, "Ya hay un emparejado en curso")
        log: list[str] = []
        try:
            if sys.platform == "darwin":
                res = await asyncio.to_thread(pair_subprocess, body.seconds)
                log = res.pop("log", [])
            else:
                res = await asyncio.to_thread(bt.pair, log.append, body.seconds, body.forget_first)
        except Exception as exc:
            raise HTTPException(500, f"{exc} | " + " / ".join(log))
        finally:
            bt_lock.release()
        res["log"] = log
        reader.retry_now()
        return res

    @app.post("/api/bluetooth/forget")
    async def api_bt_forget(body: ForgetBody):
        if bt is None:
            raise HTTPException(501, f"Sin soporte de emparejado en {sys.platform}")
        try:
            return await asyncio.to_thread(bt.forget, body.address)
        except Exception as exc:
            raise HTTPException(500, str(exc))

    return app
