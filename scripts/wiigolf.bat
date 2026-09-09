@echo off
rem Arranca el servidor Wii Golf y abre el navegador. Doble clic o acceso directo.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo No existe el entorno .venv. Ejecuta primero:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
start "" http://localhost:8000
".venv\Scripts\python.exe" scripts\04_web.py %*
