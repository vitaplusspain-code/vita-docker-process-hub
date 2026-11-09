# Diseño — Inactividad de la persona objetivo por cámara

- Fecha: 2026-11-09
- Estado: pendiente de aprobación del usuario
- Repos: `vita-docker-process-hub` (este) y `vita-docker-rule-engine`
- Slice anterior: `2026-09-02-identidad-persona-design.md`, que ya nombraba la «sedestación
  prolongada» como siguiente paso sobre pistas identificadas

## 0. El problema y el porqué

Queremos saber cuánto tiempo pasa la persona monitorizada sin hacer nada: sentada o de pie, pero
sin cambiar de sitio. Hoy la única señal de inactividad es la del reloj
(`prolonged_inactivity` en el rule engine, por pasos sin cambiar). La spec del rule engine dice
«las cámaras no participan». Si la persona no lleva el reloj, o lo lleva puesto pero no anda,
no hay señal.

El hub ya reconoce a la persona por la cara y la sigue con una pista. Este slice usa esa pista
para medir episodios de inactividad y mandarlos a AWS. En AWS, los eventos quedan en DynamoDB y
el rule engine avisa al cuidador cuando un episodio se alarga demasiado.

## 1. Decisiones del usuario

Lo que sigue lo decidió el usuario en el brainstorming:

1. **«Sin hacer nada» = no cambiar de sitio.** Mover manos o cabeza, o acomodarse, no cuenta como
   actividad.
2. **Pasar de sentada a de pie (o al revés) en el mismo sitio no corta el episodio.** Sigue siendo
   «sin hacer nada».
3. **Solo se envía algo a AWS si el episodio pasa de 5 minutos.** Los episodios más cortos no se
   envían.
4. **Al terminar un episodio enviado se envía también la duración total.**
5. **El tracker debe funcionar para esto sin depender de la detección de caídas.**
6. **Hay que resolver la pérdida de pista por ocultaciones cortas** (alguien que pasa delante
   unos segundos), con una solución mejor que el TTL fijo de 3 s.
7. **El cambio en el rule engine entra en este trabajo**, con este comportamiento (propuesta del
   asistente, aceptada por el usuario):
   - solo se abre una alerta `medium` si el episodio sigue abierto más de un umbral por persona,
     dentro de la ventana de vigilia;
   - el fin del episodio cierra esa alerta;
   - **solo los episodios que llegaron a alerta quedan en el historial** (tabla `alert`). Los
     demás quedan solo en DynamoDB `hub-events`. El usuario lo eligió para no inflar el informe
     semanal ni la lista «Todas» de la app familiar, que cuentan cualquier fila de `alert`.

## 2. Objetivos, no objetivos y anti-objetivos

Objetivos:

- El hub detecta episodios de inactividad de personas identificadas y emite
  `inactivity_started` (al llegar a 5 min) e `inactivity_ended` (al cerrar, con duración total).
- El tracker existe si está activa la detección de caídas **o** la de inactividad.
- Una persona quieta que queda tapada unos segundos conserva su identidad y su episodio.
- El rule engine consume los dos eventos nuevos con una regla nueva `camera_inactivity`.

No objetivos:

- **Mostrar la inactividad por cámara en la app familiar o en el informe diario.** Ver §6.4.
- **Fusionar la señal de cámara con la del reloj** en una sola alerta. Son dos reglas
  independientes (§6.5).
- **Cola o reintento de eventos MQTT.** Los eventos siguen siendo QoS 0, sin cola, como el resto
  (spec de uplink §2). Si un evento se pierde, el rule engine lo absorbe (§6.3).
- **Distinguir la postura** (sentada o de pie) en el evento. Al usuario le da igual.
- **Tracker real (ByteTrack).** Sigue en el backlog. Este slice solo añade el «aparcado» de
  identidad (§4).

Anti-objetivos (fallos aunque el código funcione):

- **Alertar al cuidador por cada rato de sofá.** Un episodio de 5 min nunca abre alerta por sí
  solo.
- **Atribuir la inactividad de una visita a la persona monitorizada.** El rule engine solo acepta
  episodios cuya identidad es la persona del hub (§6.1).
- **Que la inactividad tumbe la presencia o las caídas.** Igual que hoy con caídas e identidad,
  un fallo del motor nuevo se loguea una vez y no afecta al resto del frame.

