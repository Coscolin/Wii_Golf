"""
Prueba 4 - Servidor web local con visualización en tiempo real.

Arranca el servidor y abre http://localhost:8000 en el navegador (o la URL de
la red local desde una tablet/móvil conectado al mismo WiFi).

Uso:
    python scripts/04_web.py                      # tabla real, puerto 8000
    python scripts/04_web.py --port 8080
    python scripts/04_web.py --sim                # tabla simulada (sin hardware)
    python scripts/04_web.py --host 127.0.0.1     # solo este PC (sin aviso del firewall)

La primera vez que se escucha en 0.0.0.0 Windows puede pedir permiso en el
firewall para python.exe: acéptalo para poder abrirla desde la tablet.
"""

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no envía nada; solo elige la interfaz de salida
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sim", action="store_true", help="usar una tabla simulada")
    args = ap.parse_args()

    import uvicorn
    from wiigolf.web.server import create_app

    app = create_app(sim=args.sim)
    modo = "SIMULACIÓN" if args.sim else "tabla real"
    print(f"\n  Wii Golf web ({modo})")
    print(f"    este PC:       http://localhost:{args.port}")
    if args.host == "0.0.0.0":
        print(f"    tablet/móvil:  http://{lan_ip()}:{args.port}")
    print("  Ctrl+C para parar.\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
