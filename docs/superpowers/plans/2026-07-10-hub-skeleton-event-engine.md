# VitaPlus Hub — Esqueleto + Event-Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working slice of the VitaPlus hub: a Docker Compose stack (webcam → mediamtx → Frigate → MQTT) plus a fully custom `event-engine` service that turns Frigate detections into the 4 MVP business events, persists them in SQLite, and simulates cloud sync with a stub.

**Architecture:** Explicit async state machine (no FSM library — YAGNI per the design doc). `event-engine` is a single Python 3.11+ asyncio process wiring three concurrent loops: an MQTT listener (Frigate topics), a 30s rule tick, and an edge-sync stub worker — all sharing an in-memory `HubState` and a SQLite-backed `EventStore`.

**Tech Stack:** Python 3.11+, asyncio, `aiomqtt`, `pydantic` v2, `pyyaml`, stdlib `sqlite3`, `pytest` + `pytest-asyncio`, `ruff`, Docker Compose, Frigate (`stable-tensorrt-jetson`), `mediamtx`, `eclipse-mosquitto`.

**Reference spec:** `docs/superpowers/specs/2026-07-10-hub-skeleton-event-engine-design.md` (this plan implements it in full). Original context: `docs/initial-dev.md` §3, §4, §9.

## Global Constraints

- Debounce de transición de zona: **3 s** (`presence_zone`, `home_exit`/`home_entry`).
- Tick periódico de reglas temporales: **30 s** (`inactivity_prolonged`, `night_activity_unusual`).
- Backoff de reconexión MQTT: exponencial, **1 s → 60 s máx.**
- Reintentos de escritura SQLite: **3**, luego error crítico logueado (nunca se lanza el proceso).
- `confidence` fijo a **1.0** para los 4 eventos del MVP (deterministas, no probabilísticos).
- `schema_version` del evento: **"1.0"**.
- Todos los servicios de `docker-compose.yml`: `restart: always`.
- Ningún fotograma de vídeo sale de los contenedores `frigate`/`mediamtx` hacia fuera del host — solo eventos JSON vía el stub de log.
- Configuración de zonas/horarios/umbrales vive únicamente en `config/hub/rules.yaml` — nunca hardcodear.
- `local-cache` (SQLite) vive embebido en `event-engine`, tras una interfaz propia aislable (`event_store.py`).
- `edge_sync_stub` nunca bloquea el pipeline principal de generación de eventos.
- Python 3.11+ / asyncio puro, sin librería de FSM (Enfoque A del documento de diseño).

---

## File Structure

```
vitaplus-process-hub/
├── docker-compose.yml
├── docker-compose.override.yml.example
├── .github/workflows/event-engine-ci.yml
├── config/
│   ├── mosquitto/mosquitto.conf
│   ├── frigate/frigate.yml
│   └── hub/rules.yaml
├── services/
│   ├── webcam-publisher/
│   │   ├── Dockerfile
│   │   └── publish.sh
│   └── event-engine/
│       ├── Dockerfile
│       ├── pyproject.toml
│       └── src/event_engine/
│           ├── __init__.py
│           ├── main.py
│           ├── mqtt_client.py
│           ├── rules_config.py
│           ├── models.py
│           ├── state.py
│           ├── rules/
│           │   ├── __init__.py
│           │   ├── presence_zone.py
│           │   ├── home_exit_entry.py
│           │   ├── inactivity.py
│           │   └── night_activity.py
│           ├── event_store.py
│           └── edge_sync_stub.py
├── docs/verification/hub-skeleton-manual-check.md
└── tests/
    └── event_engine/
        ├── __init__.py
        ├── fixtures/
        │   ├── valid_rules.yaml
        │   ├── invalid_polygon.yaml
        │   └── invalid_schedule.yaml
        ├── test_smoke.py
        ├── test_models.py
        ├── test_rules_config.py
        ├── test_state.py
        ├── test_event_store.py
        ├── test_presence_zone.py
        ├── test_home_exit_entry.py
        ├── test_inactivity.py
        ├── test_night_activity.py
        ├── test_mqtt_client.py
        ├── test_edge_sync_stub.py
        └── test_main.py
```

**Design note on `models.py`:** the design spec's repo layout doesn't list it explicitly, but every module (`rules_config`, `state`, `rules/*`, `event_store`) needs a shared `Event` type matching the JSON contract in §5 of the design doc. Adding `models.py` keeps that single-sourced instead of duplicating the contract in every rule.

**Design note on rule dispatch and debounce:** the design doc specifies each rule as `evaluate(state, config) -> Event | None` but doesn't specify who owns debounce scheduling. This plan puts debounce scheduling in `EventEngine` (Task 12), not inside individual rule classes — rules stay pure functions of `(camera_id, zone_id, state, config, now)`, which keeps them trivially unit-testable without `asyncio`.

**Design note on `home_exit`/`home_entry` direction:** Frigate (single webcam, no door sensor) doesn't give real entry/exit direction in this dev setup. This plan uses the zone's own occupancy transition as the direction proxy: a door zone going unoccupied→occupied emits `home_entry`, occupied→unoccupied emits `home_exit`. This is a deliberate MVP simplification, not an oversight — call it out to the user if a door sensor or track-direction field becomes available later.

---

### Task 1: Event-engine project scaffolding + CI

**Files:**
- Create: `services/event-engine/pyproject.toml`
- Create: `services/event-engine/src/event_engine/__init__.py`
- Create: `services/event-engine/src/event_engine/rules/__init__.py`
- Create: `tests/event_engine/__init__.py`
- Create: `tests/event_engine/test_smoke.py`
- Create: `.github/workflows/event-engine-ci.yml`

**Interfaces:**
- Produces: installable package `event_engine` (editable install from `services/event-engine`), importable as `import event_engine`. `event_engine.__version__ == "0.1.0"`.
- Produces: repo-root test invocation convention used by every later task: `pytest tests/event_engine/<file> -v` run from the repo root, with `services/event-engine` installed editable.

- [ ] **Step 1: Create the package skeleton and `pyproject.toml`**

`services/event-engine/pyproject.toml`:
```toml
[project]
name = "event-engine"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "aiomqtt>=2.0.0",
    "pyyaml>=6.0",
    "pydantic>=2.5",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.4",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
src = ["src"]
```

`services/event-engine/src/event_engine/rules/__init__.py` (empty file).

`services/event-engine/src/event_engine/__init__.py` — leave empty for now (filled in Step 5).

`tests/event_engine/__init__.py` (empty file).

- [ ] **Step 2: Install the package in editable mode**

Run from the repo root:
```bash
pip install -e "services/event-engine[dev]"
```
Expected: `Successfully installed event-engine-0.1.0 ...` (plus `aiomqtt`, `pydantic`, `pyyaml`, `pytest`, `pytest-asyncio`, `ruff`).

- [ ] **Step 3: Write the failing smoke test**

`tests/event_engine/test_smoke.py`:
```python
import event_engine


def test_package_exposes_version():
    assert event_engine.__version__ == "0.1.0"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/event_engine/test_smoke.py -v`
Expected: FAIL with `AttributeError: module 'event_engine' has no attribute '__version__'`

- [ ] **Step 5: Implement**

`services/event-engine/src/event_engine/__init__.py`:
```python
__version__ = "0.1.0"
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/event_engine/test_smoke.py -v`
Expected: PASS

- [ ] **Step 7: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 8: Add the CI workflow**

`.github/workflows/event-engine-ci.yml`:
```yaml
name: event-engine-ci

on:
  push:
    paths:
      - "services/event-engine/**"
      - "tests/event_engine/**"
      - ".github/workflows/event-engine-ci.yml"
  pull_request:
    paths:
      - "services/event-engine/**"
      - "tests/event_engine/**"

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e "services/event-engine[dev]"
      - run: ruff check services/event-engine/src tests/event_engine
      - run: pytest tests/event_engine -v
```

- [ ] **Step 9: Commit**

```bash
git add services/event-engine/pyproject.toml \
        services/event-engine/src/event_engine/__init__.py \
        services/event-engine/src/event_engine/rules/__init__.py \
        tests/event_engine/__init__.py \
        tests/event_engine/test_smoke.py \
        .github/workflows/event-engine-ci.yml
git commit -m "chore: scaffold event-engine package and CI"
```

---

### Task 2: Event data model (`models.py`)

**Files:**
- Create: `services/event-engine/src/event_engine/models.py`
- Test: `tests/event_engine/test_models.py`

