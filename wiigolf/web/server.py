"""
Servidor web local para la Wii Balance Board.

- Un hilo lee la tabla (~100 Hz), aplica la tara, graba a CSV si hay una
  grabación activa y alimenta la captura automática de swings.
- El navegador recibe las muestras por WebSocket (~50 Hz) y controla la
  grabación, la tara, los ajustes y el análisis mediante una API REST.
- Los análisis reutilizan wiigolf.analysis y dejan la figura en out/.

Arranque:  python scripts/04_web.py [--host 0.0.0.0] [--port 8000] [--sim]

Captura automática: con el modo armado, se guarda un buffer de los últimos
segundos; cuando el % de peso en el pie trail cae ≥ AUTO_DROP_PCT puntos en
menos de AUTO_WINDOW_S (el downswing), se espera AUTO_POST_S y se guarda
data/auto_<fecha>.csv con el tramo [disparo - AUTO_PRE_S, disparo + AUTO_POST_S],
que se analiza al vuelo.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
import math
import queue
import random
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..analysis import analyze, load_csv, plot
from ..balance_board import MIN_LOAD_KG, SENSORS, BalanceBoard

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
STATIC_DIR = Path(__file__).parent / "static"
SETTINGS_FILE = DATA_DIR / "settings.json"

CSV_HEADER = ["t", "TR", "BR", "TL", "BL", "total", "cop_x", "cop_y"]
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")

STANDING_KG = 20.0       # por debajo, no hay nadie sobre la tabla
BUFFER_S = 8.0           # segundos que guarda el buffer circular
AUTO_DROP_PCT = 25.0     # caída de trail% que dispara la captura automática
AUTO_WINDOW_S = 0.5      # ... medida en esta ventana
AUTO_PRE_S = 3.0         # segundos guardados antes del disparo
AUTO_POST_S = 1.5        # segundos guardados después del disparo
AUTO_REFRACTORY_S = 4.0  # tiempo mínimo entre capturas

DEFAULT_SETTINGS = {"handed": "right", "flip_x": False, "flip_y": False}


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
        return {"name": self.path.stem, "samples": self.n, "duration": round(dur, 2)}


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
        trail, force, y = self._interp((t - self.t0) % self.PERIOD)
        total = force / 100.0 * self.bw
        right = total * trail / 100.0
        left = total - right
        ft = 0.5 + y / 2.0
        kg = {"TR": right * ft, "BR": right * (1 - ft), "TL": left * ft, "BL": left * (1 - ft)}
        return t, {k: max(0.0, v + random.gauss(0, 0.15)) for k, v in kg.items()}


# --------------------------------------------------------------------------- #
# Hilo lector
# --------------------------------------------------------------------------- #
class BoardReader(threading.Thread):
    def __init__(self, sim: bool, settings: dict):
        super().__init__(daemon=True, name="board-reader")
        self.sim = sim
        self.settings = settings
        self.stop_event = threading.Event()
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
        self.events: queue.Queue = queue.Queue()

    # ---- bucle principal ------------------------------------------------ #
    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._run_sim() if self.sim else self._run_real()
            except Exception as exc:
                with self.lock:
                    self.connected = False
                    self.hz = 0.0
                    self.error = str(exc)
                self.stop_event.wait(3.0)

    def _run_sim(self) -> None:
        sim = SimBoard()
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
            raise RuntimeError("Tabla no encontrada (VID 0x057E). ¿Está emparejada y encendida?")
        board = BalanceBoard(path=boards[0]["path"])
        board.open()
        try:
            board.init()
            board.calibrate()
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
        if tp is None:
            return
        mx = None
        for prev in reversed(self.buffer):
            if t - prev["t"] > AUTO_WINDOW_S:
                break
            if prev["total"] < STANDING_KG:
                return  # no ha estado de pie durante toda la ventana
            v = trail_pct(prev, self.settings)
            if v is not None and (mx is None or v > mx):
                mx = v
        if mx is not None and mx - tp >= AUTO_DROP_PCT:
            self._auto_trigger_t = t
            self._auto_last = t
            self.events.put({"type": "auto_trigger"})

    def _auto_save(self) -> None:
        t_trig = self._auto_trigger_t
        self._auto_trigger_t = None
        samples = [x for x in self.buffer if x["t"] >= t_trig - AUTO_PRE_S]
        name = "auto_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = unique_path(DATA_DIR / f"{name}.csv")
        write_csv(path, samples)
        self.auto_count += 1
        self.events.put({"type": "auto_saved", "name": path.stem})

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
# Análisis y listado
# --------------------------------------------------------------------------- #
_analysis_lock = threading.Lock()


def run_analysis(name: str, settings: dict, bw: float | None = None,
                 t_top: float | None = None, t_impact: float | None = None) -> dict:
    csv_path = DATA_DIR / f"{name}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"No existe {csv_path.name}")
    with _analysis_lock:
        cap = load_csv(csv_path)
        an = analyze(cap, handed=settings["handed"], flip_x=settings["flip_x"],
                     flip_y=settings["flip_y"], bw_kg=bw, t_top=t_top, t_impact=t_impact)
        png = OUT_DIR / f"{name}.png"
        plot(an, out_path=png)
    return {
        "name": name, "metrics": json_clean(an.metrics()), "notes": an.notes,
        "png": f"/out/{name}.png?v={int(png.stat().st_mtime)}",
        "samples": int(cap.t.size), "duration": round(float(cap.t[-1] - cap.t[0]), 2),
        "hz": round(cap.fs, 1),
    }


def list_swings() -> list[dict]:
    items = []
    for p in sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        png = OUT_DIR / f"{p.stem}.png"
        items.append({
            "name": p.stem, "size": p.stat().st_size, "mtime": p.stat().st_mtime,
            "analyzed": png.exists(),
            "png": f"/out/{p.stem}.png?v={int(png.stat().st_mtime)}" if png.exists() else None,
        })
    return items


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


class AnalyzeBody(BaseModel):
    bw: float | None = None
    top: float | None = None
    impact: float | None = None


def create_app(sim: bool = False) -> FastAPI:
    DATA_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
    settings = load_settings()
    reader = BoardReader(sim, settings)
    clients: set[WebSocket] = set()

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
            res = await asyncio.to_thread(run_analysis, ev["name"], dict(reader.settings))
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
                await send_all({"type": "status", **reader.status()})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        reader.start()
        task = asyncio.create_task(broadcaster())
        try:
            yield
        finally:
            task.cancel()
            reader.stop_event.set()
            reader.join(timeout=2.0)

    app = FastAPI(title="Wii Golf", lifespan=lifespan)
    app.mount("/out", StaticFiles(directory=OUT_DIR), name="out")

    def check_name(name: str) -> str:
        if not NAME_RE.match(name):
            raise HTTPException(400, "Nombre no válido")
        return name

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"type": "status", **reader.status()})
        clients.add(websocket)
        try:
            while True:
                await websocket.receive_text()  # solo keepalive; el cliente no manda datos
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(websocket)

    @app.get("/api/status")
    async def api_status():
        return reader.status()

    @app.post("/api/tare")
    async def api_tare():
        try:
            offsets = reader.set_tare()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"tare": offsets, "status": reader.status()}

    @app.post("/api/tare/clear")
    async def api_tare_clear():
        reader.clear_tare()
        return {"status": reader.status()}

    @app.post("/api/record/start")
    async def api_record_start(body: RecordStart):
        try:
            name = reader.start_recording(body.name)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"name": name, "status": reader.status()}

    @app.post("/api/record/stop")
    async def api_record_stop(body: RecordStop):
        try:
            info = reader.stop_recording()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        out: dict = {"recording": info, "status": reader.status()}
        if body.analyze and info["samples"] > 0:
            try:
                out["analysis"] = await asyncio.to_thread(run_analysis, info["name"], dict(reader.settings))
            except Exception as exc:
                out["analysis_error"] = str(exc)
        return out

    @app.post("/api/auto")
    async def api_auto(body: AutoBody):
        reader.set_auto(body.armed)
        return {"status": reader.status()}

    @app.post("/api/settings")
    async def api_settings(body: SettingsBody):
        if body.handed not in ("right", "left"):
            raise HTTPException(400, "handed debe ser right o left")
        with reader.lock:
            reader.settings.update(handed=body.handed, flip_x=body.flip_x, flip_y=body.flip_y)
            save_settings(reader.settings)
        return {"status": reader.status()}

    @app.get("/api/swings")
    async def api_swings():
        return list_swings()

    @app.post("/api/analyze/{name}")
    async def api_analyze(name: str, body: AnalyzeBody | None = None):
        check_name(name)
        body = body or AnalyzeBody()
        try:
            return await asyncio.to_thread(run_analysis, name, dict(reader.settings),
                                           body.bw, body.top, body.impact)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise HTTPException(500, f"Error en el análisis: {exc}")

    @app.get("/api/swings/{name}/csv")
    async def api_swing_csv(name: str):
        check_name(name)
        path = DATA_DIR / f"{name}.csv"
        if not path.exists():
            raise HTTPException(404, "No existe")
        return FileResponse(path, filename=path.name, media_type="text/csv")

    @app.delete("/api/swings/{name}")
    async def api_swing_delete(name: str):
        check_name(name)
        removed = []
        for p in (DATA_DIR / f"{name}.csv", OUT_DIR / f"{name}.png"):
            if p.exists():
                p.unlink()
                removed.append(p.name)
        if not removed:
            raise HTTPException(404, "No existe")
        return {"removed": removed}

    return app
