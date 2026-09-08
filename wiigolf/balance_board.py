"""
Lectura de una Wii Balance Board (RVL-WBC-01) por HID usando `hidapi`.

La Balance Board habla el mismo protocolo que un Wiimote. Sus 4 células de
carga son una "extensión" cuyos datos llegan dentro de los informes de datos.

Protocolo (resumen):
  VID 0x057E (Nintendo), PID 0x0306.
  Informes de SALIDA (host -> tabla); primer byte = report ID:
    0x11 LEDs            [0x11, flags]
    0x12 reporting mode  [0x12, flags, tipo]      tipo 0x32 = botones + 8 bytes ext.
    0x15 status          [0x15, 0x00]
    0x16 write memory    [0x16, A3,A2,A1,A0, size, data(16)]
    0x17 read memory     [0x17, A3,A2,A1,A0, size_hi, size_lo]
  Informes de ENTRADA (tabla -> host); primer byte = report ID:
    0x20 status
    0x21 read-memory data [0x21, btn,btn, SE, addr_hi,addr_lo, data(16)]
    0x32.. data reports   [0x32, btn,btn, ext0..ext7]

Las 4 células vienen como uint16 big-endian en este orden:
    Top-Right, Bottom-Right, Top-Left, Bottom-Left.

Calibración: 24 bytes en el registro 0xA40024 = 3 grupos (0 kg, 17 kg, 34 kg),
cada grupo con los 4 valores de referencia en el mismo orden de sensores.

NOTA (Windows): enviar informes de salida a dispositivos tipo Wiimote por la
pila Bluetooth de Windows es históricamente frágil. Si `init()`/`calibrate()`
no reciben respuesta, ver el README (alternativa Linux / Raspberry Pi con
xwiimote, mucho más robusta).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

try:
    import hid  # paquete PyPI: hidapi  (proporciona el módulo `hid`)
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Falta el paquete 'hidapi'. Instálalo con:  pip install hidapi"
    ) from exc


NINTENDO_VID = 0x057E
BALANCE_BOARD_PID = 0x0306

# Report IDs de salida
_OUT_LED = 0x11
_OUT_REPORTING = 0x12
_OUT_STATUS = 0x15
_OUT_WRITE_MEM = 0x16
_OUT_READ_MEM = 0x17

# Flags / tipos
_CONTINUOUS = 0x04
_MODE_EXT8 = 0x32  # botones + 8 bytes de extensión (suficiente para la tabla)

# Longitud fija de los informes de salida del Wiimote (report id + 21 bytes)
_OUT_REPORT_LEN = 22

# Registros
_REG_EXT_INIT_A = 0xA400F0
_REG_EXT_INIT_B = 0xA400FB
_REG_CALIBRATION = 0xA40024

# Orden de sensores en los datos crudos y en la calibración
SENSORS = ("TR", "BR", "TL", "BL")

# Carga mínima (kg) para calcular el centro de presión. Por debajo, el CoP es
# ruido (dividir entre ~0), así que se reporta (0, 0).
MIN_LOAD_KG = 2.0

# Distancia aproximada entre sensores (mm). Solo para pasar el CoP a cm; el
# valor normalizado [-1, 1] no depende de esto. Ajustable si se mide la tabla.
BOARD_WIDTH_MM = 433.0   # eje X (izquierda-derecha)
BOARD_LENGTH_MM = 228.0  # eje Y (atrás-delante)


@dataclass
class Reading:
    """Una muestra ya calibrada de la tabla."""

    t: float                 # segundos (time.perf_counter) al recibir el informe
    kg: dict                 # {"TR":..,"BR":..,"TL":..,"BL":..} en kg
    total: float             # peso total en kg
    cop_x: float             # centro de presión normalizado [-1(izq)..+1(der)]
    cop_y: float             # centro de presión normalizado [-1(atrás)..+1(delante)]

    @property
    def cop_x_cm(self) -> float:
        return self.cop_x * (BOARD_WIDTH_MM / 2.0) / 10.0

    @property
    def cop_y_cm(self) -> float:
        return self.cop_y * (BOARD_LENGTH_MM / 2.0) / 10.0


class BalanceBoard:
    """Envuelve una Wii Balance Board conectada por Bluetooth (HID)."""

    def __init__(self, path: bytes | None = None):
        self.path = path
        self.dev = hid.device()
        self.is_open = False
        # calibración: dict con listas [TR, BR, TL, BL] para 0, 17 y 34 kg
        self.calibration: dict | None = None

    # ------------------------------------------------------------------ #
    # Descubrimiento / conexión
    # ------------------------------------------------------------------ #
    @staticmethod
    def enumerate() -> list[dict]:
        """Todos los HID que parecen una Balance Board / Wiimote (VID Nintendo)."""
        return [d for d in hid.enumerate() if d.get("vendor_id") == NINTENDO_VID]

    def open(self) -> "BalanceBoard":
        if self.path:
            self.dev.open_path(self.path)
        else:
            self.dev.open(NINTENDO_VID, BALANCE_BOARD_PID)
        # Lectura con timeout (no bloqueante indefinido)
        try:
            self.dev.set_nonblocking(False)
        except Exception:
            pass
        self.is_open = True
        return self

    def close(self) -> None:
        if self.is_open:
            try:
                self.dev.close()
            finally:
                self.is_open = False

    def __enter__(self) -> "BalanceBoard":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Primitivas HID
    # ------------------------------------------------------------------ #
    def _write(self, data: list[int]) -> int:
        """Envía un informe de salida (rellenado a la longitud fija)."""
        buf = list(data) + [0x00] * (_OUT_REPORT_LEN - len(data))
        return self.dev.write(buf)

    def _read_raw(self, timeout_ms: int = 200) -> list[int]:
        """Lee un informe de entrada; [] si expira el timeout."""
        return self.dev.read(_OUT_REPORT_LEN, timeout_ms)

    def _write_register(self, addr: int, values: list[int]) -> None:
        payload = [
            _OUT_WRITE_MEM,
            0x04, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF,
            len(values),
        ] + list(values)
        self._write(payload)

    def _request_register(self, addr: int, size: int) -> None:
        self._write([
            _OUT_READ_MEM,
            0x04, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF,
            (size >> 8) & 0xFF, size & 0xFF,
        ])

    # ------------------------------------------------------------------ #
    # Inicialización
    # ------------------------------------------------------------------ #
    def set_led(self, on: bool = True) -> None:
        """Enciende/apaga el LED azul. Útil para confirmar que la tabla recibe
        informes de salida (si el LED responde, el canal de salida funciona)."""
        self._write([_OUT_LED, 0x10 if on else 0x00])

    def init(self) -> None:
        """Inicializa la extensión (las células de carga) y fija el modo de datos."""
        # Activa la extensión (necesario en tablas con firmware reciente)
        self._write_register(_REG_EXT_INIT_A, [0x55])
        time.sleep(0.05)
        self._write_register(_REG_EXT_INIT_B, [0x00])
        time.sleep(0.05)
        # Pide informes continuos de botones + 8 bytes de extensión
        self._write([_OUT_REPORTING, _CONTINUOUS, _MODE_EXT8])
        time.sleep(0.05)

    def calibrate(self, timeout_s: float = 3.0) -> dict:
        """Lee los 24 bytes de calibración de fábrica de la tabla."""
        self._request_register(_REG_CALIBRATION, 24)
        raw = bytearray()
        deadline = time.time() + timeout_s
        while len(raw) < 24 and time.time() < deadline:
            rpt = self._read_raw(300)
            if not rpt:
                continue
            if rpt[0] == 0x21:  # read-memory data
                se = rpt[3]
                err = se & 0x0F
                size = (se >> 4) + 1
                if err:
                    raise RuntimeError(
                        f"Error leyendo calibración (código 0x{err:X}); "
                        f"¿se llamó a init() y responde la tabla?"
                    )
                raw += bytes(rpt[6:6 + size])
        if len(raw) < 24:
            raise TimeoutError(
                "No llegó la calibración (0 bytes). En Windows suele significar "
                "que los informes de salida no alcanzan la tabla. Ver README."
            )

        def group(off: int) -> list[int]:
            return [(raw[off + 2 * i] << 8) | raw[off + 2 * i + 1] for i in range(4)]

        self.calibration = {"kg0": group(0), "kg17": group(8), "kg34": group(16)}
        return self.calibration

    # ------------------------------------------------------------------ #
    # Lectura de muestras
    # ------------------------------------------------------------------ #
    def read_raw(self, timeout_ms: int = 200) -> dict | None:
        """Devuelve los 4 valores crudos {TR,BR,TL,BL} o None si no hay dato."""
        rpt = self._read_raw(timeout_ms)
        if not rpt:
            return None
        # Informes de datos: 0x30..0x3F. Nos interesan los que traen extensión.
        if rpt[0] not in (0x32, 0x34, 0x35, 0x36, 0x37):
            return None
        ext = rpt[3:11]  # 8 bytes de extensión tras los 2 de botones
        if len(ext) < 8:
            return None
        vals = [(ext[2 * i] << 8) | ext[2 * i + 1] for i in range(4)]
        return dict(zip(SENSORS, vals))

    def _calib_sensor(self, raw: int, cal0: int, cal17: int, cal34: int) -> float:
        if raw <= cal17:
            span = cal17 - cal0
            return 0.0 if span == 0 else 17.0 * (raw - cal0) / span
        span = cal34 - cal17
        return 17.0 if span == 0 else 17.0 + 17.0 * (raw - cal17) / span

    def read(self, timeout_ms: int = 200, clamp_negative: bool = True) -> Reading | None:
        """Lee una muestra calibrada (kg, peso total y centro de presión)."""
        if self.calibration is None:
            raise RuntimeError("Llama a calibrate() antes de read().")
        raw = self.read_raw(timeout_ms)
        if raw is None:
            return None
        t = time.perf_counter()
        c = self.calibration
        kg = {}
        for i, s in enumerate(SENSORS):
            v = self._calib_sensor(raw[s], c["kg0"][i], c["kg17"][i], c["kg34"][i])
            if clamp_negative and v < 0:
                v = 0.0
            kg[s] = v
        total = sum(kg.values())
        if total >= MIN_LOAD_KG:
            cop_x = ((kg["TR"] + kg["BR"]) - (kg["TL"] + kg["BL"])) / total
            cop_y = ((kg["TL"] + kg["TR"]) - (kg["BL"] + kg["BR"])) / total
        else:
            cop_x = cop_y = 0.0
        return Reading(t=t, kg=kg, total=total, cop_x=cop_x, cop_y=cop_y)
