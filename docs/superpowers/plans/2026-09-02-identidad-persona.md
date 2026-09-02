# Identidad de persona sobre pistas — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Etiquetar los eventos `fall_*` con la identidad de la persona (`person: {id, confidence} | null`), reconocida por la cara en local contra una galería enrolada en `/data/faces/`.

**Architecture:** Se extrae el emparejamiento entre frames de `FallEngine` a un `Tracker` compartido (`analytics/tracker.py`) que asigna `track_id` y porta `identity`. Un `FaceIdentifier` nuevo (`identity/`) etiqueta de forma oportunista las pistas sin identidad usando InsightFace (SCRFD + ArcFace por onnxruntime); la etiqueta viaja con la pista hasta que caduca y sale en el payload de `fall_detected`/`fall_update`/`fall_resolved`.

**Tech Stack:** Python 3.12, insightface + onnxruntime (embeddings faciales), numpy, pytest, mypy strict, ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-identidad-persona-design.md` — leerla antes de empezar.

## Global Constraints

- Python `>=3.12`; `mypy strict = true` (los módulos nuevos van tipados al completo; `np.ndarray` siempre parametrizado como `npt.NDArray[np.float32]`); `ruff` line-length 100.
- Comentarios, docstrings, logs y mensajes de error **en español**, con el estilo del repo (comentarios que explican el porqué, no el qué).
- **stdout = eventos, stderr = logs.** Nada nuevo escribe en stdout salvo eventos por el sink.
- `schema_version` se queda en `1`: añadir el campo `person` al payload es retrocompatible.
- El hub **etiqueta, no filtra**: ningún evento se suprime por identidad. `person: null` es un resultado válido.
- Del hogar solo sale el `person_id` (alias) y la confianza. Fotos y embeddings no se emiten, no se loguean, no se suben.
- Config nueva: solo `inference.identity.{enabled, match_threshold}`. Umbrales operativos (`MIN_FACE_PX`, `ATTEMPT_EVERY_S`, `CONFIRM_MATCHES`) son constantes con comentario, no config.
- Constantes de emparejamiento (`IOU_MATCH = 0.3`, `CENTER_MATCH = 1.0`, `TRACK_TTL_S = 3.0`) se **mueven** al tracker sin cambiar valores.
- Forma exacta del campo nuevo: `"person": {"id": "<person_id>", "confidence": 0.87}` o `"person": null`, en los tres tipos `fall_*`.
- Comandos de verificación (venv del repo): `.venv/bin/python -m pytest`, `.venv/bin/ruff check src tests scripts`, `.venv/bin/mypy`.
- Commits frecuentes, mensajes en español estilo repo (`feat:`, `refactor:`, `test:`); terminar cada mensaje con la línea `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Hay cambios locales sin commitear ajenos a este plan (`app.py`, `ingest/rtsp.py`, `tests/test_rtsp_logic.py`, `hub-rule-*.json`): **no** incluirlos en los commits del plan (`git add` siempre con rutas explícitas).

---

### Task 1: `Tracker` — extraer el emparejamiento a un módulo propio

**Files:**
- Create: `src/vitahub/analytics/tracker.py`
- Test: `tests/test_tracker.py`

**Interfaces:**
- Consumes: `vitahub.models.Detection`.
- Produces (las usan Tasks 2, 3, 6 y 7 — nombres y tipos exactos):
  - `TrackIdentity(person_id: str, confidence: float)` — dataclass frozen.
  - `Track(track_id: int, bbox: tuple[int, int, int, int], last_seen: float, identity: TrackIdentity | None = None, pending_identity: TrackIdentity | None = None)` — dataclass mutable.
  - `Match(track: Track, detection: Detection, step_s: float)` — dataclass frozen. `step_s` = segundos desde la muestra anterior de esa pista; `0.0` si la pista es nueva.
  - `TrackerUpdate(matches: list[Match], lost: list[Track])` — dataclass frozen.
  - `Tracker()` con método `observe(camera_id: str, detections: list[Detection], now: float) -> TrackerUpdate`.
  - Constantes: `IOU_MATCH`, `CENTER_MATCH`, `TRACK_TTL_S`.

En este task `FallEngine` **no se toca** (su copia del matching convive un commit con la nueva; Task 2 la elimina).

- [ ] **Step 1: Escribir los tests que fallan**

Crear `tests/test_tracker.py`:

```python
from vitahub.analytics.tracker import TRACK_TTL_S, Tracker
from vitahub.models import Detection


def _person(bbox):
    return Detection("person", 0.9, bbox)


def test_overlapping_detection_keeps_track_id():
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((10, 0, 50, 120))], 0.0)
    u1 = tracker.observe("cam-a", [_person((12, 0, 52, 120))], 0.5)
    assert len(u0.matches) == 1 and len(u1.matches) == 1
    assert u1.matches[0].track.track_id == u0.matches[0].track.track_id
    assert u1.matches[0].step_s == 0.5
    assert u0.matches[0].step_s == 0.0  # pista nueva


def test_center_fallback_rescues_forward_fall():
    # Caída hacia delante: caja de pie (30,0,70,120) y tumbada (80,60,180,130)
    # no solapan (IoU = 0) pero los centros quedan a menos de un lado mayor.
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((30, 0, 70, 120))], 0.0)
    u1 = tracker.observe("cam-a", [_person((80, 60, 180, 130))], 0.5)
    assert u1.matches[0].track.track_id == u0.matches[0].track.track_id


def test_far_detection_opens_new_track():
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((0, 0, 40, 120))], 0.0)
    u1 = tracker.observe("cam-a", [_person((500, 0, 540, 120))], 0.5)
    assert u1.matches[0].track.track_id != u0.matches[0].track.track_id


def test_expired_track_is_reported_lost_before_matching():
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((10, 0, 50, 120))], 0.0)
    old_id = u0.matches[0].track.track_id
    # TRACK_TTL_S sin verse: aunque la caja nueva solape, es pista nueva.
    u1 = tracker.observe("cam-a", [_person((10, 0, 50, 120))], TRACK_TTL_S + 0.5)
    assert [t.track_id for t in u1.lost] == [old_id]
    assert u1.matches[0].track.track_id != old_id


def test_empty_frame_reports_lost_after_ttl():
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((10, 0, 50, 120))], 0.0)
    assert tracker.observe("cam-a", [], 1.0).lost == []
    u2 = tracker.observe("cam-a", [], TRACK_TTL_S + 0.5)
    assert [t.track_id for t in u2.lost] == [u0.matches[0].track.track_id]
    # Perdida una vez, no se repite.
    assert tracker.observe("cam-a", [], TRACK_TTL_S + 1.0).lost == []


def test_two_people_keep_separate_tracks():
    tracker = Tracker()
    a, b = _person((0, 0, 40, 120)), _person((300, 0, 340, 120))
    u0 = tracker.observe("cam-a", [a, b], 0.0)
    u1 = tracker.observe("cam-a", [a, b], 0.5)
    ids0 = sorted(m.track.track_id for m in u0.matches)
    ids1 = sorted(m.track.track_id for m in u1.matches)
    assert ids0 == ids1 and len(set(ids0)) == 2


def test_cameras_do_not_share_tracks():
    tracker = Tracker()
    u_a = tracker.observe("cam-a", [_person((10, 0, 50, 120))], 0.0)
    u_b = tracker.observe("cam-b", [_person((10, 0, 50, 120))], 0.0)
    assert u_a.matches[0].track.track_id != u_b.matches[0].track.track_id


def test_non_person_detections_are_ignored():
    tracker = Tracker()
    update = tracker.observe("cam-a", [Detection("dog", 0.9, (0, 0, 40, 40))], 0.0)
    assert update.matches == [] and update.lost == []


def test_degenerate_bbox_never_raises():
    tracker = Tracker()
    bad = _person((10, 10, 10, 10))
    for i in range(10):
        tracker.observe("cam-a", [bad], i * 0.5)
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_tracker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.analytics.tracker'`

- [ ] **Step 3: Implementar `analytics/tracker.py`**

El emparejamiento (`_iou`, `_center_distance`, greedy IoU + respaldo por centros) se **copia tal cual** de `fall_engine.py:68-85` y `fall_engine.py:154-199`, adaptado a `Track`:

