"""
Servidor web local para la Wii Balance Board.

- Un hilo lee la tabla (~100 Hz), aplica la tara, graba a CSV si hay una
  grabación activa y alimenta la captura automática de swings.
- Micrófono y webcam (opcionales) capturan en continuo con el mismo reloj que
  la tabla; al guardar un swing se recorta su tramo (WAV + fotogramas JPEG) y
  se busca el golpe a la bola en el audio para marcar el impacto.
- El navegador recibe las muestras por WebSocket (~50 Hz) y controla todo por
  una API REST: grabación, tara, ajustes, análisis, swing de referencia,
  exportar / importar y emparejado Bluetooth de la tabla.
- Cada swing son varios archivos en data/: <n>.csv (tabla), <n>.json (meta y
  eventos), <n>.wav (audio), <n>.frames.zip (vídeo) y out/<n>.png (figura).

Arranque:  python scripts/04_web.py [--host 0.0.0.0] [--port 8000] [--sim]

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
from ..analysis import HALF_X_CM, HALF_Y_CM, SwingAnalysis, analyze, load_csv, plot
from ..balance_board import BALANCE_BOARD_PID, MIN_LOAD_KG, SENSORS, BalanceBoard



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

CSV_HEADER = ["t", "TR", "BR", "TL", "BL", "total", "cop_x", "cop_y"]
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

DEFAULT_SETTINGS = {"handed": "right", "flip_x": False, "flip_y": False,
                    "audio": False, "video": False, "audio_device": None, "camera": 0,
                    "body_weight_kg": None}   # fijado por el asistente; referencia del % de fuerza


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


def make_sample(t: float, kg_raw: dict, tare: dict) -> dict:
    kg = {s: max(0.0, kg_raw[s] - tare[s]) for s in SENSORS}
    total = sum(kg.values())
    if total >= MIN_LOAD_KG:
        cx = ((kg["TR"] + kg["BR"]) - (kg["TL"] + kg["BL"])) / total
        cy = ((kg["TL"] + kg["TR"]) - (kg["BL"] + kg["BR"])) / total
    else:
        cx = cy = 0.0
    return {"t": t, "kg": kg, "raw": kg_raw, "total": total, "cop_x": cx, "cop_y": cy}


def trail_pct(sample: dict, settings: dict) -> float | None:
    total = sample["total"]
    if total < MIN_LOAD_KG:
        return None
    kg = sample["kg"]
    right, left = kg["TR"] + kg["BR"], kg["TL"] + kg["BL"]
    if settings["flip_x"]:
        right, left = left, right
    trail = right if settings["handed"] == "right" else left
    return trail / total * 100.0


def csv_row(sample: dict, t0: float) -> list[str]:
    kg = sample["kg"]
    return [f"{sample['t'] - t0:.4f}", f"{kg['TR']:.2f}", f"{kg['BR']:.2f}",
            f"{kg['TL']:.2f}", f"{kg['BL']:.2f}", f"{sample['total']:.2f}",
            f"{sample['cop_x']:.4f}", f"{sample['cop_y']:.4f}"]


def write_csv(path: Path, samples: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        if samples:
            t0 = samples[0]["t"]
            w.writerows(csv_row(s, t0) for s in samples)


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
        })
    return items


# --------------------------------------------------------------------------- #
# Grabación
# --------------------------------------------------------------------------- #
class Recorder:
    def __init__(self, path: Path):
        self.path = path
        self._f = path.open("w", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        self._w.writerow(CSV_HEADER)
        self.t0: float | None = None
        self.last_t: float | None = None
        self.n = 0
        self.started = time.time()

    def write(self, sample: dict) -> None:
        if self.t0 is None:
            self.t0 = sample["t"]
        self.last_t = sample["t"]
        self._w.writerow(csv_row(sample, self.t0))
        self.n += 1

    def close(self) -> dict:
        self._f.close()
        dur = (self.last_t - self.t0) if (self.t0 is not None and self.last_t is not None) else 0.0
        return {"name": self.path.stem, "samples": self.n, "duration": round(dur, 2),
                "t0": self.t0, "t_end": self.last_t}


# --------------------------------------------------------------------------- #
# Tabla simulada (para desarrollar sin hardware)
# --------------------------------------------------------------------------- #
class SimBoard:
    """Reproduce en bucle un swing de diestro (address, top, downswing, finish)."""

    # (t, trail %, fuerza % peso, y_norm)  y_norm: -1 talón .. +1 punta
    KEYS = [
        (0.0, 52, 100, -0.1), (2.5, 52, 100, -0.1), (3.3, 72, 95, 0.15),
        (3.55, 40, 130, -0.2), (3.7, 22, 92, -0.3), (4.7, 12, 100, -0.2),
        (6.5, 12, 100, -0.2), (8.0, 52, 100, -0.1),
    ]
    PERIOD = 8.0

    def __init__(self, bw_kg: float = 80.0):
        self.bw = bw_kg
        self.t0 = time.perf_counter()
        self._next = self.t0
        self.present = True   # False = nadie sobre la tabla (para probar el asistente)

    def set_present(self, present: bool) -> None:
        if present and not self.present:
            self.t0 = time.perf_counter()   # al subirse empieza en el address (quieto 2,5 s)
        self.present = present

    def _interp(self, u: float) -> tuple[float, float, float]:
        keys = self.KEYS
        for (t0, a0, f0, y0), (t1, a1, f1, y1) in zip(keys, keys[1:]):
            if t0 <= u < t1:
                w = (u - t0) / (t1 - t0)
                w = w * w * (3 - 2 * w)
                return a0 + (a1 - a0) * w, f0 + (f1 - f0) * w, y0 + (y1 - y0) * w
        _, a, f, y = keys[-1]
        return a, f, y

    def read_raw(self) -> tuple[float, dict]:
        self._next += 0.01
        delay = self._next - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        t = time.perf_counter()
        if not self.present:
            return t, {k: max(0.0, random.gauss(0, 0.05)) for k in ("TR", "BR", "TL", "BL")}
        trail, force, y = self._interp((t - self.t0) % self.PERIOD)
        total = force / 100.0 * self.bw
        right = total * trail / 100.0
        left = total - right
        ft = 0.5 + y / 2.0
        kg = {"TR": right * ft, "BR": right * (1 - ft), "TL": left * ft, "BL": left * (1 - ft)}
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
# Hilo lector
# --------------------------------------------------------------------------- #
class BoardReader(threading.Thread):
    def __init__(self, sim: bool, settings: dict):
        super().__init__(daemon=True, name="board-reader")
        self.sim = sim
        self.settings = settings
        self.stop_event = threading.Event()
        self._retry = threading.Event()
        self.lock = threading.Lock()
        self.latest: dict | None = None
        self.connected = False
        self.error: str | None = None
        self.hz = 0.0
        self.tare = {s: 0.0 for s in SENSORS}
        self.tared = False
        self.buffer: deque = deque(maxlen=int(BUFFER_S * 100))
        self.recorder: Recorder | None = None
        self.auto_armed = False
        self.auto_count = 0
        self._auto_trigger_t: float | None = None
        self._auto_last = -1e9
        self._bw_est: float | None = None   # peso corporal estimado (mediana del buffer)
        self._bw_counter = 0
        self.events: queue.Queue = queue.Queue()

    # ---- bucle principal ------------------------------------------------ #
    def run(self) -> None:
        last_err = None
        while not self.stop_event.is_set():
            try:
                self._run_sim() if self.sim else self._run_real()
            except Exception as exc:
                with self.lock:
                    self.connected = False
                    self.hz = 0.0
                    self.error = str(exc)
                if str(exc) != last_err:  # al terminal, para poder enviar el motivo
                    last_err = str(exc)
                    print(f"  [tabla] {exc}", flush=True)
                self._retry.wait(3.0)
                self._retry.clear()

    def retry_now(self) -> None:
        self._retry.set()

    def set_sim_present(self, present: bool) -> bool:
        sim = getattr(self, "_sim", None)
        if sim is None:
            return False
        sim.set_present(present)
        return True

    def _run_sim(self) -> None:
        sim = SimBoard()
        self._sim = sim
        with self.lock:
            self.connected, self.error = True, None
        n, t_hz = 0, time.perf_counter()
        while not self.stop_event.is_set():
            t, kg = sim.read_raw()
            self._ingest(t, kg)
            n += 1
            if t - t_hz >= 1.0:
                with self.lock:
                    self.hz = n / (t - t_hz)
                n, t_hz = 0, t

    def _run_real(self) -> None:
        boards = BalanceBoard.enumerate()
        if not boards:
            raise RuntimeError("Tabla no encontrada. Pulsa su botón de encendido; si no aparece, "
                               "usa 'Emparejar' en el panel de la tabla.")
        # Si hay varias entradas (macOS lista una por colección HID, y puede añadir
        # un mando sintético sin número de serie), la tabla física es la que tiene
        # PID 0x0306 y número de serie (= su dirección Bluetooth).
        boards.sort(key=lambda d: (d.get("product_id") != BALANCE_BOARD_PID,
                                   not str(d.get("serial_number") or "").strip()))
        chosen = boards[0]
        board = BalanceBoard(path=chosen["path"])
        try:
            board.open()
        except Exception as exc:
            raise RuntimeError(f"No se pudo abrir la tabla ({exc}). Si acabas de emparejarla, espera unos "
                               "segundos; si sigue, apágala y enciéndela con su botón.") from exc
        try:
            board.init()
            board.calibrate()
            print(f"  [tabla] conectada: {chosen.get('product_string') or 'HID'} "
                  f"serie={chosen.get('serial_number') or '?'} ({len(boards)} entradas HID)", flush=True)
            with self.lock:
                self.connected, self.error = True, None
            last = time.perf_counter()
            n, t_hz = 0, last
            while not self.stop_event.is_set():
                r = board.read(timeout_ms=200)
                now = time.perf_counter()
                if r is None:
                    if now - last > 5.0:
                        raise RuntimeError("La tabla dejó de enviar datos (¿se ha apagado?)")
                    continue
                last = now
                self._ingest(r.t, r.kg)
                n += 1
                if now - t_hz >= 1.0:
                    with self.lock:
                        self.hz = n / (now - t_hz)
                    n, t_hz = 0, now
        finally:
            board.close()
            with self.lock:
                self.connected, self.hz = False, 0.0

    def _ingest(self, t: float, kg_raw: dict) -> None:
        with self.lock:
            s = make_sample(t, kg_raw, self.tare)
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
            if prev["total"] < STANDING_KG:
                return  # no ha estado de pie durante toda la ventana
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
        write_csv(path, samples)
        self.auto_count += 1
        self.events.put({"type": "auto_saved", "name": path.stem,
                         "t0": samples[0]["t"], "t_end": samples[-1]["t"]})

    # ---- API usada por el servidor -------------------------------------- #
    def set_tare(self) -> dict:
        with self.lock:
            recent = [x["raw"] for x in self.buffer if self.latest and self.latest["t"] - x["t"] <= 0.5]
            if not recent:
                raise RuntimeError("Sin datos de la tabla para tarar")
            self.tare = {s: sum(r[s] for r in recent) / len(recent) for s in SENSORS}
            self.tared = True
            return dict(self.tare)

    def clear_tare(self) -> None:
        with self.lock:
            self.tare = {s: 0.0 for s in SENSORS}
            self.tared = False

    def start_recording(self, name: str | None) -> str:
        with self.lock:
            if self.recorder:
                raise RuntimeError("Ya hay una grabación en curso")
            path = unique_path(DATA_DIR / f"{sanitize_name(name)}.csv")
            self.recorder = Recorder(path)
            return path.stem

    def stop_recording(self) -> dict:
        with self.lock:
            if not self.recorder:
                raise RuntimeError("No hay ninguna grabación en curso")
            info = self.recorder.close()
            self.recorder = None
            return info

    def set_auto(self, armed: bool) -> None:
        with self.lock:
            self.auto_armed = armed
            self._auto_trigger_t = None

    def status(self) -> dict:
        with self.lock:
            rec = None
            if self.recorder:
                rec = {"name": self.recorder.path.stem, "samples": self.recorder.n,
                       "seconds": round(time.time() - self.recorder.started, 1)}
            return {
                "connected": self.connected, "sim": self.sim, "hz": round(self.hz, 1),
                "error": self.error, "tared": self.tared, "recording": rec,
                "auto": {"armed": self.auto_armed, "count": self.auto_count},
                "settings": dict(self.settings), "next_name": next_name(),
            }


# --------------------------------------------------------------------------- #
# Análisis, reproducción, exportar / importar
# --------------------------------------------------------------------------- #
_analysis_lock = threading.Lock()


def _kw(settings: dict) -> dict:
    return dict(handed=settings["handed"], flip_x=settings["flip_x"], flip_y=settings["flip_y"])


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
    ev = load_meta(name).get("events") or {}
    t_top = ev.get("t_top") if ev.get("top_source") == "manual" else None
    t_impact = ev.get("t_impact") if ev.get("impact_source") in ("manual", "audio") else None
    return analyze(cap, bw_kg=bw, t_top=t_top, t_impact=t_impact, **_kw(settings))


def run_analysis(name: str, settings: dict, bw: float | None = None, t_top: float | None = None,
                 t_impact: float | None = None, reset_events: bool = False) -> dict:
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
        an = analyze(cap, bw_kg=bw, t_top=man_top, t_impact=man_imp, **_kw(settings))
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
                    an = analyze(cap, bw_kg=bw, t_top=man_top, t_impact=det["t"], **_kw(settings))
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

    return {
        "name": name, "metrics": json_clean(an.metrics()), "notes": an.notes, "events": meta["events"],
        "impact_audio": meta.get("impact_audio"), "source": meta.get("source"),
        "png": f"/out/{name}.png?v={int(paths['png'].stat().st_mtime)}",
        "samples": int(cap.t.size), "duration": round(float(cap.t[-1] - cap.t[0]), 2), "hz": round(cap.fs, 1),
        "media": {"audio": paths["wav"].exists(), "video": paths["frames"].exists()},
        "reference": ref_block, "deltas": deltas, "path": _path_summary(an),
    }


def _path_summary(an: SwingAnalysis, step: int = 4) -> dict:
    """Trazo del CoP (cm) del tramo de pie, diezmado, con las marcas de top e impacto."""
    s, e = an.segment
    t, x, y = [], [], []
    for i in range(s, e, step):
        xi, yi = float(an.cop_x_cm[i]), float(an.cop_y_cm[i])
        if math.isnan(xi) or math.isnan(yi):
            continue
        t.append(round(float(an.t[i]), 3))
        x.append(round(xi, 2))
        y.append(round(yi, 2))
    marks = {}
    for key, i in (("top", an.i_top), ("impact", an.i_impact)):
        if i is not None and not math.isnan(float(an.cop_x_cm[i])):
            marks[key] = [round(float(an.cop_x_cm[i]), 2), round(float(an.cop_y_cm[i]), 2)]
    return {"t": t, "x": x, "y": y, "marks": marks}


def finalize_swing(name: str, t0: float, t_end: float, source: str, settings: dict,
                   media_mgr: MediaManager | None) -> dict:
    """Tras guardar el CSV: recorta audio/vídeo, escribe la meta y analiza."""
    audio_info = video_info = None
    if media_mgr is not None:
        audio_info, video_info = media_mgr.save_clip(name, t0, t_end)
    meta = load_meta(name)
    meta.update({"name": name, "created": meta.get("created") or now_iso(), "source": source,
                 "duration": round(t_end - t0, 3), "audio": audio_info, "video": video_info,
                 "settings": {k: settings[k] for k in ("handed", "flip_x", "flip_y")}})
    save_meta(name, meta)
    return run_analysis(name, settings)


def _series(an: SwingAnalysis) -> dict:
    def arr(a):
        return [None if (isinstance(v, float) and math.isnan(v)) else round(float(v), 4) for v in a]
    return {"t": arr(an.t), "trail_pct": arr(an.trail_pct), "cop_x_cm": arr(an.cop_x_cm),
            "cop_y_cm": arr(an.cop_y_cm), "force_pct": arr(an.force_pct), "segment": list(an.segment)}


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


class SimPresentBody(BaseModel):
    present: bool


class AnalyzeBody(BaseModel):
    bw: float | None = None
    top: float | None = None
    impact: float | None = None
    reset: bool = False


class PairBody(BaseModel):
    seconds: float = 10.0
    forget_first: bool = False


class ForgetBody(BaseModel):
    address: str


def create_app(sim: bool = False) -> FastAPI:
    DATA_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
    settings = load_settings()
    reader = BoardReader(sim, settings)
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
                                          dict(reader.settings), media_mgr)
            await send_all({"type": "auto_saved", "name": ev["name"], "analysis": res})
        except Exception as exc:
            await send_all({"type": "auto_saved", "name": ev["name"], "error": str(exc)})

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
                else:
                    await send_all(ev)
            with reader.lock:
                s = reader.latest
            if s is not None and s["t"] != last_t:
                last_t = s["t"]
                kg = s["kg"]
                await send_all({"type": "s", "t": round(s["t"], 4),
                                "tr": round(kg["TR"], 2), "br": round(kg["BR"], 2),
                                "tl": round(kg["TL"], 2), "bl": round(kg["BL"], 2),
                                "tot": round(s["total"], 2),
                                "x": round(s["cop_x"], 4), "y": round(s["cop_y"], 4)})
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
                                                              info["t_end"], "manual", dict(reader.settings), media_mgr)
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
        with reader.lock:
            bw = body.body_weight_kg if (body.body_weight_kg or 0) >= 20 else None
            reader.settings.update(handed=body.handed, flip_x=body.flip_x, flip_y=body.flip_y,
                                   audio=body.audio, video=body.video, audio_device=body.audio_device,
                                   camera=body.camera, body_weight_kg=bw)
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
                                           body.impact, body.reset)
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
