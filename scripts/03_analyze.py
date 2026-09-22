"""
Prueba 3 - Análisis de un swing grabado con 02_live_read.py.

Genera una figura (trazo del CoP, % peso trail vs tiempo y fuerza vertical)
con marcas de address, top e impacto, y muestra las métricas por consola.

Uso:
    python scripts/03_analyze.py data/swing_001.csv
    python scripts/03_analyze.py data/swing_001.csv --show
    python scripts/03_analyze.py data/swing_001.csv --handed left
    python scripts/03_analyze.py data/swing_001.csv --flip-x          # si trail/lead salen al revés
    python scripts/03_analyze.py data/swing_001.csv --top 2.80 --impact 3.02   # marcas manuales
    python scripts/03_analyze.py data/swing_001.csv --bw 82           # peso corporal conocido
    python scripts/03_analyze.py data/synthetic_dual.csv --gap 2      # dos tablas: hueco de 2 cm

Con dos tablas (CSV con columnas L_*/R_*) la colocación se lee del <nombre>.json que
guarda la web junto al CSV; --gap y --button la sobrescriben.

Por defecto la figura se guarda en out/<nombre_del_csv>.png
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiigolf.analysis import analyze, load_csv, plot  # noqa: E402
from wiigolf.dual import Layout  # noqa: E402


def layout_for(csv_path: Path, gap: float | None, button: str | None) -> Layout:
    """Colocación de las dos tablas: la del .json del swing, corregida por los argumentos."""
    d = {}
    try:
        meta = json.loads(csv_path.with_suffix(".json").read_text(encoding="utf-8"))
        d = (meta.get("boards") or {}).get("layout") or {}
    except Exception:
        pass
    lay = Layout.from_dict(d)
    return Layout(gap_cm=lay.gap_cm if gap is None else gap, button=button or lay.button)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="CSV grabado con 02_live_read.py")
    ap.add_argument("--handed", choices=["right", "left"], default="right",
                    help="diestro (right) o zurdo (left); por defecto right")
    ap.add_argument("--flip-x", action="store_true", help="invierte izquierda/derecha")
    ap.add_argument("--flip-y", action="store_true", help="invierte punta/talón")
    ap.add_argument("--smooth", type=int, default=5, help="ventana de suavizado (muestras)")
    ap.add_argument("--bw", type=float, default=None, help="peso corporal en kg (si no, se estima)")
    ap.add_argument("--top", type=float, default=None, help="instante del top (s), manual")
    ap.add_argument("--impact", type=float, default=None, help="instante del impacto (s), manual")
    ap.add_argument("--out", type=str, default=None, help="ruta del PNG (por defecto out/<csv>.png)")
    ap.add_argument("--show", action="store_true", help="abrir la figura en una ventana")
    ap.add_argument("--gap", type=float, default=None, help="dos tablas: hueco entre tablas (cm)")
    ap.add_argument("--button", choices=["left", "right"], default=None,
                    help="dos tablas: lado hacia el que apunta el botón de las tablas")
    args = ap.parse_args()

    cap = load_csv(args.csv)
    print(f"[+] {args.csv}: {cap.t.size} muestras, {cap.t[-1] - cap.t[0]:.2f} s, ~{cap.fs:.0f} Hz"
          + (" · 2 tablas" if cap.dual else ""))
    layout = layout_for(Path(args.csv), args.gap, args.button) if cap.dual else None
    if layout is not None:
        print(f"[+] Colocación: hueco {layout.gap_cm if layout.gap_cm is not None else '?'} cm, "
              f"centros a {layout.center_distance_cm:.1f} cm, botón hacia la "
              f"{'izquierda' if layout.button == 'left' else 'derecha'}")

    an = analyze(cap, handed=args.handed, flip_x=args.flip_x, flip_y=args.flip_y,
                 smooth_win=args.smooth, bw_kg=args.bw, t_top=args.top, t_impact=args.impact,
                 layout=layout)

    for n in an.notes:
        print(f"[!] {n}")

    print("[+] Métricas:")
    for k, v in an.metrics().items():
        print(f"    {k:<34} {v}")

    out = args.out or str(Path("out") / (Path(args.csv).stem + ".png"))
    saved = plot(an, out_path=out, show=args.show)
    if saved:
        print(f"[+] Figura guardada: {saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