```python
"""Pistas de persona por cámara: emparejamiento entre frames e identidad.

Una pista es una persona seguida entre frames por solapamiento de cajas
(IoU) y, cuando no hay solape, por cercanía de centros (una caída hacia
delante mueve la caja entera). Las pistas caducan antes de emparejar: quien
reaparece tras un hueco largo abre pista nueva, no hereda la anterior.

La identidad (FaceIdentifier) viaja aquí, colgada de la pista: se etiqueta
una vez y acompaña a la persona hasta que la pista se pierde.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from vitahub.models import Detection

# Solapamiento mínimo para decir "es la misma persona que en el frame anterior".
IOU_MATCH = 0.3
# Respaldo cuando no hay solape: distancia entre centros normalizada por el
# lado mayor de las dos cajas. Una caída hacia delante desplaza la caja hasta
# una altura de cuerpo, y a 2 fps eso puede dejar IoU = 0 entre frames
# consecutivos. Puede cruzar personas en escenas concurridas: el tracker real
# está en el backlog.
CENTER_MATCH = 1.0
# Una pista sin observación durante este tiempo se da por perdida.
TRACK_TTL_S = 3.0


@dataclass(frozen=True)
class TrackIdentity:
    person_id: str
    confidence: float


@dataclass
class Track:
    track_id: int
    bbox: tuple[int, int, int, int]
    last_seen: float
    # La rellena el FaceIdentifier; None = pista anónima (persona no enrolada,
    # cara nunca vista, o identidad apagada).
    identity: TrackIdentity | None = None
    # Primera coincidencia a la espera de confirmación (regla de 2 frames del
    # FaceIdentifier): un solo frame ruidoso no etiqueta.
    pending_identity: TrackIdentity | None = None


@dataclass(frozen=True)
class Match:
    track: Track
    detection: Detection
    # Segundos desde la muestra anterior de esta pista; 0.0 si es nueva. El
    # consumidor decide su tope (FallEngine aplica MAX_STEP_S).
    step_s: float


@dataclass(frozen=True)
class TrackerUpdate:
    matches: list[Match]
    lost: list[Track]


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _center_distance(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Distancia entre centros normalizada por el lado mayor de ambas cajas."""
    ax, ay = (a[0] + a[2]) / 2.0, (a[1] + a[3]) / 2.0
    bx, by = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    scale = max(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1])
    if scale <= 0:
        return float("inf")
    return math.hypot(ax - bx, ay - by) / scale


@dataclass
class _CameraTracks:
    tracks: list[Track] = field(default_factory=list)


class Tracker:
    """Estado en memoria por cámara; track_id único en todo el proceso."""

    def __init__(self) -> None:
        self._cameras: dict[str, _CameraTracks] = {}
        self._next_id = 1

    def observe(self, camera_id: str, detections: list[Detection], now: float) -> TrackerUpdate:
        cam = self._cameras.setdefault(camera_id, _CameraTracks())
        persons = [d for d in detections if d.label == "person"]

        # Caducar ANTES de emparejar: si la pista lleva TRACK_TTL_S sin verse,
        # la persona que aparece ahora no es "la misma de antes" aunque su
        # caja solape.
        lost = [t for t in cam.tracks if now - t.last_seen >= TRACK_TTL_S]
        cam.tracks = [t for t in cam.tracks if now - t.last_seen < TRACK_TTL_S]

        matches: list[Match] = []
        for track, det in self._match(cam.tracks, persons):
            step = max(0.0, now - track.last_seen)
            track.bbox = det.bbox
            track.last_seen = now
            matches.append(Match(track=track, detection=det, step_s=step))

        matched_dets = {id(m.detection) for m in matches}
        for det in persons:
            if id(det) in matched_dets:
                continue
            track = Track(track_id=self._next_id, bbox=det.bbox, last_seen=now)
            self._next_id += 1
            cam.tracks.append(track)
            matches.append(Match(track=track, detection=det, step_s=0.0))
        return TrackerUpdate(matches=matches, lost=lost)

    def _match(
        self, tracks: list[Track], persons: list[Detection]
    ) -> list[tuple[Track, Detection]]:
        """Greedy por mayor IoU, luego por cercanía de centros."""
        pairs = sorted(
            (
                (_iou(t.bbox, d.bbox), ti, di)
                for ti, t in enumerate(tracks)
                for di, d in enumerate(persons)
            ),
            reverse=True,
        )
        used_t: set[int] = set()
        used_d: set[int] = set()
        result: list[tuple[Track, Detection]] = []
        for iou, ti, di in pairs:
            if iou < IOU_MATCH or ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            result.append((tracks[ti], persons[di]))

        # Segunda pasada: pistas y detecciones aún sueltas, por cercanía de
        # centros (menor distancia primero). Rescata la caída sin solape.
        near = sorted(
            (
                (_center_distance(t.bbox, d.bbox), ti, di)
                for ti, t in enumerate(tracks)
                if ti not in used_t
                for di, d in enumerate(persons)
                if di not in used_d
            ),
        )
        for dist, ti, di in near:
            if dist > CENTER_MATCH or ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            result.append((tracks[ti], persons[di]))
        return result
```

Nota: el emparejamiento de detecciones no casadas usa `id(det)` porque `Detection` es frozen y dos detecciones idénticas del stub deben contar por separado.

- [ ] **Step 4: Verificar que pasan**

Run: `.venv/bin/python -m pytest tests/test_tracker.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Lint y tipos**

Run: `.venv/bin/ruff check src/vitahub/analytics/tracker.py tests/test_tracker.py && .venv/bin/mypy`
Expected: sin errores.

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/analytics/tracker.py tests/test_tracker.py
git commit -m "feat: Tracker propio — el emparejamiento entre frames sale de FallEngine"
```

---

### Task 2: `FallEngine` consume pistas del `Tracker`

**Files:**
- Modify: `src/vitahub/analytics/fall_engine.py` (elimina matching propio, nueva firma de `observe`)
- Modify: `src/vitahub/worker.py` (orquesta tracker → fall)
- Modify: `src/vitahub/app.py:69-82` (construye `Tracker`, lo pasa al worker)
- Modify: `scripts/replay_video.py:68-84` (pasa tracker)
- Modify: `tests/test_fall_engine.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: `Tracker`, `TrackerUpdate`, `Match`, `Track`, `TrackIdentity` de Task 1.
- Produces (las usan Tasks 3 y 7):
  - `FallEngine.observe(camera: Camera, update: TrackerUpdate, now: float) -> list[Event]` — nueva firma.
  - `worker.process_frame(camera, frame, detector, engine, sink, now, fall_engine=None, tracker=None) -> list[Event]` — si `fall_engine` no es None, `tracker` tampoco debe serlo (van juntos desde `app.run`).

- [ ] **Step 1: Adaptar los tests de `FallEngine` a la nueva firma**

En `tests/test_fall_engine.py`, sustituir imports, `_engine` y `_feed` (el resto de tests no cambia de aserciones):

```python
from vitahub.analytics.tracker import Tracker
```

```python
def _engine(min_score=0.3):
    clock = _Clock()
    return FallEngine(hub_id="hub-1", min_score=min_score, clock=clock), Tracker(), clock


def _feed(engine, tracker, clock, seq, step=0.5, start=0.0):
    """seq: lista de listas de detecciones, una por frame. Devuelve todos los eventos."""
    events = []
    now = start
    for dets in seq:
        events += engine.observe(CAM, tracker.observe(CAM.id, dets, now), now)
        now += step
        clock.advance(step)
    return events
```

Actualizar cada test: `eng, clock = _engine()` → `eng, tracker, clock = _engine()` y `_feed(eng, clock, seq)` → `_feed(eng, tracker, clock, seq)`. Los dos tests que llaman a `eng.observe` directo se adaptan igual:

- `test_per_camera_state_is_independent`: crear un solo `tracker` y llamar `eng.observe(CAM, tracker.observe(CAM.id, [...], now), now)` y lo mismo con `other`/`other.id`.
- `test_sampling_gap_does_not_inflate_floor_time` y `test_gap_then_reappear_resolves_and_starts_new_track`: cada `eng.observe(CAM, dets, now)` pasa a `eng.observe(CAM, tracker.observe(CAM.id, dets, now), now)` con el mismo `now`.

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_fall_engine.py -v`
Expected: FAIL — `TypeError` / `AttributeError` (la firma vieja recibe un `TrackerUpdate`).

- [ ] **Step 3: Reescribir `fall_engine.py` sin matching**

Cambios sobre el fichero actual:

