"""
Captura de audio (micrófono) y vídeo (webcam) sincronizados con la tabla.

Ambos capturan en continuo en un hilo propio y guardan en un buffer circular
con marcas de tiempo de time.perf_counter(), el mismo reloj que usa el hilo
lector de la tabla. Al guardar un swing se recorta el tramo [t_ini, t_fin]:

  - audio -> data/<nombre>.wav (mono, int16) y detección del impacto: el golpe
             a la bola es un transitorio muy corto y mucho más fuerte que el
             ruido de fondo; se busca el pico de energía y su inicio.
  - vídeo -> data/<nombre>.frames.zip: un JPEG por fotograma + index.json con
             el instante de cada uno (relativo al CSV). Se guarda como
             secuencia de JPEG y no como MP4 para no depender de códecs y poder
             reproducir a cámara lenta fotograma a fotograma en el navegador
             con sincronía exacta respecto al CoP.

Dependencias opcionales: sounddevice (audio) y opencv-python (vídeo). Si
faltan, la función correspondiente queda desactivada con un mensaje.
"""

from __future__ import annotations

import json
import os
import threading
import time
import wave
import zipfile
from collections import deque
from pathlib import Path

import numpy as np

try:
    import sounddevice as sd
    AUDIO_AVAILABLE, AUDIO_ERROR = True, None
except Exception as exc:  # falta el paquete o PortAudio
    sd, AUDIO_AVAILABLE, AUDIO_ERROR = None, False, str(exc)

try:
    import cv2
    VIDEO_AVAILABLE, VIDEO_ERROR = True, None
except Exception as exc:
    cv2, VIDEO_AVAILABLE, VIDEO_ERROR = None, False, str(exc)


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
def list_audio_inputs() -> list[dict]:
    if not AUDIO_AVAILABLE:
        return []
    out = []
    try:
        default = sd.default.device[0]
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                out.append({"index": i, "name": d["name"], "default": i == default})
    except Exception:
        pass
    return out


class AudioRecorder:
    """Micrófono en continuo con buffer circular (mono, int16)."""

    def __init__(self, buffer_s: float = 12.0, fs: int = 48000, device: int | None = None):
        self.buffer_s = buffer_s
        self.fs = fs
        self.device = device
        self.lock = threading.Lock()
        self.chunks: deque = deque()   # (t_primera_muestra, np.ndarray int16)
        self._seconds = 0.0
        self._stream = None
        self.running = False
        self.error: str | None = None
        self.device_name: str | None = None
        self.level = 0.0               # RMS del último bloque (0..1)

    def start(self) -> None:
        if not AUDIO_AVAILABLE:
            raise RuntimeError(f"Audio no disponible (sounddevice): {AUDIO_ERROR}")
        if self.running:
            return

        def callback(indata, frames, _time_info, _status):
            t = time.perf_counter() - frames / self.fs   # instante de la 1ª muestra
            data = indata[:, 0].copy()
            self.level = float(np.sqrt(np.mean((data.astype(np.float32) / 32768.0) ** 2)))
            with self.lock:
                self.chunks.append((t, data))
                self._seconds += frames / self.fs
                while self._seconds > self.buffer_s and len(self.chunks) > 1:
                    _, old = self.chunks.popleft()
                    self._seconds -= old.size / self.fs

        try:
            self._stream = sd.InputStream(samplerate=self.fs, channels=1, dtype="int16",
                                          blocksize=1024, device=self.device, callback=callback)
            self._stream.start()
            dev = self._stream.device
            info = sd.query_devices(dev) if dev is not None else sd.query_devices(kind="input")
            self.device_name = info["name"] if info else None
            self.running, self.error = True, None
        except Exception as exc:
            self._stream = None
            self.running, self.error = False, str(exc)
            raise RuntimeError(f"No se pudo abrir el micrófono: {exc}") from exc

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        self.running = False
        self.level = 0.0
        with self.lock:
            self.chunks.clear()
            self._seconds = 0.0

    def extract(self, t_start: float, t_end: float):
        """(t_primera_muestra, muestras int16) del tramo, o None."""
        with self.lock:
            chunks = list(self.chunks)
        parts, t_first = [], None
        for t, data in chunks:
            n = data.size
            if t + n / self.fs < t_start or t > t_end:
                continue
            i0 = max(0, int(round((t_start - t) * self.fs)))
            i1 = min(n, int(round((t_end - t) * self.fs)))
            if i1 <= i0:
                continue
            if t_first is None:
                t_first = t + i0 / self.fs
            parts.append(data[i0:i1])
        if not parts:
            return None
        return t_first, np.concatenate(parts)

    def save(self, path: Path, t_start: float, t_end: float, t0: float) -> dict | None:
        """Guarda el tramo como WAV. Los instantes devueltos son relativos a t0."""
        seg = self.extract(t_start, t_end)
        if seg is None:
            return None
        t_first, samples = seg
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.fs)
            w.writeframes(samples.tobytes())
        return {"file": path.name, "fs": self.fs, "t_start": round(t_first - t0, 4),
                "n": int(samples.size), "device": self.device_name}


def load_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as w:
        fs = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    x = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        x = x.reshape(-1, ch)[:, 0]
    return fs, x


