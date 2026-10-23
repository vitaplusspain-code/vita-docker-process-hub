# Diseño — Clip de vídeo como prueba de una caída con riesgo (v1)

- Fecha: 2026-10-22
- Estado: aprobado en brainstorming (lado hub); ampliado a todos los repos, pendiente de revisión
- Repos: `vita-docker-process-hub` (hub), `vitaplus-aws-architecture` (infra y migraciones),
  `vita-docker-rule-engine` (motor de reglas), `vita-web-family-app` (API y frontend)
- Slices relacionados: `2026-08-22-deteccion-caidas-design.md` (episodios y score),
  `2026-08-21-uplink-eventos-aws-design.md` (sink y policy IoT),
  `2026-10-15-video-en-directo-design.md` (credentials provider, lectura RTSP con PyAV,
  consentimiento y permiso de cámaras), `vita-docker-rule-engine/docs/superpowers/specs/2026-08-23-rule-engine-design.md`
  (regla `fall_confirmed`)

Este documento es el **spec único** de los cuatro repos, como el del directo: el contrato
(recursos AWS, eventos, `alert.detail.clip`, consentimiento y permiso) se define aquí una sola vez
(§3) y de él sale **un plan de implementación por repo**. El del hub ya existe:
`docs/superpowers/plans/2026-10-22-clip-caida-hub.md`.

## 0. El problema

Cuando el hub detecta una caída, a AWS solo llega texto: `fall_detected` con un score y unas
señales geométricas. El motor de reglas abre una alerta `fall_confirmed` y la familia la ve en la
app, pero nadie puede ver qué pasó, y el score por sí solo no basta para distinguir una caída real
de alguien que se tumba en el suelo.

El producto quiere que, **cuando una caída tenga riesgo**, el hub envíe a AWS un clip corto de ese
momento como prueba, que el clip se guarde **30 días** y que quien ya puede ver las cámaras de la
persona pueda verlo desde la alerta.

Esto cambia un principio. Hasta ahora el vídeo nunca se almacenaba: ni en el hub ni en la nube
(P1 de `context.md`; spec del directo, §8). A partir de este slice se almacena un tipo concreto
de vídeo, el clip de una caída con riesgo, y ninguno más. P2 ya preveía "como mucho, clips cortos
de alertas críticas con consentimiento": este slice es ese caso.

## 1. Decisiones

| Decisión | Elegido | Origen |
|---|---|---|
| Cuándo se genera un clip | Solo en una alerta de caída (`fall_detected`) **y** si hay riesgo (§4) | Usuario |
| Retención | 30 días en AWS; después se borra solo | Usuario. Contados desde la subida (§5.4) |
| De qué cámara | Solo de la cámara que detectó la caída, con el mismo stream que ya analiza el hub (`inference.stream`, el substream por defecto) | Usuario ("solo en su stream") |
| Criterio de riesgo | Bajada brusca (o no observada) y la persona no se ha levantado al final de la ventana (§4) | Delegado por el usuario; propuesto y aceptado |
| Ventana | 15 s antes y 10 s después de `fall_detected` | Propuesto y aceptado |
| Clips sin riesgo | Se borran en el hogar y nunca salen de casa; solo sale un evento de texto | Propuesto y aceptado |
| Transporte a AWS | `s3:PutObject` con credenciales temporales del credentials provider de IoT, con un role alias nuevo acotado por ThingName | Propuesto y aceptado |
| Consentimiento | **Propio de los clips** (`person.fall_clip_consent_at`), distinto del del directo | Decisión de diseño: guardar vídeo 30 días es otro tratamiento que mirar en directo |
| Quién ve el clip | Quien ya puede ver las cámaras de la persona: rol `admin` de la persona o `relationship.live_video` | Decisión de diseño: mismo permiso de cámaras, sin un interruptor más |
| Dónde se ve | En la alerta de caída de la app familiar (Hoy e Historial), en una página propia | Decisión de diseño |
| Cifrado del bucket | `S3_MANAGED`, como el bucket `watch-clips` existente | Decisión de diseño: el patrón del repo; una CMK no añade control de acceso real aquí |
| Notificación del clip | Ninguna en v1 | No hay suscriptores del SNS de alertas todavía |

## 2. Alcance

Dentro:

- **Hub:** subsistema `src/vitahub/clips/`, config `clips`, tres eventos nuevos y su cableado (§5).
- **Infra:** stack `hub-clips` (bucket y role alias), IAM, policy IoT, aprovisionamiento,
  migración de consentimiento y permiso de lectura para la Lambda de la app (§6).
- **Motor de reglas:** aceptar los eventos `fall_clip_*` y enlazar el clip a la alerta de caída
  en `alert.detail.clip` (§7).
- **API:** el estado del clip en `AlertDto`, un endpoint que entrega una URL presignada
  auditada y el consentimiento en el panel técnico (§8).
- **Frontend:** botón "Ver vídeo" en la alerta, página del clip y el interruptor de
  consentimiento en el panel técnico (§9).
- Reescritura de los textos de privacidad afectados (§11).

Fuera, con motivo:

- **Clips de otras cámaras de la casa**, aunque vean la escena. Decisión del usuario para v1.
- **Audio.** Igual que en el directo: no se graba.
- **Clips de `fall_update`** o de una persona que sigue en el suelo minutos después. El clip es
  único por episodio; lo que pasa después sigue llegando como texto.