1. **Borrar**: `import math`, `_iou`, `_center_distance`, `IOU_MATCH`, `CENTER_MATCH`, `TRACK_TTL_S`, el método `_match` completo, y del docstring del módulo el párrafo del emparejamiento (referir al tracker).
2. **Imports nuevos**: `from vitahub.analytics.tracker import Match, Track, TrackerUpdate` (`TrackIdentity` se importará en Task 3, cuando se use — antes sería un import muerto para ruff).
3. `_Track` pasa a llamarse `_EpisodeState` y pierde `bbox` y `last_seen` (los lleva el `Track` del tracker); conserva `history`, `state`, `posture`, `floor_time`, `upright_time`, `peak_drop`, `episode_id`, `episode_start`, `last_event_at`, `max_score`.
4. `_CameraState` pasa a `states: dict[int, _EpisodeState]` (por `track_id`) + `episodes: int`.
5. `observe` nuevo:

```python
    def observe(self, camera: Camera, update: TrackerUpdate, now: float) -> list[Event]:
        cam = self._cameras.setdefault(camera.id, _CameraState())
        events: list[Event] = []

        # El tracker ya caducó estas pistas; aquí solo queda cerrar el episodio.
        for track in update.lost:
            state = cam.states.pop(track.track_id, None)
            if state is not None and state.state == "reported":
                events.append(self._resolved(camera, state, track, now, "track_lost"))

        for match in update.matches:
            state = cam.states.setdefault(match.track.track_id, _EpisodeState())
            events += self._update_state(camera, cam, state, match, now, len(update.matches))
        return events
```

6. `_update_track` pasa a `_update_state(self, camera, cam, state, match, now, person_count)`:
   - `det = match.detection`; `track = match.track`.
   - El paso observado ya no se calcula con `last_seen`: `step = min(match.step_s, MAX_STEP_S)`.
   - Desaparecen las líneas `track.bbox = det.bbox` y `track.last_seen = now` (las hace el tracker).
   - Todas las referencias `track.<campo de episodio>` pasan a `state.<campo>`; el resto de la lógica (historial, señales, posturas, máquina de estados, score, episodios, cadencia de updates) **se queda idéntica línea a línea**.
   - `_fall_event(camera, state, track, ...)` y `_resolved(camera, state, track, ...)` reciben también el `Track` (Task 3 usará `track.identity`; en este task solo se pasa).
7. `_fall_event` y `_resolved`: leen `episode_id`/`max_score`/`episode_start` de `state`; el payload no cambia todavía.

- [ ] **Step 4: Actualizar `worker.py`**

Contenido completo nuevo:

```python
from __future__ import annotations

from vitahub.analytics.event_engine import EventEngine
from vitahub.analytics.fall_engine import FallEngine
from vitahub.analytics.tracker import Tracker
from vitahub.inference.base import Detector
from vitahub.logging_setup import get_logger
from vitahub.models import Camera, Event
from vitahub.sinks.base import EventSink

_log = get_logger("worker")

# Cámaras para las que ya se ha logueado un fallo de la analítica de caídas:
# un bug en el engine se loguea una vez, no en cada frame a 2 fps.
_fall_failure_logged: set[str] = set()


def process_frame(
    camera: Camera,
    frame: object,
    detector: Detector,
    engine: EventEngine,
    sink: EventSink,
    now: float,
    fall_engine: FallEngine | None = None,
    tracker: Tracker | None = None,
) -> list[Event]:
    detections = detector.detect(frame)
    person_count = sum(1 for d in detections if d.label == "person")
    confidence = max((d.confidence for d in detections), default=0.0)
    events = engine.observe(camera, person_count, confidence, now)
    if fall_engine is not None and tracker is not None:
        try:
            update = tracker.observe(camera.id, detections, now)
            events = events + fall_engine.observe(camera, update, now)
        except Exception:  # noqa: BLE001 — la caída no debe tumbar la presencia
            if camera.id not in _fall_failure_logged:
                _fall_failure_logged.add(camera.id)
                _log.exception(
                    "cam %s: error en la analítica de caídas (se silencia a partir de ahora)",
                    camera.id,
                )
    for event in events:
        sink.emit(event)
    return events
```

- [ ] **Step 5: Actualizar `tests/test_worker.py`**

- Import: `from vitahub.analytics.tracker import Tracker`.
- Los tres tests que pasan `fall_engine=` añaden `tracker=Tracker()` (en `test_fall_engine_exception_does_not_break_presence`, `_Boom.observe(self, camera, update, now)` — misma aridad, sigue reventando).
- `test_process_frame_without_fall_engine_is_unchanged` queda igual (sin tracker).

- [ ] **Step 6: Actualizar `app.py` y `replay_video.py`**

`app.py` — junto a la construcción de `fall_engine` (línea ~69):

```python
    tracker = Tracker() if fall_engine is not None else None
```

con import `from vitahub.analytics.tracker import Tracker`, y el hilo pasa ambos: el closure `worker(...)` y `_camera_loop(...)` ganan el parámetro `tracker` (por defecto `None`), que `_camera_loop` reenvía a `process_frame(..., fall_engine=fall_engine, tracker=tracker)`.

`replay_video.py` — en `replay()`:

```python
    fall = FallEngine("replay", min_score=min_score)
    tracker = Tracker()
```

(con su import) y `process_frame(..., fall_engine=fall, tracker=tracker)`.

- [ ] **Step 7: Verificar todo**

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check src tests scripts && .venv/bin/mypy`
Expected: PASS completo. Los tests de `test_fall_engine.py` verifican exactamente lo mismo que antes — si alguna aserción hubo que cambiarla (no debería), parar y revisar.

- [ ] **Step 8: Commit**

```bash
git add src/vitahub/analytics/fall_engine.py src/vitahub/analytics/tracker.py src/vitahub/worker.py src/vitahub/app.py scripts/replay_video.py tests/test_fall_engine.py tests/test_worker.py tests/test_tracker.py
git commit -m "refactor: FallEngine consume pistas del Tracker en vez de emparejar él mismo"
```

---

### Task 3: campo `person` en los eventos `fall_*`

**Files:**
- Modify: `src/vitahub/analytics/fall_engine.py` (`_fall_event`, `_resolved`)
- Test: `tests/test_fall_engine.py`

**Interfaces:**
- Consumes: `TrackIdentity`, `Track.identity` de Task 1; firma de Task 2.
- Produces: payload `"person": {"id": str, "confidence": float} | None` en `fall_detected`, `fall_update` y `fall_resolved`. Es lo que consume AWS; no cambia más.

- [ ] **Step 1: Tests que fallan**

Añadir a `tests/test_fall_engine.py`:

```python
from vitahub.analytics.tracker import TrackIdentity


def test_fall_events_carry_track_identity():
    eng, tracker, clock = _engine()
    update = tracker.observe(CAM.id, [standing()], 0.0)
    update.matches[0].track.identity = TrackIdentity("maria", 0.87)
    eng.observe(CAM, update, 0.0)
    clock.advance(0.5)
    events = _feed(eng, tracker, clock, [[standing()]] + [[lying()]] * 6 + [[standing()]] * 5,
                   start=0.5)
    assert [e.type for e in events] == ["fall_detected", "fall_resolved"]
    assert all(e.payload["person"] == {"id": "maria", "confidence": 0.87} for e in events)


def test_identity_arriving_after_fall_detected_shows_in_updates():
    eng, tracker, clock = _engine()
    events = _feed(eng, tracker, clock, [[standing()]] * 2 + [[lying()]] * 6)
    assert [e.type for e in events] == ["fall_detected"]
    assert events[0].payload["person"] is None
    # La cara se ve más tarde (p. ej. al girarse): update y resolved la llevan.
    update = tracker.observe(CAM.id, [lying()], 4.0)
    update.matches[0].track.identity = TrackIdentity("maria", 0.71)
    eng.observe(CAM, update, 4.0)
    clock.advance(0.5)
    # 20 frames a 0.5 s desde 4.5 llegan a 14.0: cruzan el fall_update de 13.0
    # (cadencia de 10 s desde el fall_detected de 3.0).
    late = _feed(eng, tracker, clock, [[lying()]] * 20, start=4.5)
    assert "fall_update" in [e.type for e in late]
    assert all(e.payload["person"] == {"id": "maria", "confidence": 0.71} for e in late)


