# VitaPlus Hub — Esqueleto + Event-Engine (primera entrega)

**Fecha:** 2026-07-10
**Estado:** Aprobado por el usuario, pendiente de plan de implementación
**Referencia:** `docs/initial-dev.md` (especificación completa del hub, §3, §4, §9)

## 1. Contexto y alcance

`docs/initial-dev.md` describe el hub completo de VitaPlus: 10 módulos (`camera-connector`, `vision-inference`, `tracker`, `event-engine`, `local-cache`, `clip-manager`, `edge-sync`, `health-monitor`, `ota-updater`, `privacy-guard`) a construir en ~12 semanas. Es demasiado grande para una sola entrega; este documento cubre **solo la primera iteración**: el esqueleto del hub (apoyado en Frigate, según recomienda el documento original) más el diseño completo del `event-engine`, que es la pieza de mayor valor diferencial propio.

### Alcance de esta entrega

Incluye:
- Pipeline de punta a punta funcionando con **webcam USB local** como sustituto temporal de la cámara IP RTSP (el usuario aún no tiene la Reolink; sí tiene el mini-PC/Jetson Orin de producción).
- 4 tipos de evento del MVP: `presence_zone`, `home_exit`/`home_entry`, `inactivity_prolonged`, `night_activity_unusual`.
- Configuración de zonas/horarios/umbrales en YAML local, como sustituto de la configuración que en el futuro bajará del backend cloud.
- `local-cache` (SQLite) embebido en el `event-engine`, con interfaz propia aislable.
- `edge-sync` como **stub**: no hay backend cloud todavía, así que se simula el envío con logging estructurado y el mismo ciclo de vida (`pending → syncing → synced`) que tendrá la versión real.

Explícitamente fuera de esta entrega (quedan para iteraciones posteriores, ya identificadas en `docs/initial-dev.md`): `clip-manager`, `health-monitor` completo, `ota-updater`, `privacy-guard`, detección de caída (`probable_fall`), `kitchen_activity`, `routine_missed`, cifrado de disco, integración real con el backend cloud.

### Entorno de desarrollo

- Hardware: NVIDIA Jetson Orin Nano (o similar), con aceleración TensorRT disponible para Frigate.
- Cámara: no disponible aún. Se usa una webcam USB conectada al Jetson, republicada como stream RTSP, para que el resto del pipeline sea idéntico al que se usará con la cámara IP real (basta con cambiar la URL RTSP el día que llegue).

## 2. Arquitectura de componentes

```
┌─────────────────────────── HUB (dev) ───────────────────────────┐
│                                                                   │
│  webcam (/dev/video0)                                            │
│      │ ffmpeg publish                                            │
│      ▼                                                            │
│  [mediamtx] ──RTSP──► [frigate] ──detecciones──► (MQTT)          │
│                                        │                          │
│                                        ▼                          │
│                                  [mosquitto]                      │
│                                        │                          │
│                                        ▼                          │
│                                 [event-engine]                    │
│                                  │           │                    │
│                          config/rules.yaml   │                    │
│                                        ▼                          │
│                              local-cache (SQLite, embebido)       │
│                                        │                          │
│                                        ▼                          │
│                              edge-sync (stub: log estructurado)   │
└────────────────────────────────────────────────────────────────────┘
```

| Componente | Responsabilidad | Notas |
|---|---|---|
| `webcam-publisher` | Captura `/dev/video0` con ffmpeg, publica RTSP a `mediamtx` en dos resoluciones (simula substream/mainstream) | Contenedor descartable; se elimina al llegar la cámara IP real |
| `mediamtx` | Servidor RTSP intermedio que recibe el push de ffmpeg y lo sirve como haría una cámara IP | Imagen oficial, solo configuración |
| `frigate` | Ingesta RTSP + detección de persona + zonas + publica a MQTT | Imagen oficial `frigate:stable-tensorrt-jetson`; zonas configuradas en `frigate.yml` |
| `mosquitto` | Broker MQTT local | Imagen oficial, configuración mínima |
| `event-engine` | Máquina de estados que aplica `rules.yaml` sobre las detecciones de Frigate y genera eventos de negocio | Único servicio 100% custom de esta fase |
| `event_store` (dentro de `event-engine`) | Interfaz de persistencia (`save_event`, `get_pending`, `mark_synced`) sobre SQLite | Aislado como módulo propio para poder extraerlo a servicio independiente más adelante |
| `edge_sync_stub` (dentro de `event-engine`) | Simula el envío a cloud: marca el evento `syncing`, loggea el JSON, lo marca `synced` | Se sustituye por el `edge-sync` real sin tocar el resto del pipeline |

