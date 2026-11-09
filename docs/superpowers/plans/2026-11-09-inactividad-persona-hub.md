# Inactividad de la persona objetivo (hub) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The hub detects when an identified person stays in the same spot for at least 5 minutes. It then emits `inactivity_started`. When the episode ends, it emits `inactivity_ended` with the total duration. The tracker also keeps the identity of a still person who is hidden for a few seconds.

**Architecture:**
- A new engine, `InactivityEngine` (`analytics/inactivity_engine.py`), consumes the same `TrackerUpdate` as `FallEngine`.
- `Tracker` parks the identity of identified tracks that were still when they were lost. A new anonymous track that appears in almost the same box inherits that identity, marked as provisional. `FaceIdentifier` keeps checking provisional identities.
- The tracker exists when falls or inactivity is enabled.

**Tech Stack:** Python 3.12, pytest, ruff, mypy. Same stack as the rest of the repo, with no new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-09-inactividad-persona-design.md`

**Paired plan:** the cloud side is in `vita-docker-rule-engine/docs/superpowers/plans/2026-11-09-camera-inactivity.md`. Deploy that one first (spec §7). This plan does not depend on it to compile or test.

## Global Constraints

- Comments, docstrings, log messages and docs are in Spanish, matching the repo style.
- No new top-level fields in the event envelope (`models.Event`). Everything new goes in `payload` (spec §3.5).
- Event types are exactly `inactivity_started` and `inactivity_ended`, both with severity `"info"`.
- `reason` is one of `"moved"`, `"track_lost"` or `"identity_changed"`. The hub never emits an end event on shutdown or restart (spec §3.4).
- `episode_id` format: `f"{camera.id}-{int(clock().timestamp())}-{n}"`, the same as falls.
- Default `inference.inactivity`: `enabled: false`, `min_minutes: 5.0`.
- Default `PARK_TTL_S = 120.0`.
- A failure in the inactivity engine is logged once per camera and never breaks presence or falls.
- CI gate: `ruff check . && mypy && pytest -v` all green after every task.

## Review Focus

1. **Frames stop for minutes and then come back** (RTSP drop, worker restart). Expected: the episode ends with `track_lost`, and `ended_at` is the last time the person was seen. The episode must not resume or inflate. Test in Task 5.
2. **Two identified people sitting still in the same camera.** Expected: two independent episodes with distinct `episode_id`, and the end of one does not touch the other. Test in Task 4.
3. **Detector jitter while sitting.** A single frame with the feet point far off (a bad box) must not end the episode. Test in Task 4.
4. **Inactivity enabled with `detector: person_yolo`** (no keypoints, falls off). Expected: the config loads, the identity is accepted, and the feet point uses the bottom of the box. Tests in Tasks 1 and 4.
5. **Provisional identity contradicted by the face.** A visitor sits where the target person was hidden and inherits the identity. The first valid face match of the visitor relabels the track. The inactivity engine then ends the old episode with `identity_changed`. Tests in Tasks 3 and 4.

---

### Task 1: Configuración `inference.inactivity`

**Files:**
- Modify: `src/vitahub/config.py` (dataclasses near `FallConfig`/`IdentityConfig`; `_identity_from_raw` at ~429; `load_config` at ~520)
- Modify: `config/hub.example.yaml` (if it documents `inference.fall`/`identity`, add the commented `inactivity` block next to them)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `InactivityConfig(enabled: bool = False, min_minutes: float = 5.0)`
  - `InferenceConfig.inactivity: InactivityConfig`, with `field(default_factory=InactivityConfig)`
  - `_inactivity_from_raw(raw: object) -> InactivityConfig`
  - `_identity_from_raw(raw: object, fall: FallConfig, inactivity: InactivityConfig) -> IdentityConfig`

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`, reusing that file's YAML-writing helper:
  - `test_inactivity_defaults_off`: with no `inactivity` section, `cfg.inference.inactivity == InactivityConfig(enabled=False, min_minutes=5.0)`.
  - `test_inactivity_with_identity_and_person_yolo_loads`: `detector: person_yolo`, `fall` off, `identity.enabled: true`, `inactivity: {enabled: true, min_minutes: 5}` all load. `cfg.inference.identity.enabled is True`.
  - `test_inactivity_requires_identity`: `inactivity.enabled: true` with identity off raises `ConfigError`. The message contains `"inference.inactivity.enabled requiere 'inference.identity.enabled: true'"`.
  - `test_identity_requires_fall_or_inactivity`: identity on with both fall and inactivity off raises `ConfigError`. The message contains `"caídas o inactividad"`.
  - `test_inactivity_min_minutes_must_be_positive`: `min_minutes: 0` and `min_minutes: "x"` each raise `ConfigError`.
  - Update any existing test that asserts the old identity message (`"solo etiqueta eventos de caída"`).

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/test_config.py -v`
Expected: the new tests FAIL (`InactivityConfig` is not defined).

- [ ] **Step 3: Implement the config changes**
  - `_inactivity_from_raw` follows the `_fall_from_raw` pattern: it requires a mapping, and `min_minutes` must be numeric and `> 0`. It does not check the detector.
  - In `load_config`, parse inactivity before identity and pass it to `_identity_from_raw`.
  - The identity guard becomes `enabled and not (fall.enabled or inactivity.enabled)`, with the message `"inference.identity.enabled requiere 'inference.fall.enabled: true' o 'inference.inactivity.enabled: true' (la identidad solo etiqueta caídas o inactividad)"`.
  - After both are parsed, raise if `inactivity.enabled and not identity.enabled`, with the message `"inference.inactivity.enabled requiere 'inference.identity.enabled: true' (sin identidad no hay persona objetivo)"`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `pytest tests/test_config.py -v && ruff check . && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/config.py config/hub.example.yaml tests/test_config.py
