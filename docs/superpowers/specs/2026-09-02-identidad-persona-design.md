# Diseño — Identidad de persona sobre pistas (reconocimiento facial local, v1)

- Fecha: 2026-09-02
- Estado: aprobado en brainstorming, pendiente de plan de implementación
- Repo: `vita-docker-process-hub`
- Slice anterior: `2026-08-22-deteccion-caidas-design.md`
- Abre: la identidad por persona que la sedestación prolongada (slice futuro) necesitará

## 0. El problema

El hub detecta personas y caídas, pero todas las personas son anónimas. La persona monitorizada
no vive necesariamente sola: recibe visitas o convive con más gente, y hoy una caída (o, en el
futuro, una sedestación prolongada) de cualquiera genera el mismo evento. Quien consume los
eventos no puede distinguir "se ha caído María" de "se ha caído alguien".

Este slice añade al hub la capacidad de **etiquetar los eventos de caída con la identidad de la
persona** (`person: {id, confidence}` o `null`), reconociéndola por la cara **en local**. El hub
sigue sin decidir nada: **etiqueta, no filtra**. Si una visita se cae, el evento sale igual (con
`person: null` o con otra identidad); decidir a quién alertar y por qué persona sigue siendo del
sistema futuro en AWS.

## 1. Alcance

Dentro:

- `Tracker` propio: el emparejamiento entre frames que hoy vive dentro de `FallEngine` sale a un
  módulo compartido que asigna `track_id` y porta la identidad de la pista.
- `FaceIdentifier`: detección de cara + embedding (InsightFace `buffalo_s` por `onnxruntime`),
  comparado contra una galería local de personas enroladas.
- Enrolamiento por fichero: fotos en `/data/faces/<person_id>/`, embeddings calculados al arrancar.
- Campo nuevo `person` en el payload de `fall_detected` / `fall_update` / `fall_resolved`.
- Config `inference.identity` (apagado por defecto).

Fuera, con motivo:

- **Filtrar eventos por identidad.** Un error de reconocimiento silenciaría una alerta real, y la
  caída de una visita también importa. Filtrar es del motor de reglas en AWS.
- **Evento `person_identified`.** Útil para la nube ("María vista en el salón"), pero no lo pide
  nadie todavía; se añadirá cuando el slice de sedestación lo necesite (YAGNI).
- **Sedestación prolongada.** Es el slice siguiente; nacerá ya con pistas identificadas.
- **Re-ID por apariencia corporal (ropa) — el "enfoque C".** Mantendría la identidad tras perder la
  pista sin volver a ver la cara. Se puede montar encima de este diseño si en campo la pérdida de
  identidad por oclusiones resulta un problema real.
- **Eventos de presencia con identidad.** `person_detected`/`person_count_changed` no cambian: el
  `EventEngine` trabaja con conteos y no se toca.
- **Enrolamiento remoto o con UI.** Las fotos llegan por el volumen `/data`, como la config. Un
  mecanismo de enrolamiento desde la nube necesitaría el downlink que no existe (backlog).

## 2. Decisiones cerradas en brainstorming

| Decisión | Elegido | Motivo |
|---|---|---|
| Filtrar vs etiquetar | Etiquetar, no filtrar | La caída de una visita también importa; un fallo de identidad no debe silenciar una alerta. Coherente con "el hub produce el dato, AWS decide" |
| Método de identidad | Reconocimiento facial local | Lo más fiable a medio plazo; los embeddings nunca salen del hogar |
| Descartado: solo heurísticas sin biometría | — | Justo con visitas —el caso que motiva el slice— no distingue a nadie |
| Descartado: identificar solo al disparar el evento | — | En una caída la cara casi nunca se ve (persona en el suelo, baja resolución): saldría `unknown` cuando más importa |
| Momento de identificación | Oportunista, sobre la pista | La cara se ve al entrar/andar/sentarse; la etiqueta viaja con la pista hasta la caída |
| Alcance | Solo identidad; sedestación después | Slices pequeños, como el resto del proyecto |
| Evento `person_identified` | No en v1 | YAGNI; se añade cuando la sedestación lo pida |