**Interfaces:**
- Produces: `Event` dataclass — constructor `Event(hub_id: str, user_id: str, camera_id: str, type: str, zone: str | None, start_time: datetime, severity: str = "info", confidence: float = 1.0, end_time: datetime | None = None, metadata: dict = {}, evidence: Evidence = Evidence(), schema_version: str = "1.0")`, auto-generated `event_id: str` (`evt_<uuid4 hex>`), method `to_dict() -> dict` matching the JSON contract in the design spec §5. `start_time`/`end_time` passed in MUST be timezone-aware UTC `datetime` objects — every later task honors this.
- Produces: `Evidence` dataclass — `Evidence(clip_ref: str | None = None, uploaded: bool = False)`.

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_models.py`:
```python
from datetime import datetime, timezone

from event_engine.models import Event


def _event(**overrides):
    kwargs = dict(
        hub_id="hub_dev_001",
        user_id="usr_dev_001",
        camera_id="cam_salon",
        type="presence_zone",
        zone="cocina",
        start_time=datetime(2026, 7, 10, 10, 32, 5, tzinfo=timezone.utc),
    )
    kwargs.update(overrides)
    return Event(**kwargs)


def test_event_to_dict_matches_contract():
    data = _event().to_dict()
    assert data["hub_id"] == "hub_dev_001"
    assert data["user_id"] == "usr_dev_001"
    assert data["camera_id"] == "cam_salon"
    assert data["type"] == "presence_zone"
    assert data["severity"] == "info"
    assert data["confidence"] == 1.0
    assert data["zone"] == "cocina"
    assert data["start_time"] == "2026-07-10T10:32:05Z"
    assert data["end_time"] is None
    assert data["metadata"] == {}
    assert data["evidence"] == {"clip_ref": None, "uploaded": False}
    assert data["schema_version"] == "1.0"
    assert data["event_id"].startswith("evt_")


def test_each_event_gets_a_unique_id():
    assert _event().event_id != _event().event_id


def test_end_time_serializes_when_present():
    data = _event(end_time=datetime(2026, 7, 10, 10, 33, 40, tzinfo=timezone.utc)).to_dict()
    assert data["end_time"] == "2026-07-10T10:33:40Z"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.models'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/models.py`:
```python
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Severity = Literal["info", "warning", "critical"]


@dataclass
class Evidence:
    clip_ref: str | None = None
    uploaded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"clip_ref": self.clip_ref, "uploaded": self.uploaded}


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


def _iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass
class Event:
    hub_id: str
    user_id: str
    camera_id: str
    type: str
    zone: str | None
    start_time: datetime
    severity: Severity = "info"
    confidence: float = 1.0
    end_time: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: Evidence = field(default_factory=Evidence)
    schema_version: str = "1.0"
    event_id: str = field(default_factory=_new_event_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "hub_id": self.hub_id,
            "user_id": self.user_id,
            "camera_id": self.camera_id,
            "type": self.type,
            "severity": self.severity,
            "confidence": self.confidence,
            "zone": self.zone,
            "start_time": _iso_z(self.start_time),
            "end_time": _iso_z(self.end_time) if self.end_time else None,
            "metadata": self.metadata,
            "evidence": self.evidence.to_dict(),
            "schema_version": self.schema_version,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_models.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add services/event-engine/src/event_engine/models.py tests/event_engine/test_models.py
git commit -m "feat: add Event data model matching the cloud contract"
```

---

### Task 3: Rules config loader & validation (`rules_config.py`)

**Files:**
- Create: `services/event-engine/src/event_engine/rules_config.py`
- Create: `tests/event_engine/fixtures/valid_rules.yaml`
- Create: `tests/event_engine/fixtures/invalid_polygon.yaml`
- Create: `tests/event_engine/fixtures/invalid_schedule.yaml`
- Test: `tests/event_engine/test_rules_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `load_rules_config(path: str | Path) -> RulesConfig`. `RulesConfig` (pydantic `BaseModel`) fields: `hub_id: str`, `user_id: str`, `cameras: dict[str, Camera]`, `schedules: Schedules`, `thresholds: Thresholds`. `Camera.zones: dict[str, Zone]`. `Zone.type: str` (`"room"` or `"door"`), `Zone.polygon: list[list[float]]`. `Schedules.expected_activity: ScheduleWindow`, `Schedules.sleep_window: ScheduleWindow`. `ScheduleWindow.start: time`, `ScheduleWindow.end: time`. `Thresholds.inactivity_minutes: int`. Invalid input raises `pydantic.ValidationError`. Every later task that needs a config in tests loads `tests/event_engine/fixtures/valid_rules.yaml`.

- [ ] **Step 1: Create the fixture files**

`tests/event_engine/fixtures/valid_rules.yaml`:
```yaml
hub_id: hub_test_001
user_id: usr_test_001
cameras:
  cam_salon:
    zones:
      cocina:
        type: room
        polygon: [[0, 0], [100, 0], [100, 100], [0, 100]]
      puerta:
        type: door
        polygon: [[0, 0], [20, 0], [20, 20], [0, 20]]
schedules:
  expected_activity: {start: "07:00", end: "23:00"}
  sleep_window: {start: "23:00", end: "07:00"}
thresholds:
  inactivity_minutes: 240
```

`tests/event_engine/fixtures/invalid_polygon.yaml`:
```yaml
hub_id: hub_test_001
user_id: usr_test_001
cameras:
  cam_salon:
    zones:
      cocina:
        type: room
        polygon: [[0, 0], [100, 0]]
schedules:
  expected_activity: {start: "07:00", end: "23:00"}
  sleep_window: {start: "23:00", end: "07:00"}
thresholds:
  inactivity_minutes: 240
```

`tests/event_engine/fixtures/invalid_schedule.yaml`:
```yaml
hub_id: hub_test_001
user_id: usr_test_001
cameras:
  cam_salon:
    zones:
      cocina:
        type: room
        polygon: [[0, 0], [100, 0], [100, 100], [0, 100]]
schedules:
  expected_activity: {start: "07:00", end: "23:00"}
  sleep_window: {start: "23:00", end: "23:00"}
thresholds:
  inactivity_minutes: 240
```

- [ ] **Step 2: Write the failing test**

`tests/event_engine/test_rules_config.py`:
```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from event_engine.rules_config import load_rules_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_rules_config():
    config = load_rules_config(FIXTURES / "valid_rules.yaml")
    assert config.hub_id == "hub_test_001"
    assert config.user_id == "usr_test_001"
    assert config.cameras["cam_salon"].zones["cocina"].type == "room"
    assert config.cameras["cam_salon"].zones["puerta"].type == "door"
    assert config.thresholds.inactivity_minutes == 240
    assert config.schedules.expected_activity.start.isoformat() == "07:00:00"


def test_invalid_polygon_is_rejected():
    with pytest.raises(ValidationError):
        load_rules_config(FIXTURES / "invalid_polygon.yaml")


def test_incoherent_schedule_is_rejected():
    with pytest.raises(ValidationError):
        load_rules_config(FIXTURES / "invalid_schedule.yaml")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/event_engine/test_rules_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.rules_config'`

- [ ] **Step 4: Implement**

`services/event-engine/src/event_engine/rules_config.py`:
```python
from __future__ import annotations

from datetime import time
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator


class Zone(BaseModel):
    type: str
    polygon: list[list[float]]

    @field_validator("polygon")
    @classmethod
    def polygon_has_at_least_3_points(cls, value: list[list[float]]) -> list[list[float]]:
        if len(value) < 3:
            raise ValueError("polygon must have at least 3 points")
        for point in value:
            if len(point) != 2:
                raise ValueError("each polygon point must be [x, y]")
        return value


class Camera(BaseModel):
    zones: dict[str, Zone]


class ScheduleWindow(BaseModel):
    start: time
    end: time


class Schedules(BaseModel):
    expected_activity: ScheduleWindow
    sleep_window: ScheduleWindow


class Thresholds(BaseModel):
    inactivity_minutes: int

    @field_validator("inactivity_minutes")
    @classmethod
    def must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("inactivity_minutes must be positive")
        return value


class RulesConfig(BaseModel):
    hub_id: str
    user_id: str
    cameras: dict[str, Camera]
    schedules: Schedules
    thresholds: Thresholds

    @model_validator(mode="after")
    def schedules_must_not_be_degenerate(self) -> "RulesConfig":
        for name, window in (
            ("expected_activity", self.schedules.expected_activity),
            ("sleep_window", self.schedules.sleep_window),
        ):
            if window.start == window.end:
                raise ValueError(f"schedules.{name}: start and end must differ")
        return self


def load_rules_config(path: str | Path) -> RulesConfig:
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return RulesConfig.model_validate(raw)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/event_engine/test_rules_config.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add services/event-engine/src/event_engine/rules_config.py tests/event_engine/fixtures tests/event_engine/test_rules_config.py
git commit -m "feat: load and validate rules.yaml"
```