git commit -m "feat(inactivity): configuración inference.inactivity y la identidad admite inactividad sin caídas"
```

---

### Task 2: Tracker — identidad provisional y aparcado de pistas quietas

**Files:**
- Modify: `src/vitahub/analytics/tracker.py`
- Test: `tests/test_tracker.py`

**Interfaces:**
- Produces (in `vitahub.analytics.tracker`):
  - `TrackIdentity(person_id: str, confidence: float, provisional: bool = False)`. It stays frozen, and existing two-argument calls keep working.
  - `@dataclass(frozen=True) class ParkedIdentity: track_id: int; identity: TrackIdentity; bbox: tuple[int, int, int, int]; parked_at: float`. `parked_at` is the lost track's `last_seen`.
  - `@dataclass(frozen=True) class Resume: track: Track; previous_track_id: int`
  - `TrackerUpdate` gains three fields, each with `field(default_factory=list)` so existing constructors keep working:
    - `parked: list[ParkedIdentity]`: tracks parked in this call;
    - `resumed: list[Resume]`: new tracks that inherited a parked identity in this call;
    - `expired: list[ParkedIdentity]`: parked identities that reached `PARK_TTL_S` in this call.
  - `Track` gains the stillness bookkeeping the builder needs, for example `still_bbox` and `still_since`. These are private to the tracker: no other module reads them.
  - Module constants:
    - `PARK_TTL_S = 120.0`;
    - `PARK_STILL_S`: the minimum time the track had been still when it was lost;
    - `STILL_IOU`: the IoU against the stillness reference box that still counts as "the same spot";
    - `RESUME_IOU`: stricter than `IOU_MATCH`.

    The builder picks the initial values of `PARK_STILL_S`, `STILL_IOU` and `RESUME_IOU`, with a comment that they get calibrated with `replay_video.py` (spec §8).

- [ ] **Step 1: Write the failing tests** in `tests/test_tracker.py`. Each test sets `track.identity = TrackIdentity("ana", 0.7)` on the match from the first `observe`, then drives time with explicit `now`.
  - `test_still_identified_track_is_parked_when_lost`:
    - Same box for 5 s, then empty frames until `TRACK_TTL_S` expires.
    - That update has the track in `lost`, plus one `ParkedIdentity` with `track_id` equal to the old id, `identity.person_id == "ana"` and `parked_at` equal to the last time it was seen.
  - `test_moving_track_is_not_parked`: the box shifts every frame (IoU against the previous box below `STILL_IOU`) until it is lost. `parked == []`.
  - `test_anonymous_track_is_not_parked`.
  - `test_new_track_in_same_box_resumes_parked_identity`:
    - The track is parked, and 4 s after it was last seen a detection appears in the same box.
    - The new track has `identity == TrackIdentity("ana", 0.7, provisional=True)`.
    - `resumed == [Resume(track=<new track>, previous_track_id=<old id>)]`.
    - The parked identity is consumed: a later lost/new track in that box does not resume it again.
  - `test_new_track_elsewhere_does_not_resume`: a detection far away stays anonymous and `resumed == []`.
  - `test_best_iou_parked_wins`: two parked identities ("ana" and "luis") in neighbouring boxes. The new detection overlaps "luis" more, so it inherits "luis".
  - `test_parked_identity_expires_after_ttl`:
    - No detections in the box. In the first `observe` with `now - parked_at >= PARK_TTL_S`, `expired` holds that `ParkedIdentity`.
    - It is reported once only.
  - `test_frames_resume_after_long_gap_expire_before_resume` (Review Focus 1):
    - Last seen at t=10. The next `observe` is at t=10+600 with a detection in the same box.
    - That single call reports the track as `lost`, the parked identity in `expired`, and the new track as anonymous with `resumed == []`.
    - The rule: expire parked identities before trying to resume them.
  - Existing tracker tests stay green without changes.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/test_tracker.py -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement parking in `Tracker.observe`**

Order inside `observe`, per camera:
1. Expire tracks. The same rule as today.
2. For each lost track that is identified and has been still for at least `PARK_STILL_S` at its `last_seen`, append a `ParkedIdentity` to the camera's parked list and to `update.parked`.
3. Drop parked identities with `now - parked_at >= PARK_TTL_S` into `update.expired`.
4. Match as today.
5. For each new track, find the live parked identity with the highest IoU at or above `RESUME_IOU`. If one exists, set `identity=replace(p.identity, provisional=True)`, consume the parked identity and append a `Resume`.

Stillness is updated in each match: if the IoU of the new box against `still_bbox` is below `STILL_IOU`, reset `still_bbox` to the current box and `still_since` to `now`.

Update the module docstring with one paragraph on parking. The tracker knows nothing about inactivity.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `pytest tests/test_tracker.py tests/test_fall_engine.py -v && ruff check . && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/analytics/tracker.py tests/test_tracker.py
git commit -m "feat(tracker): aparcar la identidad de pistas quietas perdidas y reanudarla en la misma caja"
```

---

### Task 3: FaceIdentifier — comprobar identidades provisionales

**Files:**
- Modify: `src/vitahub/identity/face_id.py` (`identify` at ~53, `_apply` at ~73)
- Test: `tests/test_face_identifier.py`

**Interfaces:**
- Consumes: `TrackIdentity.provisional` (Task 2).
- Produces: no new public names. This only changes behaviour.

- [ ] **Step 1: Write the failing tests** in `tests/test_face_identifier.py`, using the existing `StubFaceEngine` helpers:
  - `test_provisional_track_keeps_extraction_running`: the only track has `TrackIdentity("ana", 0.7, provisional=True)`. `identify` calls the engine (the stub records the call). With a non-provisional identity it does not call the engine, which is the existing behaviour.
  - `test_provisional_relabelled_by_any_valid_match_of_other_person`:
    - Provisional "ana" at 0.9, and the face matches "luis" with sim 0.45 (above the 0.4 threshold, below 0.9).
    - The track becomes `TrackIdentity("luis", 0.45)` with `provisional=False`.
    - Without the provisional mark, the same input keeps "ana" (existing rule: `sim > confidence`).
  - `test_provisional_confirmed_by_same_person`: a match with "ana" at sim 0.5 gives `TrackIdentity("ana", 0.7, provisional=False)`, keeping `max(sim, confidence)`.
  - `test_provisional_respects_attempt_cadence`: a second call within `ATTEMPT_EVERY_S` does not extract.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/test_face_identifier.py -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement the change**
  - The early return in `identify` checks `identity is None or identity.provisional`.
  - In `_apply`, a provisional identity:
    - is relabelled by any match of another person, with the existing warning log extended with `provisional`;
    - is confirmed by a match of the same person, which clears the mark.
  - Update the module docstring: extraction also runs while some identity is provisional.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `pytest tests/test_face_identifier.py -v && ruff check . && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/identity/face_id.py tests/test_face_identifier.py