## 3. Hub — motor de inactividad (`analytics/inactivity_engine.py`)

### 3.1 Dónde se engancha

- `worker.process_frame` llama a un `InactivityEngine.observe(camera, update, now)` justo después
  de `FallEngine`, con el mismo `TrackerUpdate`. Lo hace dentro de su propio `try`, con log único
  por cámara, como `_fall_failure_logged`.
- La condición actual `if fall_engine is not None and tracker is not None` pasa a ser «si hay
  tracker». Dentro, cada motor corre solo si existe.
- `app.run` crea el tracker si `fall_engine` o `inactivity_engine` existen.

### 3.2 Configuración

`inference.inactivity` en el YAML, con el mismo patrón que `FallConfig`:

- `enabled: bool = False`
- `min_minutes: float = 5.0`: duración a partir de la cual se emite `inactivity_started`.

Requisito: `inactivity.enabled` exige `identity.enabled`. Sin identidad no hay persona objetivo.
Con `inactivity` activo e `identity` apagado, la carga de config falla con `ConfigError`, y el
mensaje lo explica.

Hoy `_identity_from_raw` exige `fall.enabled`. *(Decisión del asistente.)* La comprobación pasa
a ser «`fall` o `inactivity`». La inactividad funciona también con el detector `person_yolo`,
usando el borde inferior de la caja (§3.3). Hay que actualizar la frase de la spec de identidad
«en v1 la identidad solo etiqueta caídas».

El resto de umbrales son constantes en el módulo, como en `fall_engine.py`. Sus valores iniciales
los fija el builder y se calibran con `scripts/replay_video.py`.

### 3.3 Qué es «mismo sitio»

- **Punto de pies.** Si hay keypoints COCO-17 (detector `person_pose`) y los tobillos tienen
  confianza suficiente, se usa el punto medio de los tobillos. Si no, el centro del borde
  inferior de la caja. Este punto apenas se mueve al sentarse o levantarse, y por eso cumple la
  decisión 2. El centro de la caja no la cumple, porque sube al levantarse.
- **Ancla.** Es el punto de pies cuando empieza el episodio.
- **Radio.** El episodio se corta si el punto de pies se aleja del ancla más de
  `RADIUS_FACTOR × escala`. La escala es el lado mayor de la caja más grande vista en el
  episodio, así que el radio es proporcional al tamaño de la persona en la imagen. La caja más
  grande suele ser la de pie, y así sentarse no encoge el radio. El valor inicial
  `RADIUS_FACTOR ≈ 0.5` es una sugerencia y se calibra.
- **Antirrebote.** Un solo frame fuera del radio no corta el episodio, porque el detector tiene
  ruido. Hace falta estar fuera del radio de forma continuada durante unos segundos
  (`MOVE_CONFIRM_S`). El valor exacto lo fija el builder.

### 3.4 Ciclo de un episodio

El episodio es por pista **identificada** (`track.identity` no nulo). Las pistas anónimas no
generan episodios.

1. **Inicio candidato.** Una pista identificada sin episodio abre uno con ancla en su punto de
   pies y `started_at` = hora de pared del frame.
2. **Movimiento antes de 5 min.** El episodio se descarta sin emitir nada y se abre uno nuevo en
   el sitio nuevo cuando la persona vuelve a estar quieta.
3. **A los `min_minutes`.** Se emite `inactivity_started` con `started_at` real, no la hora del
   aviso.
4. **Cierre de un episodio ya emitido:**
   - `moved`: se confirma el movimiento fuera del radio. Si el evento final es por movimiento,
     `ended_at` es el último instante en que estaba dentro del radio, no el momento de la
     confirmación.
   - `track_lost`: la pista se pierde y su identidad aparcada (§4) caduca sin reanudarse.
     `ended_at` es la última vez que se la vio.
   - **Parada o reinicio del hub: no se emite cierre.** *(Decisión del asistente.)* Cada cambio
     de galería reinicia el hub. Si el reinicio cerrara el episodio, el cuidador recibiría un
     «resuelto» falso y el contador de 120 min volvería a cero para alguien que no se ha movido.
     El hub calla. Tras el reinicio, si la persona sigue quieta, emite un `inactivity_started`
     nuevo, y el rule engine lo trata como continuación (§6.3).
5. **Identidad que cambia a mitad de episodio.** Si el `FaceIdentifier` reetiqueta la pista con
   otra persona, el episodio de la persona anterior se cierra con `reason: "identity_changed"` y
   empieza uno nuevo para la nueva identidad.