- **Downlink MQTT.** El hub decide solo; la nube no le puede pedir un clip, ni puede apagarle los
  clips. Activar y desactivar es `hub.yaml`, que toca el técnico (§3.4).
- **Notificar el clip** (push, SMS, email). No hay consumidores del SNS de alertas; va a backlog.
- **CloudTrail de lecturas del bucket.** No existe trail en la cuenta; el rastro de quién ve un
  clip es `audit_log` de la API (§8). Va a backlog.
- **Borrado inmediato al retirar el consentimiento.** En v1 la API deja de servir los clips al
  instante y S3 los borra en ≤ 30 días. Va a backlog.

## 3. El contrato compartido

### 3.1 Recursos AWS

| Recurso | Nombre | Quién lo usa |
|---|---|---|
| Bucket | `${p}-fall-clips-${AccountId}` (p. ej. `vita-dev-fall-clips-123456789012`; el sufijo de cuenta, como `watch-clips`, porque los nombres de bucket son globales) | Hub escribe, API lee |
| Clave del objeto | `{hub_id}/{camera_id}/{episode_id}.mp4`, con el `episode_id` del primer episodio del clip | Hub, motor, API |
| Role alias IoT | `${p}-hub-clips` | Hub |
| Rol del hub | `${p}-hub-clips`: solo `s3:PutObject` en `<bucket>/${credentials-iot:ThingName}/*` y solo si la clave no existe (`s3:if-none-match: *`) | Hub |
| Permiso de la Lambda de la app | `s3:GetObject` en `<bucket>/*` | API |

El `hub_id` va al principio de la clave porque es lo que acota la policy del hub: un certificado
robado solo puede **añadir** objetos nuevos en la carpeta de su propia casa. No puede leer, listar
ni borrar, y tampoco sobrescribir un clip ya subido: la condición `s3:if-none-match` exige que la
clave no exista (sin ella, en un bucket versionado, sobrescribir equivale a borrar el original al
día siguiente). El hub sube con `If-None-Match: *` y trata un **412** como "ya estaba subido"
(reintento tras un corte justo después de subir).

### 3.2 Eventos del hub

Viajan por el topic de siempre (`vita/hub/{hub_id}/events`) con el sobre de siempre. La regla
IoT ya copia `payload` tal cual a `${p}-hub-events`, así que no cambia.

| Tipo | Severidad | Payload | Cuándo |
|---|---|---|---|
| `fall_clip_ready` | `info` | `episode_ids` (lista), `s3_key`, `sha256`, `duration_s`, `clip_start` (ISO-8601 UTC) | Tras confirmar la subida |
| `fall_clip_discarded` | `info` | `episode_ids`, `drop_speed`, `reason` (`slow` \| `got_up`) | Al cerrar la ventana sin riesgo |
| `fall_clip_failed` | `info` | `episode_ids`, `reason` (`no_video` \| `mux_error` \| `queue_full` \| `expired` \| `upload_rejected` \| `shutdown`) | Cuando un clip con riesgo no va a llegar a S3 |

```json
{"schema_version":1,"hub_id":"hub-lopez","camera_id":"cam-salon","camera_name":"Salón",
 "type":"fall_clip_ready","severity":"info","timestamp":"2026-10-22T09:14:31.200+00:00",
 "payload":{"episode_ids":["cam-salon-1792660450-3"],
            "s3_key":"hub-lopez/cam-salon/cam-salon-1792660450-3.mp4",
            "sha256":"9f2c…","duration_s":24.6,"clip_start":"2026-10-22T09:13:56.800+00:00"}}
```

`episode_ids` es una lista (un clip puede cubrir dos caídas seguidas, §5.2) y **no** se llama
`episode_id`: así ningún código que hoy lea `payload.episode_id` confunde un `fall_clip_*` con un
evento de episodio.

`s3_key` es informativo (logs, `docker logs`). **La nube no se fía de él:** el motor de reglas
recompone la clave con el `hub_id` del topic (`topic(3)`, que la policy IoT ata al certificado),
el `camera_id` del sobre y `episode_ids[0]`, y descarta el evento si no coincide con el
`s3_key` recibido (§7). Si no, un hub comprometido podría enlazar en sus alertas el clip de otra
casa y la API se lo firmaría.

### 3.3 `alert.detail.clip`

El motor de reglas (§7) escribe en la alerta `fall_confirmed` del episodio, en `alert.detail`
(jsonb, merge), la clave `clip`:

```json
{"clip": {"status": "available",
          "s3_key": "hub-lopez/cam-salon/cam-salon-1792660450-3.mp4",
          "sha256": "9f2c…", "duration_s": 24.6,
          "clip_start": "2026-10-22T09:13:56.800+00:00",
          "camera_id": "cam-salon", "camera_name": "Salón",
          "expires_at": "2026-11-21T09:14:31.200+00:00"}}
```

o, si el hub avisó de que no llegará:

```json
{"clip": {"status": "failed", "reason": "no_video", "camera_id": "cam-salon", "camera_name": "Salón"}}
```

- `expires_at` = `timestamp` del `fall_clip_ready` + 30 días. Es la fuente de verdad de la
  caducidad para la API: el lifecycle de S3 borra con un retraso de hasta un día, y a partir de
  `expires_at` la API ya no sirve el clip aunque el objeto siga existiendo.
