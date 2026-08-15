# Backlog — Hub de procesamiento de cámaras

Trabajo diferido tras el primer slice (`docs/superpowers/plans/2026-08-15-hub-procesamiento-camaras.md`).
Todo lo de aquí salió de las revisiones de código; ninguno bloquea el slice actual, pero
condiciona el siguiente. Orden aproximado de prioridad.

## Próximo slice (mayor impacto)

### 1. Re-descubrimiento periódico + fallback por `last_ip`
Hoy el descubrimiento ONVIF corre **solo al arranque** (`app.py:run()`). Consecuencias:
- Una cámara que aparece tarde (aún arrancando cuando lo hace el hub) o que **cambia de IP**
  a mitad de ejecución no se reabsorbe hasta reiniciar el contenedor.
- El `last_ip` persistido en el registro (cuyo propósito es sobrevivir reinicios) **nunca se usa**
  para conectar: los workers se crean solo a partir de la lista descubierta en vivo.

Trabajo: un bucle de re-descubrimiento periódico (spec §5 pide ~60s, ya hay
`discovery.interval_seconds` reservado en la config), que reconcilie el registro en caliente y
arranque/actualice workers; y, para cámaras conocidas pero no descubiertas en ese ciclo, construir
una URL RTSP de fallback desde `last_ip`.

Mientras tanto (documentado en el README): una cámara añadida con el hub ya corriendo requiere
`docker compose restart`.

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
- **`registry`:** un `id` duplicado dentro de un mismo `discovered` genera un evento `ip_changed`
  espurio (el dedup pertenece a la capa de descubrimiento).
- **`event_engine`:** `_required_hold(old, new)` no usa `old` (parámetro muerto).
- **`app.py`:** `cap` se libera tras el `stop.wait(delay)` del backoff en vez de antes (higiene de
  recursos, no bug); `run()` no tiene cobertura de tests (código de integración).
- **Workflow obsoleto:** `.github/workflows/event-engine-ci.yml` referencia rutas inexistentes
  (`services/event-engine`, `tests/event_engine`) — candidato a limpieza.

## Verificación pendiente en hardware real
Las partes de red (descubrimiento ONVIF, captura RTSP) no corren en CI por diseño. Validar
extremo-a-extremo contra una cámara real: `docker compose up` con la credencial del hogar y
confirmar en `docker logs` la conexión y eventos `person_detected`.
