# Diseño — Hub de procesamiento de cámaras (primer slice)

- Fecha: 2026-08-15
- Estado: aprobado en brainstorming, pendiente de plan de implementación
- Repo: `vita-docker-process-hub` (código de aplicación del edge; **no** infraestructura AWS)
- Destino de despliegue: NVIDIA Jetson **Orin** (uno por hogar), como contenedor Docker

## 0. Contexto y encaje en el proyecto

VitaPlus (ver `vitaplus-aws-architecture/docs/information/context.md`) monitoriza a personas en su
hogar cruzando visión artificial, wearable y voz. Este repo implementa el **hub de procesamiento
edge** de la rama de cámaras: el mini-PC/Jetson que en cada casa se conecta a 3-4 cámaras IP,
convierte vídeo en **eventos** y (en el futuro) los sincroniza con AWS.

Encaje con lo ya definido en el proyecto:

- **Separación de repos** (regla repetida en el roadmap del reloj): la infraestructura AWS
  (CloudFormation, Kinesis, DynamoDB, ECR) vive en `vitaplus-aws-architecture`; el código de cada
  rama vive en su propio repo. Este repo es a las cámaras lo que `vitaplus-watch-adapter` es al
  reloj. La parte AWS que *recibe* los eventos del hub será un stack nuevo (previsiblemente
  `02-hub-ingest`) **en el repo de arquitectura, no aquí**.
- **Principios normativos respetados:** P1 (eventos, no vídeo — al exterior solo texto), P2
  (privacy-first / edge-first — todo el vídeo se procesa en el hogar), P8 (modularidad y modelo de
  datos común — el envelope de evento se normaliza en un único punto).
- **Vacío conocido:** el `context.md` cita como normativas la taxonomía de eventos (§11) y el
  modelo de datos (§10), pero **no existen todavía** (el documento se corta en §5.3). Este spec fija
  un **mínimo** de vocabulario de evento para presencia de persona, alineado con el glosario
  canónico existente (nombres `snake_case`, severidades `info/low/medium/high/critical`, envelope
  análogo al asumido por el watch-adapter). Cuando se escriba §11 formalmente, este mínimo debe
  reconciliarse con ella.

## 1. Alcance de este slice

El proyecto completo del hub tiene siete subsistemas: (A) provisioning/descubrimiento, (B) ingesta
RTSP, (C) visión artificial, (D) motor de eventos, (E) uplink a AWS, (F) telemetría de conexión,
(G) ciclo de vida. **Meter todo en un spec sería un error** — C (visión avanzada: caídas, pose) es
casi un proyecto de investigación y no debe bloquear el resto.

**Este primer slice = A + B + análisis básico (C/D en versión ligera), sin E.**

Dentro (in scope):

- Descubrimiento automático de cámaras por ONVIF WS-Discovery (auto-detección de IP).
- Ingesta RTSP multi-cámara (3-4) con reconexión ante cortes.
- **Análisis básico: detección de persona (presencia + conteo)** por cámara.
- Emisión de **eventos JSON estructurados a stdout**.
- Reconexión resiliente y auto-arranque del contenedor tras reinicio del Jetson.
- Costuras enchufables listas para el futuro: `Detector` (visión) y `EventSink` (AWS).

Fuera (out of scope) — ver §10:

- Uplink a AWS (E) y el stack `02-hub-ingest` (repo de arquitectura).
- Telemetría de conexión hacia AWS (F) — en este slice el estado de conexión solo va a logs.
- Visión avanzada (C): caídas, pose, entra/sale de zonas, actividad doméstica.
- Panel web local (se eligió solo logs por stdout).
- CI/CD de deploy a ECR.

## 2. Decisiones de partida (cerradas en brainstorming)

| Decisión | Elegido | Motivo |
|---|---|---|
| Primer slice | Provisioning + ingesta RTSP + análisis básico, sin AWS | Prueba la cadena a casa real sin el ML difícil; el AWS se enchufa después |
| Análisis básico | Detección de persona (presencia + conteo) | Cimiento sobre el que se construye entra/sale, caída, etc. |
| Entorno de desarrollo | Mac ahora, Orin después (imagen multi-arch) | Iteración rápida sin depender del hardware; misma imagen CPU/GPU |
| Salida de resultados | Solo logs estructurados JSON a stdout | Lo más simple; encaja con el futuro `AwsSink` que consumirá ese flujo |
| Credenciales de cámara | Una credencial compartida por hogar | Más ágil en campo; el hub la tiene una vez y la usa con todas |
| Enfoque técnico | Monolito Python, un contenedor, etapas enchufables | Ecosistema natural de visión/RTSP en Jetson; costuras aisladas |

## 3. Arquitectura y estructura