git commit -m "feat(identity): la cara sigue comprobando identidades heredadas hasta confirmarlas"
```

---

### Task 4: InactivityEngine — episodios, punto de pies y eventos

**Files:**
- Create: `src/vitahub/analytics/inactivity_engine.py`
- Test: `tests/test_inactivity_engine.py`

**Interfaces:**
- Consumes: `Tracker`, `TrackerUpdate`, `Match`, `TrackIdentity` (Task 2); `_person_payload` and `_utcnow` from `fall_engine`. Import them, do not copy them. If a leading underscore bothers mypy or ruff, move both to a small shared helper and import it from both engines.
- Produces:
  - `InactivityEngine(hub_id: str, min_minutes: float = 5.0, clock: Callable[[], datetime] = _utcnow)`
  - `InactivityEngine.observe(camera: Camera, update: TrackerUpdate, now: float) -> list[Event]`
  - `feet_point(det: Detection) -> tuple[float, float]`
  - `EndReason = Literal["moved", "track_lost", "identity_changed"]`
  - Constants:
    - `RADIUS_FACTOR = 0.5`;
    - `MOVE_CONFIRM_S`;
    - `ANKLE_MIN_CONF`;
    - `L_ANKLE, R_ANKLE = 15, 16`.

    The builder sets `MOVE_CONFIRM_S` and `ANKLE_MIN_CONF`, with a comment that they get calibrated.

Behaviour (spec §3.3–3.5):
- **Episodes.** Episodes live per camera, keyed by `track_id`, and only for tracks with `identity` set (provisional counts). Anonymous tracks are ignored.
- **Durations** use the monotonic `now`. **Wall-clock fields** (`started_at`, `ended_at`) are `clock()` minus the monotonic offset to the instant they describe, in ISO 8601 with offset, like `Event.timestamp`.
- **Feet point.** The midpoint of ankles 15/16 when both have `conf >= ANKLE_MIN_CONF`. Otherwise `((x1 + x2) / 2, y2)`.
- **Scale.** The largest side of the largest box seen during the episode. The episode is out of radius when `dist(feet, anchor) > RADIUS_FACTOR * scale`.
- **Movement.** It is confirmed after being continuously out of radius for `MOVE_CONFIRM_S`.
  - Not emitted yet: discard silently.
  - Already emitted: emit `inactivity_ended(reason="moved", ended_at=<last instant inside the radius>)`.
  - In both cases, start a fresh candidate anchored at the current feet point.
- **Start event.** When still time reaches `min_minutes * 60`, assign `episode_id` and emit `inactivity_started` with `{episode_id, started_at, still_s, person}`. `still_s` is rounded to 3 decimals.
- **Identity change.** When the track's `person_id` differs from the episode's person:
  - if emitted, emit `inactivity_ended(reason="identity_changed")` with `ended_at` = now;
  - then restart the episode for the new person.
- **End event.** `inactivity_ended` payload: `{episode_id, started_at, ended_at, duration_s, reason, person}`. `person` is the identity the episode started with.

- [ ] **Step 1: Write the failing tests** in `tests/test_inactivity_engine.py`
  - Setup: use a real `Tracker`, a fixed `clock`, and `Camera(id="cam1", name="salon", last_ip="10.0.0.5")`. Label the track after the first `observe` with `TrackIdentity("ana", 0.7)`.
  - Add a helper `_run(engine, tracker, frames)` that feeds `(now, detections)` at 2 fps and collects the events.
  - `test_feet_point_uses_ankles_when_confident_else_box_bottom`.
  - `test_short_episode_emits_nothing`: still for 4 min 50 s, then walks away. No events.
  - `test_started_at_min_minutes_with_real_start`:
    - Still from t=0. Exactly one `inactivity_started`, at the first sample with t ≥ 300.
    - `payload["started_at"]` equals the wall time at t=0, `payload["still_s"] >= 300`, and `payload["person"] == {"id": "ana", "confidence": 0.7}`.
    - `type == "inactivity_started"` and `severity == "info"`. It is never repeated while still.
  - `test_standing_up_in_place_does_not_end`:
    - Sitting box `(100, 200, 200, 400)` for 6 min, then standing box `(110, 0, 190, 400)`, with the same bottom.
    - No `inactivity_ended`.
  - `test_one_frame_jitter_does_not_end`: a single sample with the box 300 px away, then back. No end event (Review Focus 3).
  - `test_confirmed_move_ends_with_moved_and_last_inside_time`:
    - Still for 6 min, then out of radius from t=360 onwards.
    - One `inactivity_ended` with `reason == "moved"`, `ended_at` equal to the wall time at t=359.5, `duration_s == 359.5` and the same `episode_id` as the start.
  - `test_identity_changed_ends_episode`:
    - After `started`, set `track.identity = TrackIdentity("luis", 0.5)`.
    - Next `observe`: `inactivity_ended` with `reason == "identity_changed"` for "ana". A new silent episode for "luis" follows (Review Focus 5).
  - `test_two_people_independent_episodes`:
    - Two identified people far apart, both still for 6 min. Two `started` with distinct `episode_id`.
    - One walks away and only its episode ends (Review Focus 2).
  - `test_anonymous_track_never_emits`.
  - `test_person_yolo_boxes_without_keypoints`: same as the started test, but with detections that have `keypoints=None` (Review Focus 4).

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/test_inactivity_engine.py -v`
Expected: FAIL (the module does not exist).

