# Cómo funciona el hub de procesamiento

Este documento explica la arquitectura interna de `vitahub` y cómo fluye un fotograma hasta
convertirse en un evento. Para instalar o probar, ver [como-probar.md](como-probar.md).

## Qué es

`vitahub` es una aplicación Python que corre como **un contenedor Docker en un mini-PC/Jetson dentro
de cada hogar**. Se conecta a las cámaras IP de la casa, convierte el vídeo en **eventos** (p. ej.
"ha entrado una persona") y los emite como texto. Todo el vídeo se procesa en el hogar; al exterior
solo sale texto.

Alcance de esta versión (primer slice): **descubrimiento de cámaras + ingesta RTSP + detección de
persona (presencia y conteo) + eventos JSON por stdout**. No hay envío a AWS todavía — la costura
está preparada (ver [backlog.md](backlog.md)).

## Vista general

```
                    ┌──────────────────────────────────────────────┐
                    │  SUPERVISOR (app.py: run)                     │
                    │  arranca, cablea, vigila y apaga con orden    │
                    └───────┬───────────────┬──────────────┬───────┘
                            │               │              │
                   ┌────────▼──────┐  ┌─────▼──────┐  (1 hilo por cámara)
   ONVIF LAN  ───► │  discovery    │─►│  registry  │──────────┐
   (WS-Disc.)      │  IPs + URIs   │  │ id estable │          │
                   └───────────────┘  │ (serie)    │   ┌──────▼───────┐
                                       └────────────┘   │  ingest RTSP │  (reconexión)
                                                         └──────┬───────┘
                                                         frames │
                                                         ┌──────▼───────┐
                                                         │  inference   │  (enchufable:
                                                         │  persona     │   stub / YOLO)
                                                         └──────┬───────┘
                                                    detecciones │
                                                         ┌──────▼───────┐
                                                         │ event-engine │  (cambio de estado)
                                                         └──────┬───────┘
                                                         eventos│
                                                         ┌──────▼───────┐
                                                         │     sink     │  (enchufable:
                                                         │  stdout JSON │   AWS mañana)
                                                         └──────────────┘
```

Dos ideas de diseño que conviene retener:

- **Dos costuras enchufables.** `Detector` (visión) y `EventSink` (salida) son interfaces
  abstractas. Hoy hay `StubDetector`/`PersonDetector` y `StdoutJsonSink`; mañana entran detección de
  caídas o un `AwsSink` **sin tocar el resto**.
- **Identidad de cámara por serie ONVIF, no por IP.** Si el router le cambia la IP a una cámara tras
  un corte, el hub la reconoce como la misma y no la duplica.

## Los módulos (`src/vitahub/`)

| Módulo | Responsabilidad |
|---|---|
| `app.py` | Supervisor: arranque, cableado, un hilo por cámara, señales, heartbeat, apagado. |
| `supervisor.py` | Posee los hilos de cámara: arranca, relanza y para workers en caliente. |
| `rescan.py` | Un ciclo de rescan: descubrir → reconciliar → persistir → converger workers. |
| `control.py` | Servidor HTTP local: `POST /rescan` autenticado con token. |
| `config.py` | Carga/valida/persiste la config YAML; credencial desde entorno; *fail-fast*. |
| `models.py` | Tipos de dominio: `Camera`, `Detection`, `Event` (con `Event.to_json()`). |
| `discovery/onvif.py` | WS-Discovery + SOAP ONVIF: descubre cámaras y resuelve sus URIs RTSP. |
| `registry.py` | Reconcilia cámaras descubiertas con el registro persistido (por serie). |
| `ingest/rtsp.py` | Helpers puros (backoff, muestreo, watchdog), apertura de captura y credenciales en la URL. |
| `inference/` | `Detector` (interfaz), `StubDetector` (tests), `PersonDetector` (YOLO). |
| `analytics/event_engine.py` | Máquina de estados anti-parpadeo: detecciones → eventos. |
| `sinks/` | `EventSink` (interfaz) y `StdoutJsonSink` (salida JSON por stdout). |
| `factory.py` | Construye el `Detector` según la config (`stub` / `person_yolo`). |
| `worker.py` | `process_frame`: detecta, cuenta, alimenta el motor, emite por el sink. |
| `logging_setup.py` | Logs JSON a **stderr** + redacción de secretos. |

