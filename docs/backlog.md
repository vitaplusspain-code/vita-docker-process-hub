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

### 3. ~~Uplink a AWS~~ — HECHO (slice de 2026-08-21)

Los eventos suben a AWS IoT Core por MQTT/TLS y se guardan en DynamoDB. Ver
`docs/superpowers/specs/2026-08-21-uplink-eventos-aws-design.md`.

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
- **Supervisión desde la nube**: el uplink del punto 3 ya existe, pero le faltan los eventos de
  presencia de IoT (`hub_online`/`hub_offline`, ver «Diferidos del slice de uplink» más abajo); con
  ellos, la nube podría pedir un reinicio remoto o alertar a un humano si el hub deja de reportar —
  más lento pero con visibilidad centralizada.
No introducir ningún mecanismo hasta decidir cuál encaja con el despliegue real (uno o varios
Jetsons por hogar, acceso remoto disponible o no, etc.).

### 5. Reserva DHCP por MAC en el protocolo de instalación
El hub ya recuerda la URI RTSP de cada cámara, pero esa URI lleva la IP dentro. Si el router cambia
la IP de una cámara **y** su ONVIF está apagado (firmware que lo desactiva al reiniciar), la URI
recordada apunta a una IP muerta y solo queda el evento `camera_unreachable`. La defensa real no es
código: es **fijar una reserva DHCP por MAC** en el router de cada hogar durante la instalación.
Cinco minutos por casa y el problema desaparece de raíz.

**Nota de compra, no de software:** la cámara piloto (Tuya) **desactiva ONVIF en cada reinicio**
—medido el 2026-08-17: tras un ciclo de corriente, puerto 10000 cerrado y sonda multicast 0 de 6,
con el RTSP intacto—. Si el modelo definitivo va a ser este, cada apagón de cada hogar dependerá de
que el hub recuerde la URI. Una cámara con ONVIF estable elimina la clase entera de problemas.

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

### Diferidos del slice de rescan (2026-08-17)

Salidos de las revisiones por tarea y de la revisión final; ninguno bloquea la integración.

- **`line-length = 100` es hoy decorativo:** `ruff check` no lo verifica porque `E501` no está en el
  ruleset por defecto y el proyecto no fija `select`. Hay ya alguna línea por encima de 100. Si se
  quiere que la convención sea real, añadir `E501` al `select` de `pyproject.toml` — saldrán avisos
  en código preexistente.
- **`POST /rescan` no tiene tope de duración:** `discover()` puede tardar minutos con la red
  saturada (sonda de 3 s más 8 s por llamada SOAP y por IP que conteste al multicast). Está
  documentado para el cliente (timeout generoso, 409 = "ya en marcha"), pero el tope global encaja
  en el punto 2 de arriba, que ya va a tocar ese fichero.
- **`_touch_heartbeat` traga `OSError` sin log:** con `/data` en solo lectura el hub queda
  `unhealthy` sin ninguna pista en `docker logs`. Un log una-sola-vez lo arregla.
- **SIGTERM durante el rescan inicial no se atiende hasta que termina:** el apagado acotado no se
  alcanza hasta que `run_once()` retorna, y el descubrimiento inicial no tiene tope. Un
  `docker compose restart` durante el arranque acaba en SIGKILL. No corrompe nada (el guardado es
  atómico y ahora con `fsync`).
- **Temporales huérfanos en `/data`:** un SIGKILL entre el `mkstemp` y el `os.replace` de
  `save_cameras` deja un `.hub-*.yaml.tmp`; nadie los limpia al arrancar. Higiene.
- **`stop_all()` que agota su plazo esperando el lock retorna sin marcar `_stopped`:** en teoría un
  `apply()` en espera podría arrancar un worker después. Impacto real nulo (el control ya está
  cerrado, el hilo de rescan ya vio la parada, y el worker sería daemon).
- **`with_credentials` mete la contraseña URL-encoded en la URI** y solo está registrada para
  redacción la forma cruda. Hoy ninguna línea de log incluye una URI RTSP, así que no hay fuga;
  registrar también la forma codificada cuesta una línea y cierra el flanco.