---

### Task 4: In-memory state (`state.py`)

**Files:**
- Create: `services/event-engine/src/event_engine/state.py`
- Test: `tests/event_engine/test_state.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `HubState` — `camera(camera_id: str) -> CameraState`, `set_zone_occupied(camera_id: str, zone_id: str, occupied: bool, at: datetime) -> tuple[bool, bool]` (returns `(previous_occupied, changed)`), `touch_motion(camera_id: str, at: datetime) -> None`, `set_camera_available(camera_id: str, available: bool) -> None`, `seed_last_motion(camera_id: str, at: datetime) -> None` (only advances, never rewinds). `CameraState` fields: `zones: dict[str, ZoneState]`, `last_motion: datetime | None`, `available: bool = True`; method `zone(zone_id: str) -> ZoneState`. `ZoneState` fields: `occupied: bool = False`, `last_change: datetime | None = None`. Every rule task (5-9) and `main.py` (Task 12) depend on these exact names.

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_state.py`:
```python
from datetime import datetime, timezone

from event_engine.state import HubState

T1 = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 10, 10, 5, 0, tzinfo=timezone.utc)


def test_set_zone_occupied_reports_transition_to_true():
    state = HubState()
    previous, changed = state.set_zone_occupied("cam_salon", "cocina", True, T1)
    assert previous is False
    assert changed is True
    zone = state.camera("cam_salon").zone("cocina")
    assert zone.occupied is True
    assert zone.last_change == T1


def test_set_zone_occupied_same_value_reports_no_change():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, T1)
    previous, changed = state.set_zone_occupied("cam_salon", "cocina", True, T2)
    assert previous is True
    assert changed is False
    assert state.camera("cam_salon").zone("cocina").last_change == T1


def test_set_zone_occupied_updates_camera_last_motion():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, T1)
    assert state.camera("cam_salon").last_motion == T1


def test_touch_motion_updates_last_motion_without_changing_zones():
    state = HubState()
    state.touch_motion("cam_salon", T1)
    assert state.camera("cam_salon").last_motion == T1
    assert state.camera("cam_salon").zones == {}


def test_set_camera_available_toggle():
    state = HubState()
    assert state.camera("cam_salon").available is True
    state.set_camera_available("cam_salon", False)
    assert state.camera("cam_salon").available is False


def test_seed_last_motion_only_advances():
    state = HubState()
    state.seed_last_motion("cam_salon", T2)
    state.seed_last_motion("cam_salon", T1)
    assert state.camera("cam_salon").last_motion == T2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.state'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/state.py`:
```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ZoneState:
    occupied: bool = False
    last_change: datetime | None = None


@dataclass
class CameraState:
    zones: dict[str, ZoneState] = field(default_factory=dict)
    last_motion: datetime | None = None
    available: bool = True

    def zone(self, zone_id: str) -> ZoneState:
        return self.zones.setdefault(zone_id, ZoneState())


class HubState:
    def __init__(self) -> None:
        self._cameras: dict[str, CameraState] = {}

    def camera(self, camera_id: str) -> CameraState:
        return self._cameras.setdefault(camera_id, CameraState())

    def set_zone_occupied(
        self, camera_id: str, zone_id: str, occupied: bool, at: datetime
    ) -> tuple[bool, bool]:
        cam = self.camera(camera_id)
        zone = cam.zone(zone_id)
        previous = zone.occupied
        changed = previous != occupied
        if changed:
            zone.occupied = occupied
            zone.last_change = at
        cam.last_motion = at
        return previous, changed

    def touch_motion(self, camera_id: str, at: datetime) -> None:
        self.camera(camera_id).last_motion = at

    def set_camera_available(self, camera_id: str, available: bool) -> None:
        self.camera(camera_id).available = available

    def seed_last_motion(self, camera_id: str, at: datetime) -> None:
        cam = self.camera(camera_id)
        if cam.last_motion is None or at > cam.last_motion:
            cam.last_motion = at
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_state.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add services/event-engine/src/event_engine/state.py tests/event_engine/test_state.py
git commit -m "feat: add in-memory hub state (camera/zone occupancy, motion)"
```

---

### Task 5: Local event store — SQLite (`event_store.py`)

**Files:**
- Create: `services/event-engine/src/event_engine/event_store.py`
- Test: `tests/event_engine/test_event_store.py`

**Interfaces:**
- Consumes: `Event` (Task 2) — `event.to_dict()`, `event.event_id`, `event.camera_id`, `event.start_time`.
- Produces: `EventStore(db_path: str | Path)`. `save_event(event: Event) -> None` (idempotent by `event_id`, 3 retries then `RuntimeError` + `logging.critical`). `get_pending() -> list[dict]` (list of `to_dict()`-shaped payloads, oldest first). `mark_syncing(event_id: str) -> None`. `mark_synced(event_id: str) -> None`. `last_motion_camera(camera_id: str) -> str | None` (ISO8601 `start_time` string of the most recent event for that camera, or `None`). `close() -> None`. Task 11 (`edge_sync_stub`) and Task 12 (`main.py`) depend on these exact names.

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_event_store.py`:
```python
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from event_engine.event_store import EventStore
from event_engine.models import Event


def _event(start_time=datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)):
    return Event(
        hub_id="hub_dev_001",
        user_id="usr_dev_001",
        camera_id="cam_salon",
        type="presence_zone",
        zone="cocina",
        start_time=start_time,
    )


def test_save_and_get_pending():
    store = EventStore(":memory:")
    event = _event()
    store.save_event(event)
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0]["event_id"] == event.event_id


def test_save_event_is_idempotent_by_event_id():
    store = EventStore(":memory:")
    event = _event()
    store.save_event(event)
    store.save_event(event)
    assert len(store.get_pending()) == 1


def test_mark_synced_removes_event_from_pending():
    store = EventStore(":memory:")
    event = _event()
    store.save_event(event)
    store.mark_syncing(event.event_id)
    store.mark_synced(event.event_id)
    assert store.get_pending() == []


def test_last_motion_camera_returns_latest_start_time():
    store = EventStore(":memory:")
    store.save_event(_event(start_time=datetime(2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc)))
    store.save_event(_event(start_time=datetime(2026, 7, 10, 11, 0, 0, tzinfo=timezone.utc)))
    assert store.last_motion_camera("cam_salon") == "2026-07-10T11:00:00Z"


def test_last_motion_camera_none_when_no_events():
    store = EventStore(":memory:")
    assert store.last_motion_camera("cam_salon") is None


def test_save_event_retries_then_raises_and_logs_critical(monkeypatch, caplog):
    store = EventStore(":memory:")

    class _AlwaysFailingConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_conn", _AlwaysFailingConn())

    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(RuntimeError):
            store.save_event(_event())

    assert "failed after 3 attempts" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_event_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.event_store'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/event_store.py`:
```python
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from event_engine.models import Event

logger = logging.getLogger("event_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    camera_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
"""

_MAX_ATTEMPTS = 3


class EventStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def save_event(self, event: Event) -> None:
        payload = json.dumps(event.to_dict())
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO events "
                    "(event_id, camera_id, type, payload, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?)",
                    (
                        event.event_id,
                        event.camera_id,
                        event.type,
                        payload,
                        event.start_time.isoformat(),
                    ),
                )
                self._conn.commit()
                return
            except sqlite3.OperationalError as exc:
                last_error = exc
                logger.warning(
                    "save_event attempt %d/%d failed for %s: %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    event.event_id,
                    exc,
                )
                time.sleep(0.1 * attempt)
        logger.critical(
            "save_event failed after %d attempts for %s: %s",
            _MAX_ATTEMPTS,
            event.event_id,
            last_error,
        )
        raise RuntimeError(f"failed to persist event {event.event_id}") from last_error

    def get_pending(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT payload FROM events WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def mark_syncing(self, event_id: str) -> None:
        self._conn.execute("UPDATE events SET status = 'syncing' WHERE event_id = ?", (event_id,))
        self._conn.commit()

    def mark_synced(self, event_id: str) -> None:
        self._conn.execute("UPDATE events SET status = 'synced' WHERE event_id = ?", (event_id,))
        self._conn.commit()

    def last_motion_camera(self, camera_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT payload FROM events WHERE camera_id = ? ORDER BY created_at DESC LIMIT 1",
            (camera_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])["start_time"]

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_event_store.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add services/event-engine/src/event_engine/event_store.py tests/event_engine/test_event_store.py
git commit -m "feat: add SQLite-backed event store with retry and idempotency"
```