- [ ] **Step 3: Implement `inactivity_engine.py`**

Write a module docstring in the style of `fall_engine.py`. It must include the state diagram (candidate → emitted → ended), and say that a hub restart emits no end event and that the rule engine absorbs it (spec §3.4). Ignore `update.parked`, `update.resumed` and `update.expired` for now (Task 5).

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `pytest tests/test_inactivity_engine.py -v && ruff check . && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/analytics/inactivity_engine.py tests/test_inactivity_engine.py
git commit -m "feat(inactivity): motor de episodios de inactividad por pista identificada"
```

---

### Task 5: InactivityEngine — ocultaciones (aparcado, reanudación, caducidad)

**Files:**
- Modify: `src/vitahub/analytics/inactivity_engine.py`
- Test: `tests/test_inactivity_engine.py`

**Interfaces:**
- Consumes: `TrackerUpdate.parked`, `.resumed`, `.expired` and `.lost` (Task 2).
- Produces: no new public names.

Behaviour (spec §4.2):
- **Lost and parked** (its id is in `update.parked`): the episode is kept, keyed by the old `track_id`, with `last_seen` frozen.
- **Lost and not parked:** if emitted, emit `inactivity_ended(reason="track_lost", ended_at=<last seen>)`. Drop it in all cases.
- **`Resume(track, previous_track_id)`:** move the episode to `track.track_id`. Time keeps counting through the hidden gap, with the same anchor and `episode_id`. If the new feet point is out of radius, the normal movement rule applies.
- **`expired` parked identity:** if emitted, emit `inactivity_ended(reason="track_lost", ended_at=<parked_at wall time>)`. Drop it in all cases.

