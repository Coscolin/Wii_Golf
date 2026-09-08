"""
Genera un swing SINTÉTICO en el mismo formato CSV que 02_live_read.py.

Sirve para probar 03_analyze.py sin la tabla ni un swing real. El perfil
imita a un diestro: subir a la tabla, address, backswing (carga al trail),
downswing (transferencia rápida al lead con pico de fuerza vertical),
follow-through y bajar de la tabla.

Uso:
    python scripts/make_synthetic_swing.py                 # -> data/synthetic_swing.csv
    python scripts/make_synthetic_swing.py data/otro.csv --bw 75 --seed 3
"""

import argparse
import csv
from pathlib import Path

import numpy as np

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="data/synthetic_swing.csv")
    ap.add_argument("--bw", type=float, default=80.0, help="peso corporal (kg)")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    rows = build(args.bw, args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t", "TR", "BR", "TL", "BL", "total", "cop_x", "cop_y"])
        w.writerows(rows)
    print(f"[+] {len(rows)} muestras -> {out}  (top≈2.80 s, pico fuerza≈3.02 s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
