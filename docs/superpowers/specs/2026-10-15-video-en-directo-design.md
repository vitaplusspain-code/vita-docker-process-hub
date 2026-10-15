# Diseño — Vídeo en directo para familiares y cuidadores (v1)

- Fecha: 2026-10-15
- Estado: aprobado en brainstorming, pendiente de planes de implementación
- Repos: `vita-docker-process-hub` (hub), `vitaplus-aws-architecture` (infra y migraciones),
  `vita-web-family-app` (API y frontend)
- Slices relacionados: `2026-08-21-uplink-eventos-aws-design.md` (canal hub → AWS),
  `2026-09-12-admin-enrolamiento-design.md`

Este documento es el **spec único** de los tres lados. El contrato (recursos AWS por hub, token
de sesión y protocolo del DataChannel) se define aquí una sola vez y de él salen **tres planes de
implementación**, uno por repo. Mismo motivo que en el uplink: un contrato escrito tres veces se
desincroniza.

## 0. El problema

Hasta ahora el principio de producto era "eventos, no vídeo": al cloud solo viajaba texto y la
app familiar decía expresamente que la cámara nunca envía imágenes. Ese principio ha cambiado.
El producto quiere que un familiar o cuidador pueda **ver en directo las cámaras del hogar desde
la app web**, pero solo en las cuentas que el técnico habilite, con consentimiento registrado y
sin que el vídeo se almacene en ningún sitio.

Hoy no existe nada de esto: el hub solo lee RTSP a ~2 fps para inferencia, la policy de IoT
solo permite `Connect` y `Publish`, no hay KVS ni WebRTC en la infraestructura y la app no tiene
reproductor.

## 1. Decisiones cerradas en brainstorming

| Decisión | Elegido | Motivo |
|---|---|---|
| Desde dónde se ve | Remoto, desde la app web familiar | Es el caso de uso: el familiar no está en casa. |
| Quién puede ver | Cuentas con relación con la persona (`admin_account_id` o `relationship` `family`/`caregiver`), incluidas las `read_only` | Ver es una lectura. |
| Puerta de acceso | Flag `account.live_video_enabled`, que activa el técnico | No hay pasarela de pago ni entitlements por `plan_code`; el técnico decide. |
| Exclusividad | **Una sesión por cuenta**; cuentas distintas pueden ver a la vez, incluso la misma cámara | Petición explícita. |
| Segunda sesión de la misma cuenta | **La nueva expulsa a la anterior** | Evita quedar bloqueado por una pestaña zombi o un móvil perdido. |
| Forma de la sesión | Una cámara cada vez con selector; 10 min con "Seguir viendo"; sin audio | Limita el uso como vigilancia continua y el ancho de subida del hogar. |
| Transporte | **KVS WebRTC** (P2P, con TURN gestionado) | Latencia < 1 s, el vídeo no se almacena en la nube, sin servicio propio que operar. |
| Árbitro de las reglas | **El hub**, con un token firmado por la nube | Cortar una conexión P2P ya establecida solo puede hacerlo uno de sus extremos. |

Alternativas descartadas:

- **KVS Streams + HLS.** Uplink fijo con varios visores, pero latencia de 3–10 s, el vídeo se
  **almacena** en KVS (la reproducción HLS exige retención > 0) y necesita una orden de "empieza a
  emitir" (downlink). Peor en privacidad, que es justo lo que este slice tiene que cuidar.
- **Relay propio (MediaMTX en ECS, WHIP/WHEP).** Control total a cambio de operar un servicio
  24/7 con TLS y escalado. No compensa en un piloto.

## 2. Alcance

Dentro:

- Hub: subsistema `src/vitahub/live/` que actúa como master WebRTC del canal KVS de su hogar,
  verifica tokens y aplica las reglas de sesión.
- Infra: stack `vita-<env>-hub-live` (clave KMS, role alias de IoT), roles IAM por script,
  ampliación de la policy IoT del hub, creación del canal de señalización en
  `provision-hub.sh` y una migración de Postgres.
- API: emisión de sesiones, consulta y cierre, y el flag en el panel técnico.
- Frontend: botón en Dispositivos, página `LivePage` y el interruptor en el panel técnico.
- Reescritura de los principios de privacidad afectados (§9).