- [ ] **Step 1: Write the failing tests**
  - `test_four_second_occlusion_keeps_episode`:
    - Still for 6 min (started emitted), then empty frames for 4 s, then the same box again.
    - No end event, and the track inherits "ana" (provisional).
    - Walking away later ends with the original `episode_id`, and `duration_s` includes the hidden 4 s.
  - `test_occlusion_before_threshold_keeps_counting`: still for 3 min, hidden for 10 s, back in the same box. `started` fires at t ≥ 300 counted from the original start.
  - `test_parked_expiry_ends_with_track_lost`: started emitted, then no detections until `PARK_TTL_S` passes. `inactivity_ended` with `reason == "track_lost"` and `ended_at` equal to the wall time of the last sighting.
  - `test_long_frame_gap_ends_with_last_seen` (Review Focus 1): started emitted at t≈300, last frame at t=400, next frame at t=1000 with the same box. Exactly one `inactivity_ended(reason="track_lost")` with `ended_at` = wall(400). The new track starts a fresh silent candidate.
  - `test_moving_track_lost_ends_immediately`: the person walks while emitted (not still at loss), so the track is not parked. The end comes on the `lost` update.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/test_inactivity_engine.py -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement the handling** of `lost`, `parked`, `resumed` and `expired` in `observe`. Process them before matches, in this order: `expired`, `lost`/`parked`, then `resumed`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `pytest tests/test_inactivity_engine.py tests/test_tracker.py -v && ruff check . && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/analytics/inactivity_engine.py tests/test_inactivity_engine.py
git commit -m "feat(inactivity): el episodio sobrevive a ocultaciones cortas y cierra con track_lost al caducar"
```

---

### Task 6: Cableado en el hub, replay y documentación