---

### Task 6: Rule — `presence_zone`

**Files:**
- Create: `services/event-engine/src/event_engine/rules/presence_zone.py`
- Test: `tests/event_engine/test_presence_zone.py`

**Interfaces:**
- Consumes: `HubState` (Task 4), `RulesConfig` (Task 3), `Event` (Task 2).
- Produces: `PresenceZoneRule` — `event_type = "presence_zone"`, `evaluate(camera_id: str, zone_id: str, state: HubState, config: RulesConfig, now: datetime) -> Event | None`. Fires only when the zone is configured `type: room` and currently `occupied`. Caller (Task 12) is responsible for the 3s debounce before calling this.

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_presence_zone.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

from event_engine.rules.presence_zone import PresenceZoneRule
from event_engine.rules_config import load_rules_config
from event_engine.state import HubState

FIXTURES = Path(__file__).parent / "fixtures"
AT = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)


def _config():
    return load_rules_config(FIXTURES / "valid_rules.yaml")


def test_fires_when_room_zone_becomes_occupied():
    config = _config()
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, AT)
    event = PresenceZoneRule().evaluate("cam_salon", "cocina", state, config, AT)
    assert event is not None
    assert event.type == "presence_zone"
    assert event.zone == "cocina"
    assert event.start_time == AT


def test_none_when_zone_not_occupied():
    config = _config()
    state = HubState()
    event = PresenceZoneRule().evaluate("cam_salon", "cocina", state, config, AT)
    assert event is None


def test_none_for_door_zones():
    config = _config()
    state = HubState()
    state.set_zone_occupied("cam_salon", "puerta", True, AT)
    event = PresenceZoneRule().evaluate("cam_salon", "puerta", state, config, AT)
    assert event is None


def test_none_for_unknown_camera_or_zone():
    config = _config()
    state = HubState()
    assert PresenceZoneRule().evaluate("cam_unknown", "cocina", state, config, AT) is None
    assert PresenceZoneRule().evaluate("cam_salon", "unknown_zone", state, config, AT) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_presence_zone.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.rules.presence_zone'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/rules/presence_zone.py`:
```python
from __future__ import annotations

from datetime import datetime

from event_engine.models import Event
from event_engine.rules_config import RulesConfig
from event_engine.state import HubState


class PresenceZoneRule:
    event_type = "presence_zone"

    def evaluate(
        self,
        camera_id: str,
        zone_id: str,
        state: HubState,
        config: RulesConfig,
        now: datetime,
    ) -> Event | None:
        camera_cfg = config.cameras.get(camera_id)
        if camera_cfg is None or zone_id not in camera_cfg.zones:
            return None
        if camera_cfg.zones[zone_id].type != "room":
            return None
        zone = state.camera(camera_id).zone(zone_id)
        if not zone.occupied:
            return None
        return Event(
            hub_id=config.hub_id,
            user_id=config.user_id,
            camera_id=camera_id,
            type=self.event_type,
            zone=zone_id,
            start_time=zone.last_change or now,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_presence_zone.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add services/event-engine/src/event_engine/rules/presence_zone.py tests/event_engine/test_presence_zone.py
git commit -m "feat: add presence_zone rule"
```

---

### Task 7: Rule — `home_exit` / `home_entry`

**Files:**
- Create: `services/event-engine/src/event_engine/rules/home_exit_entry.py`
- Test: `tests/event_engine/test_home_exit_entry.py`

**Interfaces:**
- Consumes: same as Task 6.
- Produces: `HomeExitEntryRule` — `evaluate(camera_id: str, zone_id: str, state: HubState, config: RulesConfig, now: datetime) -> Event | None`. Only acts on `type: door` zones. Emits `event.type == "home_entry"` if the zone is currently occupied, `"home_exit"` if not. Caller (Task 12) calls this on *any* occupancy change of a door zone (not just entry), after the 3s debounce.

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_home_exit_entry.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

from event_engine.rules.home_exit_entry import HomeExitEntryRule
from event_engine.rules_config import load_rules_config
from event_engine.state import HubState

FIXTURES = Path(__file__).parent / "fixtures"


def _config():
    return load_rules_config(FIXTURES / "valid_rules.yaml")


def test_home_entry_when_door_zone_becomes_occupied():
    config = _config()
    state = HubState()
    at = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
    state.set_zone_occupied("cam_salon", "puerta", True, at)
    event = HomeExitEntryRule().evaluate("cam_salon", "puerta", state, config, at)
    assert event is not None
    assert event.type == "home_entry"
    assert event.zone == "puerta"


def test_home_exit_when_door_zone_becomes_unoccupied():
    config = _config()
    state = HubState()
    t1 = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 10, 10, 0, 5, tzinfo=timezone.utc)
    state.set_zone_occupied("cam_salon", "puerta", True, t1)
    state.set_zone_occupied("cam_salon", "puerta", False, t2)
    event = HomeExitEntryRule().evaluate("cam_salon", "puerta", state, config, t2)
    assert event is not None
    assert event.type == "home_exit"


def test_none_for_room_zones():
    config = _config()
    state = HubState()
    at = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
    state.set_zone_occupied("cam_salon", "cocina", True, at)
    event = HomeExitEntryRule().evaluate("cam_salon", "cocina", state, config, at)
    assert event is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_home_exit_entry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.rules.home_exit_entry'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/rules/home_exit_entry.py`:
```python
from __future__ import annotations

from datetime import datetime

from event_engine.models import Event
from event_engine.rules_config import RulesConfig
from event_engine.state import HubState

_ENTRY = "home_entry"
_EXIT = "home_exit"


class HomeExitEntryRule:
    # No fixed event_type: the emitted Event.type depends on the transition
    # direction (entry vs exit), unlike single-outcome rules.
    def evaluate(
        self,
        camera_id: str,
        zone_id: str,
        state: HubState,
        config: RulesConfig,
        now: datetime,
    ) -> Event | None:
        camera_cfg = config.cameras.get(camera_id)
        if camera_cfg is None or zone_id not in camera_cfg.zones:
            return None
        if camera_cfg.zones[zone_id].type != "door":
            return None
        zone = state.camera(camera_id).zone(zone_id)
        event_type = _ENTRY if zone.occupied else _EXIT
        return Event(
            hub_id=config.hub_id,
            user_id=config.user_id,
            camera_id=camera_id,
            type=event_type,
            zone=zone_id,
            start_time=zone.last_change or now,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_home_exit_entry.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add services/event-engine/src/event_engine/rules/home_exit_entry.py tests/event_engine/test_home_exit_entry.py
git commit -m "feat: add home_exit/home_entry rule"
```

---

### Task 8: Rule — `inactivity_prolonged`

**Files:**
- Create: `services/event-engine/src/event_engine/rules/inactivity.py`
- Test: `tests/event_engine/test_inactivity.py`

**Interfaces:**
- Consumes: same as Task 6, plus `config.schedules.expected_activity` and `config.thresholds.inactivity_minutes`.
- Produces: `InactivityProlongedRule` — `event_type = "inactivity_prolonged"`, stateful instance (must be reused across ticks, not re-created), `evaluate(camera_id: str, zone_id: str | None, state: HubState, config: RulesConfig, now: datetime) -> Event | None`. `zone_id` is ignored (camera-level rule); Task 12 calls this once per camera per tick with `zone_id=None`. Fires once per continuous inactivity episode; resets when motion resumes or the camera leaves the expected-activity window; suppressed entirely when `camera.available is False`.

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_inactivity.py`:
```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_engine.rules.inactivity import InactivityProlongedRule
from event_engine.rules_config import load_rules_config
from event_engine.state import HubState

FIXTURES = Path(__file__).parent / "fixtures"
NOON = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 7, 10, 3, 0, 0, tzinfo=timezone.utc)


def _config():
    return load_rules_config(FIXTURES / "valid_rules.yaml")


def test_none_when_no_motion_recorded():
    state = HubState()
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NOON) is None


def test_none_when_within_threshold():
    state = HubState()
    state.touch_motion("cam_salon", NOON - timedelta(minutes=30))
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NOON) is None


def test_fires_once_when_threshold_exceeded():
    state = HubState()
    state.touch_motion("cam_salon", NOON - timedelta(minutes=241))
    rule = InactivityProlongedRule()
    first = rule.evaluate("cam_salon", None, state, _config(), NOON)
    assert first is not None
    assert first.type == "inactivity_prolonged"
    second = rule.evaluate("cam_salon", None, state, _config(), NOON + timedelta(seconds=1))
    assert second is None