def test_anonymous_track_emits_person_null():
    eng, tracker, clock = _engine()
    events = _feed(eng, tracker, clock, [[standing()]] * 2 + [[lying()]] * 6 + [[]] * 7)
    assert [e.type for e in events] == ["fall_detected", "fall_resolved"]
    assert all(e.payload["person"] is None for e in events)
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_fall_engine.py -k person -v`
Expected: FAIL — `KeyError: 'person'`

- [ ] **Step 3: Implementar**

En `fall_engine.py`, helper a nivel de módulo y uso en ambos constructores de evento:

```python
def _person_payload(identity: TrackIdentity | None) -> dict[str, object] | None:
    # None = pista anónima. El hub etiqueta, no filtra: quien decide por
    # identidad es el consumidor en AWS; del hogar solo sale el alias.
    if identity is None:
        return None
    return {"id": identity.person_id, "confidence": round(identity.confidence, 3)}
```

En `_fall_event(...)` y `_resolved(...)` añadir al payload: `"person": _person_payload(track.identity)`.

- [ ] **Step 4: Verificar y commit**

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check src tests && .venv/bin/mypy`
Expected: PASS.

```bash
git add src/vitahub/analytics/fall_engine.py tests/test_fall_engine.py
git commit -m "feat: los eventos fall_* llevan la identidad de la pista (person o null)"
```

---

### Task 4: config `inference.identity`

**Files:**
- Modify: `src/vitahub/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces (lo usan Tasks 7 y 8): `IdentityConfig(enabled: bool = False, match_threshold: float = 0.4)`; `InferenceConfig` gana el campo `identity: IdentityConfig`.

- [ ] **Step 1: Tests que fallan**

Mirar cómo construyen config los tests existentes de `tests/test_config.py` (fixture de YAML + env con credenciales) y añadir con el mismo patrón:

```python
def test_identity_defaults_off(tmp_path, monkeypatch):
    cfg = _load(tmp_path, {"hub_id": "hub-1"})  # usar el helper real del fichero
    assert cfg.inference.identity.enabled is False
    assert cfg.inference.identity.match_threshold == 0.4


def test_identity_requires_fall_enabled(tmp_path):
    raw = {"hub_id": "hub-1", "inference": {"detector": "person_pose",
           "identity": {"enabled": True}}}
    with pytest.raises(ConfigError, match="inference.identity.enabled requiere"):
        _load(tmp_path, raw)


def test_identity_valid_when_fall_enabled(tmp_path):
    raw = {"hub_id": "hub-1", "inference": {"detector": "person_pose",
           "fall": {"enabled": True},
           "identity": {"enabled": True, "match_threshold": 0.5}}}
    cfg = _load(tmp_path, raw)
    assert cfg.inference.identity.enabled is True
    assert cfg.inference.identity.match_threshold == 0.5


def test_identity_threshold_out_of_range(tmp_path):
    raw = {"hub_id": "hub-1", "inference": {"identity": {"match_threshold": 1.5}}}
    with pytest.raises(ConfigError, match="match_threshold"):
        _load(tmp_path, raw)


def test_identity_threshold_not_numeric(tmp_path):
    raw = {"hub_id": "hub-1", "inference": {"identity": {"match_threshold": "alto"}}}
    with pytest.raises(ConfigError, match="match_threshold"):
        _load(tmp_path, raw)
```

(Si `test_config.py` no tiene helper `_load`, replicar el patrón que sí use — escribir el YAML con `yaml.safe_dump` a `tmp_path / "hub.yaml"` y llamar `load_config` con env `{"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"}`.)

- [ ] **Step 2: Verificar que fallan** — `.venv/bin/python -m pytest tests/test_config.py -k identity -v` → FAIL.

- [ ] **Step 3: Implementar en `config.py`**

Tras `FallConfig`:

```python
@dataclass
class IdentityConfig:
    enabled: bool = False
    # Similitud coseno mínima contra la galería para etiquetar una pista.
    # Se calibra con scripts/replay_video.py; el resto de umbrales del
    # reconocimiento son constantes en identity/face_id.py.
    match_threshold: float = 0.4
```

`InferenceConfig` gana `identity: IdentityConfig = field(default_factory=IdentityConfig)`.

Parser, junto a `_fall_from_raw`:

```python
def _identity_from_raw(raw: object, fall: FallConfig) -> IdentityConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("Sección 'inference.identity' debe ser un mapping YAML")
    enabled = bool(raw.get("enabled", False))
    try:
        match_threshold = float(raw.get("match_threshold", 0.4))
    except (ValueError, TypeError) as exc:
        raise ConfigError("Campo 'inference.identity.match_threshold' no es numérico") from exc
    if not 0.0 < match_threshold <= 1.0:
        raise ConfigError(
            "Campo 'inference.identity.match_threshold' debe estar entre 0 (excl.) y 1"
        )
    if enabled and not fall.enabled:
        # En v1 la identidad solo etiqueta eventos fall_*: sin caídas activas
        # no tiene ningún efecto y encenderla es un error de instalación.
        raise ConfigError(
            "inference.identity.enabled requiere 'inference.fall.enabled: true' "
            "(en esta versión la identidad solo etiqueta eventos de caída)"
        )
    return IdentityConfig(enabled=enabled, match_threshold=match_threshold)
```

En `load_config`, construir primero `fall = _fall_from_raw(inf_raw.get("fall"), detector)` en una variable y pasar `fall=fall, identity=_identity_from_raw(inf_raw.get("identity"), fall)` al `InferenceConfig`.

- [ ] **Step 4: Verificar y commit**

Run: `.venv/bin/python -m pytest tests/test_config.py && .venv/bin/mypy && .venv/bin/ruff check src tests`

```bash
git add src/vitahub/config.py tests/test_config.py
git commit -m "feat: config inference.identity (enabled, match_threshold) con fail-fast"
```

---

### Task 5: paquete `identity` — motor de caras y galería

**Files:**
- Create: `src/vitahub/identity/__init__.py` (vacío)
- Create: `src/vitahub/identity/base.py`
- Create: `src/vitahub/identity/stub.py`
- Create: `src/vitahub/identity/gallery.py`
- Test: `tests/test_identity_gallery.py`

**Interfaces:**
- Produces (lo usan Tasks 6 y 7):
  - `FaceObservation(bbox: tuple[int, int, int, int], embedding: npt.NDArray[np.float32])` — frozen; embedding L2-normalizado.
  - `FaceEngine` (ABC) con `extract(self, image: object) -> list[FaceObservation]`.
  - `StubFaceEngine(responses: list[list[FaceObservation]])` — devuelve `responses[i]` en la llamada i-ésima y `[]` al agotarse.
  - `Gallery(people: dict[str, npt.NDArray[np.float32]])` — frozen; embedding medio normalizado por persona.
  - `cosine_similarity(a, b) -> float`; `mean_embedding(embeddings: list[...]) -> npt.NDArray[np.float32]`; `best_match(embedding, gallery: Gallery, threshold: float) -> tuple[str, float] | None`; `load_gallery(faces_dir: Path, engine: FaceEngine) -> Gallery` (lanza `ConfigError`).

- [ ] **Step 1: Tests que fallan**

`tests/test_identity_gallery.py`:

```python
import numpy as np
import pytest

from vitahub.config import ConfigError
from vitahub.identity.base import FaceObservation
from vitahub.identity.gallery import Gallery, best_match, load_gallery, mean_embedding
from vitahub.identity.stub import StubFaceEngine


def _unit(v):
    arr = np.asarray(v, dtype=np.float32)
    return arr / np.linalg.norm(arr)


def test_mean_embedding_is_renormalized():
    mean = mean_embedding([_unit([1, 0, 0]), _unit([0, 1, 0])])
    assert np.linalg.norm(mean) == pytest.approx(1.0)


def test_best_match_returns_most_similar_above_threshold():
    gallery = Gallery(people={"maria": _unit([1, 0, 0]), "pepe": _unit([0, 1, 0])})
    got = best_match(_unit([0.9, 0.1, 0]), gallery, threshold=0.4)
    assert got is not None
    person, sim = got
    assert person == "maria" and sim > 0.9


def test_best_match_below_threshold_is_none():
    gallery = Gallery(people={"maria": _unit([1, 0, 0])})
    assert best_match(_unit([0, 0, 1]), gallery, threshold=0.4) is None


def _photo(tmp_path, person, name):
    # Un JPEG válido de 8x8 escrito con cv2 basta: el StubFaceEngine ignora
    # los píxeles, pero load_gallery debe poder leer el fichero.
    import cv2

    d = tmp_path / person
    d.mkdir(exist_ok=True)
    path = d / name
    cv2.imwrite(str(path), np.zeros((8, 8, 3), dtype=np.uint8))
    return path