## 3. Enfoque técnico del event-engine

Se evaluaron tres enfoques:

- **A — Máquina de estados explícita en asyncio puro (elegido).** Estado en memoria (`dict` por cámara/zona), actualizado por callbacks MQTT; timers de reglas temporales mediante un tick periódico de `asyncio`. Sin dependencias de librería de FSM. Encaja con el stack recomendado en `docs/initial-dev.md` (Python 3.11+ asyncio), es la opción más simple y testeable, y es suficiente para los 4 eventos de esta entrega.
- **B — Librería de FSM (`python-statemachine`/`transitions`) + APScheduler.** Más formal y auto-documentado, pero es sobre-ingeniería para 4 eventos (viola YAGNI). Se reconsiderará si el catálogo de eventos crece significativamente (p. ej. al añadir `probable_fall`).
- **C — Motor de reglas por ventana deslizante (estilo CEP).** Más flexible a largo plazo pero más lento de construir y depurar para el MVP.

Decisión: **Enfoque A**. Migrar a B es viable más adelante sin rehacer el pipeline MQTT↔local-cache, porque la interfaz de cada regla (`evaluate(state, config) -> Event | None`) ya aísla la lógica de decisión del mecanismo de disparo.

## 4. Estructura del repositorio

```
vitaplus-process-hub/
├── docker-compose.yml
├── docker-compose.override.yml.example   # overrides para hardware real (Reolink) más adelante
├── config/
│   ├── mosquitto/mosquitto.conf
│   ├── frigate/frigate.yml
│   └── hub/rules.yaml                    # zonas, horarios, umbrales (sustituto del cloud)
├── services/
│   ├── webcam-publisher/
│   │   └── publish.sh
│   └── event-engine/
│       ├── Dockerfile
│       ├── pyproject.toml
│       └── src/event_engine/
│           ├── main.py                   # arranque asyncio, wiring
│           ├── mqtt_client.py            # suscripción a topics Frigate, reconexión/backoff
│           ├── rules_config.py           # carga y validación de rules.yaml
│           ├── state.py                  # estado en memoria por cámara/zona/track
│           ├── rules/                    # una clase por tipo de evento
│           │   ├── presence_zone.py
│           │   ├── home_exit_entry.py
│           │   ├── inactivity.py
│           │   └── night_activity.py
│           ├── event_store.py            # interfaz local-cache (SQLite)
│           └── edge_sync_stub.py         # log estructurado del evento "enviado"
└── tests/
    └── event_engine/                     # pytest, eventos MQTT sintéticos
```

## 5. Flujo de datos y reglas

**Topics MQTT consumidos de Frigate:**
- `frigate/events` — JSON completo (`type`: start/update/end, cámara, zonas, `id` de track). Fuente principal de entradas/salidas de zona.
- `frigate/available` — heartbeat de Frigate; usado para pausar reglas de inactividad si la cámara está offline (evita falsos positivos), y preparado para el futuro `health-monitor`.

**Estado en memoria (`state.py`):** `camera_id → zone_id → ZoneState`, con `occupied: bool`, `last_change: datetime`, `last_motion: datetime`. Solo se actualiza con eventos MQTT; el tick no lo modifica, solo lo lee.

**Tick periódico (cada 30 s):** evalúa las reglas basadas en tiempo (`inactivity_prolonged`, `night_activity_unusual`), que no dependen de un evento puntual sino de cuánto tiempo ha transcurrido.