- Un `available` nunca se sobrescribe con un `failed` (p. ej. un `fall_clip_failed` tardío de un
  segundo episodio del mismo clip).
- `fall_clip_failed` con `reason: "shutdown"` **no** se enlaza: el hub se apagó sin saber si había
  riesgo, y mostrar "no se pudo guardar el vídeo" en una caída de la que la persona se levantó
  confundiría. El resto de motivos sí se enlazan como `failed`.
- Un único clip por alerta en v1. Si la alerta ya tiene un clip `available`, un segundo se
  descarta con log (ver §7, alerta de respaldo).

### 3.4 Consentimiento y permiso

- **Consentimiento:** `person.fall_clip_consent_at timestamptz` y `person.fall_clip_consent_by
  uuid REFERENCES account(id)` (migración, §6.4). En v1 es papel firmado que el técnico registra,
  como el del directo. Sin él la API no sirve ningún clip de esa persona.
- **Activación en el hogar:** `clips.enabled` y `clips.cameras` en `hub.yaml` (§5.6), que pone el
  técnico **después** de registrar el consentimiento. No hay enlace automático: el hub no recibe
  órdenes de la nube. Retirar el consentimiento es, para el técnico, dos pasos: el interruptor en
  el panel (efecto inmediato en la API) y `clips.enabled: false` en el hub.
- **Permiso para ver:** el de cámaras que ya existe. Ve el clip el rol `admin` de la persona
  (`person.admin_account_id`) o una relación con `relationship.live_video = true`, con o sin
  `read_only` (ver es una lectura).

## 4. Qué es una caída con riesgo

Un episodio tiene riesgo si se cumplen **las dos** condiciones:

1. **No fue un tumbarse despacio.** En el evento `fall_detected` del episodio,
   `signals.drop_speed` (el pico de bajada del episodio) es **≥ `RISK_MIN_DROP` = 0.5** alturas
   de caja por segundo, **o es `null`**. `null` significa que el hub no vio la bajada (la pista
   apareció ya en el suelo o no hay historial): no se puede descartar la caída, así que cuenta
   como riesgo. El clip es la prueba, y ante la duda se conserva.
2. **No se ha levantado.** Al terminar la ventana de 10 s posterior, el episodio sigue abierto:
   no ha llegado `fall_resolved` con `reason: "upright"` para ese `episode_id`. Un cierre con
   `reason: "track_lost"` **sí cuenta como riesgo**: la persona puede estar en el suelo detrás de
   un mueble.

Por qué la velocidad de bajada y no el `score`: el score no separa bien los dos casos. Cuando
falta `drop_speed`, su término desaparece y los pesos se renormalizan, así que una persona ya
tumbada da unos 0.79. Una persona que se tumba despacio con la cabeza baja da 0.55. Y el término
`floor_time_s` crece solo con el tiempo en el suelo, así que el máximo de la ventana superaría
cualquier umbral razonable. `drop_speed` es la única señal que mide la brusquedad.

`inference.fall.min_score` (0.3) no cambia: sigue gobernando qué eventos de texto se emiten.
`RISK_MIN_DROP` es una **constante** en `clips/`, como los pesos de `fall_signals.py`. El valor
inicial de 0.5 está a medio camino del mapeo del score (`_DROP_0` = 0.3, `_DROP_1` = 1.0); se
calibra con `scripts/replay_video.py` y pasa a config solo si la calibración por hogar lo exige.

Consecuencia temporal: la decisión se toma unos 12 s después de que la persona llega al suelo
(2 s de confirmación más 10 s de ventana). Para que el episodio se cierre como `upright` dentro de
la ventana, la persona tiene que estar de pie como muy tarde a los ~8 s, porque el engine exige
2 s de pie para cerrar.

## 5. Hub (`vita-docker-process-hub`)

### 5.1 Flujo

```
cámara ──RTSP──► ClipRecorder (hilo por cámara, PyAV, sin decodificar)
                   └─ buffer en memoria con los últimos ~45 s de paquetes de vídeo

FallEngine ──fall_detected──► sink normal ──► ClipTriggerSink
                                                 ├─ congela los 15 s anteriores
                                                 ├─ sigue recogiendo 10 s
                                                 ├─ escucha fall_resolved del episodio
                                                 └─ al cerrar la ventana evalúa el riesgo (§4)
                                                       ├─ sin riesgo: descarta en memoria
                                                       │            → fall_clip_discarded
                                                       └─ con riesgo: monta MP4 → cola en disco
                                                                     → ClipUploader → S3
                                                                     → fall_clip_ready
```

### 5.2 Piezas