6. **Un episodio por persona y cámara a la vez.** Una persona puede verse en dos cámaras. Cada
   cámara lleva su episodio y el rule engine decide (§6.2).

`episode_id` sigue el formato de caídas: `"{camera_id}-{epoch_s}-{n}"`.

### 3.5 Eventos

Mismo sobre (`models.Event`) y mismo canal MQTT que el resto. No hay campos nuevos en el sobre,
porque la regla IoT solo copia los campos que nombra en su `SELECT`.

`inactivity_started` (severity `info`):

```json
{
  "episode_id": "cam1-1762700000-3",
  "started_at": "2026-11-09T10:00:00+00:00",
  "still_s": 300.0,
  "person": {"id": "<person_key>", "confidence": 0.62}
}
```

`inactivity_ended` (severity `info`):

```json
{
  "episode_id": "cam1-1762700000-3",
  "started_at": "2026-11-09T10:00:00+00:00",
  "ended_at": "2026-11-09T11:42:10+00:00",
  "duration_s": 6130.0,
  "reason": "moved",
  "person": {"id": "<person_key>", "confidence": 0.62}
}
```

- `reason` es uno de `moved`, `track_lost` o `identity_changed`.
- `person` usa `_person_payload` igual que las caídas. En `inactivity_ended` es la identidad con
  la que se abrió el episodio.
- Las horas siguen el formato de `timestamp` (ISO 8601 con zona) y salen del mismo reloj
  inyectado (`clock`).

## 4. Hub — tracker sin depender de caídas y aparcado de identidad

### 4.1 Problema

`Tracker` caduca una pista a los 3 s sin verla (`TRACK_TTL_S`), y la identidad se pierde con
ella. Una persona sentada a la que tapa alguien 4 s vuelve como pista anónima. La identidad solo
vuelve cuando la cámara le ve otra vez la cara. Esto puede tardar mucho si está de espaldas o
mirando la tele de lado. Subir el TTL para todas las pistas no es la solución: con el
emparejamiento por cercanía de centros, un TTL largo cruzaría a personas que se mueven.

### 4.2 Solución: aparcar la identidad de pistas quietas

Esto vive en `Tracker`, así que también lo aprovechan las caídas.

- Cuando caduca una pista **identificada** que estaba **quieta** al perderse, el tracker guarda
  en una lista de aparcadas de esa cámara: su identidad, su última caja y la hora. «Quieta» quiere
  decir que su caja apenas se movió en los últimos segundos antes de perderla. El criterio exacto
  lo fija el builder.
- La identidad aparcada vive `PARK_TTL_S`. El valor propuesto es 120 s.
- Una pista **nueva y anónima** cuya primera caja solapa mucho con una aparcada vigente (IoU alto,
  más estricto que `IOU_MATCH`) **hereda** la identidad y la aparcada se consume. Si varias
  aparcadas valen, gana la de mayor IoU.
- **La identidad heredada es provisional.** *(Decisión del asistente.)* Hoy `FaceIdentifier`
  no vuelve a mirar caras si todas las pistas tienen identidad, y solo reetiqueta si la
  similitud nueva supera la confianza guardada. Por eso:
  - la pista heredada lleva una marca `provisional`;
  - su confianza baja a 0, así que cualquier coincidencia válida de otra persona la reetiqueta;
  - el identificador sigue intentándolo (cadencia actual, 1/s por cámara) mientras haya alguna
    pista provisional;
  - una coincidencia con la misma persona quita la marca.

  En los eventos, `person.confidence` de una pista provisional es la confianza original con la
  que se aparcó.
- `TrackerUpdate` informa de dos cosas al motor de inactividad:
  - qué pistas nuevas **reanudan** una identidad aparcada, con el `track_id` anterior;
  - qué aparcadas **caducan** sin reanudarse.

  El motor usa la primera para continuar el episodio y la segunda para cerrarlo con
  `track_lost`.
- El episodio de inactividad **sigue contando durante la ocultación** si la persona reaparece en
  el mismo sitio. El tiempo tapado cuenta como inactivo.

El tracker no tiene que saber nada de inactividad. Solo aparca, reanuda y caduca, y los motores
reaccionan.

### 4.3 Riesgo aceptado