def detect_impact(samples: np.ndarray, fs: int, t_first: float,
                  window: tuple[float, float] | None = None, min_snr: float = 8.0,
                  min_peak: float = 0.02) -> dict:
    """
    Busca el transitorio del golpe a la bola.

    Energía RMS en tramos de 2 ms; el impacto es el tramo de máxima energía
    (dentro de `window` si se da) siempre que supere `min_snr` veces la
    mediana. El instante devuelto es el INICIO del transitorio (se retrocede
    desde el pico mientras la energía siga por encima del 30 % del máximo).
    Además el pico debe superar `min_peak` (RMS, 1.0 = fondo de escala) para
    no dar por golpe el ruido de un micrófono en silencio.
    Devuelve {"t": instante o None, "snr": relación pico/mediana, "peak": nivel,
    "t_peak": ...} en la misma base de tiempo que `t_first`.
    """
    x = samples.astype(np.float32) / 32768.0
    frame = max(1, int(fs * 0.002))
    n = x.size // frame
    if n < 10:
        return {"t": None, "snr": 0.0, "peak": 0.0}
    e = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1))
    t_frames = t_first + (np.arange(n) * frame + frame / 2) / fs
    mask = np.ones(n, bool)
    if window is not None:
        mask = (t_frames >= window[0]) & (t_frames <= window[1])
        if not mask.any():
            mask = np.ones(n, bool)
    med = float(np.median(e)) + 1e-6
    k = int(np.argmax(np.where(mask, e, -1.0)))
    peak = float(e[k])
    snr = peak / med
    if snr < min_snr or peak < min_peak:
        return {"t": None, "snr": round(snr, 1), "peak": round(peak, 4), "t_peak": float(t_frames[k])}
    j, thr = k, 0.3 * peak
    while j > 0 and e[j - 1] >= thr and (k - j + 1) * frame / fs < 0.03:
        j -= 1
    return {"t": float(t_frames[j]), "snr": round(snr, 1), "peak": round(peak, 4), "t_peak": float(t_frames[k])}


# --------------------------------------------------------------------------- #
# Vídeo
# --------------------------------------------------------------------------- #
class VideoRecorder:
    """Webcam en continuo; guarda cada fotograma ya comprimido en JPEG."""

    def __init__(self, buffer_s: float = 12.0, device: int = 0, width: int = 640,
                 height: int = 480, fps: int = 30, quality: int = 80):
        self.buffer_s = buffer_s
        self.device = device
        self.width, self.height, self.fps, self.quality = width, height, fps, quality
        self.lock = threading.Lock()
        self.frames: deque = deque(maxlen=int(buffer_s * fps * 1.5))   # (t, jpeg)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        self.error: str | None = None
        self.fps_measured = 0.0
        self.size = (width, height)

    def start(self, timeout_s: float = 8.0) -> None:
        if not VIDEO_AVAILABLE:
            raise RuntimeError(f"Vídeo no disponible (opencv-python): {VIDEO_ERROR}")
        if self.running:
            return
        self._stop.clear()
        self.error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="video")
        self._thread.start()
        t_end = time.time() + timeout_s
        while time.time() < t_end and not self.running and self.error is None:
            time.sleep(0.05)
        if self.error:
            raise RuntimeError(self.error)
        if not self.running:
            self._stop.set()
            raise RuntimeError("La cámara no responde")

    def _run(self) -> None:
        cap = cv2.VideoCapture(self.device, cv2.CAP_DSHOW) if os.name == "nt" \
            else cv2.VideoCapture(self.device)
        if not cap.isOpened():
            self.error = f"No se pudo abrir la cámara {self.device}"
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
        n, t_hz = 0, time.perf_counter()
        fails = 0
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                t = time.perf_counter()
                if not ok:
                    fails += 1
                    if fails > 30:
                        self.error = "La cámara dejó de dar imágenes"
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                ok2, buf = cv2.imencode(".jpg", frame, params)
                if not ok2:
                    continue
                self.size = (int(frame.shape[1]), int(frame.shape[0]))
                with self.lock:
                    self.frames.append((t, buf.tobytes()))
                self.running = True
                n += 1
                if t - t_hz >= 1.0:
                    self.fps_measured = n / (t - t_hz)
                    n, t_hz = 0, t
        finally:
            cap.release()
            self.running = False

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        self.running = False
        self.fps_measured = 0.0
        with self.lock:
            self.frames.clear()

    def snapshot(self) -> bytes | None:
        with self.lock:
            return self.frames[-1][1] if self.frames else None

    def save(self, path: Path, t_start: float, t_end: float, t0: float) -> dict | None:
        """Guarda los fotogramas del tramo en un zip (JPEG + index.json)."""
        with self.lock:
            frames = [(t, b) for t, b in self.frames if t_start <= t <= t_end]
        if not frames:
            return None
        times = [round(t - t0, 4) for t, _ in frames]
        index = {"t": times, "width": self.size[0], "height": self.size[1],
                 "fps": round(self.fps_measured, 1)}
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            for i, (_, b) in enumerate(frames):
                z.writestr(f"{i:04d}.jpg", b)
            z.writestr("index.json", json.dumps(index))
        return {"file": path.name, "n": len(frames), "t_first": times[0], "t_last": times[-1],
                "width": self.size[0], "height": self.size[1], "fps": index["fps"]}


def read_frames_index(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("index.json"))


def read_frame(path: Path, i: int) -> bytes:
    with zipfile.ZipFile(path) as z:
        return z.read(f"{i:04d}.jpg")
