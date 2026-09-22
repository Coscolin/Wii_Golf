"""
Geometría de DOS Wii Balance Boards, una por pie.

Colocación (decidida para esta versión): las dos tablas lado a lado y **giradas 90°**
respecto al uso normal: el lado largo (51,1 cm) queda en la dirección punta-talón del
pie y el lado corto (31,6 cm) en el eje lateral. Ambas con el borde del botón de
encendido hacia el mismo lado del golfista (`button` = "left" o "right").

Sistema de coordenadas del golfista (cm):
    x  lateral, + hacia su derecha        y  punta-talón, + hacia la punta
La tabla izquierda está centrada en x = -D/2 y la derecha en x = +D/2, con
D = hueco entre bordes interiores + 31,6 cm.

Convención de la tabla (la misma que con una sola tabla, ver balance_board.py):
    cop_x_local > 0 -> lado de los sensores TR/BR (la derecha del golfista si la tabla
                       está en posición normal)
    cop_y_local > 0 -> sensores "top" TR/TL (el borde del botón; la punta de los pies
                       si la tabla está en posición normal)
Con el botón hacia la IZQUIERDA la tabla está girada 90° antihorario: el eje local +x
pasa a ser la punta (+y del golfista) y el eje local +y pasa a ser la izquierda (-x).
Con el botón hacia la DERECHA es el giro contrario (los dos signos cambian).

Todas las funciones aceptan escalares o arrays de numpy (misma fórmula).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .balance_board import BOARD_LENGTH_MM, BOARD_WIDTH_MM, MIN_LOAD_KG, SENSORS

# Distancia entre sensores (medio lado), en cm: define la escala del CoP local
HALF_X_CM = BOARD_WIDTH_MM / 20.0    # 21.65  (eje largo de la tabla)
HALF_Y_CM = BOARD_LENGTH_MM / 20.0   # 11.4   (eje corto)

# Dimensiones físicas exteriores de la tabla, en cm (para el hueco y las siluetas)
BOARD_PHYS_WIDTH_CM = 51.1   # lado largo
BOARD_PHYS_DEPTH_CM = 31.6   # lado corto

BUTTON_SIDES = ("left", "right")


@dataclass(frozen=True)
class Layout:
    """Colocación de las dos tablas."""

    gap_cm: float | None = None      # hueco entre bordes interiores (None = sin indicar)
    button: str = "left"             # hacia dónde apunta el botón de encendido

    def __post_init__(self):
        if self.button not in BUTTON_SIDES:
            raise ValueError("button debe ser 'left' o 'right'")

    @property
    def center_distance_cm(self) -> float:
        """Distancia entre los centros de las dos tablas (giradas: lado corto + hueco)."""
        return float(self.gap_cm or 0.0) + BOARD_PHYS_DEPTH_CM

    @property
    def sign(self) -> int:
        return 1 if self.button == "left" else -1

    def to_dict(self) -> dict:
        return {"gap_cm": self.gap_cm, "button": self.button,
                "center_distance_cm": round(self.center_distance_cm, 2)}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Layout":
        d = d or {}
        gap = d.get("gap_cm")
        return cls(gap_cm=None if gap is None else float(gap),
                   button=d.get("button") if d.get("button") in BUTTON_SIDES else "left")

    @classmethod
    def from_settings(cls, settings: dict) -> "Layout":
        return cls.from_dict({"gap_cm": settings.get("dual_gap_cm"),
                              "button": settings.get("dual_button")})


def _sum4(kg) -> np.ndarray | float:
    return kg["TR"] + kg["BR"] + kg["TL"] + kg["BL"]


def foot_cop_cm(kg: dict, layout: Layout):
    """CoP de un pie en el sistema del golfista (x lateral, y punta-talón), en cm.

    `kg` = {"TR","BR","TL","BL"} escalares o arrays. Con carga < MIN_LOAD_KG el CoP
    del pie se toma en el centro de su tabla (0, 0): dividir entre ~0 es ruido.
    Devuelve (total, x_cm, y_cm)."""
    total = _sum4(kg)
    with np.errstate(divide="ignore", invalid="ignore"):
        safe = np.where(np.asarray(total) >= MIN_LOAD_KG, total, np.nan)
        cx = ((kg["TR"] + kg["BR"]) - (kg["TL"] + kg["BL"])) / safe   # eje local largo
        cy = ((kg["TL"] + kg["TR"]) - (kg["BL"] + kg["BR"])) / safe   # eje local corto
    cx = np.nan_to_num(cx, nan=0.0)
    cy = np.nan_to_num(cy, nan=0.0)
    s = layout.sign
    x_lat = -s * cy * HALF_Y_CM
    y_ap = s * cx * HALF_X_CM
    if np.ndim(total) == 0:
        return float(total), float(x_lat), float(y_ap)
    return total, x_lat, y_ap


def combine(kg_left: dict, kg_right: dict, layout: Layout) -> dict:
    """Carga y CoP por pie y CoP global de las dos tablas.

    Devuelve {"total", "cop_x_cm", "cop_y_cm", "L": {"kg","x_cm","y_cm"}, "R": {...}}.
    El CoP global es la media de los CoP de cada pie ponderada por su carga, con cada
    tabla en x = ∓D/2. Con total < MIN_LOAD_KG el CoP global es (0, 0)."""
    w_l, xl, yl = foot_cop_cm(kg_left, layout)
    w_r, xr, yr = foot_cop_cm(kg_right, layout)
    total = w_l + w_r
    half = layout.center_distance_cm / 2.0
    with np.errstate(divide="ignore", invalid="ignore"):
        safe = np.where(np.asarray(total) >= MIN_LOAD_KG, total, np.nan)
        gx = (w_l * (xl - half) + w_r * (xr + half)) / safe
        gy = (w_l * yl + w_r * yr) / safe
    gx = np.nan_to_num(gx, nan=0.0)
    gy = np.nan_to_num(gy, nan=0.0)
    if np.ndim(total) == 0:
        total, gx, gy = float(total), float(gx), float(gy)
    return {"total": total, "cop_x_cm": gx, "cop_y_cm": gy,
            "L": {"kg": w_l, "x_cm": xl, "y_cm": yl},
            "R": {"kg": w_r, "x_cm": xr, "y_cm": yr}}


def spread_foot_load(total_kg: float, x_cm: float, y_cm: float, layout: Layout) -> dict:
    """Inversa de foot_cop_cm para el simulador y el swing sintético: reparte `total_kg`
    entre los 4 sensores de una tabla girada para que su CoP quede en (x_cm, y_cm) del
    sistema del golfista. Recorta el CoP al rectángulo de sensores."""
    s = layout.sign
    cx = float(np.clip(s * y_cm / HALF_X_CM, -1.0, 1.0))   # eje local largo
    cy = float(np.clip(-s * x_cm / HALF_Y_CM, -1.0, 1.0))  # eje local corto
    fx = 0.5 + cx / 2.0   # fracción en el lado TR/BR
    fy = 0.5 + cy / 2.0   # fracción en el lado TR/TL
    return {"TR": total_kg * fx * fy, "BR": total_kg * fx * (1 - fy),
            "TL": total_kg * (1 - fx) * fy, "BL": total_kg * (1 - fx) * (1 - fy)}


__all__ = ["Layout", "HALF_X_CM", "HALF_Y_CM", "BOARD_PHYS_WIDTH_CM", "BOARD_PHYS_DEPTH_CM",
           "BUTTON_SIDES", "SENSORS", "foot_cop_cm", "combine", "spread_foot_load"]
