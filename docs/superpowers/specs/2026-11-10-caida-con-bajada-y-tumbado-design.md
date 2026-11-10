# Diseño — Caída solo con bajada, aviso de «tumbado» y personas cortadas por el borde

- Fecha: 2026-11-10
- Estado: pendiente de aprobación del usuario
- Repos: `vita-docker-process-hub` (este) y `vita-docker-rule-engine`
- Specs relacionadas: `2026-08-22-deteccion-caidas-design.md`,
  `2026-11-09-inactividad-persona-design.md`

## 0. El problema

El 2026-11-10, en el hub piloto, el usuario estaba sentado delante del escritorio, justo
debajo de la cámara. Solo se le veían la cabeza y los hombros, cortados por el borde inferior
del fotograma. El hub emitió dos `fall_detected` y el rule engine abrió dos alertas críticas,
sin que hubiera ninguna caída.

Esto es lo que ocurrió:

- La caja de la persona salió mucho más ancha que alta (`bbox_ratio` ≈ 2.3), porque solo
  incluía cabeza y hombros.
- No se veían las caderas, así que no había ángulo de torso (`torso_angle: null`). El motor
  aplicó la regla de reserva: una caja más de 1.2 veces más ancha que alta cuenta como
  tumbado.
- La cabeza quedaba en el tercio inferior de la caja, así que contó como cabeza baja
  (`head_low`).
- Tras 2 s «en el suelo» y con un score ≥ 0.3, el motor confirmó la caída.
- La bajada máxima fue de 0.11–0.17 alturas de caja por segundo, por debajo del 0.3 en el
  que el score empieza a contarla. Es decir, no hubo bajada brusca.

## 1. Decisiones del usuario

1. **Una caída exige un cambio brusco de altura.** Estar tumbado no implica haberse caído.
2. **Estar tumbado sin caída es un aviso de un nivel menos que la caída:** severidad `high`,
   frente a `critical` de la caída.
3. **El aviso de tumbado solo se da de día.**
4. **Se avisa tras más de 30 s tumbado sin moverse.**
5. **La alerta de tumbado se cierra sola cuando la persona se levanta.**
6. Hay que corregir el caso de la persona cortada por el borde de la imagen (propuesta del
   asistente, aceptada por el usuario).

## 2. Objetivos, no objetivos y anti-objetivos

Objetivos:

- **Caída:** `fall_detected` solo se emite si hubo bajada brusca antes de quedar tumbado.
- **Tumbado sin bajada:** se convierte en un episodio nuevo con eventos `lying_detected` y
  `lying_resolved`. El rule engine abre una alerta `high` si dura más de 30 s dentro de la
  ventana de día, y la cierra cuando la persona se levanta.
- **Persona cortada:** una caja que toca el borde del fotograma sin torso visible no cuenta
  como tumbada ni como de pie, ni para caídas ni para inactividad.

No objetivos:

- **Zonas de exclusión** (sofá, cama). Siguen en el backlog. Este slice reduce el problema,
  pero no lo elimina: una persona tumbada en el sofá de día dará el aviso `high` a los 30 s,
  y así lo ha pedido el usuario.
- **Calibrar con vídeo real.** Los umbrales nuevos se calibran después con
  `replay_video.py`.
- **Clip de vídeo en el aviso de tumbado.** Decisión del usuario: sin clip por ahora. Los clips
  siguen saliendo solo con `fall_detected`.
- **Cambiar la app familiar.** La alerta nueva aparece como un tipo más, igual que
  `camera_inactivity`.

Anti-objetivos:

- **Perder una caída real.** Si una caída real no tiene bajada suficiente (por ejemplo, alguien
  que resbala despacio de la silla al suelo), debe acabar como mínimo en el aviso de tumbado.
  Nunca en nada.
- **Que la persona cortada deje de detectarse del todo.** Presencia, identidad y seguimiento
  siguen funcionando. Solo se deja de inferir la postura.

## 3. Hub — caída con bajada obligatoria (`analytics/fall_engine.py`, `fall_signals.py`)

- Constante nueva en `fall_signals.py`: `MIN_FALL_DROP`, en alturas de caja por segundo. Valor
  inicial 0.3, igual que `_DROP_0`. *(Decisión del asistente.)* Se calibra con `replay_video.py`
  dentro de 0.2–0.4. En el incidente la bajada fue de 0.11–0.17.
- Al confirmar el episodio en estado `candidate`, el código actual pide
  `horizontal and floor_time >= CONFIRM_FLOOR_S and score >= min_score`. Ahora pide además
  `peak_drop is not None and peak_drop >= MIN_FALL_DROP`.