## 3. Arquitectura y encaje

Se extrae el tracking de `FallEngine` y la identidad se monta sobre las pistas:

```
frame ─► PosePersonDetector ─► [Detection] ─► Tracker ─► [(Track, Detection)]
                                                │              │
                                                │              ├─► FallEngine ─► fall_* (con person)
                                                │              │
                                     FaceIdentifier            └─► (futuro: sedestación)
                                  (etiqueta pistas sin
                                   identidad, con frame)
             person_count ──────────────────────────────────► EventEngine ─► person_* (sin cambios)
```

**Nuevo**

| Pieza | Responsabilidad |
|---|---|
| `analytics/tracker.py` — `Tracker` | Por cámara: caducar pistas (TTL 3 s, avisando de las perdidas), emparejar detecciones con pistas por IoU y respaldo por cercanía de centros (la lógica actual de `FallEngine._match`, movida tal cual), asignar `track_id` estable y portar `identity: TrackIdentity \| None`. Devuelve los pares `(Track, Detection)` del frame y las pistas perdidas. |
| `identity/face_id.py` — `FaceIdentifier` | Con el frame y las pistas **sin identidad**: recorta la región de la persona, detecta la cara (SCRFD), calcula el embedding (ArcFace) y lo compara por similitud coseno con la galería. Dos coincidencias en frames distintos con el mismo `person_id` y similitud ≥ umbral → la pista queda etiquetada hasta perderse. Como máximo un intento por segundo y por pista sin identificar; caras < 40 px de alto no se intentan. |
| `identity/gallery.py` | Carga `/data/faces/<person_id>/*.jpg` al arrancar y calcula un embedding medio por persona. Sin caché: recalcular en cada arranque tarda segundos y hace que añadir/quitar fotos sea solo reiniciar. |
| Config `inference.identity` | `enabled: bool = false`, `match_threshold: float = 0.4`. |
| `VITAHUB_FACE_WEIGHTS` | Ruta de los modelos ONNX de InsightFace, embebidos en la imagen como los de YOLO (arranca sin internet). |

**Tocado**

| Pieza | Cambio |
|---|---|
| `analytics/fall_engine.py` | Deja de emparejar: `observe()` recibe los pares `(Track, Detection)` y las pistas perdidas del `Tracker`. El estado de episodio (`upright/candidate/reported`, señales, score) se queda como está, colgado de la pista. Los `fall_*` incluyen `person` desde la identidad de la pista. |
| `worker.process_frame` | Orquesta: detect → track → (si identidad activa) identify → fall/presencia. El identificador va en `try` propio con log una-vez-por-cámara, mismo patrón que la analítica de caídas: la identidad nunca tumba la detección. |
| `factory.py` | Construye `Tracker` (siempre que haya `fall_engine`) y `FaceIdentifier` + galería si `identity.enabled`. |
| `config.load_config` | Parsea `inference.identity`. `enabled: true` sin fotos en `/data/faces/`, sin pesos, o con `inference.fall.enabled: false` → `ConfigError` legible (fail-fast: es un error de instalación y el técnico está delante). En v1 la identidad solo etiqueta eventos `fall_*`, así que sin caídas activas no tiene efecto y encenderla sería un error de configuración. |
| `scripts/replay_video.py` | Acepta la galería para calibrar `match_threshold` con vídeos reales, como se calibró el score de caídas. |

## 4. Comportamiento de la identidad

- **Pegajosa por pista:** una vez etiquetada, la pista conserva la identidad hasta caducar. No se
  reevalúa en cada frame: el coste se paga solo mientras la pista es anónima.
