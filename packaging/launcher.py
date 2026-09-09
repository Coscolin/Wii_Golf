"""Script de arranque del .exe (PyInstaller necesita un script, no un módulo)."""

import sys

from wiigolf.app import main

if __name__ == "__main__":
    sys.exit(main())
