"""
Análisis de una captura de la Wii Balance Board para el swing de golf.

Entrada: CSV generado por scripts/02_live_read.py
         (columnas: t, TR, BR, TL, BL, total, cop_x, cop_y)

Salida: métricas + figura con
  1) trazo del centro de presión (CoP) sobre la silueta de la tabla,
  2) % de peso sobre el pie trail vs tiempo,
  3) fuerza vertical (% del peso corporal) vs tiempo,
  con marcas de address, top e impacto (≈).

Convención de ejes (una sola tabla, ambos pies):
  x > 0 = lado derecho de la tabla, y > 0 = sensores "top".
  Un diestro de pie mirando al frente de la tabla tiene el pie trail (derecho)
  en x > 0. Si en tu montaje sale invertido, usa flip_x / flip_y.

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

from .balance_board import BOARD_LENGTH_MM, BOARD_WIDTH_MM, MIN_LOAD_KG

HALF_X_CM = BOARD_WIDTH_MM / 20.0   # mitad de la distancia entre sensores, en cm
HALF_Y_CM = BOARD_LENGTH_MM / 20.0

# Umbrales de la heurística de detección
_MIN_TRAIL_SWING_PCT = 15.0   # rango mínimo de trail% para considerar que hay swing
_TOP_SEARCH_S = 1.0           # buscar el top hasta 1 s antes del downswing
_IMPACT_SEARCH_S = 0.6        # buscar el impacto hasta 0.6 s después del top


# --------------------------------------------------------------------------- #
# Carga de datos
# --------------------------------------------------------------------------- #
@dataclass
class Capture:
    """Serie cruda (ya en kg) leída del CSV."""

    t: np.ndarray
    TR: np.ndarray
    BR: np.ndarray
    TL: np.ndarray
    BL: np.ndarray
    source: str = ""

    @property
    def total(self) -> np.ndarray:
        return self.TR + self.BR + self.TL + self.BL

    @property
    def fs(self) -> float:
        """Frecuencia de muestreo estimada (Hz)."""
        dt = np.diff(self.t)
        dt = dt[dt > 0]
        return float(1.0 / np.median(dt)) if dt.size else 0.0

    def right(self) -> np.ndarray:
        return self.TR + self.BR

    def left(self) -> np.ndarray:
        return self.TL + self.BL

    def top(self) -> np.ndarray:
        return self.TR + self.TL

    def bottom(self) -> np.ndarray:
        return self.BR + self.BL


def load_csv(path: str | Path) -> Capture:
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"CSV vacío: {path}")

    def col(name: str) -> np.ndarray:
        return np.array([float(r[name]) for r in rows], dtype=float)

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

    # ---- eventos --------------------------------------------------------- #
    @property
    def t_top(self) -> float | None:
        return None if self.i_top is None else float(self.t[self.i_top])

    @property
    def t_impact(self) -> float | None:
        return None if self.i_impact is None else float(self.t[self.i_impact])

    # ---- métricas -------------------------------------------------------- #
    def metrics(self) -> dict:
        m: dict = {"peso_corporal_kg": round(self.bw_kg, 1)}
        s, e = self.segment
        seg = slice(s, e)
        if e > s:
            tp = self.trail_pct[seg]
            m["trail_pct_min_max"] = (round(float(np.nanmin(tp)), 1),
                                      round(float(np.nanmax(tp)), 1))
            i_pk = s + int(np.nanargmax(self.force_pct[seg]))
            m["pico_fuerza_pct_bw"] = round(float(self.force_pct[i_pk]), 1)
            m["t_pico_fuerza_s"] = round(float(self.t[i_pk]), 3)
            x = self.cop_x_cm[seg]
            y = self.cop_y_cm[seg]
            ok = ~np.isnan(x) & ~np.isnan(y)
            if ok.sum() > 1:
                m["cop_x_rango_cm"] = round(float(np.ptp(x[ok])), 1)
                m["cop_y_rango_cm"] = round(float(np.ptp(y[ok])), 1)
                m["cop_longitud_trazo_cm"] = round(
                    float(np.sum(np.hypot(np.diff(x[ok]), np.diff(y[ok])))), 1)
        if self.i_top is not None:
            m["t_top_s"] = round(self.t_top, 3)
            m["trail_pct_en_top"] = round(float(self.trail_pct[self.i_top]), 1)
        if self.i_impact is not None:
            m["t_impacto_s"] = round(self.t_impact, 3)
            m["lead_pct_en_impacto"] = round(100.0 - float(self.trail_pct[self.i_impact]), 1)
            m["fuerza_en_impacto_pct_bw"] = round(float(self.force_pct[self.i_impact]), 1)
        if self.i_top is not None and self.i_impact is not None:
            m["top_a_impacto_ms"] = round((self.t_impact - self.t_top) * 1000.0)
        return m


def analyze(
    cap: Capture,
    handed: str = "right",
    flip_x: bool = False,
    flip_y: bool = False,
    smooth_win: int = 5,
    bw_kg: float | None = None,
    t_top: float | None = None,
    t_impact: float | None = None,
) -> SwingAnalysis:
    if handed not in ("right", "left"):
        raise ValueError("handed debe ser 'right' o 'left'")
    notes: list[str] = []

    total = smooth(cap.total, smooth_win)
    right, left = smooth(cap.right(), smooth_win), smooth(cap.left(), smooth_win)
    top, bottom = smooth(cap.top(), smooth_win), smooth(cap.bottom(), smooth_win)
    if flip_x:
        right, left = left, right
    if flip_y:
        top, bottom = bottom, top
    trail = right if handed == "right" else left

    with np.errstate(divide="ignore", invalid="ignore"):
        safe = np.where(total >= MIN_LOAD_KG, total, np.nan)
        trail_pct = trail / safe * 100.0
        cop_x = (right - left) / safe * HALF_X_CM
        cop_y = (top - bottom) / safe * HALF_Y_CM

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

    seg = _longest_true_run(standing)
    an = SwingAnalysis(
        t=cap.t, total_kg=total, bw_kg=bw_kg, force_pct=force_pct,
        trail_pct=trail_pct, cop_x_cm=cop_x, cop_y_cm=cop_y,
        standing=standing, segment=seg, handed=handed,
        trail_is_pos_x=(handed == "right"), notes=notes, source=cap.source,
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
def plot(an: SwingAnalysis, out_path: str | Path | None = None,
         show: bool = False, title: str | None = None,
         reference: "SwingAnalysis | None" = None) -> Path | None:
    """Figura de 3 paneles. Si se da `reference`, se superpone en gris
    alineada en el impacto (o en el top, o en el inicio si no hay eventos)."""
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    s, e = an.segment
    seg = slice(s, e)
    t, x, y = an.t[seg], an.cop_x_cm[seg], an.cop_y_cm[seg]

    fig = plt.figure(figsize=(13, 7))
    gs = fig.add_gridspec(2, 2, width_ratios=[1.05, 1.45], hspace=0.35, wspace=0.28)
    ax_cop = fig.add_subplot(gs[:, 0])
    ax_tr = fig.add_subplot(gs[0, 1])
    ax_f = fig.add_subplot(gs[1, 1], sharex=ax_tr)

    ref_shift, ref_label = None, None
    if reference is not None:
        ref_label = "referencia" + (f" ({Path(reference.source).stem})" if reference.source else "")
        if an.t_impact is not None and reference.t_impact is not None:
            ref_shift = an.t_impact - reference.t_impact
        elif an.t_top is not None and reference.t_top is not None:
            ref_shift = an.t_top - reference.t_top
        else:
            ref_shift = an.t[an.segment[0]] - reference.t[reference.segment[0]]

    # --- 1) trazo del CoP sobre la tabla ---------------------------------- #
    ax_cop.add_patch(plt.Rectangle((-HALF_X_CM, -HALF_Y_CM), 2 * HALF_X_CM, 2 * HALF_Y_CM,
                                   fill=False, lw=1.5, color="0.4"))
    ax_cop.axhline(0, color="0.85", lw=0.8)
    ax_cop.axvline(0, color="0.85", lw=0.8)
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
        lc = LineCollection(segs, cmap="viridis", lw=2.0)
        lc.set_array(t[ok][:-1])
        ax_cop.add_collection(lc)
        cb = fig.colorbar(lc, ax=ax_cop, fraction=0.04, pad=0.02)
        cb.set_label("tiempo (s)")
        ax_cop.plot(x[ok][0], y[ok][0], "o", color="tab:green", ms=8, label="address")
    for idx, mk, col, lab in ((an.i_top, "^", "tab:orange", "top"),
                              (an.i_impact, "*", "tab:red", "impacto ≈")):
        if idx is not None and not np.isnan(an.cop_x_cm[idx]):
            ax_cop.plot(an.cop_x_cm[idx], an.cop_y_cm[idx], mk, color=col,
                        ms=13, mec="k", label=lab)
    lead_lbl, trail_lbl = ("lead", "trail") if an.trail_is_pos_x else ("trail", "lead")
    ax_cop.set_xlabel(f"◀ {lead_lbl}        x (cm)        {trail_lbl} ▶")
    ax_cop.set_ylabel("◀ talón        y (cm)        punta ▶")
    m = 3.0
    ax_cop.set_xlim(-HALF_X_CM - m, HALF_X_CM + m)
    ax_cop.set_ylim(-HALF_Y_CM - m, HALF_Y_CM + m)
    ax_cop.set_aspect("equal")
    ax_cop.set_title("Trazo del centro de presión")
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
    ax_f.plot(an.t, an.force_pct, color="tab:purple", lw=1.6)
    ax_f.axhline(100, color="0.6", lw=0.8, ls="--")
    ax_f.set_ylabel("fuerza vertical (% peso)")
    ax_f.set_xlabel("tiempo (s)")
    ax_f.set_title(f"Fuerza vertical  (peso corporal ≈ {an.bw_kg:.1f} kg)")

    for ax in (ax_tr, ax_f):
        ax.axvspan(an.t[s], an.t[max(s, e - 1)], color="0.95", zorder=0)
        # etiquetas a alturas distintas: top e impacto suelen estar a <300 ms
        for idx, col, lab, yf in ((an.i_top, "tab:orange", "top", 0.97),
                                  (an.i_impact, "tab:red", "impacto ≈", 0.86)):
            if idx is not None:
                ax.axvline(an.t[idx], color=col, lw=1.4, ls=":")
                ax.text(an.t[idx], ax.get_ylim()[1] * yf, f" {lab}", color=col,
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