- Si se cumple todo lo demás pero falta la bajada, el episodio pasa a ser de **tumbado**
  (§4), no de caída.
- El score y su payload no cambian. `fall_*` mantiene su contrato actual.

## 4. Hub — episodio de tumbado (en el mismo `FallEngine`)

*(Decisión del asistente.)* El episodio de tumbado vive en el mismo motor que la caída. Así
comparten historial, postura y `peak_drop`, y una misma pista nunca está a la vez en una caída
y en un tumbado.

Estados por pista: `upright → candidate → reported` (caída) o `candidate → lying` (tumbado).

- **Entrada.** En `candidate`, con `horizontal` y `floor_time >= CONFIRM_FLOOR_S`, el episodio:
  - pasa a `reported` y emite `fall_detected`, como hoy, si hay bajada suficiente y el score
    llega al mínimo;
  - pasa a `lying` y emite `lying_detected` de inmediato si no hay bajada suficiente;
  - con bajada suficiente pero score por debajo del mínimo, sigue en `candidate` mientras la
    permanencia en el suelo sube el score: si llega al mínimo antes de
    `CONFIRM_FLOOR_S + FALL_SCORE_WAIT_S` (3 s extra), emite `fall_detected`; si no, pasa a
    `lying`. *(Decisión del asistente.)* Una caída real con score justo por debajo del mínimo a
    los 2 s no puede quedarse en un tumbado, y nada que cumpla «tumbado ≥ 2 s» se queda sin
    evento. Ojo: `peak_drop` se pone a cero al volver a estar de pie, así que una caída lenta
    real acaba en tumbado, que es lo que busca el anti-objetivo.
- **Salida.** En `lying`:
  - sin postura horizontal (de pie u `other`; la muestra ciega por el borde es neutra y no
    cuenta, ver §5) durante
    `CONFIRM_UPRIGHT_S` seguidos: emite `lying_resolved` con `reason: "upright"`, que aquí
    significa «ya no está tumbada». Sentarse en la cama también cierra el episodio.
    *(Decisión del asistente.)* Si no, un episodio podría quedarse abierto para siempre en
    `other`;
  - pista perdida: emite `lying_resolved` con `reason: "track_lost"`.
- **Sin escalada.** Un episodio `lying` no se convierte después en caída: la bajada, si la
  hubo, ya pasó. *(Decisión del asistente.)* El aviso de tumbado cubre ese caso.
- **Cadencia.** Mientras dura el episodio, el hub emite `lying_update` cada `LYING_UPDATE_S`
  (60 s) con `{episode_id, person}`. Así el rule engine sabe que el hub sigue vivo y no da el
  episodio por huérfano, también en episodios largos o nocturnos. *(Decisión del asistente.)*
  Los 30 s del aviso los cuenta el rule engine desde `lying_detected`.
- **Moverse estando tumbado.** «Sin moverse» se interpreta como seguir tumbado. Un giro en la
  cama no cierra el episodio; levantarse sí. *(Decisión del asistente:* reutiliza la postura
  que el motor ya calcula, sin añadir un criterio de sitio. Se puede revisar con datos
  reales.)

Eventos (mismo sobre, mismo canal):

- `lying_detected` (severity `"high"`):
  `{episode_id, signals, person_count, person: {id, confidence} | null}`
- `lying_update` (severity `"info"`): `{episode_id, person}`
- `lying_resolved` (severity `"info"`):
  `{episode_id, duration_s, reason: "upright" | "track_lost", person}`

`episode_id` usa el mismo formato y el mismo contador de la cámara que las caídas.

## 5. Hub — persona cortada por el borde del fotograma

- El tamaño del fotograma (ancho y alto) llega a los motores: `process_frame` lo saca de
  `frame.shape` y lo pasa a `FallEngine.observe` y a `InactivityEngine.observe` como argumento
  nuevo `frame_size: tuple[int, int] | None`. `None` significa desconocido y se comporta como
  hoy.
- Una caja está **cortada** si alguno de sus lados está a `EDGE_MARGIN_PX` o menos del borde
  correspondiente del fotograma. Valor inicial: 2 px, ajustable.
- **Caídas y tumbado.** Si la caja está cortada y no hay ángulo de torso (faltan hombros o
  caderas), la muestra es ciega y **neutra en todo estado** del motor: se añade al historial, pero
  no avanza ni reinicia ningún contador de postura, se conserva la postura anterior y nunca cierra
  un episodio de tumbado ni frena una candidata. (Decisión del asistente, tras revisión.) Con
  torso visible manda el ángulo, como hoy, aunque la caja esté cortada.
