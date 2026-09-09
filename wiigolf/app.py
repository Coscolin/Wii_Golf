"""
Punto de entrada de la aplicación: el mismo para los scripts y para el .exe.

    python -m wiigolf.app [--host 0.0.0.0] [--port 8000] [--sim] [--no-browser]
    WiiGolf.exe            (mismas opciones)
    WiiGolf.exe --check    (comprueba tabla, micro, cámara y Bluetooth, y sale)

Arranca el servidor web, abre el navegador y muestra las URLs. Los datos se
guardan en la carpeta `data` junto al .exe (o en la raíz del repo).
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no envía nada; solo elige la interfaz de salida
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def check() -> int:
    """Diagnóstico rápido para el PC donde se ejecuta (útil en un equipo nuevo)."""
    frozen = getattr(sys, "frozen", False)
    print("Wii Golf - comprobacion del equipo")
    print(f"  Python {sys.version.split()[0]} ({'ejecutable empaquetado' if frozen else 'scripts'})")
    from wiigolf.web import server
    print(f"  Carpeta de datos: {server.DATA_DIR}")

    from wiigolf.balance_board import BalanceBoard
    try:
        devs = BalanceBoard.enumerate()
        if devs:
            d = devs[0]
            print(f"  Tabla (HID): encontrada -> {d.get('manufacturer_string')} {d.get('product_string')}")
        else:
            print("  Tabla (HID): no encontrada (enciendela; si no aparece, emparejala desde la web)")
    except Exception as exc:
        print(f"  Tabla (HID): error -> {exc}")

    from wiigolf import bt_win
    st = bt_win.status()
    if st.get("available"):
        r = st["radio"]
        print(f"  Bluetooth: adaptador {r['name']} [{r['address']}]")
        for b in st["boards"]:
            estado = "conectada" if b["connected"] else "no conectada"
            emp = "emparejada de forma permanente" if b["authenticated"] else "sin emparejar"
            print(f"    tabla {b['name']} [{b['address']}]: {estado}, {emp}")
        if not st["boards"]:
            print("    Windows no conoce ninguna tabla todavia")
    else:
        print(f"  Bluetooth: no disponible ({st.get('error')})")

    from wiigolf import media
    print("  Microfono: " + ("disponible" if media.AUDIO_AVAILABLE else f"no ({media.AUDIO_ERROR})"))
    if media.AUDIO_AVAILABLE:
        for d in media.list_audio_inputs()[:6]:
            print(f"    [{d['index']}] {d['name']}" + (" (predeterminado)" if d["default"] else ""))
    print("  Camara (OpenCV): " + ("disponible" if media.VIDEO_AVAILABLE else f"no ({media.VIDEO_ERROR})"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="WiiGolf", description="Servidor web local de Wii Golf")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = accesible desde la red local")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sim", action="store_true", help="usar una tabla simulada")
    ap.add_argument("--no-browser", action="store_true", help="no abrir el navegador")
    ap.add_argument("--check", action="store_true", help="comprobar el equipo y salir")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")  # consolas sin UTF-8
    except Exception:
        pass
    if args.check:
        return check()

    import uvicorn
    from wiigolf.web.server import DATA_DIR, create_app

    app = create_app(sim=args.sim)
    url = f"http://localhost:{args.port}"
    modo = "SIMULACION" if args.sim else "tabla real"
    print()
    print(f"  Wii Golf ({modo})")
    print(f"    este PC:       {url}")
    if args.host == "0.0.0.0":
        print(f"    tablet/movil:  http://{lan_ip()}:{args.port}")
    print(f"    datos en:      {DATA_DIR}")
    print("  Deja esta ventana abierta mientras uses la tabla. Ctrl+C o cerrarla para parar.")
    print()
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
