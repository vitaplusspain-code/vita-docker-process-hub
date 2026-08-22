# Diseño — Detección de caídas por pose (subsistema C, v1)

- Fecha: 2026-08-22
- Estado: aprobado en brainstorming, pendiente de plan de implementación
- Repo: `vita-docker-process-hub`
- Slice anterior: `2026-08-21-uplink-eventos-aws-design.md`
- Abre: el "subsistema C" (visión avanzada) que el spec del primer slice dejó fuera

## 0. El problema

El hub sabe si hay alguien en la habitación y cuántos; no sabe si esa persona está en el suelo.
Para población vulnerable que vive sola, la caída es el evento que justifica todo el sistema: la
presencia es el cimiento, la caída es el producto.

Este slice añade al hub la capacidad de emitir un evento `fall_detected` con un **score de
fiabilidad** y las **señales que lo explican**, para que un sistema futuro en AWS decida a quién y
cuándo avisar. **El hub no notifica a nadie**; solo produce el dato.

## 1. Alcance

Dentro:

- Detector de persona con pose (`yolo11n-pose`) enchufado en la costura `Detector` existente.
- `FallEngine`: analítica temporal por persona, determinista, sin GPU, testable con reloj inyectado.
- Tres tipos de evento nuevos: `fall_detected`, `fall_update`, `fall_resolved`.
- Config `inference.fall` (apagado por defecto).
- Script de calibración offline sobre un vídeo grabado.

Fuera, con motivo:

- **Notificación a familiares/cuidadores.** La hará un sistema en AWS que aún no existe; aquí solo
  sale el evento con score.
- **Imágenes/snapshots.** Rompería el principio "al exterior solo sale texto". Se decidirá
  conscientemente en otro slice si hace falta.
- **Modelo entrenado de caídas (clasificador temporal).** No hay dataset de hogares reales. Los
  eventos de este slice, con sus señales, son precisamente el material para construirlo después.
- **Zonas de exclusión por cámara (sofá, cama).** Requiere configuración por cámara; va al
  backlog. En v1 la brusquedad de la transición es lo que discrimina tumbarse de caerse.
- **Tracker de personas real (ByteTrack, etc.).** Emparejamiento por IoU basta para 1-2 personas a
  2 fps; si en campo se queda corto, se cambia dentro de `FallEngine` sin tocar el contrato.

## 2. Decisiones cerradas en brainstorming

| Decisión | Elegido | Motivo |
|---|---|---|
| Consumidor del evento | Solo AWS, con score; sin notificar | Fase de datos; el filtrado por umbral lo hace el sistema futuro |
| Hardware | Jetson Orin, 3-4 cámaras | Cabe un modelo de pose a 2 fps por cámara |
| Contenido del evento | Score + señales explicativas, sin imagen | Permite recalibrar en AWS sin reentrenar nada en el hub |
| Enfoque | YOLO11n-pose + reglas temporales | Un solo modelo (da cajas y keypoints), señales interpretables, ajustable en campo |
| Descartado: solo geometría de caja | — | Poco fiable con cámaras cenitales/diagonales; señales pobres |
| Descartado: clasificador entrenado | — | Sin dataset; caja negra; es el "proyecto de investigación" que el primer spec dijo no bloquear |

## 3. Arquitectura y encaje

Se respeta la cadena actual: `Detector` → analítica → `EventSink`. El `EventEngine` de presencia
no se toca y sigue recibiendo exactamente lo mismo que hoy.

```
frame ─► PosePersonDetector ─► [Detection(+keypoints)] ─┬─► EventEngine  ─► person_* ─┐
                                                        └─► FallEngine   ─► fall_*   ─┴─► sink
```

**Nuevo**