**Files:**
- Modify: `src/vitahub/worker.py` (`process_frame`)
- Modify: `src/vitahub/app.py` (~90–131 engine creation and the `worker` closure; `_camera_loop` signature ~280 and its `process_frame` call ~330)
- Modify: `scripts/replay_video.py` (the sink filter at ~42, the `replay` signature and `main` arguments)
- Modify: `docs/superpowers/specs/2026-09-02-identidad-persona-design.md` (the sentence "en v1 la identidad solo etiqueta caídas" and the out-of-date §3), `docs/backlog.md`, and `README.md` if it documents `inference.*`
- Test: `tests/test_worker.py`, `tests/test_app_loop.py`, `tests/test_replay_video.py`

**Interfaces:**
- Consumes: `InactivityEngine` (Tasks 4–5) and `InactivityConfig` (Task 1).
- Produces: `process_frame(..., inactivity_engine: InactivityEngine | None = None)` as the last keyword argument. `_camera_loop(..., inactivity_engine=None)`.

- [ ] **Step 1: Write the failing tests**
  - `tests/test_worker.py::test_inactivity_runs_without_fall_engine`: `tracker` and `inactivity_engine` set, `fall_engine=None`, and a stub detector that returns the same box. The engine's `observe` is called every frame, and its events reach the sink. Use a spy subclass or a fake with `observe`.
  - `tests/test_worker.py::test_inactivity_exception_does_not_break_presence_or_falls`:
    - The inactivity engine raises. Presence events and fall events are still emitted.
    - The error is logged once per camera, mirroring `test_fall_engine_exception_does_not_break_presence`.
  - `tests/test_worker.py::test_fall_exception_does_not_skip_inactivity`.
  - `tests/test_app_loop.py`: following the existing style there, check that with `inactivity.enabled` and falls off, `_camera_loop` receives a `Tracker` and an `InactivityEngine`. With both off, `tracker` is `None`.
  - `tests/test_replay_video.py`: `summarize` counts `inactivity_*` events, and the sink filter lets them through.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/test_worker.py tests/test_app_loop.py tests/test_replay_video.py -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement the wiring**
  - `process_frame`: `if tracker is not None:` runs observe and identify inside its own `try`, keeping the current identifier `try`. Then `fall_engine` runs in its own `try`, and `inactivity_engine` in its own `try` with `_inactivity_failure_logged`.
  - A tracker failure keeps today's semantics: it is logged once, and neither motor runs on that frame.
  - `app.run`:
    - build `InactivityEngine(cfg.hub_id, min_minutes=cfg.inference.inactivity.min_minutes)` when enabled, and log `"analítica de inactividad activada (min_minutes=%.1f)"`;
    - `tracker = Tracker() if fall_engine is not None or inactivity_engine is not None else None`;
    - thread the engine through `worker` → `_camera_loop` → `process_frame`.
  - `replay_video.py`:
    - when `--faces` is given, also build an `InactivityEngine`;
    - add `--inactivity-min` (default 5.0) so short clips can be checked with a low threshold;
    - the sink filter accepts `inactivity_`.

- [ ] **Step 4: Update the docs**
  - Identity spec: identity now also labels inactivity. Point §3 to the code (whole frame, per-camera cadence) and link the new spec.
  - `docs/backlog.md`: add these deferred items, each with a one-line reason:
    - inactivity in the daily report (needs a migration, an app change and deduplication with the watch);
    - merging watch and camera alerts;
    - calibrating `RADIUS_FACTOR`, `MOVE_CONFIRM_S`, `PARK_*` and `RESUME_IOU`;
    - known limit: a parked identity can be inherited by someone else whose face is never seen (spec §4.3).
  - README: the `inference.inactivity` block, if config is documented there.

- [ ] **Step 5: Run the full gate**

Run: `ruff check . && mypy && pytest -v`
Expected: everything PASSES.

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/worker.py src/vitahub/app.py scripts/replay_video.py tests/test_worker.py tests/test_app_loop.py tests/test_replay_video.py docs/ README.md
git commit -m "feat(inactivity): cableado en el hub, replay y documentación"
```

- [ ] **Step 7: Validate with a real video** (manual, optional before merge): `python scripts/replay_video.py <video> --faces <dir> --inactivity-min 0.5`. Check that the start and end events appear when the person sits still and when they leave. Note the calibrated values in the commit if constants change.