| Pieza | Responsabilidad |
|---|---|
| `clips/recorder.py` — `ClipRecorder` | Un hilo por cámara habilitada. Lo arranca la función `worker(camera, rtsp_url, cam_stop)` de `app.py` junto a `_camera_loop`, con la misma `rtsp_url` y el mismo `cam_stop`: así el `CameraSupervisor` ya gestiona altas, bajas y cambios de URI sin cableado nuevo. Abre esa URI con PyAV (`rtsp_transport=tcp`, solo vídeo, como `live/source.py`) y **no decodifica**: guarda los paquetes con su instante de llegada (`time.monotonic()`) en un buffer acotado a `BUFFER_S = 45` s (el clip más largo posible: 15 s de pre-roll más 30 s de post-roll). Al recortar, nunca deja el buffer empezando en un paquete que no sea keyframe. Reconecta con `backoff_delay`/`next_attempt` de `ingest/rtsp.py`. `snapshot(start, end)` devuelve los paquetes de ese intervalo, empezando en el keyframe anterior a `start`. |
| `clips/trigger.py` — `ClipTriggerSink(EventSink)` | Envuelve el sink del hub (decorador: reenvía cada evento al sink interno y después actúa). Ignora todo salvo `fall_detected` y `fall_resolved`. Con `fall_detected` de una cámara habilitada abre una ventana para ese `episode_id` (instante de disparo = `time.monotonic()` al recibir el evento). Con `fall_resolved` anota el `reason` del episodio. Un temporizador cierra cada ventana a los `POST_S = 10` s y llama al evaluador. **No hace trabajo pesado en `emit`**: el `emit` corre en el hilo de la cámara. |
| `clips/risk.py` | Función pura `assess(drop_speed: float \| None, resolved_reason) -> Literal["risk", "slow", "got_up"]`. Es lo único que codifica §4. |
| `clips/assembler.py` | Remux de los paquetes a un MP4 (`av.open(..., "w")`, `add_stream_from_template`, `movflags=+faststart`), sin recodificar. Calcula el sha256. |
| `clips/uploader.py` — `ClipUploader` | Hilo con cola persistente en `/data/clips-pending/` (MP4 + JSON de metadatos por clip). Sube con `botocore` (`PutObject`, `ContentType: video/mp4`) usando un `IotCredentialsProvider` propio con el role alias de clips. Reintenta con backoff si no hay internet. Borra el fichero local solo tras la subida confirmada. Al arrancar el hub, reanuda lo que quedara en la carpeta. |

Una sola ventana por cámara a la vez: si llega otro `fall_detected` de la misma cámara mientras su
ventana está abierta, no se abre otra. El episodio nuevo se asocia al clip en curso y la ventana
se **alarga hasta 10 s después de ese último `fall_detected`**, con un tope de 30 s de post-roll
en total (así cabe en el buffer de 45 s). Si no, un episodio que entra a los 8 s nunca podría cerrarse
como `got_up`. El clip tiene riesgo si **alguno** de sus episodios lo tiene. El evento resultante
lleva la lista de `episode_ids`.

**Apagado:** una ventana abierta cuando el hub se para se descarta sin escribir nada a disco y se
emite `fall_clip_failed` con `reason: "shutdown"`. No se puede saber si había riesgo, y un clip
sin riesgo confirmado no sale de casa. Los clips ya montados en la cola sí se conservan y se suben
en el siguiente arranque. El `ClipTriggerSink` se cierra antes que el resto del sink, para que el
evento llegue a salir.

El recorder abre **una conexión RTSP más** por cámara habilitada, igual que ya hace el directo.
No se toca `_camera_loop`.

### 5.3 Clave y metadatos en S3

- Bucket: el de §6.1. Clave: `{hub_id}/{camera_id}/{episode_id}.mp4`, con el `episode_id` del
  primer episodio del clip.
- Metadatos del objeto: `episode-ids`, `camera-id`, `clip-start` (ISO-8601 UTC) y `sha256`.
- El `hub_id` va al principio de la clave porque es lo que acota la IAM policy (§6.2).

### 5.4 Límites y fallos

- **Disco:** la cola persistente se limita a `PENDING_MAX_MB = 200`. Si no cabe un clip nuevo,
  se borra el **más antiguo** de la cola y se emite `fall_clip_failed` con `reason:
  "queue_full"` para el clip borrado.
- **Antigüedad:** un clip pendiente con más de **3 días** (`PENDING_MAX_AGE`) se borra sin subir
  (`reason: "expired"`). Los 30 días de retención cuentan desde la subida, porque así funciona el
  lifecycle de S3; con este tope, un clip vive como mucho 33 días desde la caída. Un hogar más
  de 3 días sin internet tiene un problema que el técnico tiene que ver antes que el clip.
- **Cámara sin vídeo:** si el buffer no tiene paquetes que cubran el instante de disparo, no hay
  clip (`reason: "no_video"`).
- **Fallo de remux o de subida no recuperable** (credenciales rechazadas, 403): `reason:
  "mux_error"` o `reason: "upload_rejected"`. Un 403 no se reintenta en bucle: queda en la cola y
  se reintenta con el backoff máximo, porque suele ser infra a medio desplegar.
- Ningún fallo de `clips/` puede afectar a la inferencia ni a los eventos de texto: el
  `FanoutSink` ya aísla las excepciones del sink, y el recorder y el uploader corren en sus
  propios hilos con `except Exception` y log, como el resto del hub.

### 5.5 Memoria y CPU

Con el substream a ~0,5–1 Mbps, 45 s de buffer son entre 3 y 6 MB por cámara. Con
`inference.stream: main` (4–8 Mbps) son 22–45 MB por cámara y cada clip pesa 12–25 MB, así que la
cola de 200 MB admite menos clips. Se permite igualmente: "su stream" es el que analiza el hub,
sea cual sea. El hub loguea un aviso al arrancar si `clips.enabled` y `stream: main` coinciden. El recorder solo
demultiplexa, no decodifica: su CPU es despreciable frente a los ~12 % de núcleo por viewer que
midió el spike del directo. El montaje del MP4 es un remux de unos 25 s de vídeo y ocurre una vez
por clip.