Fuera, con motivo:

- **Audio y mosaico de cámaras.** Decisiones de esta sesión: más ancho de banda y más
  invasivo, sin un caso de uso que lo pida.
- **Grabación, clips o "ver lo de hace 5 minutos".** Implican almacenar vídeo, que es otro
  debate de privacidad y otro diseño.
- **Pasarela de pago y entitlements por `plan_code`.** El gate de v1 es el flag del técnico.
- **Indicador físico en el hogar** (LED o sonido que avise a la persona mayor de que la están
  viendo). El hub no tiene hardware de aviso. Va a backlog como deuda de privacidad explícita.
- **Corte duro entre hubs distintos de la misma cuenta.** Ver §3.5: el corte es cooperativo y el
  duro llega por `exp` en 10 min o menos.
- **Downlink MQTT.** El diseño no lo necesita: el hub escucha en su canal de señalización.

## 3. El contrato compartido

```
 Navegador (viewer)              AWS (eu-west-1)                       Jetson (master)
┌───────────────────┐  HTTPS   ┌──────────────────────────────┐       ┌──────────────────┐
│ LivePage          │ ───────► │ API Lambda                   │       │ live/service     │
│                   │ ◄─────── │  ├─ comprueba flag/relación  │       │  ├─ token.py     │
│  token + STS      │          │  ├─ KMS Sign (token)         │       │  ├─ sessions.py  │
│                   │          │  └─ STS AssumeRole (viewer)  │       │  └─ peer.py      │
│                   │          │                              │       │        ▲         │
│  ConnectAsViewer  │  WSS     │ KVS signaling channel        │  WSS  │ ConnectAsMaster  │
│                   │ ◄──────► │ vita-<env>-live-<hub_id>     │ ◄───► │ (creds vía IoT   │
│                   │          └──────────────────────────────┘       │  cred. provider) │
│  <video>          │ ◄═══════ SRTP P2P (o TURN gestionado) ═════════ │ substream RTSP   │
└───────────────────┘                                                  └──────────────────┘
```

### 3.1 Recursos por hub

| Pieza | Valor | Por qué así |
|---|---|---|
| Canal de señalización | `vita-<env>-live-<hub_id>` | Derivado del `hub_id`, igual que el thing de IoT. No se inventa otro identificador. |
| Role alias de IoT | `vita-<env>-hub-live` | El hub obtiene credenciales AWS temporales con el certificado X.509 que ya tiene. Ningún secreto nuevo. |
| Permisos del hub | `ConnectAsMaster`, `GetSignalingChannelEndpoint`, `GetIceServerConfig` sobre `…:channel/vita-<env>-live-${credentials-iot:ThingName}/*` | Un certificado robado solo abre el canal de su propia casa. |
| Permisos del viewer | `ConnectAsViewer`, `GetSignalingChannelEndpoint`, `GetIceServerConfig`, acotados por *session policy* a un único canal | Las credenciales del navegador no sirven para ningún otro hogar. |

### 3.2 Token de sesión

JWT **ES256**, firmado con una clave **KMS asimétrica** (`ECC_NIST_P256`, `SIGN_VERIFY`):

```json
{"sid": "<uuid>", "sub": "<account_id>", "hub": "<hub_id>", "iat": 1760520000, "exp": 1760520600}
```

Cabecera: `{"alg": "ES256", "typ": "JWT", "kid": "<id de clave>"}`.

- `exp = iat + 600` (10 min). Es el límite de sesión y, a la vez, el corte duro de último
  recurso.
- **No lleva `camera_id`.** El token autoriza el hogar; la cámara se elige dentro de la sesión.
- El hub tiene las claves públicas en `hub.yaml` (`live.session_keys`, por `kid`) y verifica
  **sin red**. Rotar la clave es añadir un `kid` nuevo, desplegar y, después, retirar el viejo.
- KMS devuelve la firma en DER; la API la convierte al formato `r||s` de JWS antes de componer
  el token. Es fácil de olvidar y produce tokens que ninguna librería acepta.

### 3.3 Protocolo del DataChannel `control`