- **Cobertura y documentación menores:** el test de la rama `enabled: false` del supervisor solo
  asierta que no se arranca nada, no que el worker muera; `test_rescan_loop_runs_until_stopped` no
  tiene tope de tiempo (si el bucle regresara, colgaría la suite en vez de fallar);
  `test_non_positive_interval_is_rejected` cubre `0` pero no un negativo; la sección "Qué cubren y
  qué no" de `como-probar.md` no menciona la cobertura nueva pese a haberse actualizado el recuento;
  `como-funciona.md` describe el alcance como el del primer slice.

### Diferidos de la oleada de fixes final (2026-08-18)

Salidos de la revisión final del slice de URI RTSP persistida
(`docs/superpowers/plans/2026-08-18-url-rtsp-persistida.md`). Ninguno bloquea la integración de esa
oleada; se corrigieron los críticos/importantes (ver el commit de la oleada), estos quedan para
después.

- **Un `GetStreamUri` puntual fallido degrada `rtsp_sub` a `rtsp_main` de forma permanente.** Si el
  descubrimiento no consigue resolver el substream en un ciclo concreto (fallo transitorio de la
  cámara/red) y `DiscoveredCamera.rtsp_sub` acaba devolviendo el mismo valor que `rtsp_main`,
  `reconcile()` lo persiste tal cual: la `rtsp_sub` recordada queda sustituida por la del stream
  principal para siempre, no solo para ese ciclo. Antes de este slice (sin registro persistido) la
  degradación duraba un ciclo de descubrimiento y se curaba sola en el siguiente; ahora se escribe en
  el YAML y el Jetson dobla decodificación e inferencia en silencio hasta que alguien lo note.
  Arreglo probable: no persistir un `uri_changed` si el nuevo valor de `rtsp_sub` coincide con
  `rtsp_main` mientras el anterior no coincidía (indicio de degradación, no de cambio real).
- **URIs ONVIF de un solo uso (`InvalidAfterConnect` / `InvalidAfterReboot`) no se contemplan.** Si
  una cámara emite una de estas (el spec ONVIF las define, aunque la cámara piloto no las usa), cada
  `GetStreamUri` devolvería una URI distinta aunque nada real haya cambiado: `reconcile()` vería
  `uri_changed` en cada rescan periódico (cada `discovery.interval_seconds`, hoy 60 s por defecto),
  con su escritura en la eMMC y su parada/arranque de worker asociados, indefinidamente. Antes de
  actuar, comprobar si el flag `InvalidAfterConnect`/`InvalidAfterReboot` viene en la respuesta SOAP
  y, si es así, no tratar esa URI como candidata a persistir (usarla solo en memoria para esa
  conexión).
- **Una cámara conocida sin URI recordada nunca emite `camera_unreachable`.** Si el registro trae una
  cámara de la versión anterior a este slice (sin `rtsp_main`/`rtsp_sub`, aún no redescubierta) y el
  descubrimiento tampoco la encuentra, `RescanService` no le arranca worker — y sin worker, el
  `ConnectionMonitor` nunca ve ni un `on_frame` ni un `on_failed` para esa cámara. Es el único caso
  que sigue abierto del principio "que una cámara perdida no pase desapercibida": la vivienda queda a
  ciegas de esa cámara y no sale ningún evento que lo señale. Se resuelve solo cuando esa cámara
  vuelva a ser descubierta por ONVIF.
- **El warning de "0 cámaras encontradas" ahora es el arranque normal esperado, no una señal de
  fallo.** Con firmware que desactiva ONVIF al reiniciar (la cámara piloto, ver punto 5 más arriba),
  tras cada corte de luz el hub arranca, `discover()` devuelve 0, y sin embargo los workers sí
  arrancan (con la URI recordada). El log actual (`descubrimiento: 0 cámaras encontradas — revisa
  ONVIF/credencial/red`) sugiere una avería que no existe y mandaría a un técnico a perseguir un
  fantasma. Añadir al mensaje cuántos workers arrancaron igualmente (p. ej. "0 cámaras descubiertas,
  N workers arrancados con URI recordada") distingue el caso sano del caso realmente roto (0
  descubiertas y 0 arrancadas).