def test_load_gallery_averages_per_person(tmp_path):
    _photo(tmp_path, "maria", "a.jpg")
    _photo(tmp_path, "maria", "b.jpg")
    engine = StubFaceEngine([
        [FaceObservation((0, 0, 8, 8), _unit([1, 0, 0]))],
        [FaceObservation((0, 0, 8, 8), _unit([0, 1, 0]))],
    ])
    gallery = load_gallery(tmp_path, engine)
    assert set(gallery.people) == {"maria"}
    assert np.linalg.norm(gallery.people["maria"]) == pytest.approx(1.0)


def test_load_gallery_empty_dir_raises(tmp_path):
    with pytest.raises(ConfigError, match="ninguna persona enrolada"):
        load_gallery(tmp_path, StubFaceEngine([]))


def test_load_gallery_photo_without_face_raises(tmp_path):
    _photo(tmp_path, "maria", "a.jpg")
    with pytest.raises(ConfigError, match="a.jpg"):
        load_gallery(tmp_path, StubFaceEngine([[]]))


def test_load_gallery_photo_with_two_faces_raises(tmp_path):
    _photo(tmp_path, "maria", "a.jpg")
    two = [FaceObservation((0, 0, 4, 8), _unit([1, 0, 0])),
           FaceObservation((4, 0, 8, 8), _unit([0, 1, 0]))]
    with pytest.raises(ConfigError, match="a.jpg"):
        load_gallery(tmp_path, StubFaceEngine([two]))
```

- [ ] **Step 2: Verificar que fallan** — `.venv/bin/python -m pytest tests/test_identity_gallery.py -v` → FAIL (módulo inexistente).

- [ ] **Step 3: Implementar**

`src/vitahub/identity/base.py`:

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class FaceObservation:
    """Una cara encontrada en una imagen: caja en píxeles y embedding L2-normalizado."""

    bbox: tuple[int, int, int, int]
    embedding: npt.NDArray[np.float32]


class FaceEngine(ABC):
    """Detección + embedding facial. Implementaciones: InsightFace y stub de tests."""

    @abstractmethod
    def extract(self, image: object) -> list[FaceObservation]: ...
```

`src/vitahub/identity/stub.py`:

```python
from __future__ import annotations

from vitahub.identity.base import FaceEngine, FaceObservation


class StubFaceEngine(FaceEngine):
    """Motor sin modelo: devuelve respuestas programadas, una por llamada.

    Agotada la lista, devuelve [] (ninguna cara): permite simular "la cara se
    vio dos veces y luego dejó de verse" sin tocar el reloj.
    """

    def __init__(self, responses: list[list[FaceObservation]]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def extract(self, image: object) -> list[FaceObservation]:
        self.calls += 1
        if not self._responses:
            return []
        return self._responses.pop(0)
```

`src/vitahub/identity/gallery.py`:

```python
"""Galería de personas enroladas: /data/faces/<person_id>/*.jpg → embedding medio.

Sin caché a propósito: recalcular al arrancar tarda segundos y hace que
añadir o quitar fotos sea solo reiniciar el hub. Los embeddings viven solo
en memoria; del hogar no sale ninguno.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from vitahub.config import ConfigError
from vitahub.identity.base import FaceEngine

_PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png")


@dataclass(frozen=True)
class Gallery:
    people: dict[str, npt.NDArray[np.float32]]


def cosine_similarity(a: npt.NDArray[np.float32], b: npt.NDArray[np.float32]) -> float:
    # Ambos llegan L2-normalizados (contrato de FaceObservation y de Gallery):
    # el coseno es el producto escalar, sin división que pueda ser por cero.
    return float(np.dot(a, b))


def mean_embedding(embeddings: list[npt.NDArray[np.float32]]) -> npt.NDArray[np.float32]:
    mean = np.mean(np.stack(embeddings), axis=0)
    norm = float(np.linalg.norm(mean))
    if norm == 0.0:
        raise ConfigError("embeddings de enrolamiento degenerados (media nula)")
    return np.asarray(mean / norm, dtype=np.float32)


def best_match(
    embedding: npt.NDArray[np.float32], gallery: Gallery, threshold: float
) -> tuple[str, float] | None:
    best: tuple[str, float] | None = None
    for person_id, ref in gallery.people.items():
        sim = cosine_similarity(embedding, ref)
        if best is None or sim > best[1]:
            best = (person_id, sim)
    if best is None or best[1] < threshold:
        return None
    return best


def load_gallery(faces_dir: Path, engine: FaceEngine) -> Gallery:
    """Falla con ConfigError legible: es un error de instalación y el técnico está delante."""
    people: dict[str, npt.NDArray[np.float32]] = {}
    for person_dir in sorted(p for p in faces_dir.iterdir() if p.is_dir()):
        embeddings: list[npt.NDArray[np.float32]] = []
        for photo in sorted(person_dir.iterdir()):
            if photo.suffix.lower() not in _PHOTO_SUFFIXES:
                continue
            image = cv2.imread(str(photo))
            if image is None:
                raise ConfigError(f"foto de enrolamiento ilegible: {photo}")
            faces = engine.extract(image)
            if len(faces) != 1:
                # 0 caras = foto inútil; 2+ = ambigua (¿cuál es la persona?).
                raise ConfigError(
                    f"la foto de enrolamiento {photo} tiene {len(faces)} caras; "
                    "cada foto debe tener exactamente una"
                )
            embeddings.append(faces[0].embedding)
        if not embeddings:
            raise ConfigError(f"la carpeta {person_dir} no tiene fotos válidas (.jpg/.png)")
        people[person_dir.name] = mean_embedding(embeddings)
    if not people:
        raise ConfigError(
            f"ninguna persona enrolada en {faces_dir} — crea /data/faces/<person_id>/ "
            "con 3-5 fotos de la cara (distintas luces y ángulos)"
        )
    return Gallery(people=people)
```

- [ ] **Step 4: Verificar, lint, tipos, commit**

Run: `.venv/bin/python -m pytest tests/test_identity_gallery.py -v && .venv/bin/ruff check src tests && .venv/bin/mypy`

```bash
git add src/vitahub/identity tests/test_identity_gallery.py
git commit -m "feat: paquete identity — FaceEngine, stub y galería de enrolados"
```

---

### Task 6: `FaceIdentifier` — etiquetar pistas

**Files:**
- Create: `src/vitahub/identity/face_id.py`
- Test: `tests/test_face_identifier.py`

**Interfaces:**
- Consumes: `FaceEngine`, `FaceObservation`, `Gallery`, `best_match` (Task 5); `Match`, `Track`, `TrackIdentity` (Task 1).
- Produces (lo usa Task 7): `FaceIdentifier(engine: FaceEngine, gallery: Gallery, match_threshold: float)` con `identify(frame: object, camera_id: str, matches: list[Match], now: float) -> None` (muta `track.identity` / `track.pending_identity`). Constantes `MIN_FACE_PX = 40`, `ATTEMPT_EVERY_S = 1.0`.

Reglas (de la spec §3-§4):
1. Solo se extraen caras si hay al menos una pista **sin identidad** en el frame, y como mucho una vez por `ATTEMPT_EVERY_S` por cámara (una extracción sirve a todas las pistas del frame).
2. Cara con alto < `MIN_FACE_PX` px: se ignora (substream, persona lejos).
3. Cada cara se asigna a la pista cuyo bbox contiene su centro; con varias candidatas, la de menor área (la más ajustada).
4. Pista anónima: primera coincidencia → `pending_identity`; segunda coincidencia con el **mismo** `person_id` → `identity` (confianza = máx. de las dos similitudes); coincidencia con otro id → reemplaza `pending_identity`.
5. Pista ya identificada: si aparece un match de **otra** persona con similitud mayor que la registrada, se reemplaza la identidad y se loguea a stderr (corrección de cruce de pistas).

- [ ] **Step 1: Tests que fallan**

`tests/test_face_identifier.py`:

```python
import numpy as np

from vitahub.analytics.tracker import Tracker, TrackIdentity
from vitahub.identity.base import FaceObservation
from vitahub.identity.face_id import ATTEMPT_EVERY_S, FaceIdentifier
from vitahub.identity.gallery import Gallery
from vitahub.identity.stub import StubFaceEngine
from vitahub.models import Detection


def _unit(v):
    arr = np.asarray(v, dtype=np.float32)
    return arr / np.linalg.norm(arr)


MARIA = _unit([1, 0, 0])
PEPE = _unit([0, 1, 0])
GALLERY = Gallery(people={"maria": MARIA, "pepe": PEPE})
# Persona con caja (100,0,200,300); su cara, dentro y de 50 px de alto.
PERSON = Detection("person", 0.9, (100, 0, 200, 300))
FACE = FaceObservation((130, 20, 170, 70), MARIA)


def _identifier(responses):
    return FaceIdentifier(StubFaceEngine(responses), GALLERY, match_threshold=0.4)


def _matches(tracker, dets, now):
    return tracker.observe("cam-a", dets, now).matches


def test_two_consistent_matches_label_the_track():
    ident = _identifier([[FACE], [FACE]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    assert m0[0].track.identity is None  # una sola coincidencia no basta
    assert m0[0].track.pending_identity == TrackIdentity("maria", 1.0)
    m1 = _matches(tracker, [PERSON], ATTEMPT_EVERY_S)
    ident.identify(None, "cam-a", m1, ATTEMPT_EVERY_S)
    assert m1[0].track.identity == TrackIdentity("maria", 1.0)


def test_rate_limit_one_extraction_per_second_per_camera():
    ident = _identifier([[FACE], [FACE]])
    tracker = Tracker()
    ident.identify(None, "cam-a", _matches(tracker, [PERSON], 0.0), 0.0)
    ident.identify(None, "cam-a", _matches(tracker, [PERSON], 0.4), 0.4)  # dentro de la ventana
    assert ident._engine.calls == 1  # type: ignore[attr-defined]


def test_no_extraction_when_all_tracks_identified():
    ident = _identifier([[FACE]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    m0[0].track.identity = TrackIdentity("maria", 0.9)
    ident.identify(None, "cam-a", m0, 0.0)
    assert ident._engine.calls == 0  # type: ignore[attr-defined]


def test_small_face_is_ignored():
    tiny = FaceObservation((130, 20, 160, 50), MARIA)  # 30 px < MIN_FACE_PX
    ident = _identifier([[tiny], [tiny]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    assert m0[0].track.pending_identity is None


def test_face_outside_every_track_is_ignored():
    outside = FaceObservation((400, 400, 460, 460), MARIA)
    ident = _identifier([[outside]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    assert m0[0].track.pending_identity is None


def test_unknown_face_never_labels():
    stranger = FaceObservation((130, 20, 170, 70), _unit([0, 0, 1]))
    ident = _identifier([[stranger], [stranger]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    m1 = _matches(tracker, [PERSON], ATTEMPT_EVERY_S)
    ident.identify(None, "cam-a", m1, ATTEMPT_EVERY_S)
    assert m1[0].track.identity is None and m1[0].track.pending_identity is None


def test_conflicting_pending_is_replaced_not_confirmed():
    face_pepe = FaceObservation((130, 20, 170, 70), PEPE)
    ident = _identifier([[FACE], [face_pepe]])
    tracker = Tracker()
    ident.identify(None, "cam-a", _matches(tracker, [PERSON], 0.0), 0.0)
    m1 = _matches(tracker, [PERSON], ATTEMPT_EVERY_S)
    ident.identify(None, "cam-a", m1, ATTEMPT_EVERY_S)
    assert m1[0].track.identity is None
    assert m1[0].track.pending_identity == TrackIdentity("pepe", 1.0)


def test_correction_replaces_identity_with_better_match(caplog):
    # La pista quedó etiquetada como pepe con confianza baja (cruce de pistas);
    # aparece maria con similitud claramente mayor → se corrige.
    ident = _identifier([[FACE]])
    tracker = Tracker()
    far = Detection("person", 0.9, (500, 0, 600, 300))
    m0 = _matches(tracker, [PERSON, far], 0.0)
    labeled = next(m for m in m0 if m.detection is PERSON)
    labeled.track.identity = TrackIdentity("pepe", 0.45)
    ident.identify(None, "cam-a", m0, 0.0)  # far está anónima → sí extrae
    assert labeled.track.identity == TrackIdentity("maria", 1.0)
    assert any("identidad corregida" in r.message for r in caplog.records)


def test_face_assigned_to_smallest_containing_track():
    inner = Detection("person", 0.9, (120, 10, 180, 120))
    ident = _identifier([[FACE], [FACE]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON, inner], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    m1 = _matches(tracker, [PERSON, inner], ATTEMPT_EVERY_S)
    ident.identify(None, "cam-a", m1, ATTEMPT_EVERY_S)
    small = min(m1, key=lambda m: (m.track.bbox[2] - m.track.bbox[0])
                * (m.track.bbox[3] - m.track.bbox[1])).track
    assert small.identity == TrackIdentity("maria", 1.0)
```

- [ ] **Step 2: Verificar que fallan** — `.venv/bin/python -m pytest tests/test_face_identifier.py -v` → FAIL.

- [ ] **Step 3: Implementar `identity/face_id.py`**

```python
"""Etiquetado oportunista de pistas: la cara se busca mientras la pista es anónima.

No hace falta ver la cara durante la caída: si el hub se la vio al entrar o
al sentarse, la etiqueta viaja con la pista y el fall_detected sale ya
identificado. El coste se controla: solo se extraen caras si hay alguna
pista sin identidad, y como mucho una vez por segundo y por cámara (una
extracción sirve a todas las pistas del frame).
"""
from __future__ import annotations

from vitahub.analytics.tracker import Match, Track, TrackIdentity
from vitahub.identity.base import FaceEngine, FaceObservation
from vitahub.identity.gallery import Gallery, best_match
from vitahub.logging_setup import get_logger

_log = get_logger("identity")

# Alto mínimo de cara en píxeles para intentar el match: por debajo (persona
# lejos, substream) el embedding es ruido y produce falsos matches.
MIN_FACE_PX = 40
# Cadencia máxima de extracción por cámara mientras haya pistas anónimas.
ATTEMPT_EVERY_S = 1.0
# Coincidencias consistentes necesarias para etiquetar: una sola puede ser
# un frame ruidoso.
CONFIRM_MATCHES = 2


def _contains(bbox: tuple[int, int, int, int], x: float, y: float) -> bool:
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _area(bbox: tuple[int, int, int, int]) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def _owner(face: FaceObservation, matches: list[Match]) -> Track | None:
    """La pista cuyo bbox contiene el centro de la cara; la más ajustada si hay varias."""
    cx = (face.bbox[0] + face.bbox[2]) / 2.0
    cy = (face.bbox[1] + face.bbox[3]) / 2.0
    candidates = [m.track for m in matches if _contains(m.track.bbox, cx, cy)]
    if not candidates:
        return None
    return min(candidates, key=lambda t: _area(t.bbox))


class FaceIdentifier:
    def __init__(self, engine: FaceEngine, gallery: Gallery, match_threshold: float) -> None:
        self._engine = engine
        self._gallery = gallery
        self._threshold = match_threshold
        self._last_attempt: dict[str, float] = {}

    def identify(self, frame: object, camera_id: str, matches: list[Match], now: float) -> None:
        if not any(m.track.identity is None for m in matches):
            return
        last = self._last_attempt.get(camera_id)
        if last is not None and now - last < ATTEMPT_EVERY_S:
            return
        self._last_attempt[camera_id] = now

        for face in self._engine.extract(frame):
            if face.bbox[3] - face.bbox[1] < MIN_FACE_PX:
                continue
            track = _owner(face, matches)
            if track is None:
                continue
            matched = best_match(face.embedding, self._gallery, self._threshold)
            if matched is None:
                continue
            person_id, sim = matched
            self._apply(camera_id, track, person_id, sim)

    def _apply(self, camera_id: str, track: Track, person_id: str, sim: float) -> None:
        if track.identity is not None:
            # Corrección de cruce de pistas: solo si otra persona aparece con
            # una similitud claramente mejor que la que etiquetó la pista.
            if person_id != track.identity.person_id and sim > track.identity.confidence:
                _log.warning(
                    "cam %s pista %d: identidad corregida %s → %s (sim %.2f > %.2f)",
                    camera_id, track.track_id, track.identity.person_id, person_id,
                    sim, track.identity.confidence,
                )
                track.identity = TrackIdentity(person_id, sim)
            return
        pending = track.pending_identity
        if pending is not None and pending.person_id == person_id:
            track.identity = TrackIdentity(person_id, max(sim, pending.confidence))
            track.pending_identity = None
            return
        track.pending_identity = TrackIdentity(person_id, sim)
```