def test_fires_again_after_motion_resumes():
    state = HubState()
    state.touch_motion("cam_salon", NOON - timedelta(minutes=241))
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NOON) is not None
    state.touch_motion("cam_salon", NOON)
    later = NOON + timedelta(minutes=241)
    assert rule.evaluate("cam_salon", None, state, _config(), later) is not None


def test_none_outside_expected_activity_window():
    state = HubState()
    state.touch_motion("cam_salon", NIGHT - timedelta(minutes=500))
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NIGHT) is None


def test_none_when_camera_unavailable():
    state = HubState()
    state.touch_motion("cam_salon", NOON - timedelta(minutes=300))
    state.set_camera_available("cam_salon", False)
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NOON) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_inactivity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.rules.inactivity'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/rules/inactivity.py`:
```python
from __future__ import annotations

from datetime import datetime, time

from event_engine.models import Event
from event_engine.rules_config import RulesConfig
from event_engine.state import HubState


def _within_window(now: datetime, start: time, end: time) -> bool:
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


class InactivityProlongedRule:
    event_type = "inactivity_prolonged"

    def __init__(self) -> None:
        self._fired: dict[str, bool] = {}

    def evaluate(
        self,
        camera_id: str,
        zone_id: str | None,
        state: HubState,
        config: RulesConfig,
        now: datetime,
    ) -> Event | None:
        if camera_id not in config.cameras:
            return None
        camera = state.camera(camera_id)
        if not camera.available:
            return None
        window = config.schedules.expected_activity
        if not _within_window(now, window.start, window.end):
            self._fired[camera_id] = False
            return None
        if camera.last_motion is None:
            return None
        elapsed_minutes = (now - camera.last_motion).total_seconds() / 60
        if elapsed_minutes <= config.thresholds.inactivity_minutes:
            self._fired[camera_id] = False
            return None
        if self._fired.get(camera_id):
            return None
        self._fired[camera_id] = True
        return Event(
            hub_id=config.hub_id,
            user_id=config.user_id,
            camera_id=camera_id,
            type=self.event_type,
            zone=None,
            start_time=camera.last_motion,
            metadata={"inactivity_minutes": round(elapsed_minutes, 1)},
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_inactivity.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add services/event-engine/src/event_engine/rules/inactivity.py tests/event_engine/test_inactivity.py
git commit -m "feat: add inactivity_prolonged rule"
```

---

### Task 9: Rule — `night_activity_unusual`

**Files:**
- Create: `services/event-engine/src/event_engine/rules/night_activity.py`
- Test: `tests/event_engine/test_night_activity.py`

**Interfaces:**
- Consumes: same as Task 6, plus `config.schedules.sleep_window`.
- Produces: `NightActivityUnusualRule` — `event_type = "night_activity_unusual"`, stateful instance (reused across ticks), `evaluate(camera_id: str, zone_id: str, state: HubState, config: RulesConfig, now: datetime) -> Event | None`. Fires once per continuous occupied episode inside the sleep window; resets when the zone becomes unoccupied; suppressed when `camera.available is False`.

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_night_activity.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

from event_engine.rules.night_activity import NightActivityUnusualRule
from event_engine.rules_config import load_rules_config
from event_engine.state import HubState

FIXTURES = Path(__file__).parent / "fixtures"
NIGHT = datetime(2026, 7, 10, 2, 0, 0, tzinfo=timezone.utc)
DAY = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def _config():
    return load_rules_config(FIXTURES / "valid_rules.yaml")


def test_fires_when_occupied_during_sleep_window():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, NIGHT)
    event = NightActivityUnusualRule().evaluate("cam_salon", "cocina", state, _config(), NIGHT)
    assert event is not None
    assert event.type == "night_activity_unusual"


def test_none_outside_sleep_window():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, DAY)
    event = NightActivityUnusualRule().evaluate("cam_salon", "cocina", state, _config(), DAY)
    assert event is None


def test_none_when_zone_not_occupied():
    state = HubState()
    event = NightActivityUnusualRule().evaluate("cam_salon", "cocina", state, _config(), NIGHT)
    assert event is None


def test_fires_only_once_while_continuously_occupied():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, NIGHT)
    rule = NightActivityUnusualRule()
    assert rule.evaluate("cam_salon", "cocina", state, _config(), NIGHT) is not None
    later = NIGHT.replace(minute=30)
    assert rule.evaluate("cam_salon", "cocina", state, _config(), later) is None


def test_fires_again_after_zone_clears_and_reoccupies():
    state = HubState()
    t1, t2, t3 = NIGHT.replace(minute=0), NIGHT.replace(minute=10), NIGHT.replace(minute=20)
    rule = NightActivityUnusualRule()
    state.set_zone_occupied("cam_salon", "cocina", True, t1)
    assert rule.evaluate("cam_salon", "cocina", state, _config(), t1) is not None
    state.set_zone_occupied("cam_salon", "cocina", False, t2)
    rule.evaluate("cam_salon", "cocina", state, _config(), t2)
    state.set_zone_occupied("cam_salon", "cocina", True, t3)
    assert rule.evaluate("cam_salon", "cocina", state, _config(), t3) is not None


def test_none_when_camera_unavailable():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, NIGHT)
    state.set_camera_available("cam_salon", False)
    event = NightActivityUnusualRule().evaluate("cam_salon", "cocina", state, _config(), NIGHT)
    assert event is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_night_activity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.rules.night_activity'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/rules/night_activity.py`:
```python
from __future__ import annotations

from datetime import datetime, time

from event_engine.models import Event
from event_engine.rules_config import RulesConfig
from event_engine.state import HubState


def _within_window(now: datetime, start: time, end: time) -> bool:
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


class NightActivityUnusualRule:
    event_type = "night_activity_unusual"

    def __init__(self) -> None:
        self._fired: dict[tuple[str, str], bool] = {}

    def evaluate(
        self,
        camera_id: str,
        zone_id: str,
        state: HubState,
        config: RulesConfig,
        now: datetime,
    ) -> Event | None:
        camera_cfg = config.cameras.get(camera_id)
        if camera_cfg is None or zone_id not in camera_cfg.zones:
            return None
        camera = state.camera(camera_id)
        if not camera.available:
            return None
        key = (camera_id, zone_id)
        zone = camera.zone(zone_id)
        if not zone.occupied:
            self._fired[key] = False
            return None
        window = config.schedules.sleep_window
        if not _within_window(now, window.start, window.end):
            return None
        if self._fired.get(key):
            return None
        self._fired[key] = True
        return Event(
            hub_id=config.hub_id,
            user_id=config.user_id,
            camera_id=camera_id,
            type=self.event_type,
            zone=zone_id,
            start_time=zone.last_change or now,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_night_activity.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add services/event-engine/src/event_engine/rules/night_activity.py tests/event_engine/test_night_activity.py
git commit -m "feat: add night_activity_unusual rule"
```

---

### Task 10: MQTT client (`mqtt_client.py`)

**Files:**
- Create: `services/event-engine/src/event_engine/mqtt_client.py`
- Test: `tests/event_engine/test_mqtt_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure dict/JSON payloads).
- Produces: `FrigateMqttClient(host: str, port: int, on_event: Callable[[dict], Awaitable[None]], on_availability: Callable[[bool], Awaitable[None]])`. `run_forever() -> None` (connects, subscribes to `frigate/events` and `frigate/available`, reconnects on `aiomqtt.MqttError` with exponential backoff 1s→60s). `_dispatch(message) -> None` (internal, unit-tested directly with a fake message object — no broker needed): parses `frigate/events` JSON and calls `on_event(payload)`; malformed JSON or non-UTF8 payload is logged and dropped; `frigate/available` payload `"online"`/other calls `on_availability(bool)`.

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_mqtt_client.py`:
```python
import json
import logging

from event_engine.mqtt_client import FrigateMqttClient


class _FakeMessage:
    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


async def test_dispatch_valid_frigate_event_calls_on_event():
    received = []

    async def on_event(payload):
        received.append(payload)

    async def on_availability(available):
        pass

    client = FrigateMqttClient("mosquitto", 1883, on_event, on_availability)
    message = _FakeMessage("frigate/events", json.dumps({"type": "new", "after": {}}).encode())
    await client._dispatch(message)
    assert received == [{"type": "new", "after": {}}]


async def test_dispatch_malformed_json_is_discarded(caplog):
    async def on_event(payload):
        raise AssertionError("should not be called")

    async def on_availability(available):
        pass

    client = FrigateMqttClient("mosquitto", 1883, on_event, on_availability)
    message = _FakeMessage("frigate/events", b"{not valid json")
    with caplog.at_level(logging.WARNING):
        await client._dispatch(message)
    assert "malformed" in caplog.text


async def test_dispatch_availability_online_and_offline():
    seen = []

    async def on_event(payload):
        pass

    async def on_availability(available):
        seen.append(available)

    client = FrigateMqttClient("mosquitto", 1883, on_event, on_availability)
    await client._dispatch(_FakeMessage("frigate/available", b"online"))
    await client._dispatch(_FakeMessage("frigate/available", b"offline"))
    assert seen == [True, False]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_mqtt_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.mqtt_client'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/mqtt_client.py`:
