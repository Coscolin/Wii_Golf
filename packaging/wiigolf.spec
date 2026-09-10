# -*- mode: python ; coding: utf-8 -*-
# Empaquetado de Wii Golf con PyInstaller (carpeta dist/WiiGolf con WiiGolf.exe).
#
#     .venv\Scripts\python.exe -m PyInstaller packaging\wiigolf.spec --noconfirm --clean
#
# o simplemente scripts\build_exe.ps1, que ademas genera dist\WiiGolf-win64.zip.
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# uvicorn carga bucles/protocolos por nombre en tiempo de ejecucion; hay que
# incluirlos a mano. multipart es lo que FastAPI busca para subir archivos.
hidden = collect_submodules("uvicorn") + [
    "multipart", "python_multipart", "websockets", "httptools",
    "anyio._backends._asyncio", "hid",
]
if sys.platform == "darwin":  # emparejado por IOBluetooth (bt_mac.py)
    hidden += ["objc", "Foundation", "IOBluetooth", "IOBluetooth._metadata",
               "CoreBluetooth", "CoreBluetooth._metadata"]

# La interfaz web se sirve desde wiigolf/web/static (server.py la localiza
# relativa a su propio archivo, tambien dentro del paquete).
datas = [(os.path.join(ROOT, "wiigolf", "web", "static"), os.path.join("wiigolf", "web", "static"))]

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "jupyter",
              "notebook", "pytest", "setuptools", "wheel"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WiiGolf",
    console=True,          # la ventana muestra las URLs y el log; cerrarla para el servidor
    disable_windowed_traceback=False,
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="WiiGolf")
