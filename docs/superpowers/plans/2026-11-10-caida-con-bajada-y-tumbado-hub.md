# Caída con bajada, tumbado y cajas cortadas (hub) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fall needs a sharp drop. Lying without one becomes a `lying_*` episode instead. Boxes cut by the frame edge without a visible torso no longer infer a posture, and no longer give feet points that can be trusted.

**Architecture:**
- `FallEngine` gains a `lying` state next to `reported`. They share history, posture and `peak_drop`.
- A small `analytics/frame_edges.py` module decides whether a box is cut.
- `process_frame` passes `frame_size` to both engines.

**Tech Stack:** Python 3.12, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-11-10-caida-con-bajada-y-tumbado-design.md`

**Paired plan:** `vita-docker-rule-engine/docs/superpowers/plans/2026-11-10-prolonged-lying.md`. Deploy it first (spec §7).

## Global Constraints

- Comments, docstrings, logs and docs are in Spanish, matching the repo style.
- No new envelope fields. The `fall_*` payloads are unchanged.
- New event types are exactly `lying_detected` (severity `"high"`), `lying_update` (`"info"`) and `lying_resolved` (`"info"`).
- Payloads:
  - `lying_detected`: `{episode_id, signals, person_count, person}`
  - `lying_update`: `{episode_id, person}`
  - `lying_resolved`: `{episode_id, duration_s, reason: "upright" | "track_lost", person}`
- `episode_id` uses the fall format and the same per-camera counter: `f"{camera.id}-{int(clock().timestamp())}-{n}"`.
- Constants:
  - `MIN_FALL_DROP = 0.3` (box heights per second; calibrate within 0.2–0.4);
  - `LYING_UPDATE_S = 60.0`;
  - `EDGE_MARGIN_PX = 2`.
- Clips stay tied to `fall_detected` only (user decision).
- `frame_size` is `(width, height)` in original frame pixels, the same space as the detector boxes. `None` means unknown and behaves exactly as today.
- CI gate: `ruff check . && mypy && pytest -v`.

## Review Focus

1. **The incident case.** A seated person cut by the bottom edge, with no hips, a wide box and a low head, held for minutes. Expected: no `fall_*` and no `lying_*`. Test in Task 2.
2. **A real fall that is slow.** Drop below `MIN_FALL_DROP`, then lying on the floor. Expected: `lying_detected`, never nothing. Test in Task 1.
3. **Lying, then sitting up in bed** (posture `other` for at least 2 s). Expected: `lying_resolved(reason="upright")`, so the episode doesn't stay open forever. Test in Task 1.
4. **A long lying episode.** Expected: one `lying_update` every 60 s, not every frame, and no `fall_update`. Test in Task 1.
5. **A frame without a usable `shape`** (`frame_size=None`), as in old tests and stubs. Expected: today's behavior, with no exception. Test in Task 2.

---

### Task 1: Caída con bajada obligatoria y episodio de tumbado

**Files:**
- Modify: `src/vitahub/analytics/fall_signals.py` (add the `MIN_FALL_DROP` constant)
- Modify: `src/vitahub/analytics/fall_engine.py` (`_EpisodeState`, `_update_state` candidate branch around 182–200, `observe` lost handling around 113–117, new emitters, module docstring diagram)
- Modify: `scripts/replay_video.py` (sink filter around line 42: also accept `"lying_"`; `summarize` counts `lying_*`)
- Test: `tests/test_fall_engine.py`, `tests/test_replay_video.py`

**Interfaces:**
- Produces:
  - `fall_signals.MIN_FALL_DROP: float = 0.3`
  - `fall_engine.LYING_UPDATE_S: float = 60.0`
  - `_EpisodeState.state` gains the value `"lying"`
  - `LyingReason = Literal["upright", "track_lost"]`
  - Event emission as in Global Constraints.

Behaviour (spec §3–4):
- **Candidate branch.** With `horizontal and floor_time >= CONFIRM_FLOOR_S`:
  - if `peak_drop is not None and peak_drop >= MIN_FALL_DROP and score >= min_score`, the episode goes to `reported` and emits `fall_detected` (unchanged);
  - otherwise it goes to `lying` and emits `lying_detected`.

  Both paths increment `cam.episodes`.
- **`lying` state:**
  - emit `lying_update` every `LYING_UPDATE_S`;
  - keep a `not_floor_time` accumulator: add `step` on every sample where the posture is not `"floor"` (upright **or** other), reset it on `"floor"`;
  - when `not_floor_time >= CONFIRM_UPRIGHT_S`, emit `lying_resolved(reason="upright")` and go back to `upright`, resetting `peak_drop`.
- **Never escalates.** A `lying` episode never becomes `reported`.
- **Lost track.** A track lost in `lying` emits `lying_resolved(reason="track_lost")`. `duration_s` is measured from the episode start.

- [ ] **Step 1: Write the failing tests** in `tests/test_fall_engine.py`, reusing `standing()`, `lying()` and the clock helpers there.
  - `test_fall_requires_drop`: a person who is `lying(x)` from the first frame (no drop), held for 3 s. No `fall_detected`; exactly one `lying_detected` with `severity == "high"`, payload keys `{"episode_id", "signals", "person_count", "person"}`, and an `episode_id` that starts with `CAM.id`.
  - `test_sharp_drop_still_reports_fall`: the existing fall sequence (`standing` then `lying`) still emits `fall_detected` and no `lying_*`. Keep or reuse the existing test.
  - `test_slow_fall_becomes_lying` (Review Focus 2):
    - Hips lowered over 4 s, so `peak_drop < MIN_FALL_DROP`, then lying held.
    - Result: `lying_detected`, no `fall_detected`.
  - `test_lying_updates_every_60s` (Review Focus 4): lying for 130 s. Exactly 2 `lying_update` events (at ≈60 s and ≈120 s after `lying_detected`), each payload `{"episode_id", "person"}`, and no `fall_update`.
  - `test_lying_resolves_when_upright`: after `lying_detected`, `standing()` for 2.5 s gives one `lying_resolved` with `reason == "upright"` and `duration_s > 0`.
  - `test_lying_resolves_when_sitting_up` (Review Focus 3): after `lying_detected`, a detection whose torso angle falls between `UPRIGHT_ANGLE` and `HORIZONTAL_ANGLE` (posture `other`) for 2.5 s gives `lying_resolved` with `reason == "upright"`.
  - `test_lying_resolves_on_track_lost`: after `lying_detected`, no detections for `TRACK_TTL_S + 0.5` gives `lying_resolved` with `reason == "track_lost"`.
  - `test_lying_never_escalates_to_fall`: in `lying`, feed a frame with a big `drop_speed`. No `fall_detected`.
  - `tests/test_replay_video.py`: the sink filter lets `lying_*` through, and `summarize` counts them.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/test_fall_engine.py tests/test_replay_video.py -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement** in `fall_engine.py`, following the existing `_fall_event`/`_resolved` style. Update the module docstring diagram to show `candidate → lying` and its exits.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `pytest tests/test_fall_engine.py tests/test_replay_video.py tests/test_worker.py -v && ruff check . && mypy`
Expected: PASS. Any existing test that relied on a fall without a drop must be updated to include a drop. Name each one in the report.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/analytics scripts/replay_video.py tests
git commit -m "feat(fall): la caída exige bajada brusca; tumbado sin bajada es un episodio lying_*"
```