### 5.6 Config

```yaml
clips:
  enabled: false
  cameras: []          # ids de cámara con clips activados; opt-in explícito por cámara
  region: eu-west-1
```

Por entorno: `VITAHUB_CLIPS_BUCKET` y `VITAHUB_CLIPS_ROLE_ALIAS`, que imprime
`provision-hub.sh`, y `VITAHUB_IOT_CREDENTIALS_ENDPOINT`, que ya existe para el directo y pasa a
ser obligatoria también con `clips.enabled: true`. Reutiliza el certificado del uplink.

Fail-fast, con el mismo criterio que `live`: con `clips.enabled: true`, el hub **no arranca**
(`ConfigError`, código 1) si `uplink.enabled` o `inference.fall.enabled` son `false`, si
`cameras` está vacía o si falta alguna variable de entorno. Un id de `cameras` que no existe en la
sección `cameras` **no** es error, porque la sección se autopuebla al descubrir; se loguea un aviso
en cada rescan mientras no aparezca.

Las constantes `PRE_S`, `POST_S`, `BUFFER_S`, `RISK_MIN_DROP`, `PENDING_MAX_MB` y
`PENDING_MAX_AGE` no son config.

`docker-compose.yml` y `docker-compose.mac.yml` pasan las dos variables nuevas al contenedor,
vacías por defecto, igual que las del directo (ver el commit 51a52c6, que corrigió ese olvido).

### 5.7 Eventos

Los tres de §3.2, por el sink normal (stdout y AWS), con el `camera_id` de la cámara del clip.
Van con severidad `info`, que ya existe: el hub hoy solo emite `high` e `info`. Ninguno notifica a
la familia; el motor de reglas los usa solo para enlazar el clip a la alerta (§7).
`fall_clip_discarded` sirve además para calibrar `RISK_MIN_DROP` con datos reales sin enviar vídeo.

### 5.8 Tests

- `risk.py`: TDD puro con la tabla de §4 (`drop_speed` por debajo y por encima del umbral, y
  `null`; `upright`, `track_lost` y episodio abierto).
- `trigger.py`: reloj inyectado. Ventana que cierra a los 10 s; `fall_resolved` antes y después
  del cierre; segundo episodio que alarga la ventana y tope de 30 s; apagado con ventana abierta;
  cámara no habilitada; eventos ajenos ignorados; `emit` que no bloquea.
- Buffer del recorder con paquetes sintéticos: recorte a 45 s, arranque siempre en keyframe,
  `snapshot` sobre un intervalo sin vídeo.
- `assembler.py`: remux de un stream H.264 sintético generado con PyAV en el test; el MP4
  resultante se abre y tiene la duración esperada.
- `uploader.py` con un cliente S3 falso: subida y borrado, reintento sin red, reanudación al
  arrancar, tope de 200 MB, caducidad a 3 días, 403.
- `config.py`: cada caso de fail-fast de §5.6.
- La subida real a S3 y el RTSP real quedan como "runtime only", igual que `https_fetcher` y
  `open_capture`, y se cubren con la prueba manual de `como-probar.md`.

## 6. Infra (`vitaplus-aws-architecture`)

### 6.1 Stack CDK `${p}-hub-clips` (`lib/hub-clips-stack.ts`)

Se instancia siempre en `bin/vita.ts`, como `hub-live`. Stack aparte para no inflar
`hub-ingest`.

- **Bucket** `${p}-fall-clips-${AccountId}`, con el patrón de `watch-clips`
  (`lib/watch-persistent-stack.ts`): `S3_MANAGED`, `BLOCK_ALL`, `enforceSSL: true`, versionado,
  `RETAIN`, y lifecycle con **expiración a 30 días**, versiones no vigentes a 1 día y multipart
  incompleto abortado a 7 días.
- **Role alias** `iot.CfnRoleAlias` `${p}-hub-clips`, con el rol leído de SSM
  `/vita/<env>/hub-clips/hub-role-arn` y `credentialDurationSeconds: 3600`, como en
  `hub-live-stack.ts`.
- **Salidas en SSM:** `/vita/<env>/hub-clips/bucket-name` y `/vita/<env>/hub-clips/role-alias-name`.
- Helpers en `lib/config.ts`: `fallClipsRoleAliasName(cfg)` = `${p}-hub-clips`.
- Variable de entorno `FALL_CLIPS_BUCKET` en la Lambda de la app (`app-web-stack.ts`), leída de SSM.

### 6.2 IAM (`admin/bloque-hub-clips.sh`)

Siguiendo `bloque-hub-live.sh` y los helpers de `_comun.sh`:

- Rol `${p}-hub-clips`, de confianza `credentials.iot.amazonaws.com`, con una sola sentencia:
  `s3:PutObject` sobre `arn:aws:s3:::<bucket>/${credentials-iot:ThingName}/*`. Publica el ARN en
  `/vita/<env>/hub-clips/hub-role-arn`.
- Inline `${p}-app-web-fall-clips` (p. ej. `vita-dev-app-web-fall-clips`, el patrón del repo) en el rol de la Lambda de la app: `s3:GetObject` sobre
  `arn:aws:s3:::<bucket>/*`.