```python
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiomqtt

logger = logging.getLogger("mqtt_client")

FrigateEventHandler = Callable[[dict], Awaitable[None]]
AvailabilityHandler = Callable[[bool], Awaitable[None]]

_MIN_BACKOFF = 1.0
_MAX_BACKOFF = 60.0


class FrigateMqttClient:
    def __init__(
        self,
        host: str,
        port: int,
        on_event: FrigateEventHandler,
        on_availability: AvailabilityHandler,
    ) -> None:
        self._host = host
        self._port = port
        self._on_event = on_event
        self._on_availability = on_availability

    async def run_forever(self) -> None:
        backoff = _MIN_BACKOFF
        while True:
            try:
                async with aiomqtt.Client(self._host, self._port) as client:
                    backoff = _MIN_BACKOFF
                    await client.subscribe("frigate/events")
                    await client.subscribe("frigate/available")
                    async for message in client.messages:
                        await self._dispatch(message)
            except aiomqtt.MqttError as exc:
                logger.warning("mqtt connection error: %s, retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _dispatch(self, message: Any) -> None:
        topic = str(message.topic)
        try:
            raw = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("malformed mqtt payload on %s: not utf-8", topic)
            return

        if topic == "frigate/available":
            await self._on_availability(raw == "online")
            return

        if topic == "frigate/events":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("malformed mqtt payload on %s: invalid json", topic)
                return
            await self._on_event(payload)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_mqtt_client.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add services/event-engine/src/event_engine/mqtt_client.py tests/event_engine/test_mqtt_client.py
git commit -m "feat: add Frigate MQTT client with reconnect backoff"
```

---

### Task 11: Edge-sync stub (`edge_sync_stub.py`)

**Files:**
- Create: `services/event-engine/src/event_engine/edge_sync_stub.py`
- Test: `tests/event_engine/test_edge_sync_stub.py`

**Interfaces:**
- Consumes: `EventStore` (Task 5) — `get_pending()`, `mark_syncing()`, `mark_synced()`.
- Produces: `EdgeSyncStub(store: EventStore, interval_seconds: float = 5.0)`. `sync_once() -> int` (drains all pending events through `pending → syncing → synced`, logs each as structured JSON, returns count processed). `run_forever() -> None` (loops `sync_once()` every `interval_seconds`, never raises on its own — matches "no bloquea el pipeline principal").

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_edge_sync_stub.py`:
```python
import logging
from datetime import datetime, timezone

from event_engine.edge_sync_stub import EdgeSyncStub
from event_engine.event_store import EventStore
from event_engine.models import Event


def _event():
    return Event(
        hub_id="hub_dev_001",
        user_id="usr_dev_001",
        camera_id="cam_salon",
        type="presence_zone",
        zone="cocina",
        start_time=datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc),
    )


def test_sync_once_marks_all_pending_as_synced(caplog):
    store = EventStore(":memory:")
    store.save_event(_event())
    store.save_event(_event())
    stub = EdgeSyncStub(store)
    with caplog.at_level(logging.INFO):
        sent = stub.sync_once()
    assert sent == 2
    assert store.get_pending() == []
    assert "edge_sync_stub" in caplog.text


def test_sync_once_with_nothing_pending_returns_zero():
    store = EventStore(":memory:")
    stub = EdgeSyncStub(store)
    assert stub.sync_once() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_edge_sync_stub.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.edge_sync_stub'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/edge_sync_stub.py`:
```python
from __future__ import annotations

import asyncio
import logging

from event_engine.event_store import EventStore

logger = logging.getLogger("edge_sync_stub")


class EdgeSyncStub:
    def __init__(self, store: EventStore, interval_seconds: float = 5.0) -> None:
        self._store = store
        self._interval = interval_seconds

    def sync_once(self) -> int:
        pending = self._store.get_pending()
        for payload in pending:
            event_id = payload["event_id"]
            self._store.mark_syncing(event_id)
            logger.info("edge_sync_stub: sending event %s", event_id, extra={"event": payload})
            self._store.mark_synced(event_id)
        return len(pending)

    async def run_forever(self) -> None:
        while True:
            self.sync_once()
            await asyncio.sleep(self._interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_edge_sync_stub.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add services/event-engine/src/event_engine/edge_sync_stub.py tests/event_engine/test_edge_sync_stub.py
git commit -m "feat: add edge-sync stub (pending -> syncing -> synced)"
```

---

### Task 12: EventEngine orchestration (`main.py`)

**Files:**
- Create: `services/event-engine/src/event_engine/main.py`
- Test: `tests/event_engine/test_main.py`

**Interfaces:**
- Consumes: `RulesConfig`/`load_rules_config` (Task 3), `HubState` (Task 4), `EventStore` (Task 5), `PresenceZoneRule` (Task 6), `HomeExitEntryRule` (Task 7), `InactivityProlongedRule` (Task 8), `NightActivityUnusualRule` (Task 9), `FrigateMqttClient` (Task 10), `EdgeSyncStub` (Task 11).
- Produces: `EventEngine(config: RulesConfig, store: EventStore, debounce_seconds: float = 3.0, tick_seconds: float = 30.0)` with public `state: HubState`. Methods: `bootstrap_last_motion() -> None`, `on_frigate_event(payload: dict) -> None` (async), `on_frigate_availability(available: bool) -> None` (async), `run_tick_loop() -> None` (async), `run_tick_once(now: datetime | None = None) -> None`. Module-level `async def main() -> None` wires everything from env vars and runs the three loops concurrently via `asyncio.gather`.

- [ ] **Step 1: Write the failing test**

`tests/event_engine/test_main.py`:
```python
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_engine.event_store import EventStore
from event_engine.main import EventEngine
from event_engine.models import Event
from event_engine.rules_config import load_rules_config

FIXTURES = Path(__file__).parent / "fixtures"


def _engine(debounce_seconds=0.01):
    config = load_rules_config(FIXTURES / "valid_rules.yaml")
    store = EventStore(":memory:")
    return EventEngine(config, store, debounce_seconds=debounce_seconds), store


async def test_frigate_event_persists_presence_zone_after_debounce():
    engine, store = _engine()
    payload = {"after": {"camera": "cam_salon", "current_zones": ["cocina"]}}
    await engine.on_frigate_event(payload)
    await asyncio.sleep(0.05)
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0]["type"] == "presence_zone"
    assert pending[0]["zone"] == "cocina"


async def test_room_zone_exit_produces_no_extra_event():
    engine, store = _engine()
    enter = {"after": {"camera": "cam_salon", "current_zones": ["cocina"]}}
    exit_ = {"after": {"camera": "cam_salon", "current_zones": []}}
    await engine.on_frigate_event(enter)
    await asyncio.sleep(0.05)
    await engine.on_frigate_event(exit_)
    await asyncio.sleep(0.05)
    types = [e["type"] for e in store.get_pending()]
    assert types == ["presence_zone"]


async def test_door_zone_entry_produces_home_entry():
    engine, store = _engine()
    payload = {"after": {"camera": "cam_salon", "current_zones": ["puerta"]}}
    await engine.on_frigate_event(payload)
    await asyncio.sleep(0.05)
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0]["type"] == "home_entry"


async def test_door_zone_exit_produces_home_exit():
    engine, store = _engine()
    enter = {"after": {"camera": "cam_salon", "current_zones": ["puerta"]}}
    exit_ = {"after": {"camera": "cam_salon", "current_zones": []}}
    await engine.on_frigate_event(enter)
    await asyncio.sleep(0.05)
    await engine.on_frigate_event(exit_)
    await asyncio.sleep(0.05)
    types = [e["type"] for e in store.get_pending()]
    assert types == ["home_entry", "home_exit"]


async def test_availability_false_marks_all_configured_cameras_unavailable():
    engine, _ = _engine()
    await engine.on_frigate_availability(False)
    assert engine.state.camera("cam_salon").available is False


def test_run_tick_once_fires_inactivity_when_threshold_exceeded():
    engine, store = _engine()
    now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
    engine.state.touch_motion("cam_salon", now - timedelta(minutes=241))
    engine.run_tick_once(now=now)
    types = [e["type"] for e in store.get_pending()]
    assert "inactivity_prolonged" in types


def test_run_tick_once_fires_night_activity_when_zone_occupied_at_night():
    engine, store = _engine()
    now = datetime(2026, 7, 10, 2, 0, 0, tzinfo=timezone.utc)
    engine.state.set_zone_occupied("cam_salon", "cocina", True, now)
    engine.run_tick_once(now=now)
    types = [e["type"] for e in store.get_pending()]
    assert "night_activity_unusual" in types


def test_bootstrap_last_motion_seeds_state_from_store():
    config = load_rules_config(FIXTURES / "valid_rules.yaml")
    store = EventStore(":memory:")
    store.save_event(
        Event(
            hub_id="h",
            user_id="u",
            camera_id="cam_salon",
            type="presence_zone",
            zone="cocina",
            start_time=datetime(2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc),
        )
    )
    engine = EventEngine(config, store)
    engine.bootstrap_last_motion()
    assert engine.state.camera("cam_salon").last_motion == datetime(
        2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/event_engine/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'event_engine.main'`

