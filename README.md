# Wii_Golf

Usar 1 o 2 **Wii Balance Boards** para medir la transferencia de peso y el
centro de presión (CoP) durante el swing de golf, y analizarlo con Python.

El contexto y la investigación previa (dispositivos comerciales, proyectos DIY,
arquitectura con 1 vs 2 tablas y matrices de presión) están en
[`docs/chat-sensores-presion-swing.md`](docs/chat-sensores-presion-swing.md).

## Estado

Funciona la cadena completa con **una** tabla en Windows: lectura HID a
~100 Hz, grabación a CSV y análisis del swing con métricas y gráficas.

```
wiigolf/
  balance_board.py          driver HID (init, calibración, lectura, CoP)
  analysis.py               análisis del swing: métricas, detección de top/impacto, figura
scripts/
  01_detect.py              detecta la tabla, lee la calibración y comprueba la lectura
  02_live_read.py           lectura en vivo + grabación a CSV
  03_analyze.py             analiza un CSV y genera out/<nombre>.png
  04_web.py                 web local en tiempo real (CoP en vivo, grabación, captura automática)
  make_synthetic_swing.py   genera un swing sintético para probar sin la tabla
wiigolf/web/
  server.py                 FastAPI + WebSocket: hilo lector, grabación, auto-captura, API
  static/index.html         interfaz (canvas con el CoP, barras, lista de swings, análisis)
```

## Requisitos

- **Python 3.10+** en el PATH (probado con 3.14).
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
3. Debe quedar como *conectado*. Se identifica como VID `0x057E` / PID `0x0306`.

### Prueba 1 - Detección

```powershell
python scripts/01_detect.py --led
```

- Lista los HID; la tabla aparece como VID `0x057E`.
- Con `--led`, el LED azul debe parpadear (confirma el canal de salida).
- Debe imprimir la calibración de fábrica y ~100 muestras/seg.

### Prueba 2 - Lectura en vivo y grabación

```powershell
python scripts/02_live_read.py --csv data/swing_001.csv --duration 15
```

Súbete a la tabla y verás el peso total y el CoP en vivo. Cada muestra se
guarda en el CSV (`t, TR, BR, TL, BL, total, cop_x, cop_y`). `Ctrl+C` detiene
la captura. La carpeta `data/` está en `.gitignore`.

### Prueba 3 - Análisis del swing

```powershell
python scripts/03_analyze.py data/swing_001.csv
```

Guarda `out/swing_001.png` con tres paneles (trazo del CoP sobre la silueta de
la tabla, % de peso en el pie trail vs tiempo, y fuerza vertical vs tiempo) con
las marcas de *address*, *top* e *impacto*, y muestra las métricas por consola:

| Métrica | Significado |
|---|---|
| `peso_corporal_kg` | mediana del peso mientras estás de pie (o fíjalo con `--bw 80`) |
| `trail_pct_en_top` | % del peso en el pie trail en el top del backswing |
| `lead_pct_en_impacto` | % del peso en el pie lead en el impacto (≈) |
| `pico_fuerza_pct_bw` | pico de fuerza vertical en % del peso corporal (empuje del downswing) |
| `top_a_impacto_ms` | duración del downswing |
| `cop_x_rango_cm`, `cop_longitud_trazo_cm` | amplitud lateral y longitud del trazo del CoP |

Opciones útiles:

- `--handed left` si eres zurdo.
- `--flip-x` si trail/lead salen invertidos (depende de hacia dónde mires en la
  tabla); `--flip-y` para punta/talón.
- `--top 2.80 --impact 3.02` para fijar las marcas a mano.
- `--show` abre la figura en una ventana.

#### Cómo se detectan el top y el impacto

- **Top**: máximo de carga en el pie trail justo antes del desplazamiento
  lateral más rápido hacia el lead (el downswing).
- **Impacto (≈)**: pico de fuerza vertical tras el top. El pico de fuerza de
  reacción vertical se produce al final del downswing, muy cerca del impacto,
  así que es una aproximación razonable con solo la tabla. Para un marcado
  fiable hará falta una señal externa sincronizada (micrófono o IMU); está en
  el roadmap.

### Probar sin la tabla

```powershell
python scripts/make_synthetic_swing.py
python scripts/03_analyze.py data/synthetic_swing.csv
```

![Análisis del swing sintético](docs/img/synthetic_swing.png)