Un proceso Python, un contenedor. Etapas desacopladas por interfaces; cada una se entiende y testea
por separado. Un **supervisor** cablea el descubrimiento, un *worker* por cámara y el sink.

```
                    ┌──────────────────────────────────────────────┐
                    │  SUPERVISOR (app.py)                          │
                    │  arranca, cablea, vigila y apaga con orden    │
                    └───────┬───────────────┬──────────────┬───────┘
                            │               │              │
                   ┌────────▼──────┐  ┌─────▼──────┐  (1 worker/cámara)
   ONVIF LAN  ───► │  discovery    │─►│  registry  │──────────┐
   (WS-Disc.)      │  IPs+URIs     │  │ id estable │          │
                   └───────────────┘  │ estado con.│   ┌──────▼───────┐
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

Interfaces que aíslan lo que va a crecer (las dos costuras clave):

- `Detector.detect(frame) -> list[Detection]` → hoy `StubDetector` y `PersonDetector` (YOLO);
  mañana caídas/pose sin tocar el resto.
- `EventSink.emit(event)` → hoy `StdoutJsonSink`; mañana `AwsSink` sin tocar el resto.

**Identidad estable de cámara:** cada cámara se identifica por su **serie ONVIF, no por IP**. Si el
DHCP le cambia la IP tras un corte, el hub la reconoce como la misma y no duplica ni pierde config.

**Separación stdout/stderr:** los **eventos** (el producto) salen como JSON-lines por **stdout**;
los **logs operativos** (arranque, reconexiones, errores, cambios de conexión) por **stderr**. Así
el futuro `AwsSink` consume un flujo limpio y queda claro qué es dato y qué es diagnóstico.

Estructura del proyecto (sigue el estilo de `vitaplus-watch-adapter`):

```
src/vitahub/
  app.py                 # bootstrap + supervisor + apagado limpio
  config.py              # carga/valida/persiste config (fail-fast)
  models.py              # dataclasses: Camera, Detection, Event
  discovery/onvif.py     # WS-Discovery + GetStreamUri (credencial de hogar)
  registry.py            # identidad estable + estado de conexión
  ingest/rtsp.py         # captura por cámara + reconexión con backoff
  inference/
    base.py  stub.py  person_yolo.py
  analytics/event_engine.py
  sinks/
    base.py  stdout_json.py     # aws.py (futuro, no se construye)
  logging_setup.py       # logs operativos → stderr
tests/…
config/hub.example.yaml  # plantilla de config
Dockerfile               # multi-arch: CPU (Mac) / GPU-TensorRT (Orin)
docker-compose.yml
pyproject.toml
.github/workflows/ci.yml
README.md                # runbook de instalación en campo
```

## 4. Modelo de evento y flujo de datos

Vocabulario mínimo alineado con el glosario canónico existente.

**Cadencia y anti-parpadeo.** No se infiere cada frame: se **muestrea a ~2 fps** (configurable). Un
evento se emite solo cuando el estado **se mantiene** un umbral, para evitar el parpadeo del
detector:

- `person_detected`: hay ≥1 persona durante ≥2 s (cámara que estaba vacía).
- `person_absent`: 0 personas durante ≥5 s (más largo, para no disparar por oclusión breve).
- `person_count_changed`: el conteo cambia entre valores no-cero y se estabiliza.

**Envelope de evento** (una línea JSON por stdout):

```json
{
  "schema_version": 1,
  "hub_id": "hub-3f9a",
  "camera_id": "onvif-szjsa81a81e6adf9",
  "camera_name": "salon",
  "type": "person_detected",
  "severity": "info",
  "timestamp": "2026-08-15T10:00:00Z",
  "payload": { "person_count": 1, "confidence": 0.82 }
}
```

- `camera_id` es la serie ONVIF (estable ante cambios de IP).
- `severity` = `info` para presencia — el hueco donde una futura caída pondría `high`/`critical`.
- `schema_version` para que el `AwsSink` futuro evolucione el formato sin romper consumidores.
- Todo evento pasa por un **único punto de serialización** (el `sink`). Esa es la costura AWS: el
  `AwsSink` recibirá exactamente el mismo objeto `Event`.

**Estado de conexión.** Los cambios cámara-arriba/abajo y hub-online se registran como **logs
operativos por stderr** en este slice. El envelope ya está preparado para que, cuando llegue AWS,
esos cambios se emitan como eventos (`camera_connected` / `camera_disconnected`) por el sink, sin
rediseño. Se dejan fuera del stdout de eventos ahora porque no hay quién los consuma (YAGNI).

## 5. Descubrimiento y configuración de cámaras

Reutiliza lo validado con hardware real: ONVIF WS-Discovery + `GetStreamUri` (ver scripts de
diagnóstico usados en la verificación de la cámara piloto AltoBeam/Tuya, rutas RTSP estilo
Hikvision `/Streaming/Channels/1` principal y `/2` substream).

**Bucle de descubrimiento (periódico, ~60 s, configurable):**

1. Sonda multicast ONVIF (`239.255.255.250:3702`) → cámaras vivas en la LAN con su `XAddrs`.
2. Por cada cámara, autenticando con la **credencial de hogar**:
   - `GetDeviceInformation` → **serie** = `camera_id` estable.
   - `GetProfiles` + `GetStreamUri` → URI RTSP real (principal y substream).
3. **Reconciliación con el registro:**
   - Serie nueva → se añade al registro con nombre auto (`camera-1`, editable) y `enabled: true`.
   - Serie conocida → se actualiza su `last_ip` (aquí se absorbe el cambio de IP).
   - El registro se **persiste** en el fichero de config del volumen montado → sobrevive reinicios.

**Credencial de hogar.** Va por **variable de entorno** (`VITAHUB_ONVIF_USER` /
`VITAHUB_ONVIF_PASSWORD`), no en el fichero de config: no acaba en un fichero que pueda copiarse o
subirse por error, se inyecta una vez por Jetson al lanzar el contenedor, y **nunca se escribe en
logs ni eventos**.

**Fichero de config** (volumen montado, p. ej. `/data/hub.yaml`):

```yaml
hub_id: hub-3f9a            # generado una vez, persistente
discovery:
  interval_seconds: 60