- **Corrección:** si una pista ya etiquetada recibe más adelante un match claramente mejor de otra
  persona (similitud mayor que la registrada), se reemplaza la etiqueta y se loguea a stderr. Cubre
  el cruce de pistas en escenas con varias personas, la limitación conocida del matching greedy.
- **Pérdida:** pista caducada = identidad perdida. Quien reaparece tras una oclusión larga es
  `unknown` hasta que se le vuelva a ver la cara. Limitación asumida de v1; la cubriría el re-ID
  corporal (enfoque C) si en campo duele.
- **`unknown` es un resultado válido, no un error:** visitas y convivientes no enrolados salen como
  `person: null` siempre.

## 5. El evento

Mismo envelope y mismo `schema_version: 1` — añadir un campo al payload es retrocompatible:

```json
{"type": "fall_detected", "severity": "high",
 "payload": {
   "episode_id": "onvif-123-1724321702-1",
   "score": 0.82,
   "signals": {"torso_angle": 74.1, "...": "..."},
   "person_count": 2,
   "person": {"id": "maria", "confidence": 0.87}
 }}
```

- `person` aparece en `fall_detected`, `fall_update` y `fall_resolved` (en los tres: la identidad
  puede llegar después del primer evento, si la cara se ve más tarde).
- `person: null` cuando la pista no está identificada (identidad apagada, persona no enrolada o
  cara nunca vista).
- `confidence` es la similitud coseno del match que etiquetó la pista, redondeada a 3 decimales.
- **Privacidad:** del hogar sale solo el `person_id` (un alias elegido al enrolar) y un número.
  Fotos y embeddings viven en `/data` y no se emiten, no se loguean y no se suben.

## 6. Errores y aislamiento

- Fallo del `FaceIdentifier` en marcha (modelo que lanza, frame corrupto): se traga en el worker y
  se loguea una vez por cámara; caídas y presencia siguen. Sin identidad, los eventos salen con
  `person: null` — degradación silenciosa hacia el comportamiento actual.
- Fallo al construir (pesos ilegibles, galería vacía con `enabled: true`): `ConfigError` en el
  arranque, como el uplink.
- La extracción del `Tracker` no cambia ninguna constante ni umbral del emparejamiento: los valores
  (`IOU_MATCH`, `CENTER_MATCH`, `TRACK_TTL_S`) se mueven con el código.

## 7. Tests

- Los tests de emparejamiento y caducidad de `FallEngine` se mueven a tests de `Tracker` sin
  cambiar lo que verifican; los de la máquina de estados de episodio se adaptan a la nueva firma.
- `StubFaceIdentifier` (como `StubDetector`): devuelve identidades programadas, permite probar en
  `worker` y `FallEngine` el etiquetado, la regla de los 2 frames, la corrección y la pérdida de
  identidad sin modelo real.
- El matching de embeddings (similitud coseno contra la galería, umbral, media por persona) son
  funciones puras con tests directos.
- Test de integración del worker: caída de pista identificada → `fall_detected` con `person`;
  caída de pista anónima → `person: null`; excepción del identificador → los `fall_*` salen igual.
- Calibración de `match_threshold` con `scripts/replay_video.py` sobre vídeos reales antes de
  encenderlo en un hogar.

## 8. Limitaciones conocidas de v1

- **Resolución del substream:** de lejos la cara puede quedar bajo los 40 px y no identificarse
  hasta que la persona se acerque. Mitigación operativa: cámara bien situada; si no basta, evaluar
  `stream: main` solo cuando la identidad esté activa (coste CPU, decidir con datos de campo).
- **Oclusión > 3 s pierde la identidad** hasta volver a ver la cara (ver §4).
- **Escenas concurridas:** el matching greedy puede cruzar pistas; la regla de corrección de §4 lo
  amortigua, pero el tracker real (ByteTrack) sigue en el backlog.
- **Sin identidad en eventos de presencia:** decidido fuera de alcance; se revisará con la
  sedestación.