- El rol del hub lleva la condición `"Condition": {"StringEquals": {"s3:if-none-match": "*"}}`
  en su `s3:PutObject` (§3.1).
- `admin/bloque-4-passrole.sh`: `PassRoleHubClips` con `iam:PassedToService`
  `iot.amazonaws.com` y `credentials.iot.amazonaws.com`, **y** `LeerRolHubClips` (`iam:GetRole`
  sobre el rol `hub-clips`), igual que `LeerRolHubLive`: crear el role alias lo exige y
  `PowerUserAccess` no lo tiene. Sin él, el deploy falla en `CfnRoleAlias`. Relanzar con
  `FORZAR=1`.

### 6.3 IoT y aprovisionamiento

- Policy `${p}-hub-policy` (`lib/hub-ingest-stack.ts`): `iot:AssumeRoleWithCertificate` pasa a
  cubrir también `rolealias/${p}-hub-clips`. Sigue sin `Subscribe` ni `Receive`.
- `scripts/provision-hub-clips.sh` imprime `VITAHUB_CLIPS_BUCKET` y `VITAHUB_CLIPS_ROLE_ALIAS`
  (y `VITAHUB_IOT_CREDENTIALS_ENDPOINT` si no lo imprimió ya el directo). `provision-hub.sh` lo
  llama igual que a `provision-hub-live.sh`.
- La regla IoT de `hub-events` **no cambia**: no usa `SELECT *`, pero los campos nuevos van dentro
  de `payload`, que ya se copia entero. El `sk` incluye `type`, así que un `fall_clip_*` no pisa
  un `fall_*` del mismo instante.

### 6.4 Migración `NNNN_fall_clip_consent.sql`

Con el siguiente número libre al implementar (hoy sería la `0011`; hay una migración del callbot
pendiente que también reclama número, así que se comprueba antes):

```sql
ALTER TABLE person ADD COLUMN fall_clip_consent_at timestamptz;
ALTER TABLE person ADD COLUMN fall_clip_consent_by uuid REFERENCES account(id);
```

NULL = no consta consentimiento. Test en `test/db-migrations.test.ts`.

## 7. Motor de reglas (`vita-docker-rule-engine`)

Hoy `src/core/events.ts` tiene una lista cerrada de tipos (`KINDS`) y los `fall_clip_*` se
descartarían como `unknown_kind`. Y la regla `fall_confirmed` (`src/core/rules/fall-confirmed.ts:35`)
trata todo lo que empieza por `fall_` y no es `fall_detected`/`fall_update` como un
`fall_resolved`: sin cambios, un `fall_clip_*` con el `episode_id` de una alerta la cerraría.

Cambios:

1. **`events.ts`:** añadir `fall_clip_ready` y `fall_clip_failed` a `KINDS`, normalizando
   `episode_ids` como lista de cadenas. `normalize` pasa a conservar `hubId` (de `item.hub_id`) y
   `cameraId`/`cameraName` del sobre en el `DomainEvent`, que hoy los descarta.
   `fall_clip_discarded` **no** entra en `KINDS`: no hay nada que hacer con él en la nube y así
   sale como `unknown_kind`, con la métrica `EventsSkipped` que ya existe.
2. **`fall-confirmed.ts`:** excluir explícitamente el prefijo `fall_clip_` antes de cualquier otra
   rama. Test de regresión: un `fall_clip_ready` con un payload que además trajera `episode_id`
   no cierra ni anota la alerta.
3. **Regla nueva `src/core/rules/fall-clip.ts`** (pura, sin AWS, cubierta por
   `core-purity.test.ts`):
   - `fall_clip_ready` → recompone la clave `{hubId}/{cameraId}/{episodeIds[0]}.mp4`; si no
     coincide con `payload.s3_key`, `skip('clip_key_mismatch')` con log de aviso. Si coincide,
     decisión nueva `attach_clip` con `episodeIds` y el objeto `clip` de §3.3 (con la clave
     recompuesta, `status: "available"`, `expires_at` = `timestamp` del evento + 30 días).
   - `fall_clip_failed` → `attach_clip` con `status: "failed"` y `reason`, salvo
     `reason: "shutdown"` → `skip('clip_shutdown')` (§3.3).
4. **Executor, decisión `attach_clip`:** por cada `episodeId`, busca la alerta con
   `findAlertBySource(person, 'fall_confirmed', episodeId)` **en cualquier estado** (abierta,
   resuelta, `record info`) y hace merge de `{clip}` en `detail`, respetando que un `available`
   no se sustituye por un `failed` ni por otro `available`. **No publica en SNS.**
   **Alerta de respaldo:** si ningún `episodeId` tiene alerta propia ni coincide con el episodio
   en curso (punto 5), el clip se engancha a la alerta `fall_confirmed` **abierta** de la persona,
   si existe. Cubre dos casos que hoy se pierden: un segundo `fall_detected` descartado como
   `episode_open` (`fall-confirmed.ts:43`) y un `open` que reutiliza la alerta abierta de un
   episodio anterior sin cambiar su `source_ref` (`executor.ts:72-90`).
