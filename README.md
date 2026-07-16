# VitaPlus — Hub de Procesamiento Local

Documentación de referencia para cualquiera que se incorpore al repositorio.
Cubre qué es el proyecto, cómo está organizado, cómo funciona cada pieza y
cómo desplegarlo en los distintos entornos.

> **Estado del repo:** esqueleto funcional de la primera "entrega" (cámara →
> Frigate → MQTT → `event-engine` → SQLite → stub de sync a nube). No hay
> todavía entornos de pre/producción reales ni backend cloud — ver
> [§8 Entornos y despliegue](#8-entornos-y-despliegue) para el porqué y el
> camino previsto.

---

## 1. ¿Qué es VitaPlus y qué hace este repo?

VitaPlus es una plataforma de monitorización de personas mayores con tres
fuentes de datos: **cámaras** (rama "core"), **pulsera/smartwatch** y
**llamadas con IA**. Este repositorio implementa únicamente la primera pata:
el **hub local** que se instala en casa del usuario.

El hub **no graba ni retransmite vídeo**. Su trabajo es coger las señales
brutas de las cámaras y convertirlas en **eventos semánticos** ("presencia en
cocina", "entrada por la puerta", "4 horas sin movimiento") que viajarían al
backend cloud (aún no existe en este repo — se simula con un stub).

Motivos de diseño (no negociables, ver `docs/initial-dev.md` §6):

- **Privacidad:** el vídeo continuo nunca sale del hogar.
- **Coste:** procesar en local reduce ancho de banda y coste de nube.
- **Resiliencia:** una caída se detecta aunque se corte internet.

La especificación funcional completa (hardware, eventos del MVP, contrato
JSON, plan de 12 semanas, riesgos) vive en
[`docs/initial-dev.md`](docs/initial-dev.md). Este README es el mapa
operativo del código que ya existe; para el "porqué" de cada decisión de
producto, esa spec es la fuente de verdad.

---

## 2. Arquitectura general

Todo corre en contenedores Docker orquestados con Docker Compose, comunicados
por MQTT (Mosquitto) local.

```
┌─────────────────────────────── HUB LOCAL (docker compose) ───────────────────────────────┐
│                                                                                            │
│  webcam-publisher ──RTSP──► mediamtx ──RTSP (sub+main)──► frigate ──detecciones──► MQTT    │
│  (ffmpeg, dev only)      (servidor RTSP)      (NVR + IA, zonas)      (mosquitto)           │
│                                                                          │                  │
│                                                                          ▼                  │
│                                                                   event-engine               │
│                                                             (asyncio, reglas de negocio)      │
│                                                                          │                  │
│                                                              ┌───────────┴───────────┐       │
│                                                              ▼                       ▼       │
│                                                        SQLite (events.db)      edge_sync_stub │
│                                                        (cola local, estado)    (log → "cloud")│
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

En un despliegue real (cámara Reolink física en vez de webcam simulada), el
`webcam-publisher` y `mediamtx` desaparecen y Frigate se conecta directamente
al RTSP de la cámara — ver `docker-compose.override.yml.example`.

### Servicios (`docker-compose.yml`)

| Servicio | Imagen / build | Rol | Puertos |
|---|---|---|---|
| `webcam-publisher` | `services/webcam-publisher` (build local) | Sustituto de cámara real en dev: reenvía la webcam del host (`/dev/video0`) a `mediamtx` como dos streams RTSP (main + sub) | — |
| `mediamtx` | `bluenviron/mediamtx:latest` | Servidor RTSP intermedio. Recibe del publisher (o de la cámara real) y sirve a Frigate | `8554` (RTSP, solo debug local) |
| `frigate` | `ghcr.io/blakeblackshear/frigate:stable-tensorrt-jetson` | NVR + detección IA (TensorRT). Define zonas virtuales por cámara y publica detecciones en MQTT | `5000` (UI, solo debug local) |
| `mosquitto` | `eclipse-mosquitto:2` | Broker MQTT interno, único canal de comunicación entre Frigate y `event-engine` | `1883` |
| `event-engine` | `services/event-engine` (build local) | **El único servicio propio del equipo.** Máquina de estados que convierte detecciones de Frigate en eventos de negocio, los persiste y simula su envío a cloud | — |

Todos los servicios llevan `restart: always`: si el hub se reinicia (corte de
luz, etc.), todo vuelve a arrancar solo.

**Importante — hardware:** la imagen de Frigate está fijada a
`stable-tensorrt-jetson` y `config/frigate/frigate.yml` usa el detector
`tensorrt`. Esto significa que **el stack tal cual está en el repo necesita
una NVIDIA Jetson** (Orin Nano recomendado en la spec), no un portátil
genérico. Si vas a desarrollar en x86 sin Jetson, tendrás que adaptar
`config/frigate/frigate.yml` a un detector CPU/otro backend — no hay perfil
alternativo en el repo todavía.

---

## 3. Estructura de ficheros

```
vitaplus-process-hub/
├── docker-compose.yml                    # Stack completo de desarrollo
├── docker-compose.override.yml.example   # Plantilla para cámara Reolink real
├── pyproject.toml                        # Config de pytest a nivel repo-root (ver nota abajo)
├── .github/workflows/event-engine-ci.yml # CI: lint + tests del event-engine
│
├── config/                               # Configuración externa a los contenedores (montada como volumen)
│   ├── hub/rules.yaml                    # Reglas de negocio: zonas, horarios, umbrales (nunca hardcodear en código)
│   ├── frigate/frigate.yml               # Config de Frigate: cámaras, detector, zonas
│   └── mosquitto/mosquitto.conf          # Config del broker MQTT
│
├── services/
│   ├── webcam-publisher/                 # Sustituto dev de cámara real (ffmpeg + v4l2)
│   │   ├── Dockerfile
│   │   └── publish.sh
│   └── event-engine/                     # Único servicio Python del equipo
│       ├── Dockerfile
│       ├── pyproject.toml                # Dependencias del paquete (aiomqtt, pydantic, pyyaml)
│       └── src/event_engine/
│           ├── __init__.py               # __version__
│           ├── main.py                   # Entrypoint: arranca EventEngine + MQTT + edge-sync
│           ├── models.py                 # Event / Evidence (contrato JSON hacia cloud)
│           ├── state.py                  # HubState en memoria (ocupación de zonas, última actividad)
│           ├── rules_config.py           # Carga y valida rules.yaml (pydantic)
│           ├── event_store.py            # Persistencia SQLite (cola de eventos, idempotencia)
│           ├── edge_sync_stub.py         # Simula el envío a cloud (hoy: solo loguea)
│           ├── mqtt_client.py            # Cliente MQTT hacia Frigate, con reconexión con backoff
│           └── rules/                    # Una clase por evento de negocio del MVP
│               ├── presence_zone.py
│               ├── home_exit_entry.py
│               ├── inactivity.py
│               └── night_activity.py
│
├── tests/event_engine/                   # Tests (pytest + pytest-asyncio), en espejo de src/
│   ├── fixtures/                         # YAML de config válidos/inválidos para tests
│   └── test_*.py
│
└── docs/
    ├── initial-dev.md                    # Spec funcional completa del hub (fuente de verdad de producto)
    ├── verification/
    │   └── hub-skeleton-manual-check.md  # Checklist manual de verificación (requiere webcam real)
    └── superpowers/                      # Artefactos de planificación (spec de diseño + plan de implementación
        ├── specs/                        # de la entrega "hub-skeleton-event-engine"). Útiles para entender
        └── plans/                        # el porqué de decisiones concretas de implementación.
```

**Nota sobre `pyproject.toml`:** hay dos ficheros con ese nombre. El de la
raíz del repo solo contiene `[tool.pytest.ini_options]` y es el que **de
verdad** lee pytest cuando se ejecuta `pytest tests/event_engine -v` desde la
raíz (porque esa es la convención de invocación del proyecto). El de
`services/event-engine/pyproject.toml` define el paquete instalable
(dependencias, build) y también tiene su propio bloque de pytest, pero ese
es inerte para la invocación desde la raíz — solo importa si se testea el
paquete de forma aislada.

---

## 4. El `event-engine` en detalle

Es un único proceso Python 3.11+ `asyncio` (`main.py`) que lanza tres tareas
concurrentes con `asyncio.gather`:

1. **`mqtt_client.run_forever()`** — se suscribe a `frigate/events` y
   `frigate/available` en Mosquitto. Reconecta con backoff exponencial
   (1 s → 60 s máx) si se cae la conexión.
2. **`engine.run_tick_loop()`** — cada **30 s** (`TICK_SECONDS`) evalúa las
   reglas temporales (`inactivity_prolonged`, `night_activity_unusual`) para
   cada cámara/zona.
3. **`sync_stub.run_forever()`** — cada 5 s recoge eventos pendientes de
   SQLite y simula su envío a cloud (hoy solo loguea el JSON; es el punto de
   extensión futuro para hablar de verdad con el backend).

Estado compartido: `HubState` (en memoria, ocupación de zonas y última
actividad por cámara) y `EventStore` (SQLite, cola persistente de eventos).

### 4.1 Flujo de un evento

1. Frigate detecta una persona en una zona y publica en `frigate/events`.
2. `on_frigate_event` actualiza `HubState` y, si una zona cambia de estado
   (puerta, o habitación que pasa a ocupada), programa una comprobación
   **debounced a 3 s** (`DEBOUNCE_SECONDS`) por zona — un nuevo cambio dentro
   de esa ventana cancela y reprograma la tarea anterior, para evitar eventos
   duplicados por parpadeo en el borde de una zona.
3. Pasado el debounce, se evalúa la regla correspondiente
   (`PresenceZoneRule` para habitaciones, `HomeExitEntryRule` para puertas).
4. Si la regla genera un `Event`, se persiste en SQLite
   (`EventStore.save_event`, con 3 reintentos; si falla de forma persistente
   se loguea `critical` pero **nunca se lanza el proceso**) y se loguea.
5. `edge_sync_stub` recoge periódicamente los eventos `pending` y los marca
   `synced` (simulación — aquí es donde iría la llamada HTTPS/MQTT-TLS real
   al backend).

### 4.2 Los 4 eventos de negocio implementados

| Regla (fichero) | Evento(s) | Dispara cuando |
|---|---|---|
| `rules/presence_zone.py` | `presence_zone` | Una zona `type: room` pasa a ocupada |
| `rules/home_exit_entry.py` | `home_entry` / `home_exit` | Una zona `type: door` cambia de ocupación (dirección = proxy de la propia transición, sin sensor de puerta) |
| `rules/inactivity.py` | `inactivity_prolonged` | Sin movimiento en cámara durante más de `thresholds.inactivity_minutes`, dentro de `schedules.expected_activity`. Se dispara una vez por episodio; se resetea al reanudarse el movimiento |
| `rules/night_activity.py` | `night_activity_unusual` | Zona ocupada dentro de `schedules.sleep_window`. Una vez por episodio continuo de ocupación |

Todas las reglas son funciones puras `evaluate(camera_id, zone_id, state,
config, now) -> Event | None`, deliberadamente sin `asyncio` dentro para que
sean triviales de testear unitariamente. El debounce y el scheduling viven
en `EventEngine`, no en las reglas.

### 4.3 Contrato de evento (JSON hacia cloud)

Definido en `models.py::Event.to_dict()`:

```json
{
  "event_id": "evt_9f3a...",
  "hub_id": "hub_dev_001",
  "user_id": "usr_dev_001",
  "camera_id": "cam_salon",
  "type": "presence_zone",
  "severity": "info",
  "confidence": 1.0,
  "zone": "cocina",
  "start_time": "2026-07-10T10:32:05Z",
  "end_time": null,
  "metadata": {},
  "evidence": {"clip_ref": null, "uploaded": false},
  "schema_version": "1.0"
}
```

`confidence` está fijo a `1.0` para los 4 eventos del MVP (son
deterministas, no probabilísticos — no hay modelo de pose/caída todavía).

---

## 5. Configuración

Toda la configuración de negocio vive en YAML montado como volumen —
**nunca hardcodeada** en el código del `event-engine`.

### `config/hub/rules.yaml`

Define, por hub: `hub_id`, `user_id`, cámaras con sus zonas (`room` o
`door`, con polígono de al menos 3 puntos), horarios
(`expected_activity`, `sleep_window`) y umbrales (`inactivity_minutes`).
Se valida con `pydantic` en `rules_config.py` — un YAML mal formado
(polígono con <3 puntos, horario degenerado con `start == end`, umbral
negativo) lanza `ValidationError` al arrancar.

### `config/frigate/frigate.yml`

Config estándar de Frigate: conexión MQTT a `mosquitto`, detector
`tensorrt`, streams RTSP de detección (`_sub`) y grabación (`_main`), y las
mismas zonas que `rules.yaml` (coordenadas en el sistema de píxeles del
stream de detección — hoy están duplicadas manualmente entre ambos ficheros,
ojo a mantenerlas en sync).

### `config/mosquitto/mosquitto.conf`

Broker sin autenticación (`allow_anonymous true`) — válido porque MQTT nunca
sale del host/red interna del hub. **No usar así si se expone el puerto
1883 fuera del host.**

### Variables de entorno del `event-engine` (`docker-compose.yml`)

| Variable | Default (Dockerfile) | Uso |
|---|---|---|
| `RULES_CONFIG_PATH` | `/config/hub/rules.yaml` | Ruta al YAML de reglas |
| `EVENT_DB_PATH` | `/data/events.db` | Ruta a la base SQLite (en volumen `event-engine-data`) |
| `MQTT_HOST` | `mosquitto` | Host del broker |
| `MQTT_PORT` | `1883` | Puerto del broker |

---

## 6. Puesta en marcha en local (dev)

### Requisitos

- Docker + Docker Compose.
- **Hardware Jetson** (o adaptar `frigate.yml` a otro detector — ver nota de
  §2) para que Frigate arranque con el detector `tensorrt`.
- Una webcam en `/dev/video0` del host (la usa `webcam-publisher` para
  simular la cámara Reolink que aún no ha llegado).
- Python 3.11+ y `pip` si vas a correr tests/lint fuera de Docker (ya hay un
  `.venv` en el repo con el paquete instalado en editable).

### Arrancar el stack

```bash
docker compose up --build
```

Verificación rápida:

```bash
docker compose ps                    # los 5 servicios deben estar "Up" sin reinicios en bucle
docker compose logs mosquitto        # sin errores de conexión
docker compose logs frigate          # debe detectar el RTSP de mediamtx y publicar frigate/available -> online
docker compose logs -f event-engine  # aquí aparecen los "event generated: {...}"
```

Para el checklist de verificación funcional completo (latencia de eventos,
resiliencia a caída de MQTT, recuperación tras reinicio, ausencia de fuga de
vídeo), sigue
[`docs/verification/hub-skeleton-manual-check.md`](docs/verification/hub-skeleton-manual-check.md)
— requiere estar delante de la webcam, no es automatizable.

Inspeccionar la cola de eventos directamente:

```bash
docker compose exec event-engine sqlite3 /data/events.db "select type, status from events;"
```

### Parar y limpiar

```bash
docker compose down            # para los servicios, conserva los volúmenes (SQLite, media de Frigate)
docker compose down -v         # además borra los volúmenes (empieza de cero)
```

---

## 7. Tests y CI

El `event-engine` es el único código con tests automatizados del repo.

```bash
# desde la raíz del repo, con el venv activo (o pip install -e "services/event-engine[dev]")
pytest tests/event_engine -v
ruff check services/event-engine/src tests/event_engine
```

CI (`.github/workflows/event-engine-ci.yml`, GitHub Actions): se dispara en
`push`/`pull_request` **solo cuando cambian** `services/event-engine/**`,
`tests/event_engine/**` o el propio workflow. Instala el paquete en modo
editable, corre `ruff check` y luego `pytest`. No hay build/push de imágenes
Docker en CI todavía — solo lint + tests.

---

## 8. Entornos y despliegue

### Estado actual: solo existe "dev"

Hoy el repo define un único entorno, pensado para desarrollo sobre hardware
real (Jetson + webcam de pruebas): el `docker-compose.yml` de la raíz. **No
hay definición de "pre" ni "producción" todavía** — no hay backend cloud real
(el `edge_sync_stub` es un stub que solo loguea), ni registry de imágenes, ni
pipeline de despliegue, ni gestión de flota de hubs.

Esto no es un olvido de la documentación: es el estado real del proyecto en
esta fase (ver plan de 12 semanas en `docs/initial-dev.md` §9 — este repo
cubre aproximadamente las semanas S1-S4).

### Dev → "producción de hogar" (cámara real)

El primer escalón de despliegue ya previsto en el repo es sustituir la
webcam simulada por la cámara Reolink real, **sin tocar el resto del
stack**:

1. Copiar la plantilla:
   ```bash
   cp docker-compose.override.yml.example docker-compose.override.yml
   ```
2. Crear `config/frigate/frigate.reolink.yml` con las URLs RTSP reales de la
   cámara (substream de detección y mainstream de grabación) — **no incluido
   en el repo**, hay que generarlo por hogar/cámara.
3. `docker compose up --build` recogerá automáticamente el override: éste
   desactiva `webcam-publisher` y `mediamtx` (`profiles: ["disabled"]`) y
   apunta Frigate directamente a la cámara.

`docker-compose.override.yml` está en `.gitignore` por convención de Docker
Compose (no se versiona; cada hogar/hub tiene el suyo) — confirma que no está
trackeado antes de asumir que sí lo está.

### Camino previsto para "pre" y "producción" real (según `docs/initial-dev.md`)

No implementado en este repo, pero es el criterio de diseño ya decidido en la
spec de producto — tenlo en cuenta si vas a construir estas piezas:

| Aspecto | Decisión de producto |
|---|---|
| SO / runtime del hub | Ubuntu LTS + Docker Compose. BalenaOS como alternativa para gestión de flota multi-hogar (fase 2) |
| Hardware | Mini-PC x86 o NVIDIA Jetson Orin Nano por hogar |
| Actualizaciones | OTA firmada (`ota-updater`, no implementado); rechazar imágenes/modelos no verificados |
| Red | VLAN dedicada a cámaras, **cero puertos abiertos hacia internet** — el hub siempre inicia las conexiones salientes |
| Disco | Partición cifrada (LUKS o equivalente) para el buffer de clips, claves por dispositivo |
| Sync a cloud real | HTTPS o MQTT sobre TLS, cola persistente ya implementada en SQLite (`EventStore`), autenticación por certificado/clave único por hub, idempotencia por `event_id` |
| Observabilidad | Sentry (errores) + Prometheus/Grafana (métricas), heartbeat cada 60 s |
| Piloto controlado | 1-3 hogares antes de producción general, con revisión RGPD previa obligatoria |

Cuando exista un entorno "pre" real, el patrón razonable dado lo que ya hay
en el repo sería: mismo `docker-compose.yml` base + un
`docker-compose.pre.yml` de overlay (imágenes con tag fijo en vez de
`latest`/build local, `EVENT_DB_PATH` y `RULES_CONFIG_PATH` apuntando a
configuración de pre) — siguiendo el mismo mecanismo de overrides que ya usa
`docker-compose.override.yml.example` para el salto dev → cámara real.

---

## 9. Troubleshooting rápido

| Síntoma | Dónde mirar |
|---|---|
| Frigate no arranca o reinicia en bucle | `docker compose logs frigate` — normalmente detector `tensorrt` sin hardware Jetson compatible, o modelo TensorRT no disponible en la ruta configurada |
| `event-engine` no genera eventos | `docker compose logs event-engine` — comprobar que `frigate/available` esté `online` y que las zonas de `frigate.yml` coincidan en nombre con `rules.yaml` |
| Eventos duplicados o perdidos tras caída de MQTT | Revisar reconexión con backoff en `mqtt_client.py`; comprobar duplicados con `select count(*), event_id from events group by event_id having count(*) > 1;` en SQLite (debe salir vacío) |
| `inactivity_prolonged` espurio justo tras reiniciar `event-engine` | Comprobar `bootstrap_last_motion()` — debe recuperar `last_motion` desde el último evento en SQLite antes de evaluar la primera tick |
| Cambios en `rules.yaml` no se reflejan | El `event-engine` lee el YAML solo al arrancar (`load_rules_config` en `main()`) — hace falta `docker compose restart event-engine` |

---

## 10. Dónde seguir leyendo

- [`docs/initial-dev.md`](docs/initial-dev.md) — especificación funcional
  completa del hub: alcance, hardware, arquitectura objetivo (módulos no
  implementados aún como `clip-manager`, `health-monitor`, `ota-updater`,
  `privacy-guard`), requisitos de privacidad/seguridad no negociables, plan
  de 12 semanas y criterios de aceptación del MVP.
- [`docs/superpowers/specs/2026-07-10-hub-skeleton-event-engine-design.md`](docs/superpowers/specs/2026-07-10-hub-skeleton-event-engine-design.md)
  y [`docs/superpowers/plans/2026-07-10-hub-skeleton-event-engine.md`](docs/superpowers/plans/2026-07-10-hub-skeleton-event-engine.md)
  — diseño técnico y plan de implementación tarea a tarea de lo que hoy es
  el código en `services/event-engine`. Útiles para entender el "porqué" de
  decisiones concretas (p.ej. por qué el debounce vive en `EventEngine` y no
  en las reglas).
- [`docs/verification/hub-skeleton-manual-check.md`](docs/verification/hub-skeleton-manual-check.md)
  — checklist manual de aceptación de esta entrega.