| Pieza | Responsabilidad |
|---|---|
| `inference/person_pose_yolo.py` — `PosePersonDetector` | Envuelve `yolo11n-pose.pt`. Devuelve `Detection` de persona (caja + confianza, igual que `PersonDetector`) **más** `keypoints`. Mismo filtro de clase `person` y de confianza. `from_weights()` igual que el detector actual. |
| `analytics/fall_engine.py` — `FallEngine` | Por cámara: empareja personas entre frames, mantiene historial corto por pista, calcula señales y score, lleva la máquina de estados de episodio y produce eventos. Reloj inyectado como `EventEngine`. |
| Config `inference.fall` | `enabled: bool = false`, `min_score: float = 0.3`. |
| `scripts/replay_video.py` | Pasa un `.mp4` por detector + `FallEngine` + `StdoutJsonSink` a `sample_fps` e imprime los eventos. Para calibrar pesos con caídas simuladas antes de ir a un hogar. |

**Tocado**

| Pieza | Cambio |
|---|---|
| `models.Detection` | Campo nuevo `keypoints: tuple[tuple[float, float, float], ...] \| None = None` (17 puntos COCO `(x, y, conf)` en píxeles). `StubDetector` y `PersonDetector` lo dejan en `None`. |
| `inference/stub.py` | `StubDetector` acepta `keypoints` opcionales para que los tests de `worker` puedan inyectar una pose sintética. |
| `worker.process_frame` | Gana parámetro `fall_engine: FallEngine \| None`. Tras `engine.observe(...)`, si hay `fall_engine`, llama a `fall_engine.observe(camera, detections, now)` y emite sus eventos por el mismo sink. |
| `factory.build_detector` | Nuevo valor `detector: person_pose`. Error claro "pesos no encontrados en X" antes de invocar `YOLO()` (cierra el punto del backlog, para ambos detectores). |
| `config.load_config` | Parsea `inference.fall`. Si `fall.enabled` y `detector != person_pose` → `ConfigError("inference.fall requiere detector: person_pose")`. |
| `app.run` | Construye `FallEngine` solo si `fall.enabled` y lo pasa a `process_frame`. |
| `Dockerfile` | Añade `yolo11n-pose.pt` junto a `yolo11n.pt`. Variable `VITAHUB_POSE_WEIGHTS` (default `/app/models/yolo11n-pose.pt`), paralela a `VITAHUB_WEIGHTS`. |

Con `detector: person_yolo` y `fall.enabled: false` (los valores por defecto) el comportamiento del
hub es idéntico al actual. Con `detector: person_pose` se ejecuta **un solo modelo**: la presencia
sale de las mismas cajas que devuelve el modelo de pose.

## 4. Lógica de detección y score

### 4.1 Asociación de personas entre frames

Emparejamiento por IoU entre las cajas del frame anterior y las del actual (asignación greedy por
mayor IoU, umbral 0.3). Sin match → pista nueva. Una pista sin observación durante 3 s se descarta
(y si estaba en episodio, el episodio se resuelve — ver 4.4).

### 4.2 Señales por persona

Se calculan cada frame muestreado. Un keypoint cuenta si su confianza es ≥ 0.3. Si faltan los
puntos necesarios, la señal vale `None` y su término del score pesa 0 (el resto se renormaliza).

| Señal | Cálculo | Indica |
|---|---|---|
| `torso_angle` (grados) | Ángulo del vector (medio de hombros → medio de caderas) respecto a la vertical. 0° = de pie, 90° = horizontal. | Cuerpo tumbado |
| `bbox_ratio` | Ancho / alto de la caja | Redundante con el anterior; útil si faltan keypoints |
| `drop_speed` (alturas de caja / s) | Máximo en los últimos 1.5 s de la velocidad de descenso del medio de caderas, normalizada por la altura de la caja en ese momento | Transición brusca (caída) frente a lenta (tumbarse) |
| `floor_time_s` | Segundos consecutivos con `torso_angle ≥ 60°` (o, sin keypoints, `bbox_ratio ≥ 1.2`) | Sigue en el suelo |
| `head_low` (bool) | Nariz por debajo del medio de caderas, o nariz en el tercio inferior de la caja | Refuerzo de "tumbado" |
| `keypoint_conf` | Media de confianza de los keypoints usados (hombros, caderas, nariz) | Calidad de la pose; se reporta, no puntúa |

