"""
Análisis de una captura de la Wii Balance Board para el swing de golf.

Entrada: CSV generado por la web o por scripts/02_live_read.py
  - una tabla:  columnas t, TR, BR, TL, BL, total, cop_x, cop_y
  - dos tablas: columnas t, L_TR, L_BR, L_TL, L_BL, R_TR, R_BR, R_TL, R_BL, total,
                cop_x_cm, cop_y_cm, t_L, t_R   (una tabla por pie, ver dual.py)

Salida: métricas + figura con
  1) trazo del centro de presión (CoP) sobre la silueta de la(s) tabla(s),
  2) % de peso sobre el pie trail vs tiempo,
  3) fuerza vertical (% del peso corporal) vs tiempo,
  4) (dos tablas) CoP punta-talón de cada pie vs tiempo,
  con marcas de address, top e impacto (≈).

Convención de ejes (una sola tabla, ambos pies):
  x > 0 = lado derecho de la tabla, y > 0 = sensores "top".
  Un diestro de pie mirando al frente de la tabla tiene el pie trail (derecho)
  en x > 0. Si en tu montaje sale invertido, usa flip_x / flip_y.
Con dos tablas el sistema es el del golfista (x lateral + derecha, y + punta) y la
colocación la describe `dual.Layout` (hueco entre tablas y lado del botón). `flip_x`
no se aplica en dual (para cambiar de pie se intercambian las tablas); `flip_y` sí.

Detección de eventos (heurística):
  - top      = máximo de carga trail justo antes del desplazamiento lateral
               más rápido hacia lead (el downswing).
  - impacto ≈ pico de fuerza vertical tras el top. El pico de GRF vertical se
               produce al final del downswing, muy cerca del impacto, así que
               es una aproximación razonable con solo la tabla.
  Para marcar el impacto de forma fiable hace falta una señal externa
  (micrófono o IMU) sincronizada; ambos eventos admiten override manual.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .balance_board import MIN_LOAD_KG, SENSORS
from .dual import (BOARD_PHYS_DEPTH_CM, BOARD_PHYS_WIDTH_CM, HALF_X_CM, HALF_Y_CM, Layout,
                   combine)

__all__ = ["Capture", "SwingAnalysis", "analyze", "load_csv", "plot", "smooth",
           "HALF_X_CM", "HALF_Y_CM", "DUAL_HEADER", "SINGLE_HEADER"]

SINGLE_HEADER = ["t", "TR", "BR", "TL", "BL", "total", "cop_x", "cop_y"]
DUAL_HEADER = (["t"] + [f"L_{s}" for s in SENSORS] + [f"R_{s}" for s in SENSORS]
               + ["total", "cop_x_cm", "cop_y_cm", "t_L", "t_R"])

# Umbrales de la heurística de detección
_MIN_TRAIL_SWING_PCT = 15.0   # rango mínimo de trail% para considerar que hay swing
_TOP_SEARCH_S = 1.0           # buscar el top hasta 1 s antes del downswing
_IMPACT_SEARCH_S = 0.6        # buscar el impacto hasta 0.6 s después del top


# --------------------------------------------------------------------------- #
# Carga de datos
# --------------------------------------------------------------------------- #
@dataclass
class Capture:
    """Serie cruda (ya en kg) leída del CSV. Una tabla: TR/BR/TL/BL. Dos tablas: L y R
    (dict sensor -> array) con la tabla izquierda y derecha del golfista."""

    t: np.ndarray
    TR: np.ndarray | None = None
    BR: np.ndarray | None = None
    TL: np.ndarray | None = None
    BL: np.ndarray | None = None
    L: dict | None = None
    R: dict | None = None
    source: str = ""

    @property
    def dual(self) -> bool:
        return self.L is not None and self.R is not None

    @property
    def total(self) -> np.ndarray:
        return self.right() + self.left()

    @property
    def fs(self) -> float:
        """Frecuencia de muestreo estimada (Hz)."""
        dt = np.diff(self.t)
        dt = dt[dt > 0]
        return float(1.0 / np.median(dt)) if dt.size else 0.0

    def right(self) -> np.ndarray:
        if self.dual:
            return sum(self.R[s] for s in SENSORS)
        return self.TR + self.BR

    def left(self) -> np.ndarray:
        if self.dual:
            return sum(self.L[s] for s in SENSORS)
        return self.TL + self.BL

    def top(self) -> np.ndarray:
        return self.TR + self.TL

    def bottom(self) -> np.ndarray:
        return self.BR + self.BL


def load_csv(path: str | Path) -> Capture:
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = reader.fieldnames or []
    if not rows:
        raise ValueError(f"CSV vacío: {path}")

    def col(name: str) -> np.ndarray:
        return np.array([float(r[name]) for r in rows], dtype=float)

    if "L_TR" in fields:  # dos tablas
        return Capture(t=col("t"), L={s: col(f"L_{s}") for s in SENSORS},
                       R={s: col(f"R_{s}") for s in SENSORS}, source=str(path))
    return Capture(t=col("t"), TR=col("TR"), BR=col("BR"),
                   TL=col("TL"), BL=col("BL"), source=str(path))


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def smooth(x: np.ndarray, win: int = 5) -> np.ndarray:
    """Media móvil centrada con relleno por los bordes."""
    x = np.asarray(x, dtype=float)
    if win <= 1 or x.size < win:
        return x.copy()
    pad = win // 2
    xp = np.pad(x, pad, mode="edge")
    y = np.convolve(xp, np.ones(win) / win, mode="valid")
    return y[: x.size]


def _longest_true_run(mask: np.ndarray) -> tuple[int, int]:
    """(inicio, fin) del tramo True contiguo más largo; fin exclusivo."""
    best = (0, 0)
    start = None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        if start is not None and (not m or i == len(mask) - 1):
            end = i + 1 if m else i
            if end - start > best[1] - best[0]:
                best = (start, end)
            start = None
    return best


def _nearest_index(t: np.ndarray, value: float) -> int:
    return int(np.argmin(np.abs(t - value)))


def _r1(v) -> float:
    return round(float(v), 1)


# --------------------------------------------------------------------------- #
# Análisis
# --------------------------------------------------------------------------- #
@dataclass
class SwingAnalysis:
    t: np.ndarray
    total_kg: np.ndarray
    bw_kg: float
    force_pct: np.ndarray       # fuerza vertical en % del peso corporal
    trail_pct: np.ndarray       # % del peso sobre el pie trail (NaN si no hay carga)
    cop_x_cm: np.ndarray        # + = lado derecho de la tabla (tras flip)
    cop_y_cm: np.ndarray        # + = punta de los pies (tras flip)
    standing: np.ndarray        # máscara: el golfista está sobre la tabla
    segment: tuple[int, int]    # tramo de pie más largo (inicio, fin)
    handed: str
    trail_is_pos_x: bool
    i_top: int | None = None
    i_impact: int | None = None
    notes: list[str] = field(default_factory=list)
    source: str = ""
    # Dos tablas: series por pie {"lead": {...}, "trail": {...}} con kg, pct, x_cm, y_cm,
    # force_pct (x_cm/y_cm NaN cuando el pie no carga) y la colocación usada.
    feet: dict | None = None
    layout: Layout | None = None

    @property
    def dual(self) -> bool:
        return self.feet is not None

    # ---- eventos --------------------------------------------------------- #
    @property
    def t_top(self) -> float | None:
        return None if self.i_top is None else float(self.t[self.i_top])

    @property
    def t_impact(self) -> float | None:
        return None if self.i_impact is None else float(self.t[self.i_impact])

    # ---- métricas -------------------------------------------------------- #
    def metrics(self) -> dict:
        m: dict = {"peso_corporal_kg": _r1(self.bw_kg)}
        s, e = self.segment
        seg = slice(s, e)
        if e > s:
            tp = self.trail_pct[seg]
            m["trail_pct_min_max"] = (_r1(np.nanmin(tp)), _r1(np.nanmax(tp)))
            i_pk = s + int(np.nanargmax(self.force_pct[seg]))
            m["pico_fuerza_pct_bw"] = _r1(self.force_pct[i_pk])
            m["t_pico_fuerza_s"] = round(float(self.t[i_pk]), 3)
            x = self.cop_x_cm[seg]
            y = self.cop_y_cm[seg]
            ok = ~np.isnan(x) & ~np.isnan(y)
            if ok.sum() > 1:
                m["cop_x_rango_cm"] = _r1(np.ptp(x[ok]))
                m["cop_y_rango_cm"] = _r1(np.ptp(y[ok]))
                m["cop_longitud_trazo_cm"] = _r1(np.sum(np.hypot(np.diff(x[ok]), np.diff(y[ok]))))
        if self.i_top is not None:
            m["t_top_s"] = round(self.t_top, 3)
            m["trail_pct_en_top"] = _r1(self.trail_pct[self.i_top])
        if self.i_impact is not None:
            m["t_impacto_s"] = round(self.t_impact, 3)
            m["lead_pct_en_impacto"] = _r1(100.0 - float(self.trail_pct[self.i_impact]))
            m["fuerza_en_impacto_pct_bw"] = _r1(self.force_pct[self.i_impact])
        if self.i_top is not None and self.i_impact is not None:
            m["top_a_impacto_ms"] = round((self.t_impact - self.t_top) * 1000.0)
        if self.dual:
            m.update(self._foot_metrics())
        return m

    def _foot_metrics(self) -> dict:
        """Métricas por pie (dos tablas). Punta-talón: + = punta, − = talón."""
        m: dict = {}
        lead, trail = self.feet["lead"], self.feet["trail"]
        s, e = self.segment
        seg = slice(s, e)

        def at(arr, i):
            v = arr[i]
            return None if np.isnan(v) else _r1(v)

        if self.i_top is not None:
            m["trail_punta_talon_en_top_cm"] = at(trail["y_cm"], self.i_top)
            m["lead_punta_talon_en_top_cm"] = at(lead["y_cm"], self.i_top)
        if self.i_impact is not None:
            m["lead_punta_talon_en_impacto_cm"] = at(lead["y_cm"], self.i_impact)
            m["trail_punta_talon_en_impacto_cm"] = at(trail["y_cm"], self.i_impact)
            m["lead_lateral_en_impacto_cm"] = at(lead["x_cm"], self.i_impact)
        if e > s:
            for name, foot in (("lead", lead), ("trail", trail)):
                fp = foot["force_pct"][seg]
                if np.isfinite(fp).any():
                    m[f"pico_fuerza_{name}_pct_bw"] = _r1(np.nanmax(fp))
                y = foot["y_cm"][seg]
                y = y[~np.isnan(y)]
                if y.size > 1:
                    m[f"cop_{name}_rango_punta_talon_cm"] = _r1(np.ptp(y))
        return {k: v for k, v in m.items() if v is not None}


def _single_series(cap: Capture, handed: str, flip_x: bool, flip_y: bool, win: int) -> dict:
    total = smooth(cap.total, win)
    right, left = smooth(cap.right(), win), smooth(cap.left(), win)
    top, bottom = smooth(cap.top(), win), smooth(cap.bottom(), win)
    if flip_x:
        right, left = left, right
    if flip_y:
        top, bottom = bottom, top
    trail = right if handed == "right" else left
    with np.errstate(divide="ignore", invalid="ignore"):
        safe = np.where(total >= MIN_LOAD_KG, total, np.nan)
        cop_x = (right - left) / safe * HALF_X_CM
        cop_y = (top - bottom) / safe * HALF_Y_CM
    return {"total": total, "trail": trail, "cop_x": cop_x, "cop_y": cop_y, "feet": None}


def _dual_series(cap: Capture, handed: str, flip_y: bool, win: int, layout: Layout) -> dict:
    kg_l = {s: smooth(cap.L[s], win) for s in SENSORS}
    kg_r = {s: smooth(cap.R[s], win) for s in SENSORS}
    c = combine(kg_l, kg_r, layout)
    total = c["total"]
    sy = -1.0 if flip_y else 1.0
    feet = {}
    for side in ("L", "R"):
        kg = c[side]["kg"]
        loaded = kg >= MIN_LOAD_KG
        feet[side] = {"kg": kg,
                      "x_cm": np.where(loaded, c[side]["x_cm"], np.nan),
                      "y_cm": np.where(loaded, sy * c[side]["y_cm"], np.nan)}
    trail_side = "R" if handed == "right" else "L"
    lead_side = "L" if trail_side == "R" else "R"
    with np.errstate(divide="ignore", invalid="ignore"):
        safe = np.where(total >= MIN_LOAD_KG, total, np.nan)
        for f in feet.values():
            f["pct"] = f["kg"] / safe * 100.0
    cop_x = np.where(total >= MIN_LOAD_KG, c["cop_x_cm"], np.nan)
    cop_y = np.where(total >= MIN_LOAD_KG, sy * c["cop_y_cm"], np.nan)
    return {"total": total, "trail": feet[trail_side]["kg"], "cop_x": cop_x, "cop_y": cop_y,
            "feet": {"lead": feet[lead_side], "trail": feet[trail_side]}}


def analyze(
    cap: Capture,
    handed: str = "right",
    flip_x: bool = False,
    flip_y: bool = False,
    smooth_win: int = 5,
    bw_kg: float | None = None,
    t_top: float | None = None,
    t_impact: float | None = None,
    layout: Layout | None = None,
) -> SwingAnalysis:
    if handed not in ("right", "left"):
        raise ValueError("handed debe ser 'right' o 'left'")
    notes: list[str] = []

    if cap.dual:
        layout = layout or Layout()
        if layout.gap_cm is None:
            notes.append("Hueco entre tablas sin indicar: se supone que están pegadas (0 cm).")
        ser = _dual_series(cap, handed, flip_y, smooth_win, layout)
    else:
        layout = None
        ser = _single_series(cap, handed, flip_x, flip_y, smooth_win)
    total, trail, cop_x, cop_y = ser["total"], ser["trail"], ser["cop_x"], ser["cop_y"]
    with np.errstate(divide="ignore", invalid="ignore"):
        safe = np.where(total >= MIN_LOAD_KG, total, np.nan)
        trail_pct = trail / safe * 100.0

    # "De pie": relativo al peso corporal (mediana con carga), no al pico, porque
    # en un swing real la fuerza vertical llega al 150-190 % y luego cae al 40 %.
    loaded = total >= max(MIN_LOAD_KG, 10.0)
    bw0 = float(np.median(total[loaded])) if loaded.any() else 0.0
    standing = total >= 0.2 * bw0 if bw0 >= MIN_LOAD_KG else np.zeros_like(total, bool)
    if bw_kg is None:
        bw_kg = float(np.median(total[standing])) if standing.any() else float("nan")
        if not standing.any():
            notes.append("Nadie sobre la tabla durante la captura (peso ~0).")
    with np.errstate(divide="ignore", invalid="ignore"):
        force_pct = total / bw_kg * 100.0
        if ser["feet"]:
            for f in ser["feet"].values():
                f["force_pct"] = f["kg"] / bw_kg * 100.0

    seg = _longest_true_run(standing)
    an = SwingAnalysis(
        t=cap.t, total_kg=total, bw_kg=bw_kg, force_pct=force_pct,
        trail_pct=trail_pct, cop_x_cm=cop_x, cop_y_cm=cop_y,
        standing=standing, segment=seg, handed=handed,
        trail_is_pos_x=(handed == "right"), notes=notes, source=cap.source,
        feet=ser["feet"], layout=layout,
    )

    # ---- detección automática de top / impacto --------------------------- #
    s, e = seg
    fs = cap.fs
    if e - s >= max(10, int(0.5 * fs)):
        tt = cap.t[s:e]
        tp = trail_pct[s:e]
        fp = force_pct[s:e]
        if np.nanmax(tp) - np.nanmin(tp) >= _MIN_TRAIL_SWING_PCT:
            # pendiente con el paso de muestreo mediano: hay muestras con la misma
            # marca de tiempo (ráfagas Bluetooth) y np.gradient(tp, tt) dividiría por 0
            dt_med = float(np.median(np.diff(tt))) if tt.size > 1 else 0.01
            v = np.gradient(smooth(np.nan_to_num(tp, nan=50.0), 5)) / max(dt_med, 1e-3)  # %/s
            i_fast = int(np.nanargmin(v))  # bajada más rápida de trail = downswing
            w0 = int(np.searchsorted(tt, tt[i_fast] - _TOP_SEARCH_S))
            i_top_l = w0 + int(np.nanargmax(tp[w0:i_fast + 1]))
            w1 = min(len(tt) - 1, int(np.searchsorted(tt, tt[i_top_l] + _IMPACT_SEARCH_S)))
            i_imp_l = i_top_l + int(np.nanargmax(fp[i_top_l:w1 + 1]))
            an.i_top, an.i_impact = s + i_top_l, s + i_imp_l
        else:
            notes.append("No se detecta un swing claro (el reparto trail/lead "
                         f"varía menos de {_MIN_TRAIL_SWING_PCT:.0f} puntos).")
    elif standing.any():
        notes.append("Tramo de pie demasiado corto para detectar eventos.")

    # ---- overrides manuales --------------------------------------------- #
    if t_top is not None:
        an.i_top = _nearest_index(cap.t, t_top)
    if t_impact is not None:
        an.i_impact = _nearest_index(cap.t, t_impact)
    return an


# --------------------------------------------------------------------------- #
# Gráficas
# --------------------------------------------------------------------------- #
_FOOT_COLORS = {"lead": "tab:blue", "trail": "tab:orange"}


def _draw_board_outline(ax, cx: float, cy: float, sensor_w: float, sensor_h: float,
                        phys_w: float | None = None, phys_h: float | None = None) -> None:
    import matplotlib.pyplot as plt
    if phys_w and phys_h:
        ax.add_patch(plt.Rectangle((cx - phys_w / 2, cy - phys_h / 2), phys_w, phys_h,
                                   fill=False, lw=1.0, color="0.7", ls="--"))
    ax.add_patch(plt.Rectangle((cx - sensor_w / 2, cy - sensor_h / 2), sensor_w, sensor_h,
                               fill=False, lw=1.5, color="0.4"))
    ax.plot([cx - sensor_w / 2, cx + sensor_w / 2], [cy, cy], color="0.85", lw=0.8)
    ax.plot([cx, cx], [cy - sensor_h / 2, cy + sensor_h / 2], color="0.85", lw=0.8)


def plot(an: SwingAnalysis, out_path: str | Path | None = None,
         show: bool = False, title: str | None = None,
         reference: "SwingAnalysis | None" = None) -> Path | None:
    """Figura de 3 paneles (4 con dos tablas). Si se da `reference`, se superpone en gris
    alineada en el impacto (o en el top, o en el inicio si no hay eventos)."""
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    dual = an.dual
    s, e = an.segment
    seg = slice(s, e)
    t, x, y = an.t[seg], an.cop_x_cm[seg], an.cop_y_cm[seg]

    rows = 3 if dual else 2
    fig = plt.figure(figsize=(13, 9 if dual else 7))
    gs = fig.add_gridspec(rows, 2, width_ratios=[1.05, 1.45], hspace=0.4, wspace=0.28)
    ax_cop = fig.add_subplot(gs[:, 0])
    ax_tr = fig.add_subplot(gs[0, 1])
    ax_f = fig.add_subplot(gs[1, 1], sharex=ax_tr)
    ax_feet = fig.add_subplot(gs[2, 1], sharex=ax_tr) if dual else None

    ref_shift, ref_label = None, None
    if reference is not None:
        ref_label = "referencia" + (f" ({Path(reference.source).stem})" if reference.source else "")
        if an.t_impact is not None and reference.t_impact is not None:
            ref_shift = an.t_impact - reference.t_impact
        elif an.t_top is not None and reference.t_top is not None:
            ref_shift = an.t_top - reference.t_top
        else:
            ref_shift = an.t[an.segment[0]] - reference.t[reference.segment[0]]

    # --- 1) trazo del CoP sobre la(s) tabla(s) ----------------------------- #
    if dual:
        half = an.layout.center_distance_cm / 2.0
        # giradas 90°: el lado largo de la tabla va en el eje punta-talón (y)
        for cx in (-half, half):
            _draw_board_outline(ax_cop, cx, 0.0, 2 * HALF_Y_CM, 2 * HALF_X_CM,
                                BOARD_PHYS_DEPTH_CM, BOARD_PHYS_WIDTH_CM)
        lim_x = half + BOARD_PHYS_DEPTH_CM / 2 + 3.0
        lim_y = BOARD_PHYS_WIDTH_CM / 2 + 3.0
        trail_right = an.trail_is_pos_x
        for name, foot in an.feet.items():
            side = 1.0 if (name == "trail") == trail_right else -1.0
            fx, fy = foot["x_cm"][seg] + side * half, foot["y_cm"][seg]
            ok = ~np.isnan(fx) & ~np.isnan(fy)
            if ok.sum() > 1:
                ax_cop.plot(fx[ok], fy[ok], color=_FOOT_COLORS[name], lw=1.3, alpha=0.85,
                            zorder=2, label=f"pie {name}")
            for idx, mk in ((an.i_top, "^"), (an.i_impact, "*")):
                if idx is not None and not np.isnan(foot["x_cm"][idx]):
                    ax_cop.plot(foot["x_cm"][idx] + side * half, foot["y_cm"][idx], mk,
                                color=_FOOT_COLORS[name], ms=8, mec="k", zorder=4)
    else:
        _draw_board_outline(ax_cop, 0.0, 0.0, 2 * HALF_X_CM, 2 * HALF_Y_CM)
        lim_x, lim_y = HALF_X_CM + 3.0, HALF_Y_CM + 3.0
    ax_cop.axhline(0, color="0.85", lw=0.8, zorder=0)
    ax_cop.axvline(0, color="0.85", lw=0.8, zorder=0)
    if reference is not None:
        rs, re_ = reference.segment
        rx, ry = reference.cop_x_cm[rs:re_], reference.cop_y_cm[rs:re_]
        rok = ~np.isnan(rx) & ~np.isnan(ry)
        if rok.sum() > 1:
            ax_cop.plot(rx[rok], ry[rok], color="0.6", lw=1.3, alpha=0.9, zorder=1, label=ref_label)
    ok = ~np.isnan(x) & ~np.isnan(y)
    if ok.sum() > 1:
        pts = np.column_stack([x[ok], y[ok]]).reshape(-1, 1, 2)
        segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
        lc = LineCollection(segs, cmap="viridis", lw=2.0, zorder=3)
        lc.set_array(t[ok][:-1])
        ax_cop.add_collection(lc)
        cb = fig.colorbar(lc, ax=ax_cop, fraction=0.04, pad=0.02)
        cb.set_label("tiempo (s)")
        ax_cop.plot(x[ok][0], y[ok][0], "o", color="tab:green", ms=8, label="address", zorder=5)
    for idx, mk, col, lab in ((an.i_top, "^", "tab:orange", "top"),
                              (an.i_impact, "*", "tab:red", "impacto ≈")):
        if idx is not None and not np.isnan(an.cop_x_cm[idx]):
            ax_cop.plot(an.cop_x_cm[idx], an.cop_y_cm[idx], mk, color=col,
                        ms=13, mec="k", label=lab, zorder=6)
    lead_lbl, trail_lbl = ("lead", "trail") if an.trail_is_pos_x else ("trail", "lead")
    ax_cop.set_xlabel(f"◀ {lead_lbl}        x (cm)        {trail_lbl} ▶")
    ax_cop.set_ylabel("◀ talón        y (cm)        punta ▶")
    ax_cop.set_xlim(-lim_x, lim_x)
    ax_cop.set_ylim(-lim_y, lim_y)
    ax_cop.set_aspect("equal")
    ax_cop.set_title("Trazo del centro de presión" + (
        f"  (2 tablas, hueco {an.layout.gap_cm or 0:g} cm)" if dual else ""))
    if ax_cop.get_legend_handles_labels()[0]:
        ax_cop.legend(loc="upper left", fontsize=9)

    # --- 2) % peso trail vs tiempo ---------------------------------------- #
    if reference is not None:
        ax_tr.plot(reference.t + ref_shift, reference.trail_pct, color="0.55", lw=1.2, ls="--",
                   label=ref_label, zorder=1)
    ax_tr.plot(an.t, an.trail_pct, color="tab:blue", lw=1.6, label="este swing")
    if reference is not None:
        ax_tr.legend(loc="best", fontsize=8)
    ax_tr.axhline(50, color="0.6", lw=0.8, ls="--")
    ax_tr.set_ylim(0, 100)
    ax_tr.set_ylabel("% peso en pie trail")
    ax_tr.set_title(f"Reparto trail / lead  ({'diestro' if an.handed == 'right' else 'zurdo'})")

    # --- 3) fuerza vertical vs tiempo ------------------------------------- #
    if reference is not None:
        ax_f.plot(reference.t + ref_shift, reference.force_pct, color="0.55", lw=1.2, ls="--", zorder=1)
    if dual:
        for name, foot in an.feet.items():
            ax_f.plot(an.t, foot["force_pct"], color=_FOOT_COLORS[name], lw=1.0, alpha=0.8,
                      label=f"pie {name}")
    ax_f.plot(an.t, an.force_pct, color="tab:purple", lw=1.6, label="total" if dual else None)
    if dual:
        ax_f.legend(loc="upper left", fontsize=8)
    ax_f.axhline(100, color="0.6", lw=0.8, ls="--")
    ax_f.set_ylabel("fuerza vertical (% peso)")
    ax_f.set_title(f"Fuerza vertical  (peso corporal ≈ {an.bw_kg:.1f} kg)")

    # --- 4) (dos tablas) CoP punta-talón por pie vs tiempo ----------------- #
    if dual:
        if reference is not None and reference.dual:
            for name, foot in reference.feet.items():
                ax_feet.plot(reference.t + ref_shift, foot["y_cm"], color="0.55", lw=1.0, ls="--", zorder=1)
        for name, foot in an.feet.items():
            ax_feet.plot(an.t, foot["y_cm"], color=_FOOT_COLORS[name], lw=1.4, label=f"pie {name}")
        ax_feet.axhline(0, color="0.6", lw=0.8, ls="--")
        ax_feet.set_ylim(-HALF_X_CM, HALF_X_CM)
        ax_feet.set_ylabel("◀ talón   (cm)   punta ▶")
        ax_feet.set_title("CoP punta-talón de cada pie")
        ax_feet.legend(loc="upper left", fontsize=8)
        ax_feet.set_xlabel("tiempo (s)")
    else:
        ax_f.set_xlabel("tiempo (s)")

    for ax in [a for a in (ax_tr, ax_f, ax_feet) if a is not None]:
        ax.axvspan(an.t[s], an.t[max(s, e - 1)], color="0.95", zorder=0)
        # etiquetas a alturas distintas: top e impacto suelen estar a <300 ms
        for idx, col, lab, yf in ((an.i_top, "tab:orange", "top", 0.97),
                                  (an.i_impact, "tab:red", "impacto ≈", 0.86)):
            if idx is not None:
                ax.axvline(an.t[idx], color=col, lw=1.4, ls=":")
                lo, hi = ax.get_ylim()
                ax.text(an.t[idx], lo + (hi - lo) * yf, f" {lab}", color=col,
                        va="top", fontsize=9)
        ax.grid(alpha=0.25)

    fig.suptitle(title or (Path(an.source).name if an.source else "captura"), fontsize=12)

    saved = None
    if out_path:
        saved = Path(out_path)
        saved.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(saved, dpi=120, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)
    return saved