## El flujo, paso a paso

### 1. Arranque (`app.main` → `app.run`)
1. `configure_logging()` — logs JSON a stderr.
2. Lee variables de entorno (ver abajo) y `load_config()` — si la config es inválida o falta la
   credencial, **sale con código 1** (no reintenta; es un error que debe arreglar el operador).
3. `register_secret(password)` — para que la contraseña nunca aparezca en un log.
4. Instala los manejadores de `SIGTERM`/`SIGINT` **lo primero** (para poder apagar limpio incluso
   durante el arranque).
5. Construye el detector, el motor de eventos y el sink.

### 2. Descubrimiento y registro
1. `discover()` lanza una **sonda multicast ONVIF** (`239.255.255.250:3702`) y recoge las IPs vivas.
2. Por cada cámara, autenticando con la credencial del hogar, pregunta por SOAP: la **serie**
   (→ `camera_id = onvif-<serie>`) y las **URIs RTSP** (principal y substream).
3. `reconcile()` fusiona lo descubierto con el registro persistido: cámara nueva → se añade; cámara
   conocida con IP distinta → se actualiza su `last_ip`; cámara que deja de verse → **se conserva**.
4. `save_cameras()` reescribe la sección `cameras` del fichero de config de forma **atómica**.

> **Nota:** este ciclo (descubrir → reconciliar → persistir → converger workers) es el
> mismo al arrancar, cada `discovery.interval_seconds` y cuando llega un `POST /rescan`.
> El registro solo se reescribe si hubo cambios. Una cámara que deja de verse **no** se
> para: un probe multicast perdido no debe apagar una cámara que funciona.

### 3. Un hilo por cámara (`_camera_loop`)
Por cada cámara habilitada con URI resuelta se lanza un hilo *daemon* que:
1. Abre la captura RTSP **forzando TCP** (`open_capture`; buffer mínimo).
2. Lee fotogramas en bucle; **muestrea a ~2 fps** (`should_sample`) — el resto se descartan.
3. En cada fotograma muestreado llama a `process_frame`.
4. **Reconexión resiliente:** si la cámara no abre, o la lectura falla, o el stream se queda
   estancado (`is_stalled`, >10 s sin fotograma válido), reconecta con **backoff exponencial**
   (1→2→4…→30 s). Cualquier excepción de conexión se captura y reintenta: **un fallo de una cámara
   nunca tumba el proceso ni a las demás**.

### 4. De fotograma a evento
- `process_frame` pasa el fotograma al `Detector`. `PersonDetector` (YOLO) devuelve las detecciones
  de clase *persona* por encima del umbral de confianza; `person_count` es cuántas hay.
- `EventEngine.observe()` aplica **anti-parpadeo**: un evento se emite solo cuando el estado se
  mantiene un umbral, para no disparar por parpadeos del detector:
  - `person_detected` — ≥1 persona sostenida **2 s** (cámara que estaba vacía).
  - `person_absent` — 0 personas sostenidas **5 s** (más largo, para tolerar oclusiones breves).
  - `person_count_changed` — el conteo cambia entre valores no-cero y se estabiliza.
- El evento se serializa **en un único punto** (`EventSink.emit`). Hoy `StdoutJsonSink` escribe una
  línea JSON por evento a **stdout**. (Esa es la costura del futuro `AwsSink`.)

### 5. Salud y apagado
- El bucle principal refresca `/data/heartbeat` cada 5 s; el `HEALTHCHECK` de Docker
  (`scripts/healthcheck.py`) lo considera sano si el fichero tiene <60 s. Si el proceso se cuelga,
  Docker lo reinicia.
- El rescan periódico corre en **su propio hilo**: así el latido nunca depende de lo que
  tarde un descubrimiento, y un `discover()` lento no provoca un reinicio en falso.
- Aun así, el `HEALTHCHECK` da un margen de arranque (`--start-period=180s`) para que la
  carga de YOLO y un primer descubrimiento lento no cuenten como fallo antes de que el
  hub llegue a latir. Al apagar, `docker-compose.yml` fija `stop_grace_period: 30s` —
  margen por encima del plazo de apagado de los hilos de cámara (~20 s) antes de que
  Docker mande `SIGKILL`.
