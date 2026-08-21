# Diseño — Uplink de eventos a AWS por IoT Core

- Fecha: 2026-08-21
- Estado: aprobado en brainstorming, pendiente de plan de implementación
- Repos: `vita-docker-process-hub` (sink) y `vitaplus-aws-architecture` (stack `hub-ingest`)
- Slice anterior: `2026-08-18-url-rtsp-persistida-design.md`
- Cierra: backlog #3 (`docs/backlog.md`)

Este documento es el **spec único** de los dos lados. El contrato (nombre del thing, topic y
esquema del payload) se define aquí una sola vez, y de él salen **dos planes de implementación**,
uno por repo. Es la razón de que el spec no esté partido: un contrato escrito dos veces se
desincroniza.

## 0. El problema

El hub detecta, decide y emite eventos JSON correctos — y los escribe **por `stdout`**. Nadie
fuera del Jetson los lee. Un hogar con el hub funcionando perfectamente es, desde la nube,
indistinguible de un hogar sin instalar.

Todo lo que el producto promete a partir de aquí (línea base personal, semáforo familiar,
informes, supervisión de hubs caídos) empieza por que esos eventos salgan del hogar. Este slice
construye ese canal: el primero que existe entre la rama de cámaras y AWS.

## 1. Alcance

Dentro:

- `sinks/aws_iot.py` en el hub: publicación MQTT/TLS con certificado de cliente.
- `sinks/fanout.py`: `stdout` y AWS a la vez, no uno en lugar del otro.
- Stack `hub-ingest` en el repo de arquitectura: policy de IoT, TopicRule, tabla DynamoDB.
- `scripts/provision-hub.sh`: alta de un hogar (thing + certificado + policy).
- Bloque nuevo en `docs/peticion-area-admin.md` para el rol IAM de la regla.

Fuera, con motivo:

- **Imágenes o clips de vídeo.** No entran en este slice ni en ninguno cercano. `context.md` §2
  P1 ("Eventos, no vídeo") es normativo: al cloud viajan eventos, y como mucho clips cortos de
  alertas críticas con consentimiento. Lo que sube aquí es JSON de unos cientos de bytes.
- **Cola en disco para cortes de red.** Decisión explícita de este slice: lo que no sale, se
  pierde (§2). Ver §8 para lo que eso cuesta y cuándo tocará revisarlo.
- **Downlink.** El rescan remoto que anota el backlog #3 necesita `Subscribe`/`Receive` en la
  policy; no se conceden permisos que aún no se usan. Slice aparte.
- **Eventos de presencia de IoT** (`$aws/events/presence/…`, `hub_online`). Son una casilla de
  configuración de cuenta, no un recurso del stack, y no tienen consumidor todavía.
- **Archivo en S3 y analítica** (Athena, Firehose). Sin consumidor, es infraestructura que
  mantener sin nadie que la use.
- **Fleet Provisioning.** Con un piloto de pocos hogares, un certificado por instalación es
  proporcionado. Ver §5.

## 2. Decisiones cerradas en brainstorming

| Decisión | Elegido | Motivo |
|---|---|---|
| Qué sube | Los eventos JSON, sin imagen | Es lo que P1/P2 de `context.md` permiten y lo que el producto necesita. |
| Transporte | AWS IoT Core, MQTT sobre TLS mutuo | Identidad X.509 por hogar, revocable de una en una, sin secreto compartido en ningún `.env`. Trae de serie reconexión, y deja abiertos el downlink y los eventos de conexión para cuando toque. |
| Almacenamiento | DynamoDB, regla IoT directa | Consultable por hogar y ventana temporal desde el primer día; TTL da la retención sin código. Sin Lambda que mantener. |
| Sin conexión | Sin cola: se pierde | Slice mínimo. La traza local sigue en `stdout`/`docker logs`. |
| Certificados | Uno por hogar, creado en la instalación | Mismo momento y mismo técnico que ya siembran `hub.yaml`. |
| Librería MQTT | `paho-mqtt`, no `awsiotsdk` | Ver §4.2. |
| `stdout` | Se mantiene, en paralelo | `docker logs` es la herramienta de diagnóstico en campo. Un fallo de uplink no deja al técnico a ciegas. |

