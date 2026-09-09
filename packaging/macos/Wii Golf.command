#!/bin/bash
# Arranca Wii Golf en macOS: doble clic abre una ventana de Terminal con el
# servidor y el navegador en http://localhost:8000. Cierra la ventana para parar.
cd "$(dirname "$0")"
if [ ! -x ./WiiGolf ]; then
  chmod +x ./WiiGolf 2>/dev/null
fi
# Quita la cuarentena de la descarga (si no, macOS bloquea el ejecutable).
xattr -dr com.apple.quarantine . 2>/dev/null
./WiiGolf "$@"