- **Inactividad.** Si la caja está cortada **por abajo** y no hay tobillos fiables, el punto
  de pies no es fiable. La muestra no cuenta ni como dentro ni como fuera del radio: el
  episodio sigue, pero ni avanza la confirmación de movimiento ni se actualiza la escala.
  *(Decisión del asistente.)* El tiempo de inactividad sigue corriendo, igual que durante una
  ocultación. Como `continue` se salta `_advance`, también se pospone la comprobación de emisión
  de `inactivity_started` durante el tramo ciego; sale con la primera muestra normal.
- La cabeza baja (`head_low`) con la caja cortada tampoco se fía, pero queda cubierta: sin
  postura horizontal no hay episodio.

## 6. Rule engine — regla `prolonged_lying`

- Se añaden `lying_detected`, `lying_update` y `lying_resolved` a `EventKind` y `KINDS`.
- Regla nueva `prolonged_lying` en `RuleId` y `RULES`, siguiendo el patrón de
  `fall_confirmed`. El estado guarda el episodio:
  `PersonState.lying?: Record<cameraId, { episodeId, cameraName, detectedAt, lastEventAt, holdsAlert? }>`.
  Es un episodio por cámara, como en la inactividad, y una sola alerta por persona.
  *(Decisión del asistente.)* La alerta se abre por el episodio más antiguo que cumpla las
  condiciones. Cuando termina el episodio que la sostiene, pasa a otro que siga tumbado y
  cumpla las condiciones, con un `annotate`. Si no queda ninguno, se cierra. Así nunca se
  cierra mientras otra cámara la sigue viendo tumbada.
- **`lying_update`:** refresca `lastEventAt` del episodio.
- **`lying_detected`:** guarda el episodio de esa cámara y devuelve `noop('pending')`. Si la
  cámara ya tenía un episodio con otro id, el nuevo lo sustituye: el viejo no recibió su cierre
  y se resuelve como en el reinicio del hub.
- **Tick:** abre una alerta `high` cuando se cumple todo esto:
  - el episodio lleva al menos `lyingAlertSec` (30, nuevo parámetro configurable por persona);
  - el instante actual está dentro de `awakeWindow`;
  - no hay ya una alerta abierta.

  Texto de la alerta: «Se observa que lleva N segundos tumbada en {cámara} sin levantarse».
  Acción sugerida: llamar a la persona para comprobar cómo está.
- **`lying_resolved`:**
  - con la alerta abierta, `close` con `durationS` y `reason`, incluido `track_lost` (decisión
    del usuario 5);
  - sin alerta, el episodio sale del estado sin escribir en `alert`, con el mismo criterio que
    la inactividad.
- **Persona:** solo cuenta la persona del hub, con el mismo filtro `not_target` que
  `camera_inactivity`. *(Decisión del asistente.)* Con `person: null` (pista anónima) la regla
  hace `skip`. Motivo: avisar de cualquier persona tumbada de día, visitas incluidas, sería
  demasiado ruido para un aviso `high`.
- **Huérfanos:** gracias a `lying_update`, un episodio sin noticias del hub durante
  `fallOrphanMin` (10 min) sale del estado. Si
  tiene alerta, se anota, pero no se cierra, igual que la caída: que el hub calle no significa
  que la persona se haya levantado. *(Decisión del asistente.)*
- **App familiar:** no hace falta tocarla. La app no traduce los `rule_id`; solo trata aparte
  `fall_confirmed` para el clip. Comprobado en `vita-web-family-app/api/src/lib/alerts.ts`.
- **Noche:** si el episodio empieza de noche y la persona sigue tumbada al empezar la ventana
  de día, el primer tick dentro de la ventana abre la alerta. *(Decisión del asistente.)*

## 7. Despliegue

1. Rule engine primero. Mientras no se despliegue, los tipos `lying_*` se descartan con
   `unknown_kind`.
2. Después, el hub. No hace falta ningún flag nuevo, porque cuelga de `fall.enabled`. Con
   este cambio el hub emite menos caídas y añade los avisos de tumbado.

## 8. Lo que queda al builder

- Los valores iniciales de `MIN_FALL_DROP` y `EDGE_MARGIN_PX` dentro de lo indicado.
- La forma exacta de propagar `frame_size` por `process_frame` y por el replay. Las cajas del
  detector (`boxes.xyxy` de ultralytics) están en píxeles del fotograma original, el mismo
  espacio que `frame.shape`.
- Los textos de `explanation`, en el tono de las reglas existentes.
- La división en tareas entre los dos repos, respetando §7.
