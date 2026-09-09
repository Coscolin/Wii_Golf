"""
Prueba 4 - Servidor web local con visualización en tiempo real.

Envoltorio de `python -m wiigolf.app` (el mismo punto de entrada que usa el
.exe). Opciones: --host, --port, --sim, --no-browser, --check.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiigolf.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
