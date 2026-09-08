# Sensores de presión para mejorar el swing en golf

> Contexto de investigación traído desde una conversación previa de claude.ai.
>
> - **Fuente:** https://claude.ai/share/88745819-d4a3-484a-85b5-b4b8b09a36b9
> - **Autor:** Sergio
> - **Capturado:** 2026-09-08
> - **Nota:** los enlaces y datos provienen de búsquedas web hechas en esa conversación; conviene verificarlos antes de comprar hardware.

---

## Resumen ejecutivo (para el proyecto Wii_Golf)

- **Idea del proyecto:** usar **1 o 2 Wii Balance Boards** (una por pie) para medir la
  transferencia de peso y el centro de presión (CoP) durante el swing de golf, y
  procesar los datos con Python para generar trazados y pseudo-mapas de presión.
- **Camino DIY más maduro:** Wii Balance Board + software open source de rehabilitación
  (**WIIBBLE**), en lugar del BrainBLOX que usó el proyecto original de golf.
- **Salto de precisión:** **2 tablas (una por pie)** evitan el contrachapado y dan CoP por
  pie (talón/punta y lateral), similar a un *Swing Catalyst Dual Pressure Plate* básico.
- **Mapa de presión real:** requiere una **matriz de sensores** (FSR comprada o Velostat
  casera); 4 células de carga por pie no dan heatmap por cm², solo pseudo-heatmap por zonas.
- **Arquitectura tope:** matriz de presión **encima** de cada Wii Board (fuerza vertical
  calibrada + distribución), ~150 € total, igual filosofía que el Swing Catalyst Motion Plate.

---

## Turno 1 — ¿Existe algo así (comercial, DIY o GitHub)?

**Pregunta:** Buscar un dispositivo comercial, DIY o código en GitHub que analice la
presión en los pies para mejorar el swing en golf. Sensores en plataforma estilo Wii Fit
o plantilla para zapatos.

**Respuesta — existe en los tres niveles:**

### Comercial – plataformas
- **Swing Catalyst 3D Motion Plate:** placa de presión sobre placa de fuerza; mide fuerzas
  de reacción del suelo (GRF) en 3D + presión pie/suelo. Estándar en academias, miles de €.
  También sus **Dual Pressure Plates**, portátiles y ligeras.
- **BodiTrak** y **V1 Pressure Mat:** alfombras de presión (solo fuerza vertical), más
  baratas; muestran el CoP como un punto que se desplaza.
- **Bertec Swing Station:** placas de fuerza de laboratorio.

### Comercial – plantillas
- **SALTED Smart Insole:** se recortan a medida, van en cualquier zapatilla, Bluetooth con
  la app SALTED Golf; datos de balance y transferencia de peso, **4 sensores por plantilla**,
  ~200 €. Plugged In Golf las comparó con una alfombra 10× más cara y los datos eran muy
  consistentes.

### DIY / GitHub
- **Wii Balance Board:** el camino más recorrido.
  - Proyecto de la **Univ. de Tennessee**: reconvirtió una Wii Balance Board para medir
    fuerza y distribución de presión en tiempo real en clases, con plataforma de
    contrachapado encima para stances más anchos que las 20" de la tabla.
  - **Paper de 2014**: combina Wii Balance Board + Kinect para detectar errores comunes de
    postura y desplazamiento del centro de gravedad en principiantes (>80 % de acierto).
  - **`namacha/wiiboard`** (Python): devuelve el peso de cada una de las 4 esquinas y el
    total; calcular el CoP es trivial.
  - *Limitación:* solo 4 células de carga y ~100 Hz — ves transferencia de peso, no el mapa
    de presión del pie.
- **Alfombra matricial casera:** **JARSH/nuCoach** (UT Austin), app Flutter + Arduino con
  alfombra sensible a la presión que captura la distribución de peso como mapa de calor
  (es para sentadillas, pero el hardware/firmware sirve). Construida con el diseño de
  **Marco Reps** ("Hi-Res Pressure Sensor Matrix"): matriz de **Velostat** entre tiras de
  cobre multiplexadas.

*Conclusión:* no hay un repo específico "alfombra de presión para golf" mantenido; lo
habitual es **Wii Board + script propio de CoP**, o la **matriz Velostat** de Marco Reps.

---

## Turno 2 — El proyecto DIY más desarrollado y reproducible

**Pregunta:** Centrarse en proyectos DIY existentes; el más desarrollado en hardware y
software, ya aplicado al golf y fácil de reproducir.

**Respuesta (honesta):** no existe un repo DIY "de golf" completo, mantenido y reproducible.
Sí hay un proyecto de golf bien documentado en hardware + software open source maduro (de
rehabilitación) que hace lo mismo que un pressure mat de golf. Combinándolos se obtiene lo
más cercano.

