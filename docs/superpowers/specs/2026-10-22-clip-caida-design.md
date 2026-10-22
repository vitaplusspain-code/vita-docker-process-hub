# Diseño — Clip de vídeo como prueba de una caída con riesgo (v1)

- Fecha: 2026-10-22
- Estado: aprobado en brainstorming, pendiente de plan de implementación
- Repos: `vita-docker-process-hub` (hub), `vitaplus-aws-architecture` (infra)
- Slices relacionados: `2026-08-22-deteccion-caidas-design.md` (episodios y score),
  `2026-08-21-uplink-eventos-aws-design.md` (sink y policy IoT),
  `2026-10-15-video-en-directo-design.md` (credentials provider y lectura RTSP con PyAV)

## 0. El problema

Cuando el hub detecta una caída, a AWS solo llega texto: `fall_detected` con un score y unas
señales geométricas. Quien recibe la alerta no tiene forma de ver qué pasó, y el score por sí solo
no basta para distinguir una caída real de alguien que se tumba en el suelo.

El producto quiere que, **cuando una caída tenga riesgo**, el hub envíe a AWS un clip corto de ese
momento como prueba, y que el clip se guarde **30 días**.

Esto cambia un principio. Hasta ahora el vídeo nunca se almacenaba: ni en el hub ni en la nube
(directo, spec del 2026-10-15, §8). A partir de este slice se almacena un tipo concreto de vídeo,
el clip de una caída con riesgo, y ninguno más.

## 1. Decisiones cerradas en brainstorming

| Decisión | Elegido | Origen |
|---|---|---|
| Cuándo se genera un clip | Solo en una alerta de caída (`fall_detected`) **y** si hay riesgo (§3) | Usuario |
| Retención | 30 días en AWS; después se borra solo | Usuario. Contados desde la subida (§4.4) |
| De qué cámara | Solo de la cámara que detectó la caída, con el mismo stream que ya analiza el hub (`inference.stream`, el substream por defecto) | Usuario ("solo en su stream") |
| Criterio de riesgo | Score de confirmación alto y la persona no se ha levantado al final de la ventana (§3) | Delegado por el usuario; propuesto y aceptado |
| Ventana | 15 s antes y 10 s después de `fall_detected` | Propuesto y aceptado |
| Clips sin riesgo | Se borran en el hogar y nunca salen de casa; solo sale un evento de texto | Propuesto y aceptado |
| Transporte a AWS | `s3:PutObject` con credenciales temporales del credentials provider de IoT, con un role alias nuevo acotado por ThingName | Propuesto y aceptado |

## 2. Alcance

Dentro:

- Hub: subsistema `src/vitahub/clips/` (buffer, disparo, evaluación de riesgo, montaje, cola de
  subida), config `clips`, tres eventos nuevos, su cableado en `factory.py` y `app.py`, y las
  variables nuevas en los `docker-compose*.yml`.
- Infra: bucket S3 cifrado con borrado a 30 días, role alias e IAM del hub, ampliación de la
  policy IoT y cambios en `provision-hub.sh`.
- Documentación del cambio de principio de privacidad (§7).

Fuera, con motivo:

- **Ver el clip desde la app familiar.** Requiere API (URL presignada de GET, auditoría de
  accesos) y frontend. Es el siguiente slice. En v1 el clip se guarda y queda enlazado al
  episodio por el evento `fall_clip_ready`; solo lo puede abrir operaciones desde la cuenta AWS.
- **Clips de otras cámaras de la casa**, aunque vean la escena. Decisión del usuario para v1.
- **Audio.** Igual que en el directo: no se graba.
- **Clips de `fall_update`** o de una persona que sigue en el suelo minutos después. El clip es
  único por episodio; lo que pasa después sigue llegando como texto.
- **Downlink MQTT.** El hub decide solo; la nube no le puede pedir un clip.

## 3. Qué es una caída con riesgo

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

## 4. Lado hub

### 4.1 Flujo

