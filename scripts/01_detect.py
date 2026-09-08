"""
Prueba 1 - Detección y diagnóstico.

Enumera los dispositivos HID, localiza la Wii Balance Board, la abre, la
inicializa, lee la calibración de fábrica y muestra una muestra cruda.

Uso:
    python scripts/01_detect.py
    python scripts/01_detect.py --led     # además parpadea el LED (confirma salida)

Interpretación:
  - Si NO aparece ningún dispositivo Nintendo (VID 0x057E): la tabla no está
    emparejada/conectada como HID. Empareja por Bluetooth y vuelve a probar.
  - Si aparece pero la calibración da timeout: Windows no está entregando los
    informes de salida a la tabla (problema conocido). Ver README.
"""

import argparse
import sys
import time
from pathlib import Path

# Permite ejecutar el script directamente sin instalar el paquete
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiigolf import BalanceBoard, NINTENDO_VID  # noqa: E402
import hid  # noqa: E402


def list_all_hid() -> None:
    print("=" * 60)
    print("Dispositivos HID visibles:")
    print("=" * 60)
    devices = hid.enumerate()
    if not devices:
        print("  (ninguno)")
    for d in devices:
        vid, pid = d.get("vendor_id", 0), d.get("product_id", 0)
        mark = "  <-- NINTENDO" if vid == NINTENDO_VID else ""
        prod = d.get("product_string") or "?"
        manu = d.get("manufacturer_string") or "?"
        print(f"  VID=0x{vid:04X} PID=0x{pid:04X}  {manu} / {prod}{mark}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--led", action="store_true", help="parpadear el LED al abrir")
    args = ap.parse_args()

    list_all_hid()

    boards = BalanceBoard.enumerate()
    if not boards:
        print("[!] No se encontró ninguna Balance Board (VID 0x057E).")
        print("    Empareja la tabla por Bluetooth (pulsa el botón rojo del")
        print("    compartimento de pilas) y vuelve a ejecutar.")
        return 1

    print(f"[+] {len(boards)} tabla(s) Nintendo encontrada(s). Usando la primera.")
    board = BalanceBoard(path=boards[0]["path"])

    try:
        board.open()
    except Exception as exc:
        print(f"[!] No se pudo abrir el dispositivo: {exc}")
        return 1
    print("[+] Dispositivo abierto.")

    try:
        if args.led:
            print("[.] Parpadeando LED (si se enciende, la salida HID funciona)...")
            for _ in range(3):
                board.set_led(True)
                time.sleep(0.25)
                board.set_led(False)
                time.sleep(0.25)
            board.set_led(True)

        print("[.] Inicializando extensión (células de carga)...")
        board.init()

        print("[.] Leyendo calibración de fábrica...")
        cal = board.calibrate()
        print("    Calibración (valores crudos por sensor TR/BR/TL/BL):")
        print(f"      0 kg : {cal['kg0']}")
        print(f"      17 kg: {cal['kg17']}")
        print(f"      34 kg: {cal['kg34']}")

        print("[.] Leyendo muestras (5 s)...")
        t_end = time.time() + 5
        n = 0
        while time.time() < t_end:
            r = board.read(timeout_ms=200)
            if r is None:
                continue
            n += 1
            if n % 10 == 0:  # no saturar la consola
                print(
                    f"    total={r.total:6.2f} kg | "
                    f"TR={r.kg['TR']:5.1f} BR={r.kg['BR']:5.1f} "
                    f"TL={r.kg['TL']:5.1f} BL={r.kg['BL']:5.1f} | "
                    f"CoP=({r.cop_x:+.2f},{r.cop_y:+.2f})"
                )
        if n == 0:
            print("[!] No llegaron informes de datos. Ver README (Windows).")
            return 1
        print(f"[+] OK: {n} muestras en 5 s (~{n / 5:.0f} Hz).")
        return 0
    finally:
        board.close()


if __name__ == "__main__":
    raise SystemExit(main())