### El proyecto de golf más desarrollado: capstone Univ. de Tennessee (2020)
Equipo de ingeniería mecánica que construyó un entrenador de swing para un profesional.
Reutilizaron una Wii Balance Board para medir fuerza y distribución de presión en tiempo real.

- **Hardware:** plataforma de contrachapado encima para stances más anchos, con **4 topes de
  goma** en las esquinas para que la madera no flexe. Se puede alimentar por red sustituyendo
  las pilas por un pack recargable.
- **Software:** **BrainBLOX** (Univ. de Colorado) — CoP y peso total en tiempo real, exporta
  a CSV — + un **Excel con macros VBA** que grafica el CoG en X/Y y el peso en cada pie.
- **Coste:** < 50 $ por una tabla usada.
- **Punto débil:** el Excel no está publicado; problemas de emparejamiento Bluetooth
  (Windows 10 bien; Mac virtualizado, no).
- **PDF completo:** https://trace.tennessee.edu/utk_chanhonoproj/2349

### El software recomendado en lugar de BrainBLOX: WIIBBLE (GitHub, activo)
Convierte la Wii Balance Board en plataforma de fuerza clínica: cursor del CoP en tiempo
real con estela, sesiones grabadas a CSV y pipeline de análisis con **80–90 métricas**.
`.exe` para Windows con flujo **emparejar → tarar/calibrar → grabar → informe HTML**.
Licencia **GPLv3**. No es de golf, pero el CoP con estela en tiempo real es lo mismo que
enseña un BodiTrak en clase.

- **Repo:** https://github.com/NeuroRehack/WIIBBLE
- **Alternativa más simple:** **wii-balance-board-recorder** (VU Amsterdam) — TXT con CpX,
  CpY, los 4 sensores en N y peso total, más app **R/Shiny** para analizar el CoP.

### Receta reproducible en un fin de semana
1. **Wii Balance Board** de segunda mano (Wallapop, 20–40 €) + adaptador Bluetooth si el PC
   no lo trae.
2. **Tablero de contrachapado 18 mm** de ~60×40 cm atornillado encima con 4 topes de goma
   (diseño de Tennessee).
3. **WIIBBLE** en Windows: calibrar, grabar swings, ver el trazo del CoP. El CSV permite
   hacer gráficas propias (traza lateral trail→lead, % de peso por pie vs tiempo).

**Limitaciones a asumir:** 4 células de carga y ~100 Hz. Se ve transferencia de peso y trazo
del CoP con buena fidelidad (validado frente a placas de fuerza), pero **no** el mapa de
presión talón/punta ni fuerzas horizontales/torque. Para eso: plantillas SALTED o matriz
Velostat casera (mucho más trabajo).

---

## Turno 3 — Dos tablas (una por pie): viabilidad y sincronización

**Pregunta:** ¿Se puede usar 2 tablas (una por pie), evitando el contrachapado y mejorando
la precisión, exportando datos de ambas sincronizados por tiempo y luego un Python que lea
ambas y genere trazado más preciso + mapas de presión?

**Respuesta:** Sí, es la configuración más interesante, con un matiz sobre "mapas de presión".