```
cámara ──RTSP──► ClipRecorder (hilo por cámara, PyAV, sin decodificar)
                   └─ buffer en memoria con los últimos ~30 s de paquetes de vídeo

FallEngine ──fall_detected──► sink normal ──► ClipTriggerSink
                                                 ├─ congela los 15 s anteriores
                                                 ├─ sigue recogiendo 10 s
                                                 ├─ escucha fall_resolved del episodio
                                                 └─ al cerrar la ventana evalúa el riesgo (§3)
                                                       ├─ sin riesgo: descarta en memoria
                                                       │            → fall_clip_discarded
                                                       └─ con riesgo: monta MP4 → cola en disco
                                                                     → ClipUploader → S3
                                                                     → fall_clip_ready
```

### 4.2 Piezas

| Pieza | Responsabilidad |
|---|---|
| `clips/recorder.py` — `ClipRecorder` | Un hilo por cámara habilitada. Lo arranca la función `worker(camera, rtsp_url, cam_stop)` de `app.py` junto a `_camera_loop`, con la misma `rtsp_url` y el mismo `cam_stop`: así el `CameraSupervisor` ya gestiona altas, bajas y cambios de URI sin cableado nuevo. Abre esa URI con PyAV (`rtsp_transport=tcp`, solo vídeo, como `live/source.py`) y **no decodifica**: guarda los paquetes con su instante de llegada (`time.monotonic()`) en un buffer acotado a `BUFFER_S = 30` s. Al recortar, nunca deja el buffer empezando en un paquete que no sea keyframe. Reconecta con `backoff_delay`/`next_attempt` de `ingest/rtsp.py`. `snapshot(start, end)` devuelve los paquetes de ese intervalo, empezando en el keyframe anterior a `start`. |
| `clips/trigger.py` — `ClipTriggerSink(EventSink)` | Se añade al `FanoutSink`. Ignora todo salvo `fall_detected` y `fall_resolved`. Con `fall_detected` de una cámara habilitada abre una ventana para ese `episode_id` (instante de disparo = `time.monotonic()` al recibir el evento). Con `fall_resolved` anota el `reason` del episodio. Un temporizador cierra cada ventana a los `POST_S = 10` s y llama al evaluador. **No hace trabajo pesado en `emit`**: el `emit` corre en el hilo de la cámara. |
| `clips/risk.py` | Función pura `assess(drop_speed: float \| None, resolved_reason) -> Literal["risk", "slow", "got_up"]`. Es lo único que codifica §3. |
| `clips/assembler.py` | Remux de los paquetes a un MP4 (`av.open(..., "w")`, `add_stream_from_template`, `movflags=+faststart`), sin recodificar. Calcula el sha256. |
| `clips/uploader.py` — `ClipUploader` | Hilo con cola persistente en `/data/clips-pending/` (MP4 + JSON de metadatos por clip). Sube con `botocore` (`PutObject`, `ContentType: video/mp4`) usando un `IotCredentialsProvider` propio con el role alias de clips. Reintenta con backoff si no hay internet. Borra el fichero local solo tras la subida confirmada. Al arrancar el hub, reanuda lo que quedara en la carpeta. |

Una sola ventana por cámara a la vez: si llega otro `fall_detected` de la misma cámara mientras su
ventana está abierta, no se abre otra. El episodio nuevo se asocia al clip en curso y la ventana
se **alarga hasta 10 s después de ese último `fall_detected`**, con un tope de 30 s de post-roll
en total (así cabe en el buffer). Si no, un episodio que entra a los 8 s nunca podría cerrarse
como `got_up`. El clip tiene riesgo si **alguno** de sus episodios lo tiene. El evento resultante
lleva la lista de `episode_ids`.

**Apagado:** una ventana abierta cuando el hub se para se descarta sin escribir nada a disco y se
emite `fall_clip_failed` con `reason: "shutdown"`. No se puede saber si había riesgo, y un clip
sin riesgo confirmado no sale de casa. Los clips ya montados en la cola sí se conservan y se suben
en el siguiente arranque. El `ClipTriggerSink` se cierra antes que el resto del sink, para que el
evento llegue a salir.

El recorder abre **una conexión RTSP más** por cámara habilitada, igual que ya hace el directo.
No se toca `_camera_loop`.

### 4.3 Clave y metadatos en S3

- Bucket: el de §5. Clave: `{hub_id}/{camera_id}/{episode_id}.mp4`, con el `episode_id` del
  primer episodio del clip.
- Metadatos del objeto: `episode-ids`, `camera-id`, `clip-start` (ISO-8601 UTC) y `sha256`.
- El `hub_id` va al principio de la clave porque es lo que acota la IAM policy (§5.2).