El viewer crea el DataChannel `control` (ordenado y fiable) en su oferta. Mensajes JSON de una
línea:

| Dirección | Mensaje | Efecto |
|---|---|---|
| viewer → hub | `{"type":"auth","token":"…"}` | Debe llegar en **5 s** desde que el canal abre; si no, el hub cierra con `invalid`. Repetido por la misma conexión con un token nuevo del mismo `sub` = **renovación** ("Seguir viendo"). |
| hub → viewer | `{"type":"ready","cameras":[{"id":"…","name":"…"}]}` | Token válido. Lista de cámaras habilitadas del hub. También se envía tras una renovación. |
| viewer → hub | `{"type":"select","camera_id":"…"}` | El hub cambia el track de vídeo a esa cámara. |
| hub → viewer | `{"type":"closed","reason":"…"}` | Se envía **siempre** antes de cortar (salvo caída de red). |

`reason` ∈ `expired` | `replaced` | `limit` | `invalid` | `camera_unavailable` | `shutdown`.

`camera_unavailable` **no** cierra la conexión: el hub lo envía si una cámara pedida no abre, y
el viewer puede elegir otra. Es la única `reason` que no va seguida de un corte.

### 3.4 Reglas que aplica el hub

El hub es el árbitro real del vídeo, porque es el único que puede cortar una conexión P2P ya
establecida:

1. Sin `auth` válido no se envía ningún frame.
2. Token válido = firma correcta con un `kid` conocido, `exp` en el futuro y `hub == hub_id`.
3. Llega `exp` sin renovar → `closed:expired`.
4. Llega un `auth` válido de un `sub` que ya tiene **otra** conexión → la anterior recibe
   `closed:replaced` y se cierra.
5. Admitir un viewer nuevo superaría `live.max_viewers` → el nuevo recibe `closed:limit`.
6. Apagado del hub → todos reciben `closed:shutdown`.

### 3.5 Regla que aplica la nube

La tabla `live_session` (PK `account_id`) guarda el `sid` vigente de cada cuenta. Emitir un token
sustituye el anterior. Con eso se cubre el caso que el hub no ve: una misma cuenta viendo **dos
hubs distintos** en dos dispositivos. La pestaña vieja consulta `GET /api/live/session` cada
15 s, ve un `sid` distinto y se cierra sola.

Ese corte es **cooperativo** (un cliente modificado podría ignorarlo). El corte duro llega por
`exp` en 10 min o menos, porque ese cliente ya no puede renovar: renovar emite un `sid` nuevo
que vuelve a sustituir al del otro dispositivo. Para un caso de uso que exige la misma cuenta en
dos hogares a la vez, es proporcionado.

## 4. Lado hub (`vita-docker-process-hub`)

### 4.1 Aislamiento

`live/` corre en **un hilo propio con un loop `asyncio`**, arrancado por `app.py` solo si
`live.enabled`. Un fallo de señalización, de credenciales o de un peer se reintenta con backoff
y **nunca** afecta a la inferencia, los eventos ni los workers de cámara. Mismo criterio que "un
fallo de una cámara nunca tumba el proceso". El apagado de `app.py` llama a `stop()` dentro de
su secuencia ordenada, antes de cerrar el sink (para poder emitir los `live_view_ended`).

### 4.2 Piezas

| Módulo | Responsabilidad | Puro |
|---|---|---|
| `live/token.py` | Verifica el JWT ES256 contra `session_keys` por `kid`; comprueba `exp` y `hub`. Devuelve un `SessionClaims` o un motivo de rechazo. | Sí |
| `live/sessions.py` | `SessionBook`: `admit(conn_id, claims, now)` → `Admitted` / `Renewed` / `Rejected(limit)` más la lista de conexiones a cerrar por `replaced`; `tick(now)` → conexiones caducadas. Sin red ni reloj propio. | Sí |
| `live/credentials.py` | Credenciales AWS temporales vía IoT credentials provider (HTTPS con mTLS y el certificado del uplink), en caché hasta 5 min antes de caducar. | No |
| `live/signaling.py` | Cliente de señalización KVS como master: `GetSignalingChannelEndpoint`, URL WSS prefirmada con SigV4, `GetIceServerConfig` y reconexión con el backoff de `ingest/rtsp.py`. | No |
| `live/source.py` | `LiveSource` por cámara: abre el **substream RTSP solo mientras alguien la ve**, lo comparte entre viewers con `MediaRelay` y lo cierra con el último. Limita a `live.max_fps`. | No |
| `live/peer.py` | Una `RTCPeerConnection` por viewer, el DataChannel `control` con los mensajes de §3.3 y el cambio de track en `select`. | No |
| `live/service.py` | `LiveService`: orquesta lo anterior. Interfaz mínima: `start()` y `stop()`. | No |