## 3. El contrato compartido

```
Jetson (hogar)                          AWS (vita-dev, eu-west-1)
┌──────────────────────────┐            ┌────────────────────────────────┐
│ EventEngine              │            │ IoT Core                       │
│ ConnectionMonitor        │  MQTT/TLS  │  ├─ Thing: <hub_id>            │
│      ↓                   │  mTLS      │  ├─ Policy acotada al thing    │
│ FanoutSink ──┬─ stdout   │ ─────────► │  └─ TopicRule                  │
│              └─ AwsIot   │  :8883     │        ↓ (sin Lambda)          │
└──────────────────────────┘            │  DynamoDB  vita-dev-hub-events │
                                        └────────────────────────────────┘
```

| Pieza | Valor | Por qué así |
|---|---|---|
| Thing name | `hub_id` tal cual | El README ya exige que sea único por hogar. No se inventa un segundo identificador. |
| Topic | `vita/hub/{hub_id}/events` | La policy lo fija con `${iot:Connection.Thing.ThingName}`: un certificado robado solo publica por su propia casa. |
| Payload | Exactamente `Event.to_json()` | Cero cambios en `models.py`. El JSON que hoy sale por `stdout` es el que sale por MQTT: un solo esquema que mantener. |
| QoS | 0 | Coherente con "sin cola". Ver §8. |

**`topic_prefix` es configurable en el hub pero la policy de IoT lo lleva fijo** (`topic/vita/hub/…`,
§5.2). Cambiarlo en un `hub.yaml` sin cambiar la policy deja al hub publicando en un topic que su
certificado no autoriza: la conexión se establece y los `publish` se descartan. El campo existe
solo para no cablear una constante en el código; **el valor real es parte del contrato y no se
toca por hogar**. `hub.example.yaml` lo dice en el comentario.

Payload de referencia (el que ya produce el hub hoy):

```json
{"schema_version":1,"hub_id":"hub-casa-lopez","camera_id":"onvif-szjsa81a","camera_name":"camera-1","type":"person_detected","severity":"info","timestamp":"2026-08-21T10:15:03.412+00:00","payload":{"person_count":1,"confidence":0.873}}
```

**El `payload` anidado no se aplana.** La acción DynamoDBv2 lo escribe como mapa; los tipos de
evento tienen payloads distintos (`person_count` en presencia, motivo en `camera_unreachable`) y
aplanarlos obligaría a que la regla conociera cada tipo.

## 4. Lado hub (`vita-docker-process-hub`)

### 4.1 Piezas

`src/vitahub/sinks/fanout.py` — `FanoutSink(EventSink)`. Recibe una lista de sinks y emite por
todos. Un fallo en uno no impide los demás ni propaga hacia arriba. `close()` cierra todos, cada
uno garantizado pase lo que pase con el anterior (mismo criterio que `_shutdown` en `app.py`).

`src/vitahub/sinks/aws_iot.py` — `AwsIotSink(EventSink)`:

- `__init__(client, topic)`: el cliente MQTT **se inyecta**. Una función aparte, `build_client(...)`,
  hace el cableado TLS. Mismo patrón que `PersonDetector.from_weights`, y es lo que permite testear
  el sink con un doble sin tocar red.
- `emit(event)`: publica `event.to_json()` en el topic con QoS 0.
- `close()`: para el bucle de red y desconecta.

`build_sink(cfg, env)` en `factory.py`, en paralelo a `build_detector`. Con `uplink.enabled: false`
devuelve solo el sink de `stdout` — el comportamiento de hoy, bit a bit.