- [ ] **Step 4: Verificar, lint, tipos, commit**

Run: `.venv/bin/python -m pytest tests/test_face_identifier.py -v && .venv/bin/ruff check src tests && .venv/bin/mypy`

```bash
git add src/vitahub/identity/face_id.py tests/test_face_identifier.py
git commit -m "feat: FaceIdentifier — etiqueta pistas anónimas con regla de 2 coincidencias"
```

---

### Task 7: motor InsightFace, factory y cableado en worker/app

**Files:**
- Create: `src/vitahub/identity/insightface_engine.py`
- Modify: `src/vitahub/factory.py` (`build_face_identifier`, `_require_face_weights`)
- Modify: `src/vitahub/worker.py` (parámetro `identifier`, aislamiento propio)
- Modify: `src/vitahub/app.py` (construcción y paso del identifier)
- Modify: `pyproject.toml` (deps + override mypy)
- Test: `tests/test_factory.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: `FaceIdentifier`, `load_gallery` (Tasks 5-6); `IdentityConfig` (Task 4); firma del worker (Task 2).
- Produces:
  - `factory.build_face_identifier(cfg: HubConfig, env: Mapping[str, str]) -> FaceIdentifier | None` — `None` si `identity.enabled` es false; `ConfigError` si faltan pesos o galería.
  - `worker.process_frame(..., fall_engine=None, tracker=None, identifier=None)`.
  - Env vars: `VITAHUB_FACE_WEIGHTS` (default `/app/models/insightface`), `VITAHUB_FACES_DIR` (default `/data/faces`).

- [ ] **Step 1: Dependencias**

En `pyproject.toml`, `dependencies` gana:

```toml
    "insightface>=0.7",
    "onnxruntime>=1.18",
    "numpy>=1.26",
```

y el override de mypy pasa a:

```toml
module = ["ultralytics.*", "cv2.*", "insightface.*", "onnxruntime.*"]
```

Instalar en el venv: `.venv/bin/pip install -e ".[dev]"` (insightface compila una extensión; en Mac requiere Xcode CLT — si la instalación local falla, seguir igualmente: **ningún test unitario importa insightface**, el import es perezoso).

- [ ] **Step 2: Tests que fallan (factory + worker)**

Añadir a `tests/test_factory.py` (imitar el patrón de construcción de `HubConfig` que ya use el fichero; si construye configs a mano, usar `IdentityConfig(enabled=...)` dentro de `InferenceConfig`):

```python
def test_build_face_identifier_disabled_returns_none(tmp_path):
    cfg = _hub_config(identity=IdentityConfig(enabled=False))  # helper del fichero
    assert build_face_identifier(cfg, {}) is None


def test_build_face_identifier_missing_weights_raises(tmp_path):
    cfg = _hub_config(identity=IdentityConfig(enabled=True))
    env = {"VITAHUB_FACE_WEIGHTS": str(tmp_path / "no-existe"),
           "VITAHUB_FACES_DIR": str(tmp_path)}
    with pytest.raises(ConfigError, match="modelos de reconocimiento facial"):
        build_face_identifier(cfg, env)


def test_build_face_identifier_missing_faces_dir_raises(tmp_path):
    weights = tmp_path / "insightface" / "models" / "buffalo_s"
    weights.mkdir(parents=True)
    (weights / "det.onnx").touch()
    cfg = _hub_config(identity=IdentityConfig(enabled=True))
    env = {"VITAHUB_FACE_WEIGHTS": str(tmp_path / "insightface"),
           "VITAHUB_FACES_DIR": str(tmp_path / "faces-no-existe")}
    with pytest.raises(ConfigError, match="faces"):
        build_face_identifier(cfg, env)
```

(El camino feliz de `build_face_identifier` no se testea en unitario: instanciaría InsightFace de verdad. Se verifica con `replay_video.py` en Task 8 — mismo criterio que `PersonDetector.from_weights`.)

Añadir a `tests/test_worker.py`:

```python
def test_identifier_exception_does_not_break_falls(caplog, monkeypatch):
    class _BoomIdentifier:
        def identify(self, frame, camera_id, matches, now):
            raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "_identity_failure_logged", set())
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    fall = FallEngine(hub_id="hub-1", min_score=0.3)
    tracker = Tracker()  # uno solo: la pista debe persistir entre frames
    sink = _RecordingSink()
    detector = StubDetector(person_count=1, keypoints=LYING_KPS, bbox=LYING_BOX)
    all_events = []
    now = 0.0
    for _ in range(8):
        all_events += process_frame(CAM, None, detector, engine, sink, now,
                                    fall_engine=fall, tracker=tracker,
                                    identifier=_BoomIdentifier())
        now += 0.5
    # Degradación silenciosa: sin identidad, pero la caída sale igual (person null).
    fall_events = [e for e in all_events if e.type == "fall_detected"]
    assert len(fall_events) == 1
    assert fall_events[0].payload["person"] is None
    assert sum("identificador" in r.message for r in caplog.records) == 1
```

- [ ] **Step 3: Verificar que fallan** — `.venv/bin/python -m pytest tests/test_factory.py tests/test_worker.py -v` → FAIL.

- [ ] **Step 4: Implementar**

`src/vitahub/identity/insightface_engine.py` (import perezoso: el módulo se puede importar sin insightface instalado):

```python
"""Envoltorio de InsightFace (SCRFD + ArcFace) con los pesos embebidos en la imagen.

Fino a propósito y sin tests unitarios (instanciarlo carga los ONNX): se
verifica con scripts/replay_video.py sobre vídeo real, igual que los
detectores YOLO.
"""
from __future__ import annotations

from typing import Any

from vitahub.identity.base import FaceEngine, FaceObservation


class InsightFaceEngine(FaceEngine):
    def __init__(self, app: Any) -> None:
        self._app = app

    @classmethod
    def from_weights(cls, root: str) -> InsightFaceEngine:
        # Import aquí, no arriba: los tests y los hubs sin identidad no
        # necesitan tener insightface instalado.
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(
            name="buffalo_s",
            root=root,
            allowed_modules=["detection", "recognition"],
        )
        # ctx_id=0 usa GPU si onnxruntime la ofrece (Orin) y cae a CPU si no (Mac).
        app.prepare(ctx_id=0, det_size=(640, 640))
        return cls(app)

    def extract(self, image: object) -> list[FaceObservation]:
        return [
            FaceObservation(
                bbox=(int(f.bbox[0]), int(f.bbox[1]), int(f.bbox[2]), int(f.bbox[3])),
                embedding=f.normed_embedding,
            )
            for f in self._app.get(image)
        ]
```

En `factory.py`:

```python
_DEFAULT_FACE_WEIGHTS = "/app/models/insightface"
_DEFAULT_FACES_DIR = "/data/faces"


def _require_face_weights(root: str) -> None:
    # FaceAnalysis descarga el pack de internet si falta — inaceptable en un
    # hogar sin conexión y en /app de solo lectura. Mejor decir qué falta.
    pack = Path(root) / "models" / "buffalo_s"
    if not pack.is_dir() or not any(pack.iterdir()):
        raise ConfigError(
            f"modelos de reconocimiento facial no encontrados en {pack} — en el "
            "contenedor los embebe el Dockerfile; en local, descárgalos con "
            "'python scripts/download_face_models.py'"
        )


def build_face_identifier(cfg: HubConfig, env: Mapping[str, str]) -> FaceIdentifier | None:
    if not cfg.inference.identity.enabled:
        return None
    root = env.get("VITAHUB_FACE_WEIGHTS", _DEFAULT_FACE_WEIGHTS)
    faces_dir = Path(env.get("VITAHUB_FACES_DIR", _DEFAULT_FACES_DIR))
    _require_face_weights(root)
    if not faces_dir.is_dir():
        raise ConfigError(
            f"inference.identity.enabled es true pero no existe {faces_dir} — "
            "crea /data/faces/<person_id>/ con 3-5 fotos de la persona"
        )
    engine = InsightFaceEngine.from_weights(root)
    gallery = load_gallery(faces_dir, engine)
    _log.info(
        "identidad activada: %d personas enroladas (umbral %.2f)",
        len(gallery.people), cfg.inference.identity.match_threshold,
    )
    return FaceIdentifier(
        engine, gallery, match_threshold=cfg.inference.identity.match_threshold
    )