### 4.4 Límites y fallos

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

### 4.5 Memoria y CPU

Con el substream a ~0,5–1 Mbps, 30 s de buffer son entre 2 y 4 MB por cámara. Con
`inference.stream: main` (4–8 Mbps) son 15–30 MB por cámara y cada clip pesa 12–25 MB, así que la
cola de 200 MB admite menos clips. Se permite igualmente: "su stream" es el que analiza el hub,
sea cual sea. El hub loguea un aviso al arrancar si `clips.enabled` y `stream: main` coinciden. El recorder solo
demultiplexa, no decodifica: su CPU es despreciable frente a los ~12 % de núcleo por viewer que
midió el spike del directo. El montaje del MP4 es un remux de unos 25 s de vídeo y ocurre una vez
por clip.

### 4.6 Config

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

### 4.7 Eventos

Por el sink normal (stdout y AWS), con el `camera_id` de la cámara del clip:

| Tipo | Severidad | Payload | Cuándo |
|---|---|---|---|
| `fall_clip_ready` | `info` | `episode_ids`, `s3_key`, `sha256`, `duration_s`, `clip_start` | Tras confirmar la subida |
| `fall_clip_discarded` | `info` | `episode_ids`, `drop_speed`, `reason` (`slow` \| `got_up`) | Al cerrar la ventana sin riesgo |
| `fall_clip_failed` | `info` | `episode_ids`, `reason` (`no_video` \| `mux_error` \| `queue_full` \| `expired` \| `upload_rejected` \| `shutdown`) | Cuando un clip con riesgo no va a llegar a S3 |

Las tres van con severidad `info`, que ya existe: el hub hoy solo emite `high` e `info`, y no se
añade un valor nuevo que el pipeline de la nube podría no aceptar. Ninguna debe notificar a la
familia; la alerta sigue siendo `fall_detected`.

`fall_clip_discarded` sirve para calibrar `RISK_MIN_DROP` con datos reales sin enviar vídeo.

### 4.8 Tests

- `risk.py`: TDD puro con la tabla de §3 (`drop_speed` por debajo y por encima del umbral, y
  `null`; `upright`, `track_lost` y episodio abierto).
- `trigger.py`: reloj inyectado. Ventana que cierra a los 10 s; `fall_resolved` antes y después
  del cierre; segundo episodio que alarga la ventana y tope de 30 s; apagado con ventana abierta;
  cámara no habilitada; eventos ajenos ignorados; `emit` que no bloquea.
- Buffer del recorder con paquetes sintéticos: recorte a 30 s, arranque siempre en keyframe,
  `snapshot` sobre un intervalo sin vídeo.
- `assembler.py`: remux de un stream H.264 sintético generado con PyAV en el test; el MP4
  resultante se abre y tiene la duración esperada.
- `uploader.py` con un cliente S3 falso: subida y borrado, reintento sin red, reanudación al
  arrancar, tope de 200 MB, caducidad a 3 días, 403.
- `config.py`: cada caso de fail-fast de §4.6.
- La subida real a S3 y el RTSP real quedan como "runtime only", igual que `https_fetcher` y
  `open_capture`, y se cubren con la prueba manual de `como-probar.md`.

## 5. Infra (`vitaplus-aws-architecture`)

### 5.1 Stack CDK `vita-<env>-hub-clips`

- Bucket `vita-<env>-fall-clips`: Block Public Access completo, cifrado SSE-KMS con una clave
  propia (`alias/vita-<env>-fall-clips`), `aws:SecureTransport` obligatorio en la bucket policy,
  **sin versionado** (con versionado, un objeto "borrado" seguiría existiendo como versión
  anterior) y una regla de lifecycle de **expiración a 30 días** sobre todo el bucket.
- CloudTrail data events de lectura (`GetObject`) sobre el bucket, para dejar constancia de quién
  abre un clip.
- IoT role alias `vita-<env>-hub-clips` hacia el rol de §5.2.
- Salidas en SSM: nombre del bucket y nombre del role alias.

Stack aparte por la misma razón que `hub-live`.

### 5.2 IAM (`admin/bloque-hub-clips.sh`)