`app.py` cambia **una línea**: `sink = build_sink(cfg, env)` en lugar de `StdoutJsonSink()`. El
tipo declarado de `_shutdown` pasa de `StdoutJsonSink` a `EventSink`.

### 4.2 Por qué `paho-mqtt` y no `awsiotsdk`

`awsiotsdk` aporta sesión persistente, reintento con cola y helpers de ciclo de vida — las tres
cosas que la decisión "sin cola, QoS 0" descarta. A cambio arrastra `awscrt`, un binario nativo,
a una imagen que se construye sobre `l4t-pytorch` en ARM64. Si algún día no hay wheel compatible,
el fallo no es un aviso: es el build del Jetson roto, y el Dockerfile ya falla el build a propósito
cuando algo esencial no se puede resolver.

`paho-mqtt` es Python puro, hace TLS mutuo con el `ssl` de la stdlib y trae type hints (el
proyecto está en `mypy strict`). Cuando lleguen el downlink o la cola en disco, ese es el momento
de reevaluar el SDK.

### 4.3 Configuración

Sección nueva en `hub.yaml`, con los secretos fuera como manda el repo:

```yaml
uplink:
  enabled: true
  topic_prefix: vita/hub    # opcional
```

Por entorno: `VITAHUB_IOT_ENDPOINT` (sin default posible, es específico de la cuenta) y
`VITAHUB_IOT_CERT` / `VITAHUB_IOT_KEY` / `VITAHUB_IOT_CA`, con default bajo `/data/certs/`.

`enabled` por defecto **`false`**: un `hub.yaml` de un hub ya instalado, sin la sección nueva,
sigue arrancando exactamente igual que hoy.

### 4.4 Qué falla y cuándo

La distinción entre error de instalación y error de operación es deliberada:

| Momento | Comportamiento | Razón |
|---|---|---|
| Arranque, `enabled: true` sin endpoint o sin ficheros de certificado | `ConfigError` → el hub no arranca | Es un error de instalación y el técnico **está delante**. Mismo criterio que `VITAHUB_ONVIF_USER`. |
| Arranque, clave privada con permisos legibles por otros | Warning en el log, arranca igual | Barato de detectar, y el "de manera segura" se cae solo si la clave está a la vista. No se bloquea el arranque por esto: dejaría un hogar sin vigilancia por un `chmod`. |
| En marcha, el broker no responde o cae la red | Warning (una vez por episodio), reintento en segundo plano, **el hub sigue detectando** | Un hogar sin uplink todavía vigila. Un hogar con el proceso muerto, no. Nunca se tumban los workers por un fallo de cloud. |
| En marcha, `publish` lanza | Se traga en el sink, se registra | Ya hay dos capas por encima (`_emit_all` y el `try` del bucle de cámara); esta es la tercera y la más específica. |

### 4.5 Ficheros tocados

Nuevos: `sinks/aws_iot.py`, `sinks/fanout.py`, `tests/test_aws_iot_sink.py`,
`tests/test_fanout_sink.py`.

Modificados: `config.py` (`UplinkConfig` + validación), `factory.py` (`build_sink`), `app.py` (una
línea), `pyproject.toml` (`paho-mqtt`), `config/hub.example.yaml`, `README.md`,
`docs/como-funciona.md`, `docs/como-probar.md`, `docs/backlog.md` (marcar #3 y anotar los
diferidos de §8).

### 4.6 Tests

Todos con dobles, cero red en CI, igual que el resto de la suite:

- `emit` publica en el topic exacto y con el JSON exacto.
- Un `publish` que lanza no propaga.
- `close` desconecta.
- El fanout aísla un fallo: si el primer sink lanza, el segundo recibe igual.
- El fanout cierra todos aunque uno falle al cerrar.
- `enabled: true` sin `VITAHUB_IOT_ENDPOINT` → `ConfigError`.
- `enabled: true` con endpoint pero sin fichero de certificado → `ConfigError`.
- Un `hub.yaml` sin sección `uplink` carga y da `enabled: false`.
- `build_sink` con `enabled: false` devuelve solo el sink de `stdout`.