- [ ] **Step 3: Implement**

`services/event-engine/src/event_engine/main.py`:
```python
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from event_engine.edge_sync_stub import EdgeSyncStub
from event_engine.event_store import EventStore
from event_engine.mqtt_client import FrigateMqttClient
from event_engine.rules.home_exit_entry import HomeExitEntryRule
from event_engine.rules.inactivity import InactivityProlongedRule
from event_engine.rules.night_activity import NightActivityUnusualRule
from event_engine.rules.presence_zone import PresenceZoneRule
from event_engine.rules_config import RulesConfig, load_rules_config
from event_engine.state import HubState

logger = logging.getLogger("event_engine")

DEBOUNCE_SECONDS = 3.0
TICK_SECONDS = 30.0


class EventEngine:
    def __init__(
        self,
        config: RulesConfig,
        store: EventStore,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.config = config
        self.store = store
        self.state = HubState()
        self._debounce_seconds = debounce_seconds
        self._tick_seconds = tick_seconds
        self.presence_rule = PresenceZoneRule()
        self.door_rule = HomeExitEntryRule()
        self.inactivity_rule = InactivityProlongedRule()
        self.night_rule = NightActivityUnusualRule()

    def bootstrap_last_motion(self) -> None:
        for camera_id in self.config.cameras:
            last = self.store.last_motion_camera(camera_id)
            if last:
                self.state.seed_last_motion(camera_id, datetime.fromisoformat(last))

    async def on_frigate_event(self, payload: dict) -> None:
        after = payload.get("after") or {}
        camera_id = after.get("camera")
        if camera_id is None or camera_id not in self.config.cameras:
            return
        now = datetime.now(timezone.utc)
        current_zones = set(after.get("current_zones") or [])
        self.state.touch_motion(camera_id, now)

        camera_cfg = self.config.cameras[camera_id]
        for zone_id, zone_cfg in camera_cfg.zones.items():
            occupied = zone_id in current_zones
            _, changed = self.state.set_zone_occupied(camera_id, zone_id, occupied, now)
            if not changed:
                continue
            if zone_cfg.type == "door" or (zone_cfg.type == "room" and occupied):
                asyncio.create_task(self._debounced_zone_check(camera_id, zone_id))

    async def _debounced_zone_check(self, camera_id: str, zone_id: str) -> None:
        await asyncio.sleep(self._debounce_seconds)
        zone_cfg = self.config.cameras[camera_id].zones[zone_id]
        now = datetime.now(timezone.utc)
        if zone_cfg.type == "door":
            event = self.door_rule.evaluate(camera_id, zone_id, self.state, self.config, now)
        else:
            if not self.state.camera(camera_id).zone(zone_id).occupied:
                return
            event = self.presence_rule.evaluate(camera_id, zone_id, self.state, self.config, now)
        if event is not None:
            self._persist_and_log(event)

    async def on_frigate_availability(self, available: bool) -> None:
        for camera_id in self.config.cameras:
            self.state.set_camera_available(camera_id, available)

    def _persist_and_log(self, event) -> None:
        self.store.save_event(event)
        logger.info("event generated: %s", event.to_dict())

    async def run_tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_seconds)
            self.run_tick_once()

    def run_tick_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        for camera_id, camera_cfg in self.config.cameras.items():
            event = self.inactivity_rule.evaluate(camera_id, None, self.state, self.config, now)
            if event is not None:
                self._persist_and_log(event)
            for zone_id in camera_cfg.zones:
                event = self.night_rule.evaluate(camera_id, zone_id, self.state, self.config, now)
                if event is not None:
                    self._persist_and_log(event)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config_path = os.environ.get("RULES_CONFIG_PATH", "/config/hub/rules.yaml")
    db_path = os.environ.get("EVENT_DB_PATH", "/data/events.db")
    mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))

    config = load_rules_config(config_path)
    store = EventStore(db_path)
    engine = EventEngine(config, store)
    engine.bootstrap_last_motion()

    sync_stub = EdgeSyncStub(store)
    mqtt_client = FrigateMqttClient(
        mqtt_host, mqtt_port, engine.on_frigate_event, engine.on_frigate_availability
    )

    await asyncio.gather(
        mqtt_client.run_forever(),
        engine.run_tick_loop(),
        sync_stub.run_forever(),
    )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/event_engine/test_main.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Run the full test suite**

Run: `pytest tests/event_engine -v`
Expected: PASS, all tests across every task pass together (no cross-task regressions).

- [ ] **Step 6: Lint**

Run: `ruff check services/event-engine/src tests/event_engine`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add services/event-engine/src/event_engine/main.py tests/event_engine/test_main.py
git commit -m "feat: wire event-engine orchestration (mqtt, tick loop, debounce, sync)"
```

---

### Task 13: Docker Compose skeleton (mediamtx, Frigate, mosquitto, webcam-publisher, event-engine)

**Files:**
- Create: `docker-compose.yml`
- Create: `docker-compose.override.yml.example`
- Create: `config/mosquitto/mosquitto.conf`
- Create: `config/frigate/frigate.yml`
- Create: `config/hub/rules.yaml`
- Create: `services/webcam-publisher/Dockerfile`
- Create: `services/webcam-publisher/publish.sh`
- Create: `services/event-engine/Dockerfile`

**Interfaces:**
- Consumes: `event_engine.main:main` (Task 12) as the `event-engine` container's entrypoint; `RULES_CONFIG_PATH`, `EVENT_DB_PATH`, `MQTT_HOST`, `MQTT_PORT` env vars (Task 12).
- Produces: a `docker compose config` — validated stack the rest of the repo's manual verification (Task 14) runs against.

- [ ] **Step 1: Mosquitto config**

`config/mosquitto/mosquitto.conf`:
```
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest stdout
```

- [ ] **Step 2: Hub rules config (dev)**

`config/hub/rules.yaml`:
```yaml
hub_id: hub_dev_001
user_id: usr_dev_001
cameras:
  cam_salon:
    zones:
      cocina:
        type: room
        polygon: [[0, 0], [100, 0], [100, 100], [0, 100]]
      puerta:
        type: door
        polygon: [[0, 0], [20, 0], [20, 20], [0, 20]]
schedules:
  expected_activity: {start: "07:00", end: "23:00"}
  sleep_window: {start: "23:00", end: "07:00"}
thresholds:
  inactivity_minutes: 240
```
Note: polygon coordinates are placeholders on a 0-100 schematic scale, matching the design spec's own example. Replace with real detect-resolution pixel coordinates via Frigate's zone editor once the webcam framing is fixed — this doesn't block the skeleton entrega.

- [ ] **Step 3: Frigate config**

`config/frigate/frigate.yml`:
```yaml
mqtt:
  host: mosquitto
  port: 1883
  topic_prefix: frigate

detectors:
  tensorrt:
    type: tensorrt
    device: 0

model:
  path: /config/model_cache/tensorrt/yolov7-320.trt
  input_tensor: nchw
  input_pixel_format: rgb
  width: 320
  height: 320

cameras:
  cam_salon:
    ffmpeg:
      inputs:
        - path: rtsp://mediamtx:8554/cam_salon_sub
          roles:
            - detect
        - path: rtsp://mediamtx:8554/cam_salon_main
          roles:
            - record
    detect:
      width: 640
      height: 360
      fps: 5
    record:
      enabled: false
    zones:
      cocina:
        coordinates: 0,0,100,0,100,100,0,100
      puerta:
        coordinates: 0,0,20,0,20,20,0,20

version: 0.14
```