La fuente en directo es **una conexión RTSP aparte** de la de inferencia, al substream. No se
toca el bucle de `worker.py` ni su muestreo a ~2 fps.

### 4.3 Librería WebRTC

**`aiortc`**: WebRTC en Python puro, sin GStreamer ni el SDK en C de KVS. Encaja con el stack,
el empaquetado Docker actual y `mypy strict`, y se puede testear en proceso.

Coste conocido: recodifica (VP8/H.264 por software) **por viewer**. Mitigaciones: substream de
baja resolución, `max_fps: 15` y `max_viewers: 3`.

**Riesgo a validar primero.** El primer task del plan del hub es un spike que mide la CPU del
Jetson con la inferencia corriendo y 1, 2 y 3 viewers. Si no da, el plan B es GStreamer
`webrtcbin` con H.264 en passthrough (sin recodificar). **El contrato de §3 no cambia** con ese
cambio de librería.

### 4.4 Config

```yaml
live:
  enabled: false
  max_viewers: 3
  max_fps: 15
  region: eu-west-1
  session_keys:            # kid -> clave pública PEM (la imprime scripts/live-public-key.sh)
    k1: |
      -----BEGIN PUBLIC KEY-----
      ...
      -----END PUBLIC KEY-----
```

Por entorno: `VITAHUB_IOT_CREDENTIALS_ENDPOINT` (específico de la cuenta, sin default) y
`VITAHUB_LIVE_ROLE_ALIAS`. Reutiliza el certificado y la clave del uplink. Fail-fast: con
`live.enabled: true` y `uplink.enabled: false`, sin `session_keys`, con un PEM ilegible o sin
las variables de entorno, el hub **no arranca** y sale con código 1.

El nombre del canal se deriva de `hub_id`; no es configurable, por la misma razón que
`topic_prefix` en el uplink: es parte del contrato con la policy.

### 4.5 Eventos

Cada sesión emite por el sink normal (stdout y AWS):

- `live_view_started` (severidad `info`), payload `{sid, account_id, camera_id}`, al primer
  `select` admitido y en cada cambio de cámara.
- `live_view_ended` (severidad `info`), payload `{sid, account_id, reason, seconds}`.

Así hay rastro de auditoría también del lado hub, independiente del de la API. `camera_id` en
el sobre del evento es la cámara vista (o vacío en `live_view_ended` si nunca se eligió una).

### 4.6 Tests

- `token.py` y `sessions.py`: TDD puro (firma inválida, `kid` desconocido, caducado, hub ajeno,
  renovación, sustitución, límite, `tick`).
- `signaling.py`: contra un servidor WSS falso en proceso.
- e2e local: un viewer `aiortc` contra `LiveService` con señalización en memoria y una fuente
  sintética (frames de color), comprobando `ready`, recepción de frames, `replaced` y `expired`.
- Prueba manual documentada en `como-probar.md` contra la cuenta real.

## 5. Infra (`vitaplus-aws-architecture`)

### 5.1 Stack CDK `vita-<env>-hub-live`

- Clave KMS asimétrica `ECC_NIST_P256` / `SIGN_VERIFY`, alias `alias/vita-<env>-live-session`.
- IoT role alias `vita-<env>-hub-live` apuntando al rol `hub-live` (ARN por SSM, como el resto
  de roles).
- Salidas en SSM: ARN de la clave y nombre del role alias.

Stack aparte para no inflar `hub-ingest`, que es persistente y ya tiene su propio ciclo.

### 5.2 IAM (`admin/bloque-hub-live.sh`)