### 4.3 Score

Suma ponderada, en [0, 1]. Cada término se mapea linealmente y se recorta a [0, 1]:

| Término | Peso | Mapeo |
|---|---|---|
| Horizontalidad | 0.35 | `torso_angle` 45° → 0, 80° → 1 |
| Brusquedad | 0.30 | `drop_speed` 0.3 → 0, 1.0 → 1 |
| Permanencia | 0.25 | `floor_time_s` 0 → 0, 5 → 1 |
| Cabeza baja | 0.10 | `head_low` 0 / 1 |

Los pesos, mapeos y umbrales (60°, 45°, IoU 0.3, 1.5 s, 2 s, 10 s, 3 s) son **constantes con
comentario en `fall_engine.py`**, no config: se calibran una vez con el script de replay y datos
reales. Si en campo hiciera falta tocarlos por hogar, se pasan a config entonces.

### 4.4 Máquina de estados por pista

```
upright ──torso_angle ≥ 60°──► candidate ──score ≥ min_score y floor_time_s ≥ 2──► reported
   ▲                               │                                                  │
   └──torso_angle < 45° durante 2 s┘                        torso_angle < 45° 2 s ────┤
                                                            o pista desaparece (3 s) ─┘ → fall_resolved
```

- `candidate`: se recalcula el score cada frame; no se emite nada. Agacharse y levantarse se queda
  aquí y vuelve a `upright` sin ruido.
- Entrada en `reported`: se emite **un** `fall_detected` por episodio. La condición de 2 s en el
  suelo evita disparar por un frame ruidoso.
- En `reported`: cada 10 s desde el último evento del episodio se emite `fall_update` con el score
  recalculado (nunca antes de 10 s, aunque el score suba). Así AWS ve "lleva 40 s en el suelo" sin
  inundar el topic.
- Salida de `reported` (se levanta 2 s, o la pista desaparece): `fall_resolved` con duración y
  score máximo del episodio.

### 4.5 Falsos positivos conocidos y aceptados en v1

Sentarse en el suelo, tumbarse rápido en el sofá, jugar con niños en la alfombra. La brusquedad y
la permanencia bajan el score pero no lo anulan. Es aceptable porque el consumidor filtra por
score, y porque esos eventos son justo los ejemplos negativos que necesita el futuro clasificador.

## 5. Contrato del evento

Mismo envelope `Event`, `schema_version` sigue en 1: se añaden tipos, no cambian campos.

```json
{"schema_version":1,"hub_id":"hub-x","camera_id":"onvif-123","camera_name":"salon",
 "type":"fall_detected","severity":"high","timestamp":"2026-08-22T10:15:02+00:00",
 "payload":{
   "episode_id":"onvif-123-1724321702",
   "score":0.82,
   "signals":{"torso_angle":74.1,"bbox_ratio":1.9,"drop_speed":0.85,
              "floor_time_s":2.5,"head_low":true,"keypoint_conf":0.61},
   "person_count":1
 }}
```

| Tipo | Severity | Payload |
|---|---|---|
| `fall_detected` | `high` | `episode_id`, `score`, `signals`, `person_count` |
| `fall_update` | `high` | Igual que `fall_detected`, recalculado |
| `fall_resolved` | `info` | `episode_id`, `duration_s`, `max_score` |

- `episode_id` = `<camera_id>-<epoch de inicio del episodio>`. Enlaza los tres eventos en AWS sin
  que el consumidor guarde estado.
- Señales `None` se serializan como `null`; `score` y señales numéricas con 3 decimales.
- `person_count` es el del frame en que se emite: permite saber si había alguien más en la
  habitación.