- [ ] **Step 4: Webcam publisher**

`services/webcam-publisher/publish.sh`:
```bash
#!/bin/sh
set -eu

MEDIAMTX_HOST="${MEDIAMTX_HOST:-mediamtx}"

ffmpeg -f v4l2 -i /dev/video0 \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -vf scale=1280:720 -r 20 \
  -f rtsp "rtsp://${MEDIAMTX_HOST}:8554/cam_salon_main" &

ffmpeg -f v4l2 -i /dev/video0 \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -vf scale=640:360 -r 8 \
  -f rtsp "rtsp://${MEDIAMTX_HOST}:8554/cam_salon_sub" &

wait -n
```

`services/webcam-publisher/Dockerfile`:
```dockerfile
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY publish.sh /publish.sh
RUN chmod +x /publish.sh
ENTRYPOINT ["/publish.sh"]
```

- [ ] **Step 5: Event-engine Dockerfile**

`services/event-engine/Dockerfile`:
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV RULES_CONFIG_PATH=/config/hub/rules.yaml
ENV EVENT_DB_PATH=/data/events.db
CMD ["python", "-m", "event_engine.main"]
```

- [ ] **Step 6: Docker Compose stack**

`docker-compose.yml`:
```yaml
services:
  webcam-publisher:
    build: ./services/webcam-publisher
    devices:
      - /dev/video0:/dev/video0
    depends_on:
      - mediamtx
    restart: always

  mediamtx:
    image: bluenviron/mediamtx:latest
    ports:
      - "8554:8554"
    restart: always

  frigate:
    image: ghcr.io/blakeblackshear/frigate:stable-tensorrt-jetson
    shm_size: "256mb"
    volumes:
      - ./config/frigate/frigate.yml:/config/config.yml:ro
      - frigate-media:/media/frigate
    ports:
      - "5000:5000"
    depends_on:
      - mediamtx
      - mosquitto
    restart: always

  mosquitto:
    image: eclipse-mosquitto:2
    volumes:
      - ./config/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
    ports:
      - "1883:1883"
    restart: always

  event-engine:
    build: ./services/event-engine
    environment:
      RULES_CONFIG_PATH: /config/hub/rules.yaml
      EVENT_DB_PATH: /data/events.db
      MQTT_HOST: mosquitto
      MQTT_PORT: "1883"
    volumes:
      - ./config/hub/rules.yaml:/config/hub/rules.yaml:ro
      - event-engine-data:/data
    depends_on:
      - mosquitto
    restart: always

volumes:
  frigate-media:
  event-engine-data:
```

- [ ] **Step 7: Override template for the real Reolink camera**

`docker-compose.override.yml.example`:
```yaml
# Copy to docker-compose.override.yml once the Reolink RLC-810A/520A arrives.
# Drops the webcam-publisher/mediamtx substitutes and points Frigate
# directly at the camera's own RTSP substream/mainstream.
#
# Before using this, create config/frigate/frigate.reolink.yml with the
# camera's real RTSP URLs (out of scope for this entrega).
services:
  webcam-publisher:
    profiles: ["disabled"]

  mediamtx:
    profiles: ["disabled"]

  frigate:
    volumes:
      - ./config/frigate/frigate.reolink.yml:/config/config.yml:ro
```

- [ ] **Step 8: Validate the compose file**

Run: `docker compose config --quiet`
Expected: no output, exit code 0 (validates YAML syntax and interpolation without starting containers).

- [ ] **Step 9: Commit**

```bash
git add docker-compose.yml docker-compose.override.yml.example \
        config/mosquitto/mosquitto.conf config/frigate/frigate.yml config/hub/rules.yaml \
        services/webcam-publisher services/event-engine/Dockerfile
git commit -m "feat: add docker-compose skeleton (webcam, mediamtx, frigate, mosquitto, event-engine)"
```

---

### Task 14: Manual integration verification checklist

**Files:**
- Create: `docs/verification/hub-skeleton-manual-check.md`

**Interfaces:**
- Consumes: the full stack from Task 13, running via `docker compose up`.
- Produces: a checklist document mapping directly to design spec §8 acceptance criteria — this is the non-automated verification the design doc calls out explicitly (not part of CI).

- [ ] **Step 1: Write the checklist**

`docs/verification/hub-skeleton-manual-check.md`:
```markdown
# Verificación manual — Esqueleto del hub + event-engine

No automatizada (requiere webcam real). Ejecutar tras completar las Tareas 1-13
del plan de implementación, antes de dar por cerrada la entrega.

## Arranque

1. `docker compose up --build`
2. Confirmar que los 5 servicios arrancan sin reinicios en bucle:
   `docker compose ps` — todos `Up`.
3. `docker compose logs mosquitto` — sin errores de conexión.
4. `docker compose logs frigate` — confirma que detecta el stream RTSP de
   `mediamtx` y publica en MQTT (`frigate/available` → `online`).

## Criterios de aceptación (spec §8)

- [ ] **Criterio 1 — Latencia y persistencia de `presence_zone`:** ponerse
      delante de la webcam, dentro de la zona `cocina`. En
      `docker compose logs event-engine` debe aparecer
      `event generated: {... "type": "presence_zone" ...}` en menos de 5 s.
      Verificar persistencia:
      `docker compose exec event-engine sqlite3 /data/events.db "select type, status from events;"`
      debe listar la fila con `status` en `pending` o `synced`.
- [ ] **Criterio 2 — Resiliencia MQTT:** `docker compose stop mosquitto`,
      esperar 15 s, `docker compose start mosquitto`. En los logs de
      `event-engine` debe verse el mensaje de reconexión (`retrying in Xs`)
      y, tras reconectar, ningún evento se pierde ni se duplica (comparar
      `select count(*), event_id from events group by event_id having count(*) > 1;`
      → vacío).
- [ ] **Criterio 3 — Recuperación tras reinicio:** generar un evento,
      `docker compose restart event-engine`, revisar logs de arranque: no
      debe generarse un `inactivity_prolonged` espurio inmediatamente
      después del reinicio (confirma que `bootstrap_last_motion` recuperó
      `last_motion` desde SQLite).
- [ ] **Criterio 4 — Sin fuga de vídeo:** confirmar que ningún servicio
      publica frames fuera del host: solo `edge_sync_stub` escribe al log
      con el JSON del evento (sin campos de imagen/vídeo). El puerto 5000
      (UI de Frigate) y 8554 (RTSP de mediamtx) son solo para depuración
      local en el propio host, no se exponen a Internet.

## Registro de resultado

Fecha, hardware usado (Jetson Orin Nano u otro), y resultado de cada
criterio (PASS/FAIL + nota) deben añadirse aquí antes de cerrar la entrega.
```

- [ ] **Step 2: Commit**

```bash
git add docs/verification/hub-skeleton-manual-check.md
git commit -m "docs: add manual verification checklist for hub skeleton entrega"
```

---

## Self-Review Notes

- **Spec coverage:** every §5 rule (`presence_zone`, `home_exit`/`home_entry`, `inactivity_prolonged`, `night_activity_unusual`) has a task (6-9); the event contract (§5) is `models.py` (Task 2); `rules.yaml` (§5 example) is `rules_config.py` (Task 3) + the dev config (Task 13); §6 resilience items are covered — MQTT backoff (Task 10), malformed-message discard (Task 10), SQLite retry (Task 5), edge-sync non-blocking (Task 11), restart recovery of `last_motion` (Task 12 `bootstrap_last_motion`), camera-down pausing inactivity rules (Task 8's `camera.available` check, set by Task 12's `on_frigate_availability`); §7 unit/integration/CI testing is Tasks 1-13 (unit) + Task 14 (manual); §8 acceptance criteria are the Task 14 checklist; the repo structure (§4) is implemented file-for-file, plus the two documented additions (`models.py`, this plan's design notes).
- **Placeholder scan:** no TBD/TODO markers; the one intentionally unfinished piece (`frigate.reolink.yml` referenced by the override example) is explicitly out of scope per the design spec itself (§1 "fuera de esta entrega") and is called out as a note, not hidden.
- **Type consistency:** `Event`, `Evidence`, `RulesConfig`/`Camera`/`Zone`/`Schedules`/`ScheduleWindow`/`Thresholds`, `HubState`/`CameraState`/`ZoneState`, `EventStore`, `EdgeSyncStub`, `FrigateMqttClient`, and `EventEngine` method signatures are identical every place they're consumed across Tasks 2-13.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-10-hub-skeleton-event-engine.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
