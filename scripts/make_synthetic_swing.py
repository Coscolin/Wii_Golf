"""
Genera un swing SINTÉTICO en el mismo formato CSV que 02_live_read.py.

Sirve para probar 03_analyze.py sin la tabla ni un swing real. El perfil
imita a un diestro: subir a la tabla, address, backswing (carga al trail),
downswing (transferencia rápida al lead con pico de fuerza vertical),
follow-through y bajar de la tabla.

Uso:
    python scripts/make_synthetic_swing.py                 # -> data/synthetic_swing.csv
    python scripts/make_synthetic_swing.py data/otro.csv --bw 75 --seed 3
    python scripts/make_synthetic_swing.py --dual          # -> data/synthetic_dual.csv (2 tablas)

Con --dual se generan las dos tablas (una por pie, giradas 90°, ver wiigolf/dual.py):
el pie trail rueda hacia la punta en el top y el lead carga el talón en el impacto y
pasa a la punta en el finish. --gap y --button fijan la colocación simulada.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiigolf.analysis import DUAL_HEADER, SINGLE_HEADER  # noqa: E402
from wiigolf.dual import Layout, combine, spread_foot_load  # noqa: E402

FS = 100.0

# (t_inicio, t_fin, trail% inicio->fin, fuerza %BW inicio->fin, y_norm inicio->fin)
# y_norm: -1 talón .. +1 punta (centro de presión antero-posterior)
PHASES = [
    (0.00, 0.50, (50, 50), (0, 100),   (0.0, -0.1)),   # subir a la tabla
    (0.50, 2.00, (50, 53), (100, 100), (-0.1, -0.1)),  # address
    (2.00, 2.80, (53, 72), (100, 95),  (-0.1, 0.15)),  # backswing -> top
    (2.80, 3.02, (72, 40), (95, 130),  (0.15, -0.2)),  # downswing: pico de fuerza
    (3.02, 3.15, (40, 22), (130, 92),  (-0.2, -0.3)),  # impacto / descarga
    (3.15, 4.00, (22, 12), (92, 100),  (-0.3, -0.2)),  # follow-through
    (4.00, 4.50, (12, 12), (100, 100), (-0.2, -0.2)),  # finish
    (4.50, 5.00, (12, 12), (100, 0),   (-0.2, 0.0)),   # bajar de la tabla
]


# Dos tablas: CoP punta-talón de cada pie (cm, + punta) y lateral (cm, + derecha del
# golfista) por fase, para el pie trail y el lead: (y_trail, y_lead, x_trail, x_lead)
FEET_PHASES = [
    ((-1, -1), (-1, -1), (0, 0), (0, 0)),        # subir
    ((-1, -1), (-1, -1), (0, 0), (0, 0)),        # address: ligeramente al talón
    ((-1, 6), (-1, -3), (0, 2), (0, -1)),        # backswing: trail a la punta, lead al talón
    ((6, 2), (-3, -6), (2, 1), (-1, -2)),        # downswing: lead hunde el talón
    ((2, -2), (-6, -4), (1, 0), (-2, -1)),       # impacto
    ((-2, -4), (-4, 4), (0, -1), (-1, 1)),       # follow-through: lead pasa a la punta
    ((-4, -4), (4, 6), (-1, -1), (1, 2)),        # finish
    ((-4, -4), (6, 6), (-1, -1), (2, 2)),        # bajar
]


def smoothstep(u: np.ndarray) -> np.ndarray:
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


def build(bw: float, seed: int) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    t_end = PHASES[-1][1]
    n = int(t_end * FS) + 1
    t = np.arange(n) / FS + rng.normal(0, 0.0008, n)  # jitter de ~1 ms
    t = np.maximum.accumulate(t) - t[0]

    trail = np.zeros(n)
    force = np.zeros(n)
    ynorm = np.zeros(n)
    for t0, t1, (a0, a1), (f0, f1), (y0, y1) in PHASES:
        m = (t >= t0) & (t < t1) if t1 < t_end else (t >= t0)
        u = smoothstep((t[m] - t0) / (t1 - t0))
        trail[m] = a0 + (a1 - a0) * u
        force[m] = f0 + (f1 - f0) * u
        ynorm[m] = y0 + (y1 - y0) * u

    total = force / 100.0 * bw
    right = total * trail / 100.0          # diestro: trail = derecha de la tabla
    left = total - right
    f_top = 0.5 + ynorm / 2.0              # fracción de carga en sensores "top"
    sensors = {
        "TR": right * f_top, "BR": right * (1 - f_top),
        "TL": left * f_top, "BL": left * (1 - f_top),
    }
    rows = []
    for i in range(n):
        kg = {k: max(0.0, v[i] + rng.normal(0, 0.15)) for k, v in sensors.items()}
        tot = sum(kg.values())
        if tot >= 2.0:
            cx = ((kg["TR"] + kg["BR"]) - (kg["TL"] + kg["BL"])) / tot
            cy = ((kg["TL"] + kg["TR"]) - (kg["BL"] + kg["BR"])) / tot
        else:
            cx = cy = 0.0
        rows.append([f"{t[i]:.4f}", f"{kg['TR']:.2f}", f"{kg['BR']:.2f}",
                     f"{kg['TL']:.2f}", f"{kg['BL']:.2f}", f"{tot:.2f}",
                     f"{cx:.4f}", f"{cy:.4f}"])
    return rows


def build_dual(bw: float, seed: int, layout: Layout) -> list[list[str]]:
    """Dos tablas giradas (una por pie), diestro: trail = tabla derecha."""
    rng = np.random.default_rng(seed)
    t_end = PHASES[-1][1]
    n = int(t_end * FS) + 1
    t = np.arange(n) / FS + rng.normal(0, 0.0008, n)
    t = np.maximum.accumulate(t) - t[0]

    trail = np.zeros(n)
    force = np.zeros(n)
    feet = np.zeros((4, n))   # y_trail, y_lead, x_trail, x_lead (cm)
    for (t0, t1, (a0, a1), (f0, f1), _y), fp in zip(PHASES, FEET_PHASES):
        m = (t >= t0) & (t < t1) if t1 < t_end else (t >= t0)
        u = smoothstep((t[m] - t0) / (t1 - t0))
        trail[m] = a0 + (a1 - a0) * u
        force[m] = f0 + (f1 - f0) * u
        for k, (v0, v1) in enumerate(fp):
            feet[k, m] = v0 + (v1 - v0) * u

    total = force / 100.0 * bw
    kg_trail = total * trail / 100.0
    kg_lead = total - kg_trail
    rows = []
    for i in range(n):
        # trail = tabla derecha (R), lead = tabla izquierda (L)
        r = spread_foot_load(kg_trail[i], feet[2, i], feet[0, i], layout)
        l = spread_foot_load(kg_lead[i], feet[3, i], feet[1, i], layout)
        r = {k: max(0.0, v + rng.normal(0, 0.15)) for k, v in r.items()}
        l = {k: max(0.0, v + rng.normal(0, 0.15)) for k, v in l.items()}
        c = combine(l, r, layout)
        # la tabla derecha llega ~4 ms después: se guarda su instante real
        rows.append([f"{t[i]:.4f}"] + [f"{l[s]:.2f}" for s in ("TR", "BR", "TL", "BL")]
                    + [f"{r[s]:.2f}" for s in ("TR", "BR", "TL", "BL")]
                    + [f"{c['total']:.2f}", f"{c['cop_x_cm']:.3f}", f"{c['cop_y_cm']:.3f}",
                       f"{t[i]:.4f}", f"{t[i] - 0.004:.4f}"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=None,
                    help="CSV de salida (por defecto data/synthetic_swing.csv o data/synthetic_dual.csv)")
    ap.add_argument("--bw", type=float, default=80.0, help="peso corporal (kg)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dual", action="store_true", help="dos tablas, una por pie (giradas 90°)")
    ap.add_argument("--gap", type=float, default=0.0, help="hueco entre tablas (cm) con --dual")
    ap.add_argument("--button", choices=["left", "right"], default="left",
                    help="lado hacia el que apunta el botón de las tablas con --dual")
    args = ap.parse_args()

    if args.dual:
        layout = Layout(gap_cm=args.gap, button=args.button)
        rows, header = build_dual(args.bw, args.seed, layout), DUAL_HEADER
        out = Path(args.out or "data/synthetic_dual.csv")
    else:
        rows, header = build(args.bw, args.seed), SINGLE_HEADER
        out = Path(args.out or "data/synthetic_swing.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    if args.dual:
        # la web guarda la colocación en la meta del swing; aquí igual, para que el análisis
        # (y 03_analyze.py) usen el mismo hueco con el que se generó
        out.with_suffix(".json").write_text(json.dumps({
            "name": out.stem, "source": "synthetic",
            "boards": {"mode": "dual", "layout": layout.to_dict()}}, indent=1), encoding="utf-8")
    print(f"[+] {len(rows)} muestras -> {out}  (top≈2.80 s, pico fuerza≈3.02 s"
          + (f", 2 tablas, hueco {args.gap:g} cm)" if args.dual else ")"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
