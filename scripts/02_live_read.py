"""
Prueba 2 - Lectura en vivo con opción de grabar a CSV.

Muestra en tiempo real el peso total, el reparto por sensor y el centro de
presión (CoP). Con --csv guarda cada muestra con marca de tiempo, listo para
analizar después (trazo del CoP, % de peso por pie, etc.).

Uso:
    python scripts/02_live_read.py
    python scripts/02_live_read.py --csv data/swing_001.csv
    python scripts/02_live_read.py --csv data/swing_001.csv --duration 15

Detener: Ctrl+C.

Convención de ejes (una sola tabla bajo ambos pies):
    CoP_x > 0  -> peso hacia la derecha
    CoP_y > 0  -> peso hacia delante (punta de los pies)
Para golf con UNA tabla, "derecha/izquierda" equivale a trail/lead según cómo
te coloques; con DOS tablas (una por pie) se separa por pie (siguiente fase).
"""

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiigolf import BalanceBoard  # noqa: E402


def open_board() -> BalanceBoard:
    boards = BalanceBoard.enumerate()
    if not boards:
        print("[!] No se encontró ninguna Balance Board (VID 0x057E).")
        print("    Empareja la tabla por Bluetooth y vuelve a ejecutar.")
        raise SystemExit(1)
    board = BalanceBoard(path=boards[0]["path"])
    board.open()
    print("[.] Inicializando y calibrando...")
    board.init()
    board.calibrate()
    print("[+] Lista. Súbete a la tabla.")
    return board


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=str, default=None, help="ruta del CSV a grabar")
    ap.add_argument("--duration", type=float, default=None,
                    help="segundos a capturar (por defecto: hasta Ctrl+C)")
    args = ap.parse_args()

    board = open_board()

    writer = None
    csv_file = None
    if args.csv:
        path = Path(args.csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        csv_file = open(path, "w", newline="", encoding="utf-8")
        writer = csv.writer(csv_file)
        writer.writerow(["t", "TR", "BR", "TL", "BL", "total", "cop_x", "cop_y"])
        print(f"[+] Grabando en {path}")

    t0 = None
    t_end = None
    n = 0
    try:
        while True:
            r = board.read(timeout_ms=200)
            if r is None:
                continue
            if t0 is None:
                t0 = r.t
                if args.duration:
                    t_end = t0 + args.duration
            rel = r.t - t0
            n += 1

            if writer:
                writer.writerow([
                    f"{rel:.4f}",
                    f"{r.kg['TR']:.2f}", f"{r.kg['BR']:.2f}",
                    f"{r.kg['TL']:.2f}", f"{r.kg['BL']:.2f}",
                    f"{r.total:.2f}", f"{r.cop_x:.4f}", f"{r.cop_y:.4f}",
                ])

            # Refresco de consola en una sola línea
            print(
                f"\r t={rel:6.2f}s  total={r.total:6.2f}kg  "
                f"CoP=({r.cop_x:+.2f},{r.cop_y:+.2f})  "
                f"[TR{r.kg['TR']:4.0f} BR{r.kg['BR']:4.0f} "
                f"TL{r.kg['TL']:4.0f} BL{r.kg['BL']:4.0f}]   ",
                end="", flush=True,
            )

            if t_end is not None and r.t >= t_end:
                break
    except KeyboardInterrupt:
        pass
    finally:
        print()
        if csv_file:
            csv_file.close()
        board.close()
        dur = (time.perf_counter() - t0) if t0 else 0
        if n and dur > 0:
            print(f"[+] {n} muestras en {dur:.1f} s (~{n / dur:.0f} Hz).")
        if args.csv:
            print(f"[+] CSV guardado: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