- El sink de AWS no cambia: publica el mismo JSON en el mismo topic. La tabla DynamoDB recibe los
  tipos nuevos sin tocar la regla.

## 6. Errores y rendimiento

- `FallEngine.observe` nunca lanza por datos: sin personas, sin keypoints suficientes o con cajas
  degeneradas simplemente no calcula. Una excepción inesperada dentro del engine se captura en
  `process_frame`, se loguea por stderr **una vez por cámara** y no afecta ni al worker ni a los
  eventos de presencia.
- Pesos de pose ausentes → `ConfigError` claro con la ruta antes de invocar `YOLO()` (hoy
  Ultralytics intenta descargarlos y revienta con un traceback confuso). Se aplica también a
  `person_yolo`.
- Coste: `yolo11n-pose` es ~10 % más pesado que `yolo11n`; un modelo en lugar de dos. A 2 fps × 4
  cámaras en Orin sobra. Se mide en campo y el número se apunta en `como-probar.md`.
- Estado de episodios solo en memoria (como `EventEngine` y `ConnectionMonitor`). Un reinicio del
  hub a mitad de episodio pierde el `fall_resolved`. Se añade a la nota ya existente del backlog.
- Memoria acotada: historial por pista limitado a 3 s de frames; pistas caducan a los 3 s.

## 7. Pruebas

Sin GPU en CI, como hasta ahora. Todo lo determinista se prueba con poses sintéticas y reloj
inyectado.

- `tests/test_fall_engine.py`:
  - caída brusca → `fall_detected` a los 2 s de suelo, score ≥ 0.7, `episode_id` estable;
  - tumbarse lento (sin `drop_speed`) → score < 0.5; con `min_score` 0.5 no emite;
  - agacharse y levantarse en < 2 s → sin eventos;
  - persona en el suelo 30 s → un `fall_detected` y `fall_update` cada 10 s, no más;
  - se levanta 2 s → `fall_resolved` con `duration_s` y `max_score` correctos;
  - pista desaparece 3 s en `reported` → `fall_resolved`;
  - keypoints ausentes / confianza baja → señales `None`, score renormalizado, no lanza;
  - dos personas: cada una con su pista y episodio; IoU no las cruza.
- `tests/test_pose_detector.py`: mapeo de resultados Ultralytics falsos (boxes + keypoints) a
  `Detection` con `keypoints`; filtro de clase y confianza; `results` vacío.
- `tests/test_config.py` / `test_factory.py`: `fall` ausente → apagado; `fall.enabled` con
  `person_yolo` → `ConfigError`; `min_score` no numérico → `ConfigError`; pesos ausentes →
  `ConfigError` con la ruta.
- `tests/test_worker.py`: presencia y caída salen por el mismo sink; `fall_engine=None` no cambia
  nada; excepción en `FallEngine` no rompe presencia y se loguea una vez.
- `tests/test_models.py`: `Detection` sin `keypoints` sigue construyéndose igual (compatibilidad).

Verificación manual (no CI): `scripts/replay_video.py` con 3-4 vídeos grabados en la oficina —
caída frontal, caída lateral, tumbarse en el sofá, agacharse a coger algo — y revisar que el orden
de los scores es el esperado. Después, un hogar piloto con `fall.enabled: true` y observar los
eventos en DynamoDB durante una semana.

## 8. Al backlog al terminar

- Zonas de exclusión por cámara (sofá, cama) para bajar el score en ellas.
- Tracker real si el IoU greedy confunde personas en hogares con más ocupación.
- Clasificador temporal entrenado con los episodios recogidos (opción 3 del brainstorming).
- Pasar pesos/umbrales a config si la calibración por hogar resulta necesaria.
- Persistir el estado de episodios para emitir `fall_resolved` tras un reinicio (junto con la nota
  existente del `ConnectionMonitor`).