5. **Carrera clip antes que alerta:** la alerta se abre con el `fall_update` (10 s después de
   `fall_detected`) o con el tick (30 s). El `fall_clip_ready` llega unos 12 s después del
   `fall_detected` más lo que tarde la subida, así que normalmente la alerta ya existe; si no, el
   clip se guarda en `PersonState.fall.pendingClip` cuando el `episode_id` coincide con el episodio
   en curso, y se incluye en `detail` cuando la regla abre la alerta o hace `record`. Si no hay ni
   alerta propia, ni episodio en curso, ni alerta abierta de respaldo, `skip('clip_no_alert')` con
   log: el objeto sigue en S3 y caduca a los 30 días.
6. Tests (Vitest, convenciones del repo): `test/core/events.test.ts` con los tres tipos;
   `test/core/rules/fall-clip.test.ts` (incluida la clave que no coincide y `shutdown`);
   regresión en `fall-confirmed.test.ts`; executor con `aws-sdk-client-mock` para `attach_clip`
   (alerta abierta, resuelta, inexistente, respaldo a la abierta, `available` no pisado);
   fixtures `fallClipReady/Failed/Discarded` en `test/fixtures/items.ts`.
7. README: la sección de reglas y eventos aceptados.

## 8. API (`vita-web-family-app/api`)

### 8.1 Estado del clip en las alertas

- `ALERT_SELECT` (`lib/alerts.ts`) lee también `detail`.
- `AlertDto` (`shared/src/index.ts`) gana `clip: AlertClip | null`, con
  `AlertClip = { status: 'available' | 'expired' | 'failed'; durationS?: number; clipStart?: string; cameraName?: string }`.
  - `available` si `detail.clip.status = 'available'` y `now < expires_at`; `expired` si ya pasó;
    `failed` si el hub avisó de que no llegará.
  - `null` si la alerta no tiene `detail.clip`, si la cuenta que pide **no tiene permiso de
    cámaras** (§3.4) o si la persona **no tiene consentimiento de clips**. Quien no puede ver el
    vídeo no sabe que existe.

### 8.2 `GET /api/alerts/:id/clip`

Sigue el patrón de `routes/live.ts`, con las comprobaciones en este orden:

1. `id` es UUID; si no, 400.
2. `getAlertScope` → relación con la persona dueña; si no, **404** (nunca 403, para no revelar
   que la alerta existe).
3. Permiso de cámaras (rol `admin` o `relationship.liveVideo`); si no, 403 `camera_permission`.
4. `person.fall_clip_consent_at` no nulo; si no, 403 `fall_clip_no_consent`.
5. `alert.rule_id = 'fall_confirmed'` y `detail.clip.status = 'available'`; si no, 404 `no_clip`.
6. `now < detail.clip.expires_at`; si no, **410** `clip_expired`.
7. `FALL_CLIPS_BUCKET` configurado; si no, 503 `clip_unavailable`.
8. URL presignada de `GetObject` con `@aws-sdk/s3-request-presigner`, **300 s**,
   `ResponseContentType: video/mp4` y `ResponseContentDisposition: inline`.
9. `insertAudit(accountId, personId, 'fall_clip_viewed', {alert_id, s3_key})` **antes** de
   responder; nunca se audita la URL.

Respuesta: `{ url, expiresAt, durationS, clipStart, cameraName }`.

Dependencias nuevas: `@aws-sdk/client-s3` y `@aws-sdk/s3-request-presigner`. `config.ts` lee
`FALL_CLIPS_BUCKET`.

### 8.3 Panel técnico

- `PATCH /api/admin/persons/:id` acepta `{ fallClipConsent: boolean }`: fija o borra
  `fall_clip_consent_at` y `fall_clip_consent_by`, como `liveVideoConsent`.
- `AuditAction` gana `fall_clip_viewed` y `admin.person.fall_clip_consent`.

### 8.4 Tests

Mismo estilo que los de `live`: cada rama de §8.2 (incluidos 404 sin relación, 403 sin permiso
con relación `read_only` y sin `live_video`, 410 en el segundo exacto de `expires_at`), que la
auditoría se escribe antes de responder, y la derivación de `AlertDto.clip` de §8.1.

## 9. Frontend (`vita-web-family-app/frontend`)

- **Alerta (Hoy, `components/AlertItem.tsx`, e Historial, `pages/HistoryTab.tsx`):** según
  `alert.clip`:
  - `available` → botón **"Ver vídeo"** que lleva a la página del clip.
  - `expired` → texto "El vídeo de esta caída ya no está disponible: se borra a los 30 días."
  - `failed` → texto "No se pudo guardar el vídeo de esta caída."
  - `null` → nada.
  - En una caída agrupada (`FallGroup`), el botón va en la fila del grupo y abre el clip
    `available` **más reciente** del grupo.
- **Página `FallClipPage`**, ruta `/persona/:id/alerta/:alertId/video`, en carga diferida como
  `LivePage`. Al montar pide `GET /api/alerts/:alertId/clip` y muestra un
  `<video controls playsInline controlsList="nodownload" disablePictureInPicture>` con la URL,
  y encima cámara, hora de la caída y duración. La URL no se guarda en ningún estado persistente;
  si caduca (5 min) mientras la página sigue abierta, el reproductor vuelve a pedirla al fallar la
  carga. Errores con texto propio: 403 `camera_permission`/`fall_clip_no_consent` → "No tienes
  permiso para ver este vídeo"; 410 → el texto de `expired`; 404/503 → "El vídeo no está
  disponible".