- **`RescanResult` y la respuesta de `POST /rescan` no exponen `uri_changed`.** El registro
  (`registry.RegistryChange`) ya distingue `"added"` / `"ip_changed"` / `"uri_changed"` — este último
  es justo el cambio que introduce este slice (URI recordada actualizada) — pero `RescanResult` solo
  agrega `added` e `ip_changed`; un operador mirando la respuesta de `POST /rescan` no puede ver que
  una URI cambió. Añadir `uri_changed: list[str]` en paralelo a `ip_changed`.
- **El estado del `ConnectionMonitor` vive solo en memoria.** Si el hub reinicia entre un
  `camera_unreachable` ya emitido y la reconexión, el `camera_reachable` de cierre de ese episodio no
  sale nunca (el proceso nuevo arranca con `_states` vacío, así que la próxima conexión no cuenta como
  "recuperación" de nada). Hoy es inocuo: son eventos por stdout que nadie más consulta. Pasa a
  importar en cuanto exista el uplink a AWS (punto 3 de arriba) y algo consuma esos eventos para
  decidir si mandar a alguien a la vivienda — un episodio que nunca se cierra formalmente podría
  confundir a ese consumidor.
- **Una cámara declarada a mano que luego aparece por ONVIF entra como cámara distinta.** Ya
  documentado en `como-probar.md` §3.5 (se pierde la identidad estable al elegir un `id` a mano), pero
  vale la pena registrar el efecto en código: dos entradas en el registro para la misma cámara física
  (`camara-salon` a mano + `onvif-<serie>` cuando ONVIF vuelve a verla) significan **dos workers**
  sobre el mismo RTSP, eventos de presencia duplicados y doble decodificación/inferencia en el Jetson
  sin que nada lo señale. Mitigación manual hoy: borrar la entrada a mano del YAML en cuanto aparezca
  la descubierta. Arreglo de código pendiente de diseñar (¿deduplicar por URI RTSP normalizada?).

### Diferidos del slice de uplink (2026-08-21)

- **Sin cola: lo que no sale, se pierde.** Decisión consciente del slice. Duele en la
  línea base personal, que es el núcleo del producto: un hueco de tres horas no es un dato
  perdido, es una rutina mal aprendida. Primer candidato del siguiente slice de uplink.
- **Downlink**: el rescan remoto necesita `Subscribe`/`Receive` en la policy de IoT, que
  hoy no se conceden. La costura sigue lista: `RescanService.run_once()`.
- **Eventos de presencia de IoT** (`$aws/events/presence/…`) para `hub_online` /
  `hub_offline`: es lo que cierra el punto 4 de este backlog (supervisión desde la nube de
  un hub colgado). Es configuración de cuenta, no un recurso del stack.
- **El estado del `ConnectionMonitor` sigue solo en memoria.** Este backlog ya avisaba de
  que pasa a importar "en cuanto exista el uplink a AWS" — y ya existe. Un episodio
  `camera_unreachable` que nunca se cierra ahora llega a una tabla que alguien consultará.

## Verificación pendiente en hardware real
Las partes de red (descubrimiento ONVIF, captura RTSP) no corren en CI por diseño. Validar
extremo-a-extremo contra una cámara real: `docker compose up` con la credencial del hogar y
confirmar en `docker logs` la conexión y eventos `person_detected`.

Del slice de rescan quedan además estos cuatro, que solo se pueden comprobar con cámaras:

1. Conectar una cámara con el hub ya corriendo → aparece sola en ≤ `interval_seconds`, sin reiniciar.
2. `POST /rescan` con token válido → la da de alta en el momento y responde con `added` y `started`.
3. Cambiar la IP de una cámara registrada → su worker se relanza con la URI nueva, sin duplicar
   eventos.
4. Apagar una cámara → su worker reintenta con backoff y **no** desaparece del registro.

Y tres números que se eligieron por estimación razonada, sin medir en un Jetson real: el
`--start-period=180s` del `HEALTHCHECK`, el plazo de apagado de 20 s y el `stop_grace_period: 30s`.
Si en campo el arranque con varias cámaras lentas supera los 180 s, o si un apagado que iba bien se
corta, son los tres primeros a revisar.