### Qué se gana con dos tablas
Cada Wii Board tiene 4 células en las esquinas. Con **una sola tabla** bajo ambos pies solo
hay un **CoP global** (izquierda/derecha, no dentro de cada pie). Con **una tabla por pie**,
por cada pie: carga total (N), **CoP antero-posterior** (talón/punta) y **CoP lateral**. Es
lo que da un Swing Catalyst Dual Pressure Plate a nivel básico (p. ej. "en el top estás en
la punta del pie trasero", "en el impacto el pie adelantado carga en el talón"). Además se
evita el contrachapado y su flexión, que degrada la lectura.

### Qué NO se gana
Un **mapa de presión real** (heatmap del pie por cm²) requiere una matriz de sensores; 4
células por pie no lo dan. Sí se puede dibujar la **trayectoria del CoP sobre una silueta
del pie** y un **pseudo-heatmap** (tiempo acumulado por zona): visualmente parecido y útil,
pero no distribución por cm².

### Sincronización
Cada tabla es un dispositivo Bluetooth HID independiente que emite ~100 informes/s. Método:
- **Un solo proceso Python** conecta las dos.
- Timestamps con `time.perf_counter()` al recibir cada informe.
- Remuestrear ambas series a una **rejilla común de 100 Hz** con interpolación.
- El jitter entre conexiones BT es de pocos ms; el downswing dura 250–300 ms → suficiente.
- **Evento de sincronía** al inicio (pisotón simultáneo con ambos pies) para alinear los
  picos y eliminar offset sistemático.

### Software (aquí está el trabajo)
WIIBBLE y BrainBLOX manejan **una sola** tabla. Para varias hay que ir a librería de bajo nivel:
- **Linux (PC o Raspberry Pi):** `xwiimote` / `evdev`; cada tabla es un dispositivo
  independiente — lo más robusto.
- **Windows:** **WiimoteLib** (C#) soporta múltiples; en Python es más frágil.
- Las librerías leen la calibración de fábrica (**0/17/34 kg** por sensor) de la EEPROM, así
  que las 8 lecturas salen ya en kg.

### Qué sacaría el script
- % peso trasero/adelantado vs tiempo con marcas de **top** e **impacto**.
- Trazo del **CoP global** (trail→lead) y trazo del CoP **dentro de cada pie** sobre silueta.
- **Fuerza vertical total** vs tiempo: con células a 100 Hz se ve el pico de empuje por
  encima del 100 % del peso corporal en el downswing (métrica real de GRF vertical).

*Requisito para el script:* definir si la captura será **Linux, Raspberry o Windows** (cambia
la parte de conexión).

---

## Turno 4 — Matriz de sensores por pie: comprar o fabricar

**Pregunta:** Para una matriz de sensores por pie, ¿hay hardware existente barato que
reutilizar o fabricar de forma sencilla?

**Respuesta:** Sí, dos vías (de menos a más trabajo).

### 1. Comprar la matriz hecha (recomendado)
Fabricantes chinos venden **láminas FSR matriciales** con forma de plantilla, típicamente
**16×16 o 32×32** puntos, en PET < 0,5 mm con conector 2,54 mm o ZIF.
- **Westbay:** matrices de 3 g a 300 kg de rango, anunciadas para análisis de pisada y golf.
- **AliExpress:** buscar "**foot pressure sensor matrix**" o "**FSR matrix insole**", ~30–80 €
  la unidad. Algunos venden también la **placa de escaneo USB/serie** (ahorra electrónica).
- *Limitación:* ~10 % de error, no sirve como báscula (sensor de reacción a la fuerza, no de
  presión absoluta). Para mapa relativo y CoP dentro del pie, sobra.

### 2. Fabricarla: matriz de Velostat
- **Diseño de Marco Reps** (referencia): tiras de cinta de cobre de 1 cm separadas 1 cm,
  Velostat, y otra capa perpendicular; **cada cruce es un sensor**. Distingue talón, arco y
  antepié. Velostat ~5 € la lámina de 28×28 cm (Adafruit), suficiente para un pie.
- **Chris Coulson** lo mejoró sustituyendo la cinta de cobre por **PCBs flexibles** a ambos
  lados del Velostat (montaje más limpio y repetible); un flex PCB de 30×15 cm con 16 pistas
  por cara cuesta poco en JLCPCB.
- **eTextile resistiveMatrix** (30×30 cm, HW/SW open source, demos Arduino/Teensy): textil,
  pensado para música — no ideal bajo un pie.

### Electrónica de lectura (común a ambas)
- Matriz 16×16 leída con **ESP32-S3** o **Teensy 4.1** + dos multiplexores **CD74HC4067**:
  uno selecciona la fila alimentada, otro lleva cada columna al ADC.
- **Dos detalles críticos** (que muchos tutoriales omiten):
  1. Poner a masa las **filas no seleccionadas** (o usar diodos por columna) para evitar el
     *crosstalk* fantasma entre cruces.
  2. Usar un **divisor con resistencia** adecuada al rango del Velostat/FSR.
- 256 puntos a 100 Hz = 25.600 lecturas/s → un Teensy va sobrado; el ESP32 va justo (60–80 Hz).
- Salida por **USB serie o WiFi** a Python.

### La combinación que realmente vale la pena
Poner la **matriz encima de cada Wii Board**:
- Las células de carga dan **fuerza vertical real y calibrada**.
- La matriz da la **distribución**.
- Se puede **normalizar en tiempo real** la suma de la matriz contra la lectura de la tabla,
  corrigiendo deriva y no linealidad del FSR.
- Es la arquitectura del **Swing Catalyst Motion Plate** (placa de presión sobre placa de
  fuerza), por **~150 € en total**.

---

## Próximos pasos abiertos (propuestos en la conversación)

1. **Script Python** que lea el CSV de WIIBBLE y saque gráficas típicas de golf (trazo del
   CoP y % peso trail/lead vs tiempo con marcas de top e impacto). — *1 tabla.*
2. **Paquete de 2 tablas:** captura (Linux/Raspberry/Windows — decidir) con CSV de 8 sensores
   + timestamp, y análisis/gráficas con numpy + matplotlib.
3. **Matriz de presión:** decidir comprar (FSR) o fabricar (Velostat) → esquema de conexión,
   firmware de escaneo y script de visualización.