Según la convención del repo, los roles no se declaran en CDK:

- Rol `vita-<env>-hub-live`, de confianza `credentials.iot.amazonaws.com`, con los permisos de
  §3.1 acotados por `${credentials-iot:ThingName}`.
- Rol `vita-<env>-live-viewer`, de confianza el rol de la Lambda de la app, con los permisos de
  viewer sobre `vita-<env>-live-*`. La Lambda lo **acota a un único canal** con una session
  policy en cada `AssumeRole`. Duración 15 min (el mínimo de STS), suficiente para conectar; la
  conexión WebRTC ya establecida no depende de que las credenciales sigan vivas.
- Al rol de la Lambda de la app: `kms:Sign` sobre la clave y `sts:AssumeRole` sobre
  `live-viewer`.

### 5.3 IoT y aprovisionamiento

- Policy `${p}-hub-policy`: se añade **solo** `iot:AssumeRoleWithCertificate` sobre el role
  alias. Sigue sin `Subscribe` ni `Receive`.
- `scripts/provision-hub.sh`: crea también el canal `vita-<env>-live-<hub_id>` (idempotente: si
  existe, no falla).
- `scripts/live-public-key.sh`: imprime la clave pública en PEM y su `kid` para pegarlos en
  `hub.yaml`.

### 5.4 Migración `00NN_live_video.sql`

```sql
ALTER TABLE account ADD COLUMN live_video_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE live_session (
  account_id  <tipo de account.id> PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
  sid         uuid        NOT NULL,
  hub_id      text        NOT NULL,
  issued_at   timestamptz NOT NULL,
  expires_at  timestamptz NOT NULL
);
```

Además, ampliar el `CHECK` de `consent.kind` con `'live_video'`.

## 6. API (`vita-web-family-app/api`)

| Ruta | Qué hace |
|---|---|
| `POST /api/persons/:id/live` | Emite una sesión (§6.1). Devuelve `LiveSessionGrant`. |
| `GET /api/live/session` | `{sid}` vigente de la cuenta, o `{sid: null}`. Es el polling de 15 s. |
| `DELETE /api/live/session` | Cierre explícito, best effort. Borra la fila si el `sid` coincide. |
| `PATCH /api/admin/accounts/:id` | Admite `live_video_enabled`. Solo `is_admin`, con `audit_log`. Rechaza `true` (409 `live_video_consent_missing`) si la cuenta no tiene un `consent` `live_video` vigente (la tabla `consent` ya es por cuenta; si al implementar resulta que no, el plan lo resuelve antes de tocar la API). |

`GET /api/me` añade `live_video_enabled`.

### 6.1 Emisión de sesión

En orden, con la regla actual de 404 en vez de 403 para lo que queda fuera del ámbito:

1. Cuenta `active`; si no, el comportamiento actual de la app.
2. Relación con la persona (`admin_account_id` o `relationship`); si no, **404**.
3. `live_video_enabled`; si no, **403** `{"error":"live_video_disabled"}`.
4. La persona tiene un `device` `kind='hub'`; si no, **404** `{"error":"no_hub"}`.
5. Firma el token con `kms:Sign` (DER → `r||s`), asume `live-viewer` con una session policy
   limitada al canal del hub, hace upsert de `live_session` y escribe en `audit_log`
   (`action='live_session_issued'`, `sid`, `hub_id`).

Respuesta:

```json
{"token": "…", "sid": "…", "expires_at": "…", "region": "eu-west-1",
 "channel_arn": "arn:aws:kinesisvideo:…", "credentials": {"accessKeyId": "…", "secretAccessKey": "…", "sessionToken": "…"}}
```

"Seguir viendo" vuelve a llamar a este `POST`: token y `sid` nuevos. El viewer los manda en un
`auth` por la **misma** conexión y el hub lo trata como renovación (§3.3).

## 7. Frontend (`vita-web-family-app/frontend`)

- **Pestaña Dispositivos:** sustituye "El estado de la cámara llegará próximamente" por el
  botón **"Ver cámaras en directo"** si `me.live_video_enabled` y la persona tiene hub. Lleva a
  `/persons/:id/live`.