## 5. Lado AWS (`vitaplus-aws-architecture`)

### 5.1 Un solo stack, y persistente

Se llama **`hub-ingest`** (`stackName: vita-dev-hub-ingest`), sin el `02-` que usa el backlog: esa
numeración venía de los YAML de `cloud-formation-arch/`, y los stacks de la app CDK no la llevan
(`network`, `watch-persistent`, `watch-ingest`).

`bin/vita.ts` lo instancia **siempre**, como `network` y `watch-persistent` — no lleva puerta de
contexto como el `-c imageUri` de `watch-ingest`, porque no depende de ninguna imagen.

Los nombres físicos de §5.2 salen todos de `prefix(cfg)`; aparecen aquí con el prefijo `vita-dev-`
por concreción, no cableados.

`watch-ingest` se destruye cada sesión porque quema dinero en reposo (NLB, ECS, y sobre todo el
NAT Gateway de `network`, ~33 USD/mes). `hub-ingest` no toca ninguno de los tres: **el hub se
conecta desde el hogar por internet directamente a IoT Core, sin pasar por la VPC**. En reposo
cuesta 0 y no depende de `network`, así que apagarlo no ahorra nada y solo añade una forma de
romperlo. Queda fuera de `destroy-all.sh`, con la misma nota explícita que ya lleva
`watch-persistent`.

Eso además esquiva la trampa documentada en `lib/watch-persistent-stack.ts`: un recurso `RETAIN`
dentro de un stack que se destruye sobrevive y hace fallar el despliegue siguiente por nombre
duplicado. Si el stack no se destruye nunca, la tabla puede llevar `RETAIN` sin ese riesgo.

### 5.2 Recursos

| Recurso | Detalle |
|---|---|
| `AWS::DynamoDB::Table` `vita-dev-hub-events` | PK `hub_id` (S), SK `sk` (S), `PAY_PER_REQUEST`, PITR activado, TTL sobre `expires_at`, `RemovalPolicy.RETAIN` |
| `AWS::IoT::Policy` `vita-dev-hub-policy` | `iot:Connect` solo sobre `client/${iot:Connection.Thing.ThingName}`; `iot:Publish` solo sobre `topic/vita/hub/${iot:Connection.Thing.ThingName}/events`. Ni `Subscribe` ni `Receive`. |
| `AWS::IoT::TopicRule` `vita_dev_hub_events` | SQL de §5.3, acción DynamoDBv2 con rol importado, `errorAction` a CloudWatch Logs |
| `AWS::Logs::LogGroup` `vita-dev-hub-ingest-errors` | Destino del `errorAction`; retención de `cfg.logRetentionDays` |

Exports nuevos en `EXPORT_SUFFIXES`: `HubEventsTableArn`, `HubEventsTableName`,
`HubIotPolicyName`.

El endpoint de datos ATS **no es un recurso de CloudFormation** y por tanto no puede ser un
export: lo resuelve `provision-hub.sh` con `aws iot describe-endpoint --endpoint-type iot:Data-ATS`.

### 5.3 La regla

```sql
SELECT topic(3) AS hub_id,
       schema_version, camera_id, camera_name, type, severity, timestamp, payload,
       concat(timestamp, '#', camera_id, '#', type) AS sk,
       floor(timestamp() / 1000) + 7776000 AS expires_at
FROM 'vita/hub/+/events'
```