inference:
  detector: person_yolo     # o "stub"
  sample_fps: 2
  confidence: 0.4
  stream: substream         # usa Channels/2 para ahorrar CPU
cameras:                    # se autopobla al descubrir; editable
  - id: onvif-szjsa81a81e6adf9
    name: salon
    last_ip: 192.168.1.190
    enabled: true
```

**Resiliencia de IP y cortes:**

- Identidad por serie → corte de luz + nueva IP DHCP → la cámara reaparece en el descubrimiento con
  otra IP, el hub la reconoce y reconecta al stream nuevo sin intervención.
- Cámara conocida que deja de descubrirse **y** cuyo stream cae → se marca `disconnected` (log por
  stderr). Al volver, se reconecta.

**Override manual (robustez de campo).** Si el multicast falla en alguna casa (WiFi que aísla
clientes), se puede añadir una cámara por IP en el fichero; el hub resuelve su `GetStreamUri` por
unicast directo. Descubrimiento automático como norma, override como red de seguridad.

**Flujo de instalación:** el instalador, desde el móvil, mete la cámara en WiFi + activa ONVIF +
pone la contraseña del hogar. El hub (ya corriendo en el Jetson) la descubre en menos de un
intervalo, autentica con la credencial del hogar y empieza a analizar. Se confirma con
`docker logs`.

## 6. Ingesta RTSP e inferencia

Viven en el mismo **worker por cámara** (un hilo por cámara: aísla fallos — una cámara lenta o
caída no bloquea a las demás).

**Ingesta RTSP:**

- Captura con OpenCV (backend FFmpeg) **forzando RTSP sobre TCP** (`rtsp_transport=tcp`) — es el
  transporte con el que la cámara piloto respondió limpio; UDP en WiFi doméstico da cortes.
- **Buffer mínimo** (`CAP_PROP_BUFFERSIZE=1`) y lectura continua para drenar el buffer; la
  inferencia solo corre sobre el frame muestreado a `sample_fps` (~2 fps). El resto se descarta.
- Usa el **substream por defecto** (`Channels/2`): menos resolución, mucho menos CPU; la presencia
  no necesita 1080p.
- **Reconexión con backoff exponencial** (1→2→4…→30 s tope) ante fallo de lectura o EOF: cierra,
  espera, reabre con la IP más reciente del registro. Cada cambio arriba/abajo → log por stderr.
- **Watchdog de stream estancado:** si no llega frame válido en N s (aunque el socket siga
  "abierto"), fuerza reconexión — es el fallo silencioso típico de estas cámaras.

**Inferencia (costura enchufable):**

- Interfaz `Detector.detect(frame) -> list[Detection]`, con `Detection = {label, confidence, bbox}`.
- `StubDetector`: sin modelo, salida determinista (0 o una detección sintética). Para probar el
  pipeline en el Mac sin descargar pesos y para tests deterministas en CI.
- `PersonDetector`: **YOLO nano vía Ultralytics** (p. ej. `yolo11n`), filtrado a clase `person` con
  umbral de confianza. `person_count` = nº de detecciones sobre el umbral → alimenta el
  event-engine.
- **Runtime portable:** en el Mac corre por CPU; en el Orin, export a TensorRT/CUDA. Misma
  interfaz, misma imagen multi-arch; el detector detecta el dispositivo disponible. **Pesos fijados
  a versión y embebidos en la imagen** (arranca sin depender de internet, coherente con edge-first).
- Carga suficiente: 3-4 cámaras a 2 fps con YOLO-nano es holgado en Orin; en el Mac va más lento
  pero vale para validar la lógica.

Toda la visión avanzada futura (caídas, pose, zonas) entra detrás de `Detector.detect()` sin tocar
ingesta ni eventos.

## 7. Errores, resiliencia y ciclo de vida

**Regla de oro: distinguir permanente de transitorio.**

- **Permanente** (config inválida, falta credencial de hogar, YAML mal formado) → `exit(1)` con
  mensaje claro por stderr. El operador debe arreglarlo; reintentar no sirve.
- **Transitorio** (cámara caída, corte de red, frame corrupto, error de inferencia) → se captura,
  se loguea y se recupera en el bucle; **nunca tumba el proceso**.
- **Worker por cámara:** cualquier excepción se captura, se loguea y el worker se reinicia solo con
  backoff. Una cámara jamás se lleva por delante a las demás ni al proceso.
- **Última red de seguridad:** handler global de excepción no capturada → log de emergencia por
  stderr + `exit(1)` para que el contenedor reinicie.

**Resiliencia y ciclo de vida (Jetson que se reinicia solo):**

- Contenedor con **`restart: unless-stopped`** vía `docker-compose.yml`, y el servicio Docker
  habilitado al arranque del Jetson. Vuelve la luz → arranca Docker → arranca el hub, sin
  intervención.
- **Apagado limpio con SIGTERM:** para workers, vacía el sink, `exit(0)`. Reinicios y redeploys
  limpios.
- **Estado en volumen `/data`:** config + registro de cámaras + `hub_id` persisten reinicios y
  recreación del contenedor.
- **Healthcheck:** el bucle principal refresca un *heartbeat file*; un `HEALTHCHECK` de Docker
  comprueba su frescura. Si el proceso se cuelga (no crashea pero deja de procesar), Docker lo
  detecta y reinicia.

## 8. Testing

Mismo criterio que `watch-adapter`: determinista, sin hardware ni red en CI.

- **Unitarios (pytest)** de la lógica con estado:
  - **event-engine**: máquina de anti-parpadeo con secuencias sintéticas de detecciones (lo más
    delicado — probar transiciones, umbrales de tiempo y debounce).
  - **registry**: reconciliación nueva / conocida / cambio de IP.
  - **config**: validación y casos fail-fast.
  - `StubDetector`: salida determinista.
- **Descubrimiento y RTSP** no se testean en CI (necesitan hardware); test de integración
  **manual/opcional** contra la cámara real o un origen RTSP local.
- **CI (GitHub Actions `ci.yml`):** `ruff` (lint) + `mypy` (tipos) + `pytest`, en PR.

## 9. Empaquetado y despliegue

- **Dockerfile multi-stage y multi-arch** (`linux/amd64` + `linux/arm64`) con `ARG BASE_IMAGE`: por
  defecto base CPU (Mac/dev); para Orin se construye con base **L4T/JetPack (CUDA)**. Pesos del
  modelo embebidos.
- `docker-compose.yml` (restart policy, volumen `/data`, env de credenciales),
  `config/hub.example.yaml`, y **README con runbook de instalación en campo**.
- Sin CI de deploy a ECR todavía (no hay AWS en este slice); se añade cuando exista el stack
  `02-hub-ingest`.

## 10. Decisiones abiertas y evolución futura

- **Uplink a AWS (E):** el candidato natural para el hub es **AWS IoT Core** (MQTT/TLS con X.509,
  registro de dispositivos y eventos de lifecycle de conexión nativos) — a diferencia del reloj,
  que no podía hablar MQTT y obligó al NLB+Fargate a medida. A decidir al diseñar `02-hub-ingest`
  en el repo de arquitectura. La costura en este repo es `EventSink`.
- **Telemetría de conexión (F):** cuando exista AWS, emitir `camera_connected` /
  `camera_disconnected` / `hub_online` como eventos por el sink (ya contemplado en el envelope).
- **Visión avanzada (C):** caídas, pose, entra/sale de zonas, actividad — todo detrás de
  `Detector.detect()`. Reconciliar con la taxonomía §11 cuando se escriba.
- **Rendimiento en Orin:** si 3-4 cámaras se quedan cortas, evolución natural a un pipeline
  GStreamer/DeepStream (era el Enfoque 3, descartado como punto de partida).

## 11. Explícitamente fuera de alcance del slice

- Uplink a AWS y stack `02-hub-ingest`.
- Telemetría de conexión hacia AWS (solo logs en este slice).
- Detección de caídas, pose, zonas, actividad doméstica.
- Panel web local.
- CI/CD de deploy a ECR.
- Gestión remota de la flota de hubs.