- Ante `SIGTERM`/`SIGINT` se activa un `Event` de parada, los hilos terminan, el sink se vacía y el
  proceso sale limpio.

## El envelope de evento

Una línea JSON por evento en stdout:

```json
{
  "schema_version": 1,
  "hub_id": "hub-3f9a",
  "camera_id": "onvif-szjsa81a81e6adf9",
  "camera_name": "salon",
  "type": "person_detected",
  "severity": "info",
  "timestamp": "2026-08-15T10:00:00+00:00",
  "payload": { "person_count": 1, "confidence": 0.82 }
}
```

- `camera_id` es la serie ONVIF (estable ante cambios de IP).
- `severity` es `info` para presencia — el hueco donde una futura caída pondría `high`/`critical`.
- `schema_version` permite que el `AwsSink` futuro evolucione el formato sin romper consumidores.

## Separación stdout / stderr

Regla estricta: **stdout = eventos** (el producto, JSON-lines), **stderr = logs operativos**
(arranque, reconexiones, errores; también JSON). Así el consumidor de eventos recibe un flujo limpio
y queda claro qué es dato y qué es diagnóstico. La contraseña ONVIF nunca se escribe en ninguno de
los dos (va por entorno y está registrada para redacción).

## Configuración

**Fichero** (`/data/hub.yaml` en el contenedor; ver `config/hub.example.yaml`):

```yaml
hub_id: hub-3f9a            # único por hogar, generado una vez
discovery:
  interval_seconds: 60      # cadencia del redescubrimiento (debe ser > 0)
inference:
  detector: person_yolo     # o "stub"
  sample_fps: 2
  confidence: 0.4
  stream: substream         # "substream" (Channels/2) ahorra CPU; "main" para más resolución
cameras: []                 # se autopobla al descubrir
```

**Variables de entorno:**

| Variable | Por defecto | Uso |
|---|---|---|
| `VITAHUB_ONVIF_USER` | — (obligatoria) | Usuario ONVIF/RTSP compartido del hogar. |
| `VITAHUB_ONVIF_PASSWORD` | — (obligatoria) | Contraseña. **Nunca** va en el fichero ni en logs. |
| `VITAHUB_CONFIG` | `/data/hub.yaml` | Ruta del fichero de config. |
| `VITAHUB_WEIGHTS` | `/app/models/yolo11n.pt` | Pesos YOLO (embebidos en la imagen). |
| `VITAHUB_LOG_LEVEL` | `INFO` | Nivel de log. |
| `VITAHUB_ADMIN_TOKEN` | — (vacío = deshabilitado) | Token de `POST /rescan`. Sin él no se abre puerto. |
| `VITAHUB_ADMIN_PORT` | `8787` | Puerto del endpoint de control. |

## Empaquetado y despliegue

- **Dockerfile multi-arch** (`ARG BASE_IMAGE`): por defecto CPU (`python:3.12-slim`); para el Jetson
  Orin se construye con una base L4T/CUDA. Los pesos del modelo van **embebidos** → arranca sin
  internet.
- **`docker-compose.yml`**: `restart: unless-stopped` (se relanza tras cortes de luz),
  `network_mode: host` (necesario para el multicast ONVIF), volumen `/data` (config + registro +
  heartbeat persisten reinicios), credencial por entorno.

## Runtime de inferencia (Mac vs Orin)

El mismo contenedor corre en los dos: en el Mac la inferencia va por **CPU** (para desarrollo); en el
Orin, por **GPU/TensorRT**. La interfaz `Detector` es idéntica; cambia solo la base de la imagen.

## Limitaciones conocidas de esta versión

Documentadas en detalle en [backlog.md](backlog.md). Las de más impacto:

- **Sin fallback por `last_ip`**: una cámara conocida solo se reconecta cuando el
  descubrimiento vuelve a verla (≤60 s). No se construye una URL RTSP a partir de la IP
  guardada, porque la ruta del stream varía según el fabricante.
- **Sin comando remoto**: el `POST /rescan` solo es alcanzable desde la LAN del hogar. El
  disparo desde la nube llegará con el uplink (ver [backlog.md](backlog.md)).
- **Endpoints ONVIF asumidos** (`:10000` y rutas fijas): funciona con la cámara piloto (Tuya), pero
  otros modelos usan otros puertos/rutas. El siguiente slice los derivará de `XAddrs`.