---

### Task 2: Cajas cortadas por el borde (caídas, inactividad y cableado)

**Files:**
- Create: `src/vitahub/analytics/frame_edges.py`
- Modify: `src/vitahub/analytics/fall_engine.py` (`observe` and `_update_state` signatures; posture decision)
- Modify: `src/vitahub/analytics/inactivity_engine.py` (`observe` signature; the per-match sample in the matches loop around lines 138–186)
- Modify: `src/vitahub/worker.py` (`process_frame`: compute `frame_size` and pass it to both engines)
- Test: `tests/test_frame_edges.py`, `tests/test_fall_engine.py`, `tests/test_inactivity_engine.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: the `lying` behaviour from Task 1.
- Produces:
  - `frame_edges.EDGE_MARGIN_PX: int = 2`
  - `frame_edges.touches_edge(bbox: tuple[int, int, int, int], frame_size: tuple[int, int] | None) -> bool`: true if any side is within `EDGE_MARGIN_PX` of the frame border. Always false for `None`.
  - `frame_edges.touches_bottom(bbox, frame_size) -> bool`: same rule, bottom side only.
  - `FallEngine.observe(camera, update, now, frame_size: tuple[int, int] | None = None)`
  - `InactivityEngine.observe(camera, update, now, frame_size: tuple[int, int] | None = None)`
  - In `worker.py`, `frame_size` is `(frame.shape[1], frame.shape[0])` when `frame` has a `shape` of length ≥ 2, else `None`.

Behaviour (spec §5):
- **Falls.** When `touches_edge(det.bbox, frame_size)` and `signals.torso_angle is None`, the sample's posture is `"other"`: neither horizontal nor upright. Leave the rest of the posture logic untouched.
- **Inactivity.** When `touches_bottom(det.bbox, frame_size)` and the ankles are not both confident (same threshold `ANKLE_MIN_CONF` that `feet_point` uses):
  - the sample is neutral: do not call `_advance`, and do not update `scale`;
  - the episode stays, and its time keeps running;
  - identity-change handling still runs before this check.

- [ ] **Step 1: Write the failing tests**
  - `tests/test_frame_edges.py`:
    - `touches_edge((0, 50, 100, 200), (640, 360))` is true (left side);
    - `touches_edge((10, 50, 100, 359), (640, 360))` is true (bottom, within 2 px);
    - `touches_edge((10, 50, 100, 300), (640, 360))` is false;
    - `touches_bottom` is false for a box touching only the top;
    - `touches_edge(..., None)` is false.
  - `tests/test_fall_engine.py::test_cut_box_without_torso_never_falls_nor_lies` (Review Focus 1):
    - Frame `(640, 360)`, box `(200, 250, 600, 360)`, keypoints with nose only (shoulders and hips at conf 0), held for 120 s with `frame_size` passed.
    - Result: no `fall_*` and no `lying_*`.
  - `tests/test_fall_engine.py::test_cut_box_with_torso_uses_angle`: same box, with shoulders and hips giving a horizontal torso, after a sharp drop. `fall_detected` is still emitted.
  - `tests/test_fall_engine.py::test_frame_size_none_keeps_old_behaviour` (Review Focus 5): the incident box with `frame_size=None` gives `lying_detected`, which is the Task 1 behaviour when there is no edge info.
  - `tests/test_inactivity_engine.py::test_cut_bottom_sample_is_neutral`:
    - Episode emitted, then 10 s of boxes touching the bottom with no ankles, whose bottom-centre is far from the anchor.
    - Result: no `inactivity_ended`. The next normal sample at the anchor keeps the same `episode_id`.
  - `tests/test_worker.py::test_process_frame_passes_frame_size`: with a fake frame object that has `shape == (360, 640, 3)`, both engine spies receive `frame_size == (640, 360)`. With a frame that has no `shape`, they receive `None`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/test_frame_edges.py tests/test_fall_engine.py tests/test_inactivity_engine.py tests/test_worker.py -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement** `frame_edges.py`, the engine changes and the worker plumbing. Update existing spies or fakes in `tests/test_worker.py` to accept the new keyword.

- [ ] **Step 4: Run the full gate**

Run: `ruff check . && mypy && pytest -v`
Expected: everything PASSES.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub tests
git commit -m "feat(fall): una caja cortada por el borde sin torso no infiere postura ni punto de pies"
```

---

### Task 3: Documentación

**Files:**
- Modify: `README.md` (fall section: falls need a drop; the `lying_*` events and when they fire)
- Modify: `docs/superpowers/specs/2026-08-22-deteccion-caidas-design.md` (one note at the top pointing to the new spec)
- Modify: `docs/backlog.md`. Add:
  - calibrate `MIN_FALL_DROP` (0.2–0.4) and `EDGE_MARGIN_PX` with `replay_video.py`;
  - a clip for the lying alert, deferred by user decision;
  - exclusion zones are still pending (sofa or bed lying gives the `high` alert at 30 s by design).

- [ ] **Step 1: Make the doc edits.**
- [ ] **Step 2: Run** `ruff check . && mypy && pytest -q`. Expected: green.
- [ ] **Step 3: Commit**

```bash
git add README.md docs
git commit -m "docs(fall): caída con bajada, eventos lying_* y pendientes de calibración"
```