**Reglas** (una clase por tipo de evento, todas implementan `evaluate(state, config) -> Event | None`):
- `presence_zone`: transición `False→True` de `ZoneState.occupied`, con debounce de 3 s para evitar parpadeo en el borde de una zona.
- `home_exit`/`home_entry`: igual que `presence_zone` pero restringido a zonas `type: door`, usando la dirección del track si Frigate la aporta.
- `inactivity_prolonged`: en el tick, si `now - last_motion > umbral` (de `rules.yaml`) dentro del horario "se espera actividad", emite el evento una única vez hasta que vuelva a haber movimiento.
- `night_activity_unusual`: en el tick, si `occupied=True` fuera de la franja de sueño configurada.

**`config/hub/rules.yaml`** (ejemplo, un fichero por hogar):
```yaml
hub_id: hub_dev_001
user_id: usr_dev_001
cameras:
  cam_salon:
    zones:
      cocina: {type: room, polygon: [[0,0],[100,0],[100,100],[0,100]]}
      puerta: {type: door, polygon: [[0,0],[20,0],[20,20],[0,20]]}
schedules:
  expected_activity: {start: "07:00", end: "23:00"}
  sleep_window: {start: "23:00", end: "07:00"}
thresholds:
  inactivity_minutes: 240
```

**Contrato de evento** (idéntico al §4.3 de `docs/initial-dev.md`):
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
  "evidence": { "clip_ref": null, "uploaded": false },
  "schema_version": "1.0"
}
```
`confidence` se fija a `1.0` para estos 4 eventos deterministas por regla (no probabilísticos); el campo se deja preparado para `probable_fall` en una iteración futura.

## 6. Manejo de errores y resiliencia

- **MQTT desconectado:** reintentos con backoff exponencial (1 s → 60 s máx.); el estado en memoria se conserva y, al reconectar, Frigate reenvía sus últimos mensajes retenidos para resincronizar zonas ocupadas.
- **Mensaje MQTT malformado:** se loggea y se descarta; nunca tira el proceso.
- **Escritura en SQLite:** síncrona antes de considerar un evento "generado"; 3 reintentos y luego error crítico logueado (perder un evento es peor que duplicarlo — la deduplicación por `event_id` la hará el backend).
- **`edge_sync_stub` caído/lento:** no bloquea el pipeline; eventos quedan `synced=false` en SQLite y un worker separado reintenta, igual que hará el `edge-sync` real.
- **Reinicio del contenedor:** `restart: always` en todos los servicios; `event-engine` arranca con `state` en memoria vacío, pero `last_motion` se recupera del último evento en SQLite si existe, para no generar falsos positivos de inactividad tras un reinicio.
- **Cámara/webcam caída:** si `frigate/available != online`, las reglas de inactividad se pausan en vez de dispararse (evita falsos positivos por pérdida de señal, no por ausencia real de la persona).

## 7. Testing

- **Unit tests (`tests/event_engine/`, pytest + `pytest-asyncio`):** cada regla se testea de forma aislada inyectando payloads MQTT sintéticos de Frigate, sin necesidad de Frigate/MQTT/webcam reales.
- **Test de `event_store`:** contra SQLite en memoria; verifica `save_event`, `get_pending`, `mark_synced` e idempotencia por `event_id`.
- **Test de `rules_config`:** un `rules.yaml` inválido (polígono mal formado, horario incoherente) debe fallar rápido y con mensaje claro al arrancar.
- **Test de integración manual (no automatizado en esta fase):** `docker-compose up` con webcam real; verificar en logs que aparece `presence_zone` al entrar en cuadro y que queda persistido en SQLite. Sirve como criterio de aceptación de esta entrega, no como test en CI.
- **CI (GitHub Actions):** lint + unit tests del `event-engine` en cada push. No se levanta Frigate/webcam en CI (no hay hardware disponible ahí).

## 8. Criterios de aceptación de esta entrega

1. Al detectar una persona en una zona configurada, se genera `presence_zone` en menos de 5 s y queda persistido en SQLite.
2. Cortar la conexión MQTT y restaurarla no pierde ni duplica eventos.
3. Reiniciar el contenedor `event-engine` recupera `last_motion` desde SQLite sin necesidad de reconfigurar nada manualmente.
4. Ningún fotograma de vídeo sale de los contenedores `frigate`/`mediamtx` hacia fuera del host — solo eventos JSON vía el stub de log.