> **Corregido el 2026-08-21, durante la implementación.** Este spec decía originalmente
> `SELECT *, concat(...)`, y la revisión final de la rama encontró el agujero: `hub_id` es la
> clave de partición de la tabla, y con el comodín salía del **payload** — que el emisor
> controla — en vez del topic. La policy de IoT acota el *topic* al thing que se conecta, pero
> nada acotaba el *campo*: quien tuviera el certificado de una casa podía publicar en su propio
> topic autorizado con `{"hub_id": "otro-hogar"}` y escribir en la partición de otra familia,
> mientras §3 de este mismo documento afirmaba que eso era imposible.
>
> Se descartó el arreglo mínimo (`SELECT *, topic(3) AS hub_id`) porque depende de que el alias
> **sobreescriba** el campo del payload, y AWS no documenta esa regla en ningún sitio: todos los
> ejemplos de `SELECT *, <expr> AS <name>` de la referencia oficial añaden una clave nueva,
> ninguno pisa una existente. Una propiedad de seguridad no se apoya en comportamiento no
> documentado.

**`hub_id` sale del topic, no del mensaje.** `topic(3)` es el segmento que IoT ya autenticó
contra el certificado al aceptar la conexión. Por eso **no** se usa `SELECT *`: sin enumerar,
el `hub_id` del payload entraría en la tabla.

**El precio, que hay que recordar:** si el evento gana un campo de primer nivel, hay que añadirlo
a este SELECT o no llegará a la tabla. Los ocho campos enumerados son el contrato de §3, y
`schema_version` existe para señalar el momento en que cambie.

**La clave de ordenación es compuesta, no solo `timestamp`.** Dos cámaras del mismo hub pueden
emitir en el mismo instante ISO y una se comería a la otra en silencio.

**El TTL cuenta desde la llegada, no desde el `timestamp` del evento.** `timestamp()` es la hora
del servidor. El reloj del Jetson puede ir mal —es un aparato doméstico que arranca tras cortes de
luz— y un timestamp corrido metería registros eternos o los borraría al instante. 90 días.

Dos detalles con dientes:

- **Los nombres de TopicRule no admiten guiones** (solo alfanuméricos y `_`). La regla es
  `vita_dev_hub_events` y rompe la convención de prefijo del resto del repo. Lleva comentario en
  el código para que nadie lo "arregle".
- **El `errorAction` no es adorno.** Sin él, una escritura fallida en Dynamo se descarta en
  silencio y el hogar deja de reportar sin que nada lo señale — exactamente el modo de fallo que
  este proyecto persigue en todas partes.

### 5.4 La dependencia del área de admin

El rol que la regla asume para escribir en Dynamo es `AWS::IAM::*`, prohibido en este repo (para
que baste `PowerUserAccess`). Se importa por ARN desde SSM `/vita/dev/hub/topic-rule-role-arn`,
igual que los dos roles de ECS.

Hay que añadir a `docs/peticion-area-admin.md` un bloque nuevo con:

1. El rol `vita-dev-hub-topic-rule-role`: confianza `iot.amazonaws.com`; permisos
   `dynamodb:PutItem` sobre el ARN de la tabla y `logs:CreateLogStream`/`logs:PutLogEvents` sobre
   el log group de errores.
2. El parámetro SSM con su ARN.
3. El `iam:PassRole` para quien despliega `vita-dev-hub-ingest`, mismo patrón que el bloque 4.

**Sin esto el stack falla con `AccessDenied` en el `deploy`, no en la síntesis.**

### 5.5 `scripts/provision-hub.sh <hub_id>`

Se ejecuta una vez por hogar. Comprueba cuenta y perfil como hace `deploy-ingest.sh`, y luego:

1. `aws iot create-thing --thing-name <hub_id>`
2. `aws iot create-keys-and-certificate --set-as-active`
3. Guarda `certificate.pem.crt` y `private.pem.key` en un directorio local **gitignored**
4. Descarga la Amazon Root CA 1
5. `aws iot attach-policy` + `aws iot attach-thing-principal`
6. Resuelve el endpoint ATS e imprime las líneas exactas a ejecutar en el Jetson

