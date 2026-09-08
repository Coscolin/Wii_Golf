# Wii_Golf

Usar 1 o 2 **Wii Balance Boards** para medir la transferencia de peso y el
centro de presión (CoP) durante el swing de golf, y analizarlo con Python.

El contexto y la investigación previa (dispositivos comerciales, proyectos DIY,
arquitectura con 1 vs 2 tablas y matrices de presión) están en
[`docs/chat-sensores-presion-swing.md`](docs/chat-sensores-presion-swing.md).

## Estado

Fase inicial: leer **una** Wii Balance Board por Bluetooth (HID) y visualizar
peso total + CoP en tiempo real, con grabación a CSV.

```
wiigolf/
  balance_board.py   # driver HID de la tabla (init, calibración, lectura, CoP)
scripts/
  01_detect.py       # detecta la tabla, lee calibración y comprueba la lectura
  02_live_read.py    # lectura en vivo + grabación a CSV
```

## Requisitos

- **Python 3.10+** en el PATH.
- Una Wii Balance Board **emparejada por Bluetooth** con el PC.
- `pip install -r requirements.txt` (instala `hidapi`, `numpy`, `matplotlib`).

## Puesta en marcha (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Emparejar la tabla

1. Abre el compartimento de pilas de la tabla y pulsa el **botón rojo SYNC**
   (la tabla entra en modo emparejamiento durante ~20 s).
2. En *Configuración de Bluetooth* de Windows, añade el dispositivo
   **"Nintendo RVL-WBC-01"**. Si pide PIN, déjalo en blanco / omítelo.
3. Debe quedar como *conectado*.

### Prueba 1 - Detección

```powershell
python scripts/01_detect.py --led
```

- Lista los HID; la tabla aparece como VID `0x057E`.
- Con `--led`, el LED azul debe parpadear (confirma el canal de salida).
- Debe imprimir la calibración de fábrica y ~alguna decena de muestras/seg.

### Prueba 2 - Lectura en vivo

```powershell
python scripts/02_live_read.py --csv data/swing_001.csv --duration 15
```

Súbete a la tabla y verás el peso total y el CoP en vivo. Cada muestra se
guarda en el CSV (`t, TR, BR, TL, BL, total, cop_x, cop_y`) para analizarla
después. `Ctrl+C` detiene la captura.

## Nota importante sobre Windows

Enviar informes de salida a dispositivos tipo Wiimote a través de la pila
Bluetooth de Windows es **históricamente frágil**. Si la tabla se detecta pero
`01_detect.py` se queda sin recibir calibración/muestras, la vía más robusta es
**Linux o una Raspberry Pi** con `xwiimote`/`evdev` (cada tabla aparece como un
dispositivo independiente, ideal además para la fase de 2 tablas). Se documentará
esa ruta si hace falta.

## Roadmap

1. **[en curso]** Leer 1 tabla: CoP + peso a CSV.
2. Análisis del CSV: trazo del CoP y % de peso trail/lead vs tiempo, con marcas
   de *top* e *impacto*.
3. **2 tablas** (una por pie) sincronizadas: CoP por pie y GRF vertical.
4. Matriz de presión (FSR/Velostat) sobre cada tabla para el mapa de presión.