Si la persona se levanta durante la ocultación y otra persona se sienta en el mismo sitio antes
de que la cara la desmienta, el episodio sigue con la identidad equivocada. Dura hasta que el
identificador ve la cara (§4.2, identidad provisional). Si la cámara nunca le ve la cara, el
error se mantiene. Queda documentado como límite conocido.

## 5. Hub — documentación y pruebas

- Se actualiza la spec de identidad (§3 desfasado, mención al slice) y `docs/backlog.md` con lo
  que se difiera.
- Las pruebas siguen el estilo del repo: dobles inyectados (`StubDetector`, `StubFaceEngine`),
  reloj y `now` inyectados, sin hilos. Hay que cubrir como mínimo:
  - episodio de menos de 5 min que no emite nada;
  - `started` a los 5 min con `started_at` real;
  - levantarse en el sitio, que no corta;
  - movimiento confirmado, que cierra con `moved` y `ended_at` correcto;
  - ruido de un frame, que no corta;
  - ocultación de 4 s con reanudación y el episodio intacto;
  - aparcada caducada, que cierra con `track_lost`;
  - reetiquetado, que cierra con `identity_changed`;
  - inactividad sin caídas activas;
  - config inválida (`inactivity` sin `identity`).
- La validación con vídeo real se hace con `scripts/replay_video.py`, que mostrará los episodios.

## 6. Rule engine (`vita-docker-rule-engine`)

### 6.1 Ingesta

- Se añaden `inactivity_started` e `inactivity_ended` a `EventKind` y `KINDS`
  (`src/core/events.ts`).
- La regla nueva solo acepta el evento si `payload.person.id` es la persona del hub:
  `personKey(personId)` = `person.id` en minúscula y sin guiones, igual que la galería. Con otra
  identidad, o con `person` nulo, la regla hace `skip('not_target')`. Este es el filtro de
  «persona objetivo».
- *(Decisión del asistente.)* Solo cuentan las identidades de la galería de la nube. Una carpeta
  creada a mano por un técnico (`/data/faces/<nombre>/`) produce `person.id = "<nombre>"` y
  nunca es objetivo. Un hub enrolado solo así no genera alertas de inactividad hasta que la
  persona tenga fotos desde la app. Queda en el README del rule engine.

### 6.2 Regla `camera_inactivity`

Se añade a `RuleId` y a `RULES`. El estado va en `PersonState.cameraInactivity`, indexado por
`episode_id`, porque puede haber episodios en dos cámaras a la vez.

- **`inactivity_started`.** Guarda el episodio: `startedAt`, `cameraName`, `cameraId` y
  `lastEventAt`. No abre alerta.
- **Tick.** Si algún episodio abierto lleva más de `cameraInactivityMin` desde `startedAt`, y el
  instante actual cae en `awakeWindow`, se abre una alerta `medium` con su `explanation` en el
  estilo de las demás reglas. Ejemplo: «Se observa que lleva N minutos sin moverse del sitio en
  {cámara}». Con `already_open` no se duplica. Si hay varios episodios, basta uno para abrir.
- **Una sola alerta por persona** (índice único de Aurora en `(person_id, rule_id)`).
  *(Decisión del asistente.)* El estado guarda qué episodio la sostiene. Si ese episodio termina
  y queda otro que ya supera el umbral, la alerta **pasa** a ese episodio con un `annotate` y
  sigue abierta. Así el cuidador no recibe un «resuelto» seguido de una alerta nueva minutos
  después. La alerta se cierra cuando termina el último episodio que la sostiene.
- **Cámara caída.** *(Decisión del asistente.)* Si `state.cameras[cameraId]` está `offline`
  (regla `camera_offline`), el tick no abre alerta por un episodio de esa cámara. Al pasar la
  cámara a `offline`, sus episodios se cierran:
  - con alerta abierta, `close` con `closedBy: 'camera_offline'`;
  - sin alerta, el episodio sale del estado sin escribir nada.

  Una cámara muerta no es evidencia de que la persona siga quieta.
- **`inactivity_ended`.**
  - Si su episodio tiene la alerta abierta, se hace `close` con `durationS` y `reason`.
  - Si no, `noop('below_threshold')`: no se escribe nada en `alert` (decisión del usuario, §1.7).
    El evento queda en DynamoDB.
  - En los dos casos el episodio sale del estado.
  - Un `ended` sin `started` previo (el `started` se perdió) es `skip('unknown_episode')`.
