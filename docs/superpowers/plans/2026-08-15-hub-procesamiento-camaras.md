# Hub de procesamiento de cámaras — Plan de implementación (primer slice)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un contenedor Docker que descubre cámaras ONVIF en la LAN de un hogar, ingesta sus streams RTSP, detecta presencia y conteo de personas por cámara, y emite eventos JSON estructurados por stdout — sin AWS todavía, con costuras listas para enchufarlo.

**Architecture:** Monolito Python de un solo proceso. Un supervisor cablea un bucle de descubrimiento (ONVIF), un worker por cámara (captura RTSP → inferencia → motor de eventos) y un sink. Dos interfaces aíslan lo que crecerá: `Detector` (visión) y `EventSink` (salida). Los eventos van a stdout; los logs operativos a stderr.

**Tech Stack:** Python 3.12 · OpenCV (opencv-python-headless) · Ultralytics YOLO · PyYAML · ONVIF por SOAP crudo (stdlib `urllib`/`socket`, sin dependencia `onvif-zeep`) · pytest · ruff · mypy · Docker multi-arch.

## Global Constraints

Cada tarea hereda implícitamente estas reglas (valores exactos del spec `docs/superpowers/specs/2026-08-15-hub-procesamiento-camaras-design.md`):

- **Python 3.12.**
- **Dependencias de runtime:** `opencv-python-headless`, `ultralytics`, `PyYAML`. Nada más. ONVIF se implementa con `urllib`/`socket` de la stdlib (ya validado contra hardware real).
- **Credencial solo por entorno:** `VITAHUB_ONVIF_USER` / `VITAHUB_ONVIF_PASSWORD`. Nunca en ficheros, logs ni eventos.
- **`camera_id`** = `"onvif-<serie>"` (serie ONVIF, estable ante cambios de IP).
- **Eventos** → stdout como JSON-lines (una línea por evento). **Logs operativos** → stderr.
- **`schema_version` = 1** en todo evento.
- **Tipos de evento:** `person_detected`, `person_absent`, `person_count_changed`. **Severidad** `info`.
- **Cadencia:** muestreo ~2 fps. Umbrales anti-parpadeo: presencia tras **2 s**, ausencia tras **5 s**.
- **RTSP sobre TCP**, substream por defecto.
- **Contenedor** `restart: unless-stopped`; estado persistente en volumen `/data`.
- **Disciplina:** TDD, DRY, YAGNI, commits frecuentes. Lint `ruff`, tipos `mypy`, tests `pytest` en verde antes de cada commit.

---

### Task 1: Scaffolding del proyecto

**Files:**
- Create: `pyproject.toml`
- Create: `src/vitahub/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_smoke.py`
- Create: `.github/workflows/ci.yml`
- Create: `.gitignore` (append)

**Interfaces:**
- Consumes: nada.
- Produces: paquete importable `vitahub` con `__version__: str`.

- [ ] **Step 1: Write the failing test**

`tests/test_smoke.py`:
```python
import vitahub


def test_package_exposes_version():
    assert isinstance(vitahub.__version__, str)
    assert vitahub.__version__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub'`.

- [ ] **Step 3: Write minimal implementation**

`pyproject.toml`:
```toml
[project]
name = "vitahub"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "opencv-python-headless>=4.9",
    "ultralytics>=8.2",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.5", "mypy>=1.10", "types-PyYAML>=6"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.mypy]
python_version = "3.12"
packages = ["vitahub"]
mypy_path = "src"
strict = true
```

`src/vitahub/__init__.py`:
```python
__version__ = "0.1.0"
```

`tests/__init__.py`: empty file.

`.gitignore` (append):
```
__pycache__/
*.pyc
.venv/
.mypy_cache/
.pytest_cache/
.ruff_cache/
/data/
*.pt
```

`.github/workflows/ci.yml`:
```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: ruff check src tests
      - run: mypy
      - run: pytest -v
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pip install -e ".[dev]" && python -m pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src tests .github .gitignore
git commit -m "chore: scaffold vitahub python package + CI"
```

---

### Task 2: Modelos de dominio

**Files:**
- Create: `src/vitahub/models.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Consumes: nada.
- Produces:
  - `Detection(label: str, confidence: float, bbox: tuple[int,int,int,int])` — frozen.
  - `Camera(id: str, name: str, last_ip: str, enabled: bool = True)`.
  - `Event(hub_id, camera_id, camera_name, type, severity, timestamp, payload: dict, schema_version: int = 1)` — frozen; método `to_json() -> str`.

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:
```python
import json

from vitahub.models import Camera, Detection, Event


def test_detection_is_frozen():
    d = Detection(label="person", confidence=0.9, bbox=(0, 0, 10, 20))
    assert d.bbox == (0, 0, 10, 20)


def test_event_to_json_is_single_line_and_ordered():
    ev = Event(
        hub_id="hub-1",
        camera_id="onvif-abc",
        camera_name="salon",
        type="person_detected",
        severity="info",
        timestamp="2026-08-15T10:00:00Z",
        payload={"person_count": 1, "confidence": 0.82},
    )
    line = ev.to_json()
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["schema_version"] == 1
    assert parsed["type"] == "person_detected"
    assert parsed["payload"]["person_count"] == 1
    assert list(parsed.keys())[0] == "schema_version"


def test_camera_defaults_enabled():
    c = Camera(id="onvif-abc", name="salon", last_ip="192.168.1.190")
    assert c.enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.models'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/models.py`:
```python
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    bbox: tuple[int, int, int, int]


@dataclass
class Camera:
    id: str
    name: str
    last_ip: str
    enabled: bool = True


@dataclass(frozen=True)
class Event:
    hub_id: str
    camera_id: str
    camera_name: str
    type: str
    severity: str
    timestamp: str
    payload: dict[str, object] = field(default_factory=dict)
    schema_version: int = 1

    def to_json(self) -> str:
        ordered = {
            "schema_version": self.schema_version,
            "hub_id": self.hub_id,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "type": self.type,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }
        return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/models.py tests/test_models.py
git commit -m "feat: domain models (Camera, Detection, Event)"
```

---

### Task 3: Carga y validación de config (fail-fast)

**Files:**
- Create: `src/vitahub/config.py`
- Create: `tests/test_config.py`
- Create: `config/hub.example.yaml`

**Interfaces:**
- Consumes: `Camera` (Task 2).
- Produces:
  - `class ConfigError(Exception)`.
  - dataclasses `DiscoveryConfig(interval_seconds: int = 60)`, `InferenceConfig(detector: str = "person_yolo", sample_fps: float = 2.0, confidence: float = 0.4, stream: str = "substream")`, `Credentials(onvif_user: str, onvif_password: str)`, `HubConfig(hub_id: str, discovery: DiscoveryConfig, inference: InferenceConfig, cameras: list[Camera], credentials: Credentials)`.
  - `load_config(path: Path, env: Mapping[str, str]) -> HubConfig`.
  - `save_cameras(path: Path, cameras: list[Camera]) -> None` (reescribe solo la sección `cameras`, preservando el resto).

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
from pathlib import Path

import pytest

from vitahub.config import ConfigError, load_config, save_cameras

VALID_YAML = """
hub_id: hub-3f9a
discovery:
  interval_seconds: 30
inference:
  detector: person_yolo
  sample_fps: 2
  confidence: 0.4
  stream: substream
cameras:
  - id: onvif-abc
    name: salon
    last_ip: 192.168.1.190
    enabled: true
"""