### Prueba 4 - Web local en tiempo real

```powershell
python scripts/04_web.py
```

Abre `http://localhost:8000` en el PC o la URL que imprime (`http://<ip>:8000`)
desde una **tablet o móvil en el mismo WiFi**, para tenerla al lado de la
tabla. La primera vez Windows puede pedir permiso en el firewall para
`python.exe`; acéptalo para poder entrar desde la tablet.

Qué hay en la pantalla:

- **CoP en vivo** sobre la silueta de la tabla, con estela de 1,5 s. El punto
  crece y se pone naranja cuando la fuerza vertical supera el 115 % del peso.
- **Peso total**, **fuerza vertical** (% del peso corporal) y barra
  **trail / lead**. El peso corporal se estima solo; con *Fijar peso* se toma
  la mediana del último segundo estando quieto.
- **Tarar**: con la tabla vacía (o con la alfombra encima) pone las cuatro
  células a cero.
- **Grabar**: graba a `data/<nombre>.csv` y, al detener, analiza y muestra la
  figura y las métricas.
- **Captura automática**: sin pulsar nada entre swings. Mantiene un buffer de
  los últimos segundos; cuando el % de peso en el trail cae 25 puntos en menos
  de 0,5 s (el downswing), guarda `data/auto_<fecha>.csv` con los 3 s previos y
  1,5 s posteriores, y lo analiza al vuelo.
- **Swings grabados**: re-analizar, borrar o descargar (`/api/swings/<n>/csv`).
- Ajustes de diestro/zurdo e inversión de ejes: se guardan en el navegador y
  en `data/settings.json` y los usa también el análisis.

Para desarrollar sin la tabla: `python scripts/04_web.py --sim` reproduce en
bucle un swing simulado.

## Orientación sobre la tabla

- Colócate con los pies **a lo ancho** de la tabla: el eje x es el lateral
  (trail/lead) y el eje y es punta/talón.
- Un diestro mirando al frente de la tabla tiene el pie trail en x > 0. Si en
  tu montaje sale al revés, usa `--flip-x`.
- El stance cabe para hierros; con driver (stance más ancho que los 51 cm de la
  tabla) hace falta un tablero de contrachapado encima (diseño de la Univ. de
  Tennessee, ver docs) o la segunda tabla.

## Hallazgos hasta ahora

- **Windows 11 + Python 3.14 + `hidapi` funciona de principio a fin**: LED,
  init de la extensión, calibración de fábrica y streaming a ~99 Hz. No ha hecho
  falta la ruta Linux/xwiimote que se temía por la fragilidad histórica de los
  informes de salida HID en Windows.
- La calibración de fábrica (0 / 17 / 34 kg por sensor) se lee de la EEPROM de
  la tabla y las lecturas salen ya en kg.
- Con la tabla vacía el ruido es < 0,1 kg; por debajo de 2 kg el CoP se reporta
  como (0, 0) para no dividir entre ruido.
- Pipeline de análisis validado con un swing sintético: detecta el top
  (2,79 s vs 2,80 diseñado) y el impacto (3,02 s), pico de fuerza 129,5 % BW.

## Nota sobre Windows

Enviar informes de salida a dispositivos tipo Wiimote por la pila Bluetooth de
Windows es históricamente frágil. Aquí ha funcionado, pero si en otra máquina la
tabla se detecta y `01_detect.py` no recibe calibración/muestras, la vía más
robusta es **Linux o una Raspberry Pi** con `xwiimote`/`evdev` (cada tabla
aparece como un dispositivo independiente, ideal también para la fase de dos
tablas).

## Roadmap

1. ✅ Leer 1 tabla: CoP + peso a CSV.
2. ✅ Análisis del CSV: trazo del CoP, % trail/lead, fuerza vertical, marcas de
   top e impacto (heurístico).
3. ✅ Visualización en tiempo real: web local (FastAPI + WebSocket + canvas) con
   estela del CoP, barras trail/lead, fuerza vertical, grabación y captura
   automática de swings, accesible desde tablet.
4. Validar con swings reales y ajustar la heurística; marcado fiable del impacto
   (micrófono o IMU sincronizado).
5. Comparar varios swings (superposición alineada en el impacto, consistencia).
6. **2 tablas** (una por pie) sincronizadas: CoP por pie y GRF vertical.
7. Matriz de presión (FSR/Velostat) sobre cada tabla para el mapa de presión.