- **`reason: "track_lost"` con alerta abierta.** Se cierra igual. A diferencia de las caídas,
  dejar de ver a alguien quieto no es una urgencia. Se anota `trackLost: true` en el detalle.

Parámetros nuevos en `RuleParams` y `DEFAULTS`, que se pueden sobrescribir en
`care_plan.params`:

- `cameraInactivityMin = 120`
- `cameraInactivityMaxMin = 720` (§6.3)

### 6.3 Episodios huérfanos

Durante un episodio largo el hub no envía nada entre `started` y `ended`. Por eso no se puede usar
el silencio del hub como señal, como hace `fallOrphanMin`. Hay dos salidas:

- Un episodio abierto más de `cameraInactivityMaxMin` se cierra en el tick. Si tenía alerta,
  hace `close` con `closedBy: 'timeout'`. Si no, sale del estado sin escribir nada.
- Un `inactivity_started` nuevo **de la misma cámara** con otro `episode_id`, mientras el viejo
  sigue abierto (no llegó su `ended`), es una **continuación**. El caso típico es un reinicio del
  hub, que no emite cierre (§3.4). Se conservan `startedAt` y la alerta del viejo, y se cambia
  `episode_id` por el nuevo, para que el `ended` futuro case. *(Decisión del asistente.)*
  Riesgo aceptado: si se perdió un `ended` real y la persona volvió a quedarse quieta en esa
  cámara, el episodio se ve más largo de lo que fue. Lo acota `cameraInactivityMaxMin`.

### 6.4 Informe diario (fuera de este slice)

En el brainstorming se propuso que los episodios sumaran a los minutos de inactividad del informe
diario. Al revisarlo, hacerlo bien implica tres cosas:

- una columna nueva en `person_daily_summary`, con migración en `vitaplus-aws-architecture`;
- un cambio en la app familiar;
- resolver la doble cuenta con los minutos del reloj.

Queda diferido al backlog del rule engine. En este slice solo los episodios que llegaron a alerta
quedan en la tabla `alert`. Los demás quedan en DynamoDB `hub-events` (TTL 90 días), de donde un
informe futuro puede sacarlos.

### 6.5 Relación con `prolonged_inactivity`

Son reglas independientes. Si la persona lleva el reloj y está quieta en el sofá, puede haber dos
alertas `medium`, una por reloj y otra por cámara. Se acepta en v1. La fusión va al backlog.

### 6.6 Pruebas

Se siguen los tests existentes de las reglas. Hay que cubrir como mínimo:

- persona no objetivo, que da `skip`;
- `started` sin alerta antes del umbral;
- alerta en el tick tras el umbral y dentro de la ventana;
- fuera de la ventana, sin alerta;
- `ended` que cierra la alerta;
- `ended` sin alerta, que no escribe nada en `alert`;
- `ended` sin `started`, que hace `skip`;
- huérfano por `cameraInactivityMaxMin`;
- continuación por un `started` nuevo de la misma cámara (reinicio del hub);
- dos cámaras a la vez, con la alerta que pasa de un episodio a otro;
- cámara `offline`, que no abre alerta y cierra sus episodios.

Hay que actualizar la spec del rule engine, donde dice «las cámaras no participan», y su README.

## 7. Despliegue

- **Orden.** Primero se despliega el rule engine. Hasta entonces, un hub nuevo mandaría tipos que
  el rule engine descarta con `unknown_kind`. Eso no rompe nada, pero se pierde el dato.
- **Activación.** El hub viene con `inactivity.enabled: false` y se activa por hub.
- La regla IoT y DynamoDB no cambian, porque el `payload` pasa entero. Se tiene en cuenta que la
  `sk` de DynamoDB es `timestamp#camera_id#type`. `started` y `ended` tienen distinto `type`, así
  que no colisionan.

## 8. Lo que queda al builder

- Valores iniciales de `RADIUS_FACTOR`, `MOVE_CONFIRM_S`, del criterio de «pista quieta» al
  aparcar y del IoU de reanudación, calibrados con `replay_video.py`.
- Confianza mínima de tobillos para usar keypoints.
- La forma exacta de informar de reanudaciones y caducidades en `TrackerUpdate`.
- Los textos de `explanation` de la alerta, en el tono de las reglas
  existentes.
- La división en tareas y el orden entre los dos repos, respetando §7.