ENV = {"VITAHUB_ONVIF_USER": "admin", "VITAHUB_ONVIF_PASSWORD": "secret"}


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "hub.yaml"
    p.write_text(text)
    return p


def test_load_valid_config(tmp_path):
    cfg = load_config(_write(tmp_path, VALID_YAML), ENV)
    assert cfg.hub_id == "hub-3f9a"
    assert cfg.discovery.interval_seconds == 30
    assert cfg.inference.sample_fps == 2.0
    assert cfg.credentials.onvif_user == "admin"
    assert cfg.cameras[0].id == "onvif-abc"


def test_missing_credential_env_raises(tmp_path):
    with pytest.raises(ConfigError, match="VITAHUB_ONVIF_PASSWORD"):
        load_config(_write(tmp_path, VALID_YAML), {"VITAHUB_ONVIF_USER": "admin"})


def test_missing_hub_id_raises(tmp_path):
    with pytest.raises(ConfigError, match="hub_id"):
        load_config(_write(tmp_path, "discovery: {}\n"), ENV)


def test_defaults_applied_when_sections_absent(tmp_path):
    cfg = load_config(_write(tmp_path, "hub_id: hub-x\n"), ENV)
    assert cfg.discovery.interval_seconds == 60
    assert cfg.inference.detector == "person_yolo"
    assert cfg.cameras == []