Rol `vita-<env>-hub-clips`, de confianza `credentials.iot.amazonaws.com`, con solo:

- `s3:PutObject` sobre `arn:aws:s3:::vita-<env>-fall-clips/${credentials-iot:ThingName}/*`.
- `kms:GenerateDataKey` sobre la clave del bucket.

Sin `GetObject`, `ListBucket` ni `DeleteObject`: un certificado robado solo puede **añadir**
clips en la carpeta de su propia casa; no puede leer ni borrar nada.

### 5.3 IoT y aprovisionamiento

- Policy `${p}-hub-policy`: se añade `iot:AssumeRoleWithCertificate` sobre el role alias
  nuevo. Sigue sin `Subscribe` ni `Receive`.
- `scripts/provision-hub.sh` imprime `VITAHUB_CLIPS_BUCKET` y `VITAHUB_CLIPS_ROLE_ALIAS`.
- Los eventos `fall_clip_*` entran por el pipeline de `hub-events` existente. **Primera tarea del
  plan de infra:** comprobar que la regla IoT, el esquema de destino y los consumidores (incluidas
  las notificaciones de la app) no filtran por una lista cerrada de `type`. Si la hay, se añaden
  los tres tipos nuevos en este slice, sin notificación. Este diseño no lo ha podido verificar
  porque el repo de infra no estaba en el workspace.

## 6. Privacidad

- **Minimización:** solo salen de casa los clips de caídas con riesgo, de la cámara que la vio y
  sin audio. Un clip sin riesgo nunca se escribe a disco: se descarta en memoria.
- **Opt-in explícito por hogar y por cámara** (`clips.enabled` y `clips.cameras`). Lo activa el
  técnico solo después de recoger el consentimiento firmado de la persona observada, en papel,
  como en el directo. Excluir una cámara (por ejemplo, la del baño) es dejarla fuera de la lista.
- **Retención:** 30 días en S3 desde la subida, aplicada por lifecycle. Lo que espera en la cola
  del hogar no pasa de 3 días, así que un clip existe como mucho 33 días desde la caída.
- **Acceso:** el hub solo puede escribir. Leer un clip deja rastro en CloudTrail.
- **Cifrado:** TLS en tránsito y SSE-KMS en reposo. En el hub, la cola de pendientes está en
  `/data` sin cifrar; es la misma confianza que ya tienen los certificados del hogar en ese disco.

## 7. Documentación que cambia

Forma parte del slice: los textos actuales dicen que el vídeo nunca se almacena.

- Hub: `docs/como-funciona.md` (la frase "al exterior solo salen eventos de texto" de la
  introducción, el subsistema `clips/` en la vista general y en la tabla de módulos, y los
  eventos nuevos), `docs/como-probar.md` (prueba manual con una caída simulada contra la cuenta
  real) y `docs/backlog.md` (§9).
- `vitaplus-aws-architecture/docs/information/context.md`: los principios de privacidad pasan a
  "vídeo en directo bajo demanda y nunca almacenado, salvo el clip de una caída con riesgo,
  guardado 30 días".
- La nota de privacidad de la app familiar (`2026-09-05-family-app-v2-design.md`) se actualiza
  en el slice que muestre el clip, no en este.

## 8. Orden de implementación

1. **Infra:** stack, IAM, policy y `provision-hub.sh`. Sin esto la subida no funciona.
2. **Hub, partes puras:** `risk.py`, buffer del recorder y `trigger.py` con reloj inyectado.
3. **Hub, partes con E/S:** `assembler.py`, `uploader.py`, config, `factory.py` y `app.py`.
4. **Prueba manual** contra la cuenta real (caída simulada, clip en S3, evento `fall_clip_ready`,
   lifecycle visible en el bucket) y documentación de §7.

Los pasos 1 y 2 pueden ir en paralelo.

## 9. Lo que queda para backlog

- Ver el clip desde la app familiar (URL presignada y auditoría en la API).
- **Desmayo lento:** una persona que se desploma despacio no supera `RISK_MIN_DROP` y no genera
  clip, aunque siga en el suelo. Revisar con datos de `fall_clip_discarded`.
- Calibrar `RISK_MIN_DROP` con `replay_video.py` sobre vídeos reales antes del piloto.
- Cifrar la cola de pendientes en el hub si se cifra el resto de `/data`.