Todo cae dentro de `PowerUserAccess` — `attach-policy` aquí es policy de IoT, no de IAM — así que
**este script no depende del área de admin**. Documenta también la revocación
(`aws iot update-certificate --new-status REVOKED`), que es lo que se ejecuta si un Jetson se
pierde o lo roban.

La clave privada solo se puede descargar en el momento de crearla: si se pierde, no se recupera,
se emite otra y se revoca la anterior. El script lo dice en pantalla.

### 5.6 Tests (vitest)

En la línea de los que ya hay:

- La tabla con su nombre, clave, TTL, PITR y `RETAIN`.
- **La policy acotada al literal `${iot:Connection.Thing.ThingName}`.** Esta aserción *es* toda la
  garantía de aislamiento entre hogares: si alguien la relaja a `*`, el test debe ponerse rojo.
- La policy **sin** `iot:Subscribe` ni `iot:Receive`.
- La regla apuntando a `vita/hub/+/events` y con `errorAction` presente.
- Cero recursos `AWS::IAM::*` en la plantilla.
- Los exports nuevos, en `exports.test.ts`.

### 5.7 Documentación

`docs/architecture/02-hub-ingest.md` (nuevo — el fichero de documentación **sí** conserva la
numeración de la carpeta, es el stack el que no la lleva en el nombre), fila en la tabla de
contenido del `README.md`, nota en `destroy-all.sh` sobre lo que sobrevive, `bin/vita.ts`,
`lib/exports.ts`, y el bloque nuevo de `peticion-area-admin.md`.

## 6. Coste

A este volumen es ruido: IoT Core factura por mensaje (~1 USD por millón en `eu-west-1`) y
DynamoDB on-demand por escritura (~1,25 USD por millón). Un hogar con dos cámaras genera del orden
de decenas de eventos al día. El stack **no enciende ningún NAT Gateway ni ninguna tarea ECS**,
que es donde está el dinero de esta cuenta.

## 7. Verificación

En CI, sin red: la suite de `pytest` del hub y la de `vitest` del repo de arquitectura.

Extremo a extremo, manual y en este orden — cada paso aísla un lado:

1. **Solo AWS**: `mosquitto_pub` con el certificado de un hub de prueba publica un JSON de
   ejemplo → aparece el ítem en la tabla. Verifica stack, regla, rol y policy sin tocar el hub.
2. **Certificado equivocado**: publicar en el topic de *otro* `hub_id` con ese mismo certificado →
   la conexión se rechaza. Verifica el aislamiento entre hogares, que es la garantía central.
3. **Hub real**: `docker compose up` con `uplink.enabled: true` y una cámara → los eventos
   `person_detected` aparecen en la tabla y **siguen apareciendo en `docker logs`**.
4. **Corte de red**: desconectar el enlace del Jetson → el hub sigue detectando y logueando, sin
   reiniciarse; al volver la red, los eventos nuevos vuelven a llegar (los del corte, no — es la
   decisión de §2).

## 8. Al backlog al terminar

- **Cola en disco.** Lo que no sale se pierde. Duele en la línea base personal: un hueco de tres
  horas no es solo un dato perdido, es una rutina mal aprendida. Es el primer candidato del
  siguiente slice de uplink.
- **Downlink** para el rescan remoto (el backlog #3 ya anota que la costura está lista:
  `RescanService.run_once()`).
- **Eventos de presencia de IoT** para `hub_online`/`hub_offline`, que es lo que cierra el punto 4
  del backlog (supervisión desde la nube de un hub colgado).
- **El estado del `ConnectionMonitor` sigue solo en memoria.** El backlog ya avisa de que esto
  pasa a importar "en cuanto exista el uplink a AWS" — y a partir de este slice, existe. Un
  episodio `camera_unreachable` que nunca se cierra ahora llega a una tabla que alguien va a
  consultar.
- **Archivo en S3** y consulta analítica, cuando haya consumidor.
- **Fleet Provisioning**, cuando los hogares dejen de contarse con los dedos.