```

con imports `from collections.abc import Mapping`, `from vitahub.identity.face_id import FaceIdentifier`, `from vitahub.identity.gallery import load_gallery`, `from vitahub.identity.insightface_engine import InsightFaceEngine`.

En `worker.py` — parámetro nuevo y aislamiento propio (la identidad degrada a `person: null`, nunca tumba la caída):

```python
_identity_failure_logged: set[str] = set()
```

firma: `..., tracker: Tracker | None = None, identifier: FaceIdentifier | None = None` (import `from vitahub.identity.face_id import FaceIdentifier`). Dentro del bloque de caídas, entre `tracker.observe` y `fall_engine.observe`:

```python
            if identifier is not None:
                try:
                    identifier.identify(frame, camera.id, update.matches, now)
                except Exception:  # noqa: BLE001 — sin identidad hay person null, no un hub caído
                    if camera.id not in _identity_failure_logged:
                        _identity_failure_logged.add(camera.id)
                        _log.exception(
                            "cam %s: error en el identificador (se silencia a partir de ahora)",
                            camera.id,
                        )
```

En `app.py` — tras construir el sink (para que un `ConfigError` de identidad salga en arranque como los demás):

```python
    identifier = build_face_identifier(cfg, env)
```

(import de `build_face_identifier` junto a los otros de factory; `build_face_identifier` ya devuelve `None` con identidad apagada y ya loguea la activación). El closure `worker(...)` y `_camera_loop` reenvían `identifier` a `process_frame`, igual que `tracker` en Task 2.

- [ ] **Step 5: Verificar todo y commit**

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check src tests scripts && .venv/bin/mypy`
Expected: PASS.

```bash
git add src/vitahub/identity/insightface_engine.py src/vitahub/factory.py src/vitahub/worker.py src/vitahub/app.py pyproject.toml tests/test_factory.py tests/test_worker.py
git commit -m "feat: identidad cableada — InsightFace, factory y aislamiento en el worker"
```

---

### Task 8: replay con galería, Dockerfile, script de descarga y docs

**Files:**
- Modify: `scripts/replay_video.py` (`--faces`, `--match-threshold`)
- Create: `scripts/download_face_models.py`
- Modify: `Dockerfile` (deps de compilación + pesos embebidos + env)
- Modify: `config/hub.example.yaml` (bloque `identity`)
- Modify: `docs/como-funciona.md` (sección Identidad, tablas de env y módulos, payload)
- Test: `tests/test_replay_video.py` (solo si testea el parser de argumentos — mantenerlo verde)

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: calibración end-to-end (`replay_video.py --faces`) y despliegue (imagen con pesos embebidos).

- [ ] **Step 1: `scripts/download_face_models.py`**

```python
"""Descarga el pack buffalo_s de InsightFace a la ruta de pesos embebidos.

Uso (build de la imagen y desarrollo local):

    python scripts/download_face_models.py [/app/models/insightface]
"""
from __future__ import annotations

import sys


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "/app/models/insightface"
    from insightface.app import FaceAnalysis

    # Instanciar descarga el pack si falta; prepare() valida que los ONNX cargan.
    app = FaceAnalysis(name="buffalo_s", root=root,
                       allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=0, det_size=(640, 640))
    print(f"modelos buffalo_s listos en {root}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: `replay_video.py` con galería**

- Args nuevos: `parser.add_argument("--faces", default="", help="carpeta de enrolados (activa identidad)")` y `parser.add_argument("--match-threshold", type=float, default=0.4)`.
- `replay(...)` gana `faces: str` y `match_threshold: float`; tras crear `tracker`:

```python
    identifier = None
    if faces:
        from vitahub.identity.face_id import FaceIdentifier
        from vitahub.identity.gallery import load_gallery
        from vitahub.identity.insightface_engine import InsightFaceEngine

        engine_face = InsightFaceEngine.from_weights(
            os.environ.get("VITAHUB_FACE_WEIGHTS", "/app/models/insightface")
        )
        identifier = FaceIdentifier(
            engine_face, load_gallery(Path(faces), engine_face), match_threshold
        )
```

(con `import os` y `from pathlib import Path` arriba) y `process_frame(..., fall_engine=fall, tracker=tracker, identifier=identifier)`. `main()` pasa los dos args nuevos.

- [ ] **Step 3: Dockerfile**

- Línea de apt: añadir `build-essential` (insightface compila una extensión C++ al instalarse):

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 ffmpeg build-essential && rm -rf /var/lib/apt/lists/*
```

- Tras la descarga de pesos YOLO:

```dockerfile
RUN python scripts/download_face_models.py /app/models/insightface
```

- Al bloque `ENV`: `VITAHUB_FACE_WEIGHTS=/app/models/insightface`.

- [ ] **Step 4: `config/hub.example.yaml`**

Dentro de `inference:`, tras el bloque `fall:`:

```yaml
  identity:
    enabled: false          # true para etiquetar los fall_* con la persona (requiere fall.enabled)
    match_threshold: 0.4    # similitud mínima contra /data/faces/<person_id>/ para etiquetar
```

- [ ] **Step 5: `docs/como-funciona.md`**

1. Tabla de módulos: filas nuevas para `analytics/tracker.py`, `identity/face_id.py`, `identity/gallery.py`, `identity/insightface_engine.py`; actualizar la fila de `analytics/fall_engine.py` ("consume pistas del tracker") y la de `worker.py` (orquesta tracker + identidad).
2. Tabla de variables de entorno: `VITAHUB_FACE_WEIGHTS` (default `/app/models/insightface`) y `VITAHUB_FACES_DIR` (default `/data/faces`).
3. YAML de configuración de ejemplo: el bloque `identity` de arriba.
4. En «Caídas», actualizar el párrafo del emparejamiento (ahora vive en el tracker) y el JSON de ejemplo del evento añadiendo `"person":{"id":"maria","confidence":0.87}`; en la tabla de tipos, añadir `person` al payload de los tres `fall_*`.
5. Sección nueva «### Identidad de la persona» tras «El evento» dentro de «Caídas», con este contenido (ajustar el tono al documento):

> Opcional (`inference.identity.enabled`, requiere `inference.fall.enabled`). El instalador deja
> 3-5 fotos de la cara de cada persona en `/data/faces/<person_id>/`; al arrancar, el hub calcula
> un embedding medio por persona (InsightFace, en local). Mientras una pista es anónima, el hub
> busca su cara como mucho una vez por segundo y por cámara; dos coincidencias consistentes por
> encima de `match_threshold` etiquetan la pista, y la etiqueta viaja con la persona hasta que la
> pista se pierde (3 s sin verse). Por eso no hace falta ver la cara durante la caída: basta con
> habérsela visto al entrar. Los eventos `fall_*` llevan `person: {id, confidence}` o `null`
> (persona no enrolada, cara nunca vista o identidad apagada). **El hub etiqueta, no filtra**: la
> caída de una visita se emite igual; decidir por identidad es del consumidor en AWS. Del hogar
> sale solo el alias: ni fotos ni embeddings se emiten, loguean ni suben. Limitaciones v1: caras
> < 40 px no se intentan (persona lejos en substream) y una oclusión > 3 s vuelve a `unknown`
> hasta ver la cara otra vez. El umbral se calibra con
> `scripts/replay_video.py --faces <carpeta>` sobre vídeo real.

- [ ] **Step 6: Verificación final completa**

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check src tests scripts && .venv/bin/mypy`
Expected: PASS. Si hay un vídeo de prueba y los pesos descargados en local, humo opcional:
`python scripts/replay_video.py <video>.mp4 --weights yolo11n-pose.pt --faces /ruta/faces` → los `fall_*` salen con `person`.

- [ ] **Step 7: Commit**

```bash
git add scripts/replay_video.py scripts/download_face_models.py Dockerfile config/hub.example.yaml docs/como-funciona.md tests/test_replay_video.py
git commit -m "feat: replay con galería, pesos faciales embebidos y docs de identidad"
```

---

## Verificación de cierre (tras Task 8)

- [ ] `.venv/bin/python -m pytest` — todo verde.
- [ ] `.venv/bin/ruff check src tests scripts` y `.venv/bin/mypy` — limpios.
- [ ] Recorrer la spec sección a sección y confirmar cobertura (§3 → Tasks 1-2-5-6-7; §4 → Task 6; §5 → Task 3; §6 → Tasks 4-7; §7 → tests de cada task; §8 → docs en Task 8).
- [ ] Invocar la skill `superpowers:requesting-code-review` antes de dar la rama por terminada.