- **Panel técnico:** `FallClipConsentSection` en `DataTab`, igual que
  `LiveVideoConsentSection`, con este texto de confirmación: "Registra que consta el
  consentimiento firmado para guardar vídeo de las caídas con riesgo durante 30 días. Después,
  activa `clips` en el hub de este hogar." Al retirarlo: "Los clips dejan de verse en la app al
  momento. Desactiva también `clips` en el hub."
- `controlsList="nodownload"` es cosmético: quien ve el vídeo puede grabar la pantalla. Lo que
  protege es quién puede pedir la URL y la auditoría.

## 10. Privacidad

- **Minimización:** solo salen de casa los clips de caídas con riesgo, de la cámara que la vio y
  sin audio. Un clip sin riesgo nunca se escribe a disco: se descarta en memoria.
- **Consentimiento propio** (`person.fall_clip_consent_at`) y **opt-in explícito por hogar y por
  cámara** (`clips.enabled` y `clips.cameras`). Excluir una cámara (por ejemplo, la del baño) es
  dejarla fuera de la lista.
- **Retención:** 30 días en S3 desde la subida, aplicada por lifecycle; la API deja de servirlo en
  `expires_at`. Lo que espera en la cola del hogar no pasa de 3 días, así que un clip existe como
  mucho 33 días desde la caída.
- **Acceso:** el hub solo puede añadir clips nuevos en su carpeta. Entre los servicios, solo la
  Lambda de la app lee, y solo entrega URLs de 5 minutos a quien tiene permiso de cámaras y con
  consentimiento registrado. Cada URL entregada deja una fila `fall_clip_viewed` en `audit_log`.
- **Operadores:** quien despliega con `PowerUserAccess` puede leer cualquier objeto del bucket
  desde la consola, y eso no deja rastro en v1 (no hay CloudTrail de data events). Se documenta
  como límite conocido y va a backlog junto con CloudTrail; una bucket policy con `Deny` explícito
  necesitaría además un acceso de emergencia, y no compensa antes del piloto.
- **Cifrado:** TLS en tránsito (`enforceSSL`) y SSE-S3 en reposo. En el hub, la cola de pendientes
  está en `/data` sin cifrar; es la misma confianza que ya tienen los certificados del hogar en ese
  disco.

## 11. Documentación que cambia

Forma parte del slice: los textos actuales dicen que el vídeo nunca se almacena.

- **Hub:** `docs/como-funciona.md` (la frase "al exterior solo salen eventos de texto" de la
  introducción, el subsistema `clips/` y los eventos nuevos), `docs/como-probar.md` y
  `docs/backlog.md`.
- **Infra:** `docs/information/context.md`, P1 y P2: P1 pasa a "eventos, no vídeo almacenado,
  con dos excepciones: el directo bajo demanda, que nunca se almacena, y el clip de una caída con
  riesgo, guardado 30 días con consentimiento propio". `docs/architecture/02-hub-ingest.md` (los
  tres eventos) y `docs/architecture/04-rule-engine.md` (`attach_clip`).
- **Motor de reglas:** README (tipos aceptados y la regla nueva).
- **App:** la nota de privacidad de `docs/superpowers/specs/2026-09-05-family-app-v2-design.md`
  ("no se graba ni se almacena vídeo en ningún sitio") se reescribe con la excepción del clip y,
  de paso, corrige "cuentas que el técnico habilita", que ya es un permiso por relación.

## 12. Orden de implementación

Un plan por repo, todos contra este spec:

1. **Infra:** stack, IAM, policy, aprovisionamiento y migración. Sin esto ni el hub sube ni la
   API firma.
2. **Hub** (plan existente) y **motor de reglas**, en paralelo. El motor puede ir antes que el
   hub: sin eventos `fall_clip_*` no hace nada.
3. **API**, después **frontend**.
4. **Antes de la e2e,** un `PutObject` real con el certificado de un hogar: con una clave nueva
   debe dar 200, y repetido sobre la misma clave 412. Toda la subida depende de que S3 aplique
   `s3:if-none-match` en la policy. Si no lo hiciera, el hub recibiría un deny sin que aparezca
   ningún error en AWS.
5. Prueba e2e manual contra la cuenta real: caída simulada → objeto en S3 → `detail.clip` en la
   alerta → "Ver vídeo" en la app con una cuenta con permiso → fila `fall_clip_viewed`; misma
   prueba con una cuenta sin permiso (no ve el botón) y tras retirar el consentimiento.

## 13. Lo que queda para backlog

- **Desmayo lento:** una persona que se desploma despacio no supera `RISK_MIN_DROP` y no genera
  clip, aunque siga en el suelo. Revisar con datos de `fall_clip_discarded`.
- Calibrar `RISK_MIN_DROP` con `replay_video.py` sobre vídeos reales antes del piloto.
- Notificar a la familia que hay vídeo (cuando exista un consumidor del SNS de alertas).
- Borrar los clips de una persona al retirar su consentimiento, sin esperar al lifecycle.
- CloudTrail con data events de lectura del bucket, como segundo rastro independiente de la API,
  y una bucket policy que niegue `GetObject` a todo lo que no sea la Lambda de la app (con un
  acceso de emergencia).
- Varios clips por alerta (`detail.clips` como lista) si en el piloto aparecen alertas con dos
  caídas grabadas.
- Cifrar la cola de pendientes en el hub si se cifra el resto de `/data`.