- **`LivePage`:**
  1. `POST /api/persons/:id/live`.
  2. Conecta como viewer con `amazon-kinesis-video-streams-webrtc` usando las credenciales STS.
  3. Abre el DataChannel `control` y envía `auth`.
  4. Con `ready`, muestra el selector y envía `select` con la primera cámara.
  5. `<video>` sin audio y contador regresivo.
  6. A 1 min del final aparece **"Seguir viendo"**, que renueva sin cortar el vídeo.
  7. Polling de `GET /api/live/session` cada 15 s; si el `sid` difiere, cierra.
  8. Al salir de la página o en `beforeunload`, cierra la conexión y llama a
     `DELETE /api/live/session`.
- **Panel técnico:** interruptor **"Vídeo en directo"** en la ficha de la cuenta, que muestra el
  error de consentimiento ausente si el `PATCH` lo rechaza.
- **`shared/`:** tipos `LiveSessionGrant`, `LiveControlMessage` y `LiveCloseReason`. El hub, en
  Python, replica el contrato a mano y este spec es la fuente de verdad.

Mensajes:

| Caso | Texto |
|---|---|
| `replaced` o `sid` distinto | "Has abierto el directo en otro dispositivo." |
| `limit` | "Ya hay demasiadas personas viendo este hogar. Inténtalo en unos minutos." |
| `expired` | "La sesión ha terminado." + botón "Volver a ver" |
| `camera_unavailable` | "Esta cámara no está disponible ahora." (el selector sigue activo) |
| `shutdown` o sin conexión al hub en 15 s | "No se puede conectar con el hogar. Comprueba que el equipo está encendido." |
| `403 live_video_disabled` | Vuelve a Dispositivos. |

Tests: la máquina de estados de `LivePage` va en un reducer puro con tests unitarios; la API,
con tests de las cuatro comprobaciones de §6.1 y del `PATCH` con y sin consentimiento.

## 8. Privacidad

- **Consentimiento:** activar el flag exige un `consent` `live_video` registrado (§6).
- **Auditoría doble:** `audit_log` en la nube por cada token emitido, y `live_view_started` /
  `live_view_ended` del hub en `hub-events`. Si alguien ve vídeo, queda constancia en los dos
  lados, y uno no depende del otro.
- **Sin almacenamiento:** el vídeo no se graba ni se persiste en el hub, en KVS ni en S3. KVS
  WebRTC solo señaliza y, si hace falta, retransmite por TURN.
- **Ancho de banda del hogar:** cada viewer es un flujo de subida propio; el tope
  `max_viewers` lo acota.
- **Deuda explícita:** sin indicador en el hogar de que alguien está viendo (backlog).

## 9. Documentación que cambia

Forma parte del slice, no es opcional: los textos actuales afirman lo contrario de lo que hará
el sistema.

- `vitaplus-aws-architecture/docs/information/context.md`: P1 y P2 pasan de "el cloud no recibe
  vídeo continuo" a "vídeo solo en directo, bajo demanda, por cuenta habilitada con
  consentimiento y nunca almacenado", con la ruta P2P/TURN.
- `vita-web-family-app/docs/superpowers/specs/2026-09-05-family-app-v2-design.md`: la nota de
  privacidad ("la cámara solo comunica detecciones, nunca envía imágenes") se actualiza y
  referencia este diseño.
- Hub: `docs/como-funciona.md` (subsistema `live/` en la vista general y en la tabla de
  módulos; la frase "al exterior solo sale texto"), `docs/como-probar.md` (prueba manual con un
  viewer real) y `docs/backlog.md` (indicador en el hogar y corte duro entre hubs).

## 10. Orden de implementación

1. **Spike de CPU** en el Jetson (`aiortc`, 1–3 viewers con inferencia activa). Decide la
   librería del hub. Throwaway.
2. **Infra:** stack, IAM, policy, canal, migración. Sin esto nada conecta.
3. **Hub:** `token` y `sessions` (puros), luego credenciales, señalización, fuente, peer y
   servicio.
4. **API**, después **frontend**.
5. Prueba e2e manual contra la cuenta real y documentación de §9.

Los pasos 3 y 4 pueden ir en paralelo una vez cerrado el contrato y desplegada la infra.