def test_save_cameras_roundtrip(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    cfg = load_config(path, ENV)
    cfg.cameras.append(
        __import__("vitahub.models", fromlist=["Camera"]).Camera(
            id="onvif-new", name="cocina", last_ip="192.168.1.191"
        )
    )
    save_cameras(path, cfg.cameras)
    reloaded = load_config(path, ENV)
    assert {c.id for c in reloaded.cameras} == {"onvif-abc", "onvif-new"}
    assert reloaded.hub_id == "hub-3f9a"  # resto preservado
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.config'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/config.py`:
```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vitahub.models import Camera


class ConfigError(Exception):
    pass


@dataclass
class DiscoveryConfig:
    interval_seconds: int = 60


@dataclass
class InferenceConfig:
    detector: str = "person_yolo"
    sample_fps: float = 2.0
    confidence: float = 0.4
    stream: str = "substream"


@dataclass
class Credentials:
    onvif_user: str
    onvif_password: str


@dataclass
class HubConfig:
    hub_id: str
    discovery: DiscoveryConfig
    inference: InferenceConfig
    cameras: list[Camera]
    credentials: Credentials


def _credentials_from_env(env: Mapping[str, str]) -> Credentials:
    user = env.get("VITAHUB_ONVIF_USER")
    password = env.get("VITAHUB_ONVIF_PASSWORD")
    if not user:
        raise ConfigError("Falta la variable de entorno VITAHUB_ONVIF_USER")
    if not password:
        raise ConfigError("Falta la variable de entorno VITAHUB_ONVIF_PASSWORD")
    return Credentials(onvif_user=user, onvif_password=password)


def _cameras_from_raw(raw: list[dict]) -> list[Camera]:
    cameras: list[Camera] = []
    for entry in raw:
        try:
            cameras.append(
                Camera(
                    id=str(entry["id"]),
                    name=str(entry["name"]),
                    last_ip=str(entry["last_ip"]),
                    enabled=bool(entry.get("enabled", True)),
                )
            )
        except KeyError as exc:
            raise ConfigError(f"Cámara en config sin campo obligatorio {exc}") from exc
    return cameras


def load_config(path: Path, env: Mapping[str, str]) -> HubConfig:
    credentials = _credentials_from_env(env)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"No se pudo leer/parsear la config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("La config raíz debe ser un mapping YAML")

    hub_id = raw.get("hub_id")
    if not hub_id:
        raise ConfigError("Falta 'hub_id' en la config")

    disc_raw = raw.get("discovery") or {}
    inf_raw = raw.get("inference") or {}
    return HubConfig(
        hub_id=str(hub_id),
        discovery=DiscoveryConfig(
            interval_seconds=int(disc_raw.get("interval_seconds", 60))
        ),
        inference=InferenceConfig(
            detector=str(inf_raw.get("detector", "person_yolo")),
            sample_fps=float(inf_raw.get("sample_fps", 2.0)),
            confidence=float(inf_raw.get("confidence", 0.4)),
            stream=str(inf_raw.get("stream", "substream")),
        ),
        cameras=_cameras_from_raw(raw.get("cameras") or []),
        credentials=credentials,
    )


def save_cameras(path: Path, cameras: list[Camera]) -> None:
    raw = yaml.safe_load(path.read_text()) or {}
    raw["cameras"] = [
        {"id": c.id, "name": c.name, "last_ip": c.last_ip, "enabled": c.enabled}
        for c in cameras
    ]
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
```

`config/hub.example.yaml`:
```yaml
hub_id: hub-CAMBIAME        # genera uno único por hogar y no lo cambies
discovery:
  interval_seconds: 60
inference:
  detector: person_yolo     # o "stub" para probar sin modelo
  sample_fps: 2
  confidence: 0.4
  stream: substream         # "substream" ahorra CPU; "main" para más resolución
cameras: []                 # se autopobla al descubrir; editable a mano
# La credencial NO va aquí: usa VITAHUB_ONVIF_USER / VITAHUB_ONVIF_PASSWORD
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/config.py tests/test_config.py config/hub.example.yaml
git commit -m "feat: config loader with env credentials and fail-fast validation"
```

---

### Task 4: Logging estructurado a stderr con redacción de secretos

**Files:**
- Create: `src/vitahub/logging_setup.py`
- Create: `tests/test_logging_setup.py`

**Interfaces:**
- Consumes: nada.
- Produces:
  - `configure_logging(level: str = "INFO") -> None` — configura el root logger para emitir JSON a **stderr**.
  - `get_logger(name: str) -> logging.Logger`.
  - `redact(text: str, secrets: Iterable[str]) -> str` — sustituye cada secreto por `***`.

- [ ] **Step 1: Write the failing test**

`tests/test_logging_setup.py`:
```python
import json
import logging

from vitahub.logging_setup import configure_logging, get_logger, redact


def test_redact_hides_secret():
    assert redact("user=admin pass=secret123", ["secret123"]) == "user=admin pass=***"
    assert redact("nada que ocultar", ["secret123"]) == "nada que ocultar"


def test_logs_go_to_stderr_as_json(capsys):
    configure_logging("INFO")
    get_logger("test").info("hola %s", "mundo")
    captured = capsys.readouterr()
    assert captured.out == ""  # nada por stdout (reservado a eventos)
    record = json.loads(captured.err.strip().splitlines()[-1])
    assert record["level"] == "INFO"
    assert record["message"] == "hola mundo"
    assert record["logger"] == "test"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_logging_setup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.logging_setup'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/logging_setup.py`:
```python
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_logging_setup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/logging_setup.py tests/test_logging_setup.py
git commit -m "feat: structured stderr logging with secret redaction"
```

---

### Task 5: Interfaz EventSink + StdoutJsonSink

**Files:**
- Create: `src/vitahub/sinks/__init__.py`
- Create: `src/vitahub/sinks/base.py`
- Create: `src/vitahub/sinks/stdout_json.py`
- Create: `tests/test_stdout_sink.py`

**Interfaces:**
- Consumes: `Event` (Task 2).
- Produces:
  - `class EventSink(ABC)` con `emit(self, event: Event) -> None` y `close(self) -> None`.
  - `class StdoutJsonSink(EventSink)` — escribe `event.to_json() + "\n"` a stdout y hace flush.

- [ ] **Step 1: Write the failing test**

`tests/test_stdout_sink.py`:
```python
import json

from vitahub.models import Event
from vitahub.sinks.stdout_json import StdoutJsonSink


def _event() -> Event:
    return Event(
        hub_id="hub-1",
        camera_id="onvif-abc",
        camera_name="salon",
        type="person_detected",
        severity="info",
        timestamp="2026-08-15T10:00:00Z",
        payload={"person_count": 1},
    )


def test_emit_writes_json_line_to_stdout(capsys):
    sink = StdoutJsonSink()
    sink.emit(_event())
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert json.loads(out.strip())["type"] == "person_detected"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stdout_sink.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.sinks'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/sinks/__init__.py`: empty file.

`src/vitahub/sinks/base.py`:
```python
from __future__ import annotations

from abc import ABC, abstractmethod

from vitahub.models import Event


class EventSink(ABC):
    @abstractmethod
    def emit(self, event: Event) -> None: ...

    @abstractmethod
    def close(self) -> None: ...
```

`src/vitahub/sinks/stdout_json.py`:
```python
from __future__ import annotations

import sys

from vitahub.models import Event
from vitahub.sinks.base import EventSink


class StdoutJsonSink(EventSink):
    def emit(self, event: Event) -> None:
        sys.stdout.write(event.to_json() + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        sys.stdout.flush()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stdout_sink.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/sinks tests/test_stdout_sink.py
git commit -m "feat: EventSink interface + StdoutJsonSink"
```

---

### Task 6: Interfaz Detector + StubDetector

**Files:**
- Create: `src/vitahub/inference/__init__.py`
- Create: `src/vitahub/inference/base.py`
- Create: `src/vitahub/inference/stub.py`
- Create: `tests/test_stub_detector.py`

**Interfaces:**
- Consumes: `Detection` (Task 2).
- Produces:
  - `class Detector(ABC)` con `detect(self, frame: object) -> list[Detection]`.
  - `class StubDetector(Detector)` — `__init__(self, person_count: int = 0, confidence: float = 0.99)`; devuelve `person_count` detecciones sintéticas de `label="person"`.

- [ ] **Step 1: Write the failing test**

`tests/test_stub_detector.py`:
```python
from vitahub.inference.stub import StubDetector


def test_stub_returns_configured_number_of_persons():
    det = StubDetector(person_count=3)
    result = det.detect(frame=None)
    assert len(result) == 3
    assert all(d.label == "person" for d in result)


def test_stub_zero_by_default():
    assert StubDetector().detect(frame=None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stub_detector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.inference'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/inference/__init__.py`: empty file.

`src/vitahub/inference/base.py`:
```python
from __future__ import annotations

from abc import ABC, abstractmethod

from vitahub.models import Detection


class Detector(ABC):
    @abstractmethod
    def detect(self, frame: object) -> list[Detection]: ...
```

`src/vitahub/inference/stub.py`:
```python
from __future__ import annotations

from vitahub.inference.base import Detector
from vitahub.models import Detection


class StubDetector(Detector):
    def __init__(self, person_count: int = 0, confidence: float = 0.99) -> None:
        self._person_count = person_count
        self._confidence = confidence

    def detect(self, frame: object) -> list[Detection]:
        return [
            Detection(label="person", confidence=self._confidence, bbox=(0, 0, 1, 1))
            for _ in range(self._person_count)
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stub_detector.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/inference tests/test_stub_detector.py
git commit -m "feat: Detector interface + StubDetector"
```

---

### Task 7: Motor de eventos (máquina anti-parpadeo)

Es la lógica con estado más delicada del slice. El tiempo se **inyecta** (`now: float` monotónico) y el reloj de pared se inyecta por constructor, para tests deterministas.

**Files:**
- Create: `src/vitahub/analytics/__init__.py`
- Create: `src/vitahub/analytics/event_engine.py`
- Create: `tests/test_event_engine.py`

**Interfaces:**
- Consumes: `Camera`, `Event` (Task 2).
- Produces:
  - `class EventEngine` con:
    - `__init__(self, hub_id: str, present_after_s: float = 2.0, absent_after_s: float = 5.0, clock: Callable[[], datetime] = <utcnow>)`.
    - `observe(self, camera: Camera, person_count: int, confidence: float, now: float) -> list[Event]` — `now` en segundos monotónicos; devuelve 0 o 1 eventos.

Semántica de la máquina, por cámara (estado interno keyed por `camera_id`):
- Estado reportado inicial = `0` personas.
- Si `person_count == reportado` → se limpia cualquier candidato pendiente.
- Si `person_count != reportado` → se vuelve "candidato". Debe **sostenerse** el mismo valor un tiempo mínimo antes de confirmarse:
  - `0 → >0`: `present_after_s` → emite `person_detected`.
  - `>0 → 0`: `absent_after_s` → emite `person_absent`.
  - `>0 → >0` (cambia el conteo): `present_after_s` → emite `person_count_changed`.
- Al confirmarse: `reportado = person_count` y se emite el evento con `severity="info"`.

- [ ] **Step 1: Write the failing test**

`tests/test_event_engine.py`:
```python
from datetime import datetime, timezone

from vitahub.analytics.event_engine import EventEngine
from vitahub.models import Camera

CAM = Camera(id="onvif-abc", name="salon", last_ip="10.0.0.5")


def _engine() -> EventEngine:
    return EventEngine(
        hub_id="hub-1",
        present_after_s=2.0,
        absent_after_s=5.0,
        clock=lambda: datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


def test_no_event_before_threshold():
    eng = _engine()
    assert eng.observe(CAM, person_count=1, confidence=0.9, now=0.0) == []
    assert eng.observe(CAM, person_count=1, confidence=0.9, now=1.9) == []


def test_person_detected_after_present_threshold():
    eng = _engine()
    eng.observe(CAM, 1, 0.9, now=0.0)
    events = eng.observe(CAM, 1, 0.9, now=2.0)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "person_detected"
    assert ev.severity == "info"
    assert ev.payload["person_count"] == 1
    assert ev.camera_id == "onvif-abc"
    assert ev.timestamp == "2026-08-15T10:00:00+00:00"


def test_flicker_below_threshold_emits_nothing():
    eng = _engine()
    eng.observe(CAM, 1, 0.9, now=0.0)   # candidato presencia
    eng.observe(CAM, 0, 0.0, now=1.0)   # vuelve a 0 antes de 2s -> cancela
    assert eng.observe(CAM, 0, 0.0, now=10.0) == []  # sigue en 0 reportado


def test_absent_after_absent_threshold():
    eng = _engine()
    eng.observe(CAM, 1, 0.9, now=0.0)
    eng.observe(CAM, 1, 0.9, now=2.0)   # confirmado presente
    eng.observe(CAM, 0, 0.0, now=3.0)   # candidato ausencia
    assert eng.observe(CAM, 0, 0.0, now=7.0) == []       # <5s
    events = eng.observe(CAM, 0, 0.0, now=8.0)           # >=5s
    assert len(events) == 1
    assert events[0].type == "person_absent"


def test_count_changed_between_nonzero():
    eng = _engine()
    eng.observe(CAM, 1, 0.9, now=0.0)
    eng.observe(CAM, 1, 0.9, now=2.0)   # presente=1
    eng.observe(CAM, 2, 0.9, now=3.0)   # candidato 2
    events = eng.observe(CAM, 2, 0.9, now=5.0)  # +2s
    assert len(events) == 1
    assert events[0].type == "person_count_changed"
    assert events[0].payload["person_count"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_event_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.analytics'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/analytics/__init__.py`: empty file.

`src/vitahub/analytics/event_engine.py`:
```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from vitahub.models import Camera, Event


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _CameraState:
    reported_count: int = 0
    pending_count: int | None = None
    pending_since: float = 0.0


class EventEngine:
    def __init__(
        self,
        hub_id: str,
        present_after_s: float = 2.0,
        absent_after_s: float = 5.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._hub_id = hub_id
        self._present_after_s = present_after_s
        self._absent_after_s = absent_after_s
        self._clock = clock
        self._states: dict[str, _CameraState] = {}

    def observe(
        self, camera: Camera, person_count: int, confidence: float, now: float
    ) -> list[Event]:
        state = self._states.setdefault(camera.id, _CameraState())

        if person_count == state.reported_count:
            state.pending_count = None
            return []

        if state.pending_count != person_count:
            state.pending_count = person_count
            state.pending_since = now
            return []

        required = self._required_hold(state.reported_count, person_count)
        if now - state.pending_since < required:
            return []

        event_type = self._event_type(state.reported_count, person_count)
        state.reported_count = person_count
        state.pending_count = None
        return [
            Event(
                hub_id=self._hub_id,
                camera_id=camera.id,
                camera_name=camera.name,
                type=event_type,
                severity="info",
                timestamp=self._clock().isoformat(),
                payload={
                    "person_count": person_count,
                    "confidence": round(confidence, 3),
                },
            )
        ]

    def _required_hold(self, old: int, new: int) -> float:
        if new == 0:
            return self._absent_after_s
        return self._present_after_s

    def _event_type(self, old: int, new: int) -> str:
        if old == 0 and new > 0:
            return "person_detected"
        if new == 0:
            return "person_absent"
        return "person_count_changed"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_event_engine.py -v`
Expected: PASS (todos los casos).

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/analytics tests/test_event_engine.py
git commit -m "feat: event engine with anti-flicker debounce state machine"
```

---

### Task 8: Registro de cámaras (reconciliación pura)

**Files:**
- Create: `src/vitahub/registry.py`
- Create: `tests/test_registry.py`

**Interfaces:**
- Consumes: `Camera` (Task 2).
- Produces:
  - `@dataclass(frozen=True) DiscoveredCamera(id: str, ip: str, rtsp_main: str, rtsp_sub: str)`.
  - `@dataclass(frozen=True) RegistryChange(camera_id: str, kind: str)` con `kind` ∈ `{"added", "ip_changed"}`.
  - `reconcile(existing: list[Camera], discovered: list[DiscoveredCamera]) -> tuple[list[Camera], list[RegistryChange]]` — devuelve la lista fusionada (mismo objeto `Camera` actualizado para conocidas, nuevo para desconocidas) y los cambios detectados. No borra cámaras que dejan de verse (eso es estado de conexión en runtime, no del registro).

- [ ] **Step 1: Write the failing test**

`tests/test_registry.py`:
```python
from vitahub.models import Camera
from vitahub.registry import DiscoveredCamera, reconcile


def _disc(id_, ip):
    return DiscoveredCamera(
        id=id_, ip=ip,
        rtsp_main=f"rtsp://{ip}:554/Streaming/Channels/1",
        rtsp_sub=f"rtsp://{ip}:554/Streaming/Channels/2",
    )


def test_new_camera_is_added():
    merged, changes = reconcile([], [_disc("onvif-a", "10.0.0.5")])
    assert [c.id for c in merged] == ["onvif-a"]
    assert merged[0].name == "camera-1"
    assert merged[0].last_ip == "10.0.0.5"
    assert changes == [__import__("vitahub.registry", fromlist=["RegistryChange"]).RegistryChange("onvif-a", "added")]


def test_known_camera_ip_change_is_updated():
    existing = [Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")]
    merged, changes = reconcile(existing, [_disc("onvif-a", "10.0.0.9")])
    assert merged[0].last_ip == "10.0.0.9"
    assert merged[0].name == "salon"  # nombre editado se preserva
    assert [ch.kind for ch in changes] == ["ip_changed"]


def test_known_camera_same_ip_no_change():
    existing = [Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")]
    merged, changes = reconcile(existing, [_disc("onvif-a", "10.0.0.5")])
    assert changes == []


def test_unseen_camera_is_kept():
    existing = [Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")]
    merged, changes = reconcile(existing, [])
    assert [c.id for c in merged] == ["onvif-a"]
    assert changes == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.registry'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/registry.py`:
```python
from __future__ import annotations

from dataclasses import dataclass

from vitahub.models import Camera


@dataclass(frozen=True)
class DiscoveredCamera:
    id: str
    ip: str
    rtsp_main: str
    rtsp_sub: str


@dataclass(frozen=True)
class RegistryChange:
    camera_id: str
    kind: str  # "added" | "ip_changed"


def reconcile(
    existing: list[Camera], discovered: list[DiscoveredCamera]
) -> tuple[list[Camera], list[RegistryChange]]:
    by_id = {c.id: c for c in existing}
    changes: list[RegistryChange] = []
    next_index = len(existing) + 1

    for dc in discovered:
        known = by_id.get(dc.id)
        if known is None:
            by_id[dc.id] = Camera(
                id=dc.id, name=f"camera-{next_index}", last_ip=dc.ip, enabled=True
            )
            changes.append(RegistryChange(dc.id, "added"))
            next_index += 1
        elif known.last_ip != dc.ip:
            known.last_ip = dc.ip
            changes.append(RegistryChange(dc.id, "ip_changed"))

    return list(by_id.values()), changes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/registry.py tests/test_registry.py
git commit -m "feat: camera registry reconciliation (stable id, ip-change absorption)"
```

---

### Task 9: Descubrimiento ONVIF (funciones puras + sonda de red)

La parte de red (socket multicast, HTTP SOAP) no corre en CI. Se aíslan las **funciones puras** de construcción/parseo y se testean con fixtures XML. La orquestación de red queda como función fina no cubierta por unit tests.

**Files:**
- Create: `src/vitahub/discovery/__init__.py`
- Create: `src/vitahub/discovery/onvif.py`
- Create: `tests/test_onvif_parsing.py`

**Interfaces:**
- Consumes: `Credentials` (Task 3), `DiscoveredCamera` (Task 8).
- Produces (funciones puras, testeables):
  - `build_probe() -> bytes` — mensaje WS-Discovery Probe.
  - `parse_probe_matches(xml: str) -> list[str]` — devuelve IPs (extraídas de los `XAddrs`).
  - `wsse_header(user: str, password: str, created: str, nonce: bytes) -> str` — cabecera WS-Security PasswordDigest.
  - `parse_serial(xml: str) -> str | None` — serie de `GetDeviceInformationResponse`.
  - `parse_stream_uri(xml: str) -> str | None` — URI de `GetStreamUriResponse`.
- Produce (orquestación de red, no en CI):
  - `discover(creds: Credentials, timeout: float = 3.0) -> list[DiscoveredCamera]`.

- [ ] **Step 1: Write the failing test**

`tests/test_onvif_parsing.py`:
```python
from vitahub.discovery.onvif import (
    build_probe,
    parse_probe_matches,
    parse_serial,
    parse_stream_uri,
    wsse_header,
)

PROBE_MATCH = """<?xml version="1.0"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <e:Body><d:ProbeMatches><d:ProbeMatch>
   <d:XAddrs>http://192.168.1.190:10000/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>"""

DEVINFO = """<Envelope><Body><GetDeviceInformationResponse>
 <SerialNumber>szjsa81a81e6adf9</SerialNumber>
</GetDeviceInformationResponse></Body></Envelope>"""

STREAMURI = """<Envelope><Body><GetStreamUriResponse><MediaUri>
 <Uri>rtsp://192.168.1.190/Streaming/Channels/1</Uri>
</MediaUri></GetStreamUriResponse></Body></Envelope>"""


def test_build_probe_is_ws_discovery():
    probe = build_probe()
    assert b"Probe" in probe
    assert b"discovery" in probe


def test_parse_probe_matches_extracts_ip():
    assert parse_probe_matches(PROBE_MATCH) == ["192.168.1.190"]


def test_parse_serial():
    assert parse_serial(DEVINFO) == "szjsa81a81e6adf9"
    assert parse_serial("<empty/>") is None


def test_parse_stream_uri():
    assert parse_stream_uri(STREAMURI) == "rtsp://192.168.1.190/Streaming/Channels/1"


def test_wsse_header_contains_digest_and_nonce():
    header = wsse_header("admin", "secret", "2026-08-15T10:00:00Z", b"0123456789abcdef")
    assert "UsernameToken" in header
    assert "admin" in header
    assert "secret" not in header  # la clave nunca va en claro
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_onvif_parsing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.discovery'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/discovery/__init__.py`: empty file.

`src/vitahub/discovery/onvif.py`:
```python
from __future__ import annotations

import base64
import hashlib
import re
import socket
import urllib.request
import uuid
from datetime import datetime, timezone

from vitahub.config import Credentials
from vitahub.logging_setup import get_logger
from vitahub.registry import DiscoveredCamera

_log = get_logger("discovery")

_MCAST = ("239.255.255.250", 3702)


def build_probe() -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>"
        '<w:To e:mustUnderstand="true">'
        "urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        '<w:Action e:mustUnderstand="true">'
        "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header><e:Body><d:Probe>"
        "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe></e:Body></e:Envelope>"
    ).encode()


def parse_probe_matches(xml: str) -> list[str]:
    ips: list[str] = []
    for xaddr in re.findall(r"<[^>]*XAddrs>(.*?)</[^>]*XAddrs>", xml, re.S):
        for url in xaddr.split():
            m = re.search(r"https?://([\d.]+)", url)
            if m and m.group(1) not in ips:
                ips.append(m.group(1))
    return ips


def wsse_header(user: str, password: str, created: str, nonce: bytes) -> str:
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()
    b64nonce = base64.b64encode(nonce).decode()
    return (
        '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org'
        '/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f"<UsernameToken><Username>{user}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{b64nonce}</Nonce>"
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        "</UsernameToken></Security></s:Header>"
    )


def _tag(xml: str, name: str) -> str | None:
    m = re.search(rf"<(?:\w+:)?{name}[^>]*>(.*?)</(?:\w+:)?{name}>", xml, re.S)
    return m.group(1).strip() if m else None


def parse_serial(xml: str) -> str | None:
    return _tag(xml, "SerialNumber")


def parse_stream_uri(xml: str) -> str | None:
    return _tag(xml, "Uri")


def discover(creds: Credentials, timeout: float = 3.0) -> list[DiscoveredCamera]:
    """Orquestación de red. No cubierto por unit tests (requiere LAN/hardware)."""
    ips = _probe_network(timeout)
    cameras: list[DiscoveredCamera] = []
    for ip in ips:
        try:
            cam = _interrogate(ip, creds)
            if cam is not None:
                cameras.append(cam)
        except OSError as exc:  # red/timeout de una cámara concreta
            _log.warning("no se pudo interrogar %s: %s", ip, exc)
    return cameras


def _probe_network(timeout: float) -> list[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    sock.bind(("0.0.0.0", 0))
    found: list[str] = []
    try:
        sock.sendto(build_probe(), _MCAST)
        while True:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            for ip in parse_probe_matches(data.decode("utf-8", "replace")):
                if ip not in found:
                    found.append(ip)
    finally:
        sock.close()
    return found


def _soap(url: str, body: str, creds: Credentials) -> str:
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    import os

    header = wsse_header(creds.onvif_user, creds.onvif_password, created, os.urandom(16))
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{header}<s:Body>{body}</s:Body></s:Envelope>"
    )
    req = urllib.request.Request(
        url, data=envelope.encode(),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.read().decode("utf-8", "replace")


def _interrogate(ip: str, creds: Credentials) -> DiscoveredCamera | None:
    dev_url = f"http://{ip}:10000/onvif/device_service"
    media_url = f"http://{ip}:10000/onvif/media_service"
    serial = parse_serial(_soap(dev_url, "<tds:GetDeviceInformation/>", creds))
    if not serial:
        return None
    profiles = _soap(media_url, "<trt:GetProfiles/>", creds)
    tokens = re.findall(r'token="([^"]+)"', profiles)
    main = sub = ""
    for i, tok in enumerate(dict.fromkeys(tokens)):
        uri = parse_stream_uri(
            _soap(
                media_url,
                "<trt:GetStreamUri><trt:StreamSetup>"
                "<tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>"
                "<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{tok}</trt:ProfileToken></trt:GetStreamUri>",
                creds,
            )
        )
        if uri and i == 0:
            main = uri
        elif uri:
            sub = uri
    return DiscoveredCamera(
        id=f"onvif-{serial}", ip=ip, rtsp_main=main, rtsp_sub=sub or main
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_onvif_parsing.py -v`
Expected: PASS (funciones puras). La orquestación de red se valida a mano en la Task 13.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/discovery tests/test_onvif_parsing.py
git commit -m "feat: ONVIF discovery (pure parsers tested, network orchestration)"
```

---

### Task 10: Detector de personas con YOLO

`PersonDetector` recibe el modelo por inyección para poder testear el parseo sin descargar pesos ni torch. Un `from_weights()` construye el real con Ultralytics.

**Files:**
- Create: `src/vitahub/inference/person_yolo.py`
- Create: `tests/test_person_detector.py`

**Interfaces:**
- Consumes: `Detector`, `Detection` (Tasks 2, 6).
- Produces:
  - `class PersonDetector(Detector)` con `__init__(self, model: Callable, confidence: float = 0.4)` y `detect(frame) -> list[Detection]` (filtra clase persona = 0 y umbral).
  - `classmethod from_weights(cls, weights_path: str, confidence: float = 0.4) -> PersonDetector`.

- [ ] **Step 1: Write the failing test**

`tests/test_person_detector.py`:
```python
from types import SimpleNamespace

from vitahub.inference.person_yolo import PersonDetector


class _FakeBoxes:
    """Imita ultralytics Results[0].boxes: .cls, .conf, .xyxy como listas."""

    def __init__(self, rows):
        self.cls = [r[0] for r in rows]
        self.conf = [r[1] for r in rows]
        self.xyxy = [r[2] for r in rows]

    def __len__(self):
        return len(self.cls)


def _fake_model(rows):
    result = SimpleNamespace(boxes=_FakeBoxes(rows))
    return lambda frame, verbose=False: [result]


def test_filters_persons_above_threshold():
    # (clase, conf, bbox): persona=0. Una persona a 0.8, un coche(2) a 0.9, persona a 0.2
    model = _fake_model([
        (0, 0.8, (0, 0, 10, 20)),
        (2, 0.9, (5, 5, 15, 25)),
        (0, 0.2, (1, 1, 2, 2)),
    ])
    det = PersonDetector(model=model, confidence=0.4)
    result = det.detect(frame=None)
    assert len(result) == 1
    assert result[0].label == "person"
    assert result[0].confidence == 0.8
    assert result[0].bbox == (0, 0, 10, 20)


def test_no_boxes_returns_empty():
    det = PersonDetector(model=_fake_model([]), confidence=0.4)
    assert det.detect(frame=None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_person_detector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.inference.person_yolo'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/inference/person_yolo.py`:
```python
from __future__ import annotations

from collections.abc import Callable

from vitahub.inference.base import Detector
from vitahub.models import Detection

_PERSON_CLASS_ID = 0


def _to_float(value: object) -> float:
    item = getattr(value, "item", None)  # soporta tensores torch
    return float(item()) if callable(item) else float(value)  # type: ignore[arg-type]


def _to_bbox(value: object) -> tuple[int, int, int, int]:
    tolist = getattr(value, "tolist", None)
    coords = tolist() if callable(tolist) else list(value)  # type: ignore[arg-type]
    x1, y1, x2, y2 = (int(round(float(c))) for c in coords)
    return (x1, y1, x2, y2)


class PersonDetector(Detector):
    def __init__(self, model: Callable, confidence: float = 0.4) -> None:
        self._model = model
        self._confidence = confidence

    def detect(self, frame: object) -> list[Detection]:
        results = self._model(frame, verbose=False)
        if not results:
            return []
        boxes = results[0].boxes
        detections: list[Detection] = []
        for cls, conf, xyxy in zip(boxes.cls, boxes.conf, boxes.xyxy, strict=True):
            if int(_to_float(cls)) != _PERSON_CLASS_ID:
                continue
            score = _to_float(conf)
            if score < self._confidence:
                continue
            detections.append(
                Detection(label="person", confidence=round(score, 3), bbox=_to_bbox(xyxy))
            )
        return detections

    @classmethod
    def from_weights(cls, weights_path: str, confidence: float = 0.4) -> PersonDetector:
        from ultralytics import YOLO  # import perezoso: torch solo en runtime real

        return cls(model=YOLO(weights_path), confidence=confidence)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_person_detector.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/inference/person_yolo.py tests/test_person_detector.py
git commit -m "feat: YOLO person detector (injectable model, tested filtering)"
```

---

### Task 11: Ingesta RTSP (lógica pura + captura con reconexión)

Se aíslan las decisiones puras (backoff, muestreo, watchdog de estancamiento) y se testean. El bucle con OpenCV queda como función fina no cubierta por unit tests.

**Files:**
- Create: `src/vitahub/ingest/__init__.py`
- Create: `src/vitahub/ingest/rtsp.py`
- Create: `tests/test_rtsp_logic.py`

**Interfaces:**
- Consumes: nada (funciones puras); usa OpenCV en runtime.
- Produces:
  - `backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float` — exponencial cap-eado (`attempt` empieza en 0).
  - `should_sample(last_sample_t: float, now: float, sample_fps: float) -> bool`.
  - `is_stalled(last_frame_t: float, now: float, max_stale_s: float) -> bool`.
  - `open_capture(rtsp_url: str) -> cv2.VideoCapture` — fuerza TCP y buffer mínimo (runtime).

- [ ] **Step 1: Write the failing test**

`tests/test_rtsp_logic.py`:
```python
from vitahub.ingest.rtsp import backoff_delay, is_stalled, should_sample


def test_backoff_is_capped_exponential():
    assert backoff_delay(0) == 1.0
    assert backoff_delay(1) == 2.0
    assert backoff_delay(2) == 4.0
    assert backoff_delay(10) == 30.0  # cap


def test_should_sample_respects_fps():
    # 2 fps => 1 muestra cada 0.5s
    assert should_sample(last_sample_t=0.0, now=0.4, sample_fps=2.0) is False
    assert should_sample(last_sample_t=0.0, now=0.5, sample_fps=2.0) is True


def test_is_stalled():
    assert is_stalled(last_frame_t=0.0, now=9.0, max_stale_s=10.0) is False
    assert is_stalled(last_frame_t=0.0, now=11.0, max_stale_s=10.0) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rtsp_logic.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.ingest'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/ingest/__init__.py`: empty file.

`src/vitahub/ingest/rtsp.py`:
```python
from __future__ import annotations

import os

# Fuerza RTSP sobre TCP en el backend FFmpeg de OpenCV (debe fijarse antes de importar cv2).
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    return min(cap, base * (2**attempt))


def should_sample(last_sample_t: float, now: float, sample_fps: float) -> bool:
    if sample_fps <= 0:
        return True
    return (now - last_sample_t) >= (1.0 / sample_fps)


def is_stalled(last_frame_t: float, now: float, max_stale_s: float) -> bool:
    return (now - last_frame_t) > max_stale_s


def open_capture(rtsp_url: str):  # type: ignore[no-untyped-def]
    """Runtime only. No cubierto por unit tests (requiere stream real)."""
    import cv2

    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_rtsp_logic.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/ingest tests/test_rtsp_logic.py
git commit -m "feat: RTSP ingest logic (backoff, sampling, stall watchdog)"
```

---

### Task 12: Supervisor y worker por cámara

Cablea todo. El worker de una cámara se prueba en aislamiento con componentes falsos (captura, detector, sink) verificando que un frame con personas acaba produciendo un evento en el sink. La función `main()` (bucle real, señales) queda fina.

**Files:**
- Create: `src/vitahub/factory.py`
- Create: `src/vitahub/worker.py`
- Create: `src/vitahub/app.py`
- Create: `tests/test_worker.py`
- Create: `tests/test_factory.py`

**Interfaces:**
- Consumes: `Camera`, `Detector`, `EventEngine`, `EventSink`, `InferenceConfig`, `StubDetector`, `PersonDetector`.
- Produces:
  - `build_detector(cfg: InferenceConfig, weights_path: str) -> Detector` (factory: `"stub"` → `StubDetector`; `"person_yolo"` → `PersonDetector.from_weights`).
  - `process_frame(camera, frame, detector, engine, sink, now: float) -> list[Event]` — corre detección, alimenta el engine, emite por el sink lo que salga; devuelve los eventos emitidos.
  - `run(config_path: Path, weights_path: str, env) -> None` (en `app.py`) — bootstrap real.
  - `main() -> None` — entrypoint con `configure_logging`, manejo de SIGTERM y salida `exit(1)` en `ConfigError`.

- [ ] **Step 1: Write the failing test**

`tests/test_worker.py`:
```python
from vitahub.analytics.event_engine import EventEngine
from vitahub.inference.stub import StubDetector
from vitahub.models import Camera, Event
from vitahub.worker import process_frame


class _RecordingSink:
    def __init__(self):
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


CAM = Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")


def test_process_frame_emits_person_detected_after_threshold():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1)

    assert process_frame(CAM, None, detector, engine, sink, now=0.0) == []
    emitted = process_frame(CAM, None, detector, engine, sink, now=2.0)

    assert len(emitted) == 1
    assert emitted[0].type == "person_detected"
    assert sink.events == emitted
```

`tests/test_factory.py`:
```python
from vitahub.config import InferenceConfig
from vitahub.factory import build_detector
from vitahub.inference.stub import StubDetector


def test_build_stub_detector():
    det = build_detector(InferenceConfig(detector="stub"), weights_path="unused.pt")
    assert isinstance(det, StubDetector)


def test_unknown_detector_raises():
    import pytest

    with pytest.raises(ValueError, match="desconocido"):
        build_detector(InferenceConfig(detector="nope"), weights_path="unused.pt")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_worker.py tests/test_factory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vitahub.worker'`.

- [ ] **Step 3: Write minimal implementation**

`src/vitahub/factory.py`:
```python
from __future__ import annotations

from vitahub.config import InferenceConfig
from vitahub.inference.base import Detector
from vitahub.inference.person_yolo import PersonDetector
from vitahub.inference.stub import StubDetector


def build_detector(cfg: InferenceConfig, weights_path: str) -> Detector:
    if cfg.detector == "stub":
        return StubDetector()
    if cfg.detector == "person_yolo":
        return PersonDetector.from_weights(weights_path, confidence=cfg.confidence)
    raise ValueError(f"Detector desconocido: {cfg.detector}")
```

`src/vitahub/worker.py`:
```python
from __future__ import annotations

from vitahub.analytics.event_engine import EventEngine
from vitahub.inference.base import Detector
from vitahub.models import Camera, Event
from vitahub.sinks.base import EventSink


def process_frame(
    camera: Camera,
    frame: object,
    detector: Detector,
    engine: EventEngine,
    sink: EventSink,
    now: float,
) -> list[Event]:
    detections = detector.detect(frame)
    person_count = sum(1 for d in detections if d.label == "person")
    confidence = max((d.confidence for d in detections), default=0.0)
    events = engine.observe(camera, person_count, confidence, now)
    for event in events:
        sink.emit(event)
    return events
```

`src/vitahub/app.py`:
```python
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

from vitahub.analytics.event_engine import EventEngine
from vitahub.config import ConfigError, HubConfig, load_config, save_cameras
from vitahub.discovery.onvif import discover
from vitahub.factory import build_detector
from vitahub.ingest.rtsp import (
    backoff_delay,
    is_stalled,
    open_capture,
    should_sample,
)
from vitahub.logging_setup import configure_logging, get_logger
from vitahub.models import Camera
from vitahub.registry import DiscoveredCamera, reconcile
from vitahub.sinks.stdout_json import StdoutJsonSink
from vitahub.worker import process_frame

_log = get_logger("app")
_HEARTBEAT_FILE = Path("/data/heartbeat")


def run(config_path: Path, weights_path: str, env: dict[str, str]) -> None:
    cfg = load_config(config_path, env)
    _log.info("hub %s arrancando", cfg.hub_id)
    detector = build_detector(cfg.inference, weights_path)
    engine = EventEngine(cfg.hub_id)
    sink = StdoutJsonSink()
    stop = threading.Event()

    # Descubrimiento inicial + reconciliación persistida.
    discovered = discover(cfg.credentials, timeout=float(cfg.discovery.interval_seconds))
    cfg.cameras, changes = reconcile(cfg.cameras, discovered)
    for ch in changes:
        _log.info("registro: %s %s", ch.kind, ch.camera_id)
    save_cameras(config_path, cfg.cameras)

    uris = {dc.id: (dc.rtsp_sub if cfg.inference.stream == "substream" else dc.rtsp_main)
            for dc in discovered}

    threads = [
        threading.Thread(
            target=_camera_loop,
            args=(cam, uris.get(cam.id), detector, engine, sink, cfg, stop),
            name=f"cam-{cam.id}",
            daemon=True,
        )
        for cam in cfg.cameras
        if cam.enabled and uris.get(cam.id)
    ]
    for t in threads:
        t.start()

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(timeout=5.0):
        _touch_heartbeat()
    _log.info("apagando (SIGTERM)")
    sink.close()


def _camera_loop(camera, rtsp_url, detector, engine, sink, cfg, stop):  # type: ignore[no-untyped-def]
    attempt = 0
    while not stop.is_set():
        cap = open_capture(rtsp_url)
        if not cap.isOpened():
            delay = backoff_delay(attempt)
            _log.warning("cam %s no abre, reintento en %.0fs", camera.id, delay)
            attempt += 1
            stop.wait(delay)
            continue
        _log.info("cam %s conectada", camera.id)
        attempt = 0
        last_sample = 0.0
        last_frame = time.monotonic()
        while not stop.is_set():
            ok, frame = cap.read()
            now = time.monotonic()
            if not ok:
                if is_stalled(last_frame, now, max_stale_s=10.0):
                    _log.warning("cam %s estancada, reconecto", camera.id)
                    break
                continue
            last_frame = now
            if should_sample(last_sample, now, cfg.inference.sample_fps):
                last_sample = now
                try:
                    process_frame(camera, frame, detector, engine, sink, now)
                except Exception:  # noqa: BLE001 — un frame malo no tumba el worker
                    _log.exception("cam %s error procesando frame", camera.id)
        cap.release()
        _log.info("cam %s desconectada", camera.id)


def _touch_heartbeat() -> None:
    try:
        _HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HEARTBEAT_FILE.write_text(str(time.time()))
    except OSError:
        pass


def main() -> None:
    configure_logging(os.environ.get("VITAHUB_LOG_LEVEL", "INFO"))
    config_path = Path(os.environ.get("VITAHUB_CONFIG", "/data/hub.yaml"))
    weights_path = os.environ.get("VITAHUB_WEIGHTS", "/app/models/yolo11n.pt")
    try:
        run(config_path, weights_path, dict(os.environ))
    except ConfigError as exc:
        _log.error("config inválida: %s", exc)
        sys.exit(1)
    except Exception:  # noqa: BLE001 — última red de seguridad
        _log.exception("fallo no controlado")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_worker.py tests/test_factory.py -v`
Expected: PASS.

- [ ] **Step 5: Full suite + lint + types + commit**

```bash
ruff check src tests && mypy && python -m pytest -v
git add src/vitahub/factory.py src/vitahub/worker.py src/vitahub/app.py tests/test_worker.py tests/test_factory.py
git commit -m "feat: supervisor, per-camera worker loop, entrypoint"
```

---

### Task 13: Empaquetado, healthcheck y runbook

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `scripts/healthcheck.py`
- Create: `scripts/download_model.py`
- Create: `README.md`

**Interfaces:**
- Consumes: todo lo anterior; `app.main` como entrypoint.
- Produces: imagen Docker multi-arch ejecutable, healthcheck, y runbook de campo.

- [ ] **Step 1: Write the healthcheck test**

`tests/test_healthcheck.py`:
```python
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "healthcheck", Path(__file__).resolve().parents[1] / "scripts" / "healthcheck.py"
)
healthcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthcheck)  # type: ignore[union-attr]


def test_fresh_heartbeat_is_healthy(tmp_path):
    hb = tmp_path / "heartbeat"
    hb.write_text("0")
    assert healthcheck.is_healthy(hb, now=30.0, max_age_s=60.0) is True


def test_stale_heartbeat_is_unhealthy(tmp_path):
    hb = tmp_path / "heartbeat"
    hb.write_text("0")
    assert healthcheck.is_healthy(hb, now=120.0, max_age_s=60.0) is False


def test_missing_heartbeat_is_unhealthy(tmp_path):
    assert healthcheck.is_healthy(tmp_path / "nope", now=1.0, max_age_s=60.0) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_healthcheck.py -v`
Expected: FAIL — no existe `scripts/healthcheck.py`.

- [ ] **Step 3: Write the implementation**

`scripts/healthcheck.py`:
```python
from __future__ import annotations

import sys
import time
from pathlib import Path


def is_healthy(heartbeat: Path, now: float, max_age_s: float = 60.0) -> bool:
    try:
        written = float(heartbeat.read_text().strip())
    except (OSError, ValueError):
        return False
    return (now - written) <= max_age_s


if __name__ == "__main__":
    ok = is_healthy(Path("/data/heartbeat"), now=time.time())
    sys.exit(0 if ok else 1)
```

`scripts/download_model.py`:
```python
"""Descarga los pesos YOLO en build (para embeberlos en la imagen)."""
from __future__ import annotations

import sys

from ultralytics import YOLO

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "yolo11n.pt"
    YOLO(target)  # descarga a la cache; se copia en el Dockerfile
    print(f"modelo {target} descargado")
```

`Dockerfile`:
```dockerfile
# Base por defecto CPU (Mac/dev). Para Orin: --build-arg BASE_IMAGE=nvcr.io/nvidia/l4t-pytorch:<tag>
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE} AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Embebe los pesos del modelo (arranca sin internet).
RUN python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')" \
    && mkdir -p /app/models \
    && cp "$(python -c 'import ultralytics, os; print(os.path.join(os.getcwd(), "yolo11n.pt"))')" /app/models/yolo11n.pt || true

COPY scripts ./scripts

ENV VITAHUB_CONFIG=/data/hub.yaml \
    VITAHUB_WEIGHTS=/app/models/yolo11n.pt \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python /app/scripts/healthcheck.py || exit 1

ENTRYPOINT ["python", "-m", "vitahub.app"]
```

`docker-compose.yml`:
```yaml
services:
  hub:
    build: .
    image: vitahub:local
    restart: unless-stopped
    network_mode: host          # necesario para el multicast ONVIF de descubrimiento
    environment:
      VITAHUB_ONVIF_USER: ${VITAHUB_ONVIF_USER:?falta usuario ONVIF}
      VITAHUB_ONVIF_PASSWORD: ${VITAHUB_ONVIF_PASSWORD:?falta contraseña ONVIF}
      VITAHUB_LOG_LEVEL: INFO
    volumes:
      - ./data:/data
```

`README.md`: incluir (a) qué es el hub, (b) desarrollo local, (c) runbook de instalación en campo:
````markdown
# vita-docker-process-hub

Hub de procesamiento edge de VitaPlus (rama cámaras). Descubre cámaras ONVIF,
ingesta RTSP, detecta presencia/conteo de personas y emite eventos JSON por stdout.

## Desarrollo local (Mac, CPU)

```bash
pip install -e ".[dev]"
export VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=xxxx
export VITAHUB_CONFIG=./config/hub.example.yaml
python -m vitahub.app
```

Para probar sin cámaras ni modelo: pon `detector: stub` en la config.

## Docker

```bash
export VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=xxxx
docker compose up --build
```

## Instalación en campo (por hogar)

1. Con el móvil: mete cada cámara en el WiFi del hogar, activa ONVIF y ponle la
   contraseña **del hogar** (la misma para todas).
2. En el Jetson: define `VITAHUB_ONVIF_USER`/`VITAHUB_ONVIF_PASSWORD` (esa clave) y
   `docker compose up -d`.
3. Verifica: `docker logs -f <container>` — deberías ver "cam onvif-... conectada" y
   eventos `person_detected` por stdout.
4. El contenedor se relanza solo tras cortes de luz (`restart: unless-stopped`).

## Despliegue en Jetson Orin (GPU)

```bash
docker build --build-arg BASE_IMAGE=nvcr.io/nvidia/l4t-pytorch:r36.2.0-pth2.2-py3 -t vitahub:orin .
```
````

- [ ] **Step 4: Run test + build to verify**

Run: `python -m pytest tests/test_healthcheck.py -v` → PASS.
Run (opcional, requiere Docker): `docker compose build` → imagen construye.
Run (opcional, requiere cámara real en LAN): `docker compose up` con `detector: person_yolo` y verifica eventos `person_detected` en `docker logs`. Esta es la validación de integración manual de las Tasks 9 y 11 (descubrimiento y RTSP), que no corren en CI.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml scripts README.md tests/test_healthcheck.py
git commit -m "feat: packaging (multi-arch Dockerfile, healthcheck, field runbook)"
```

---

## Self-Review (rellenado por el autor del plan)

**1. Cobertura del spec:**
- §3 Arquitectura/estructura → Tasks 1-12 (estructura montada incrementalmente); costuras `Detector`/`EventSink` → Tasks 5, 6, 10.
- §4 Modelo de evento (envelope, tipos, severidad, stdout/stderr) → Tasks 2 (envelope), 4 (stderr), 5 (stdout), 7 (tipos/severidad/cadencia).
- §5 Descubrimiento y config (ONVIF, credencial env, identidad estable, reconciliación, override) → Tasks 3, 8, 9. (Override manual por IP: soportado porque `reconcile` fusiona por `id` y la config es editable a mano; documentado en README.)
- §6 Ingesta e inferencia (TCP, buffer, substream, backoff, watchdog, YOLO/stub, pesos embebidos) → Tasks 10, 11, 13.
- §7 Errores/resiliencia/ciclo de vida (fail-fast, worker aislado, uncaught, restart, SIGTERM, /data, healthcheck) → Tasks 3, 12, 13.
- §8 Testing (event-engine, registry, config, stub) → Tasks 3, 6, 7, 8 + unit puros en 9, 10, 11, 13.
- §9 Empaquetado → Task 13.

**2. Placeholder scan:** sin `TBD`/`TODO`/"handle errors"; todos los pasos con código real. Los bloques marcados "no cubierto por unit tests" (red/OpenCV) son decisión explícita del spec (§8), con validación manual en Task 13, no placeholders.

**3. Consistencia de tipos:** `Event`/`Camera`/`Detection` (Task 2) usados con las mismas firmas en 5, 6, 7, 8, 10, 12. `EventEngine.observe(camera, person_count, confidence, now)` idéntico en 7 y 12. `Detector.detect(frame)` idéntico en 6, 10, 12. `DiscoveredCamera(id, ip, rtsp_main, rtsp_sub)` definido en 8 y producido en 9. `Credentials` definido en 3 y consumido en 9. `reconcile`/`RegistryChange` de 8 usados en 12.

**Nota de dependencia entre repos:** este plan no toca AWS. Cuando exista `02-hub-ingest` en `vitaplus-aws-architecture`, se añadirá `sinks/aws.py` implementando `EventSink` — sin tocar el resto.
