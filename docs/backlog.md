# Backlog — Hub de procesamiento de cámaras

Trabajo diferido tras el primer slice (`docs/superpowers/plans/2026-08-15-hub-procesamiento-camaras.md`).
Todo lo de aquí salió de las revisiones de código; ninguno bloquea el slice actual, pero
condiciona el siguiente. Orden aproximado de prioridad.

## Próximo slice (mayor impacto)

### 1. ~~Re-descubrimiento periódico~~ — HECHO (slice de 2026-08-17)

El hub re-descubre cada `discovery.interval_seconds` y admite un `POST /rescan` en LAN.
Ver `docs/superpowers/specs/2026-08-17-rescan-camaras-design.md`.

**Descartado con motivo — fallback RTSP por `last_ip`:** construir la URL del stream desde
la IP guardada obliga a adivinar la ruta, que cambia con cada fabricante (la misma
fragilidad del `:10000` hardcodeado del punto 2). Con rescan periódico, una cámara conocida
que vuelve se recupera sola por descubrimiento, que devuelve la URI real. Si algún día una
cámara resultara indescubrible pero alcanzable, se reabre.

**Pendiente relacionado — comando remoto:** el disparo de rescan desde la nube (app del
técnico fuera del hogar) necesita el uplink del punto 3. La costura está lista: el downlink
solo tiene que llamar a `RescanService.run_once()`.

### 2. Endpoints ONVIF derivados de `XAddrs` / `GetServices`
`discovery/onvif.py` **hardcodea** `http://{ip}:10000/onvif/device_service` y asume la ruta del
media service. `parse_probe_matches` descarta el `XAddrs` autoritativo (que trae puerto y ruta
reales). Funciona para la cámara piloto (Tuya en `:10000`), pero **no para otros modelos**
(`:80`/`:8000`, rutas distintas). Trabajo: parsear puerto+ruta desde `XAddrs` para el device
service, y resolver el media service vía capabilities/`GetServices` en vez de adivinarlo.

### 3. Uplink a AWS (cruza al repo de arquitectura)
- Stack `02-hub-ingest` en `vitaplus-aws-architecture` (candidato: AWS IoT Core — MQTT/TLS, registro
  de dispositivos y eventos de conexión nativos).
- Implementar `sinks/aws.py` (`EventSink`) en este repo — la costura ya está lista, es un swap de un
  fichero.
- Telemetría de conexión (F): emitir `camera_connected`/`camera_disconnected`/`hub_online` por el
  sink cuando exista consumidor.

### 4. Watchdog real de proceso (decisión pendiente)
`restart: unless-stopped` solo actúa cuando el proceso **sale**; el `HEALTHCHECK` de Docker
únicamente marca el contenedor `unhealthy` — Docker Engine en solitario no lo recrea por eso (eso
es Swarm/Kubernetes). Si el proceso se cuelga de verdad (queda vivo pero no avanza: hilo bloqueado,
deadlock...), hoy **nada** lo reinicia solo. Para un aparato desatendido en casa de población
vulnerable, un cuelgue silencioso significa una habitación sin vigilancia y nadie enterándose.
Opciones a decidir (no es una decisión de este repo, es de despliegue):
- **`docker-autoheal`** (o equivalente): un contenedor sidecar que reinicia lo que Docker marca
  `unhealthy`. Más simple, corre en el propio Jetson, sin tocar el host.
- **`systemd` en el Jetson**: unidad con `Restart=` vigilando el propio `docker run`/`compose`, o un
  watchdog a nivel de host que compruebe `/data/heartbeat` directamente.
- **Supervisión desde la nube**: si el hub deja de reportar (cuando exista el uplink del punto 3),
  la nube puede pedir un reinicio remoto o alertar a un humano — más lento pero con visibilidad
  centralizada.
No introducir ningún mecanismo hasta decidir cuál encaja con el despliegue real (uno o varios
Jetsons por hogar, acceso remoto disponible o no, etc.).

## Robustez / calidad (menor, oportunista)

- **Cobertura de tests diferida:**
  - `event_engine`: candidato >0 que revierte al valor reportado >0 (los otros casos delicados ya
    tienen test tras la oleada de fixes).
  - `config`: test de YAML ilegible; el mensaje de error de cámara no nombra el índice.
  - `person_yolo`: guard `if not results` (early-return) sin test directo.
  - `registry`: test de múltiples altas en una misma llamada (numeración `camera-N`).
  - smoke test: cross-chequear el literal de versión.
- **`config`:** `bool("false")` mis-coerce a `True` (solo afecta a un `"false"` entrecomillado; el
  `false` nativo de YAML va bien). Considerar un coercer string-aware.
- **`event_engine`:** `_required_hold(old, new)` no usa `old` (parámetro muerto).
- **`app.py`:** `cap` se libera tras el `stop.wait(delay)` del backoff en vez de antes (higiene de
  recursos, no bug); `run()` no tiene cobertura de tests (código de integración).
- **`person_yolo.from_weights`:** si el fichero de pesos (`VITAHUB_WEIGHTS`) no existe, Ultralytics
  intenta descargarlo al directorio padre de esa ruta — que puede ser de solo lectura (p. ej. el
  `/app/models` del contenedor corriendo en local) — y peta con un traceback confuso. Dar un error
  claro ("pesos no encontrados en X") antes de invocar `YOLO()`.
- **Workflow obsoleto:** `.github/workflows/event-engine-ci.yml` referencia rutas inexistentes
  (`services/event-engine`, `tests/event_engine`) — candidato a limpieza.

## Verificación pendiente en hardware real
Las partes de red (descubrimiento ONVIF, captura RTSP) no corren en CI por diseño. Validar
extremo-a-extremo contra una cámara real: `docker compose up` con la credencial del hogar y
confirmar en `docker logs` la conexión y eventos `person_detected`.
