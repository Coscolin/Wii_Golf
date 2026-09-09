"""
Punto de entrada de la aplicación: el mismo para los scripts y para el
ejecutable (WiiGolf.exe en Windows, WiiGolf en macOS).

    python -m wiigolf.app [--host 0.0.0.0] [--port 8000] [--sim] [--no-browser]
    WiiGolf --check        comprueba tabla, micro, cámara y Bluetooth, y sale
    WiiGolf --pair         empareja la tabla por Bluetooth (pulsa SYNC antes) y sale

Arranca el servidor web, abre el navegador y muestra las URLs. Los datos se
guardan en la carpeta `data` junto al ejecutable (o en la raíz del repo).
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


def bt_module():
    """Módulo de emparejado Bluetooth del sistema actual (o None)."""
    if sys.platform == "win32":
        from wiigolf import bt_win
        return bt_win
    if sys.platform == "darwin":
        from wiigolf import bt_mac
        return bt_mac
    return None


def check() -> int:
    """Diagnóstico rápido del equipo (útil en un PC o Mac nuevo)."""
    frozen = getattr(sys, "frozen", False)
    print("Wii Golf - comprobacion del equipo")
    print(f"  Sistema: {sys.platform}  Python {sys.version.split()[0]} "
          f"({'ejecutable empaquetado' if frozen else 'scripts'})")
    from wiigolf.web import server
    print(f"  Carpeta de datos: {server.DATA_DIR}")

    from wiigolf.balance_board import BalanceBoard
    try:
        devs = BalanceBoard.enumerate()
        if devs:
            d = devs[0]
            print(f"  Tabla (HID): encontrada -> {d.get('manufacturer_string')} {d.get('product_string')}")
        else:
            print("  Tabla (HID): no encontrada (enciendela; si no aparece, emparejala con --pair o desde la web)")
    except Exception as exc:
        print(f"  Tabla (HID): error -> {exc}")

    bt = bt_module()
    if bt is None:
        print(f"  Bluetooth: sin soporte de emparejado en {sys.platform}")
    else:
        st = bt.status()
        if st.get("available"):
            r = st["radio"]
            print(f"  Bluetooth: adaptador {r['name']} [{r['address']}]")
            for b in st["boards"]:
                estado = "conectada" if b["connected"] else "no conectada"
                emp = "emparejada de forma permanente" if b["authenticated"] else "sin emparejar"
                print(f"    tabla {b['name']} [{b['address']}]: {estado}, {emp}")
            if not st["boards"]:
                print("    el sistema no conoce ninguna tabla todavia")
        else:
            print(f"  Bluetooth: no disponible ({st.get('error')})")
            if sys.platform == "darwin":
                print("    (en macOS: Ajustes del Sistema > Privacidad y seguridad > Bluetooth > permitir a Terminal)")

    from wiigolf import media
    print("  Microfono: " + ("disponible" if media.AUDIO_AVAILABLE else f"no ({media.AUDIO_ERROR})"))
    if media.AUDIO_AVAILABLE:
        for d in media.list_audio_inputs()[:6]:
            print(f"    [{d['index']}] {d['name']}" + (" (predeterminado)" if d["default"] else ""))
    print("  Camara (OpenCV): " + ("disponible" if media.VIDEO_AVAILABLE else f"no ({media.VIDEO_ERROR})"))
    return 0


def pair_cli() -> int:
    """Empareja la tabla desde la consola. En macOS debe ir en el hilo principal (run loop)."""
    bt = bt_module()
    if bt is None:
        print(f"ERROR: sin soporte de emparejado en {sys.platform}")
        return 1
    try:
        res = bt.pair(log=print)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(("OK: " if res.get("ok") else "ERROR: ") + str(res.get("message")))
    return 0 if res.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="WiiGolf", description="Servidor web local de Wii Golf")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = accesible desde la red local")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sim", action="store_true", help="usar una tabla simulada")
    ap.add_argument("--no-browser", action="store_true", help="no abrir el navegador")
    ap.add_argument("--check", action="store_true", help="comprobar el equipo y salir")
    ap.add_argument("--pair", action="store_true", help="emparejar la tabla (pulsa SYNC antes) y salir")
    ap.add_argument("--bt-status", action="store_true", help="estado Bluetooth en JSON y salir (uso interno)")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")  # consolas sin UTF-8
    except Exception:
        pass
    if args.check:
        return check()
    if args.pair:
        return pair_cli()
    if args.bt_status:
        import json
        bt = bt_module()
        print(json.dumps(bt.status() if bt else {"available": False, "error": f"sin soporte en {sys.platform}",
                                                  "radio": None, "boards": []}))
        return 0

    import uvicorn
    from wiigolf.web.server import DATA_DIR, create_app

    app = create_app(sim=args.sim)
    url = f"http://localhost:{args.port}"
    modo = "SIMULACION" if args.sim else "tabla real"
    print()
    print(f"  Wii Golf ({modo})")
    print(f"    este equipo:   {url}")
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
