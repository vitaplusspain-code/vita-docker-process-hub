# Detección de caídas por pose — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el hub emita `fall_detected` / `fall_update` / `fall_resolved` con score y señales explicativas a partir de la pose de las personas, sin tocar la detección de presencia, apagado por defecto.

**Architecture:** Un detector nuevo (`PosePersonDetector`, `yolo11n-pose`) devuelve las mismas `Detection` de persona que hoy más 17 keypoints. Un `FallEngine` determinista (emparejamiento IoU, señales geométricas, score ponderado, máquina de estados `upright → candidate → reported`) convierte esas poses en eventos por el mismo sink. Presencia (`EventEngine`) y caídas corren en paralelo sobre las mismas detecciones dentro de `process_frame`.

**Tech Stack:** Python 3.12, ultralytics (`yolo11n-pose.pt`), OpenCV (solo en el script de replay), pytest, ruff, mypy strict.

Spec: `docs/superpowers/specs/2026-08-22-deteccion-caidas-design.md`.

## Global Constraints

- Con `detector: person_yolo` y `inference.fall.enabled: false` (defaults) el hub se comporta **idéntico** al actual; todos los tests existentes siguen en verde sin modificarlos.
- `schema_version` del evento sigue en **1**.
- Severidades: `fall_detected` y `fall_update` → `high`; `fall_resolved` → `info`.
- Pesos y umbrales son **constantes con comentario** en `fall_signals.py` / `fall_engine.py`, no config.
- `FallEngine.observe` **nunca lanza por datos** (sin personas, sin keypoints, cajas degeneradas).
- Ningún test necesita GPU, pesos ni red. Ultralytics se importa perezosamente (solo en `from_weights`).
- Comentarios, docstrings, mensajes de log y de error en **castellano**, como el resto del repo.
- Comandos de verificación: `pytest -q`, `ruff check src tests`, `mypy` (strict, paquete `vitahub`). Los tres deben pasar en cada commit.
- Keypoints en formato COCO-17 `(x, y, conf)` en píxeles. Índices usados: 0 nariz, 5/6 hombros, 11/12 caderas.
- Coordenadas de imagen: **y crece hacia abajo** (bajar = y aumenta).

---

## Mapa de ficheros

| Fichero | Responsabilidad |
|---|---|
| `src/vitahub/models.py` (modificar) | `Detection.keypoints` opcional. |
| `src/vitahub/inference/stub.py` (modificar) | `StubDetector` acepta keypoints sintéticos. |
| `src/vitahub/inference/person_pose_yolo.py` (crear) | `PosePersonDetector`: cajas + keypoints desde ultralytics. |
| `src/vitahub/analytics/fall_signals.py` (crear) | Geometría pura: señales por persona y score. Sin estado. |
| `src/vitahub/analytics/fall_engine.py` (crear) | Pistas, historial, máquina de estados, eventos. |
| `src/vitahub/config.py` (modificar) | `FallConfig` dentro de `InferenceConfig`, validación. |
| `src/vitahub/factory.py` (modificar) | `person_pose`, comprobación de pesos. |
| `src/vitahub/worker.py` (modificar) | `process_frame` llama al `FallEngine` si existe. |
| `src/vitahub/app.py` (modificar) | Construye `FallEngine` y elige pesos de pose. |
| `scripts/download_model.py` (modificar) | Descarga el modelo cuyo nombre sea el del destino. |
| `Dockerfile` (modificar) | Embebe `yolo11n-pose.pt`, `VITAHUB_POSE_WEIGHTS`. |
| `scripts/replay_video.py` (crear) | Calibración offline sobre un `.mp4`. |
| `config/hub.example.yaml`, `README.md`, `docs/como-funciona.md`, `docs/como-probar.md`, `docs/backlog.md` (modificar) | Documentación. |

---

### Task 1: `Detection.keypoints` y stub con pose

**Files:**
- Modify: `src/vitahub/models.py:8-12`
- Modify: `src/vitahub/inference/stub.py`
- Test: `tests/test_models.py`, `tests/test_stub_detector.py`

**Interfaces:**
- Produces: `Detection(label, confidence, bbox, keypoints: Keypoints | None = None)` con `Keypoints = tuple[tuple[float, float, float], ...]` exportado desde `vitahub.models`.
- Produces: `StubDetector(person_count=0, confidence=0.99, keypoints: Keypoints | None = None, bbox=(0, 0, 1, 1))` — todas las personas que devuelve llevan los mismos `keypoints` y `bbox`.

- [ ] **Step 1: Tests que fallan**

Añadir a `tests/test_models.py`:

```python
def test_detection_keypoints_default_none():
    d = Detection(label="person", confidence=0.9, bbox=(0, 0, 10, 20))
    assert d.keypoints is None


def test_detection_accepts_keypoints():
    kps = tuple((float(i), float(i), 0.9) for i in range(17))
    d = Detection(label="person", confidence=0.9, bbox=(0, 0, 10, 20), keypoints=kps)
    assert d.keypoints is not None
    assert len(d.keypoints) == 17
```

Añadir a `tests/test_stub_detector.py`:

```python
def test_stub_without_keypoints_returns_none():
    det = StubDetector(person_count=1)
    assert det.detect(frame=None)[0].keypoints is None


def test_stub_with_keypoints_and_bbox():
    kps = tuple((1.0, 2.0, 0.9) for _ in range(17))
    det = StubDetector(person_count=2, keypoints=kps, bbox=(10, 10, 50, 100))
    result = det.detect(frame=None)
    assert len(result) == 2
    assert all(d.keypoints == kps for d in result)
    assert all(d.bbox == (10, 10, 50, 100) for d in result)
```

- [ ] **Step 2: Comprobar que fallan**

Run: `pytest tests/test_models.py tests/test_stub_detector.py -q`
Expected: FAIL (`TypeError: unexpected keyword argument 'keypoints'`).

- [ ] **Step 3: Implementar**

En `src/vitahub/models.py`, sustituir la clase `Detection`:

```python
# Un keypoint COCO-17: (x, y, conf) en píxeles de la imagen analizada.
Keypoints = tuple[tuple[float, float, float], ...]


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    bbox: tuple[int, int, int, int]
    # Solo lo rellena un detector de pose (person_pose). None = detector sin
    # pose; la analítica de caídas degrada a geometría de caja en ese caso.
    keypoints: Keypoints | None = None
```

Sustituir `src/vitahub/inference/stub.py`:

```python
from __future__ import annotations

from vitahub.inference.base import Detector
from vitahub.models import Detection, Keypoints


class StubDetector(Detector):
    """Detector sin modelo: devuelve N personas fijas. Para pruebas y config sin GPU."""

    def __init__(
        self,
        person_count: int = 0,
        confidence: float = 0.99,
        keypoints: Keypoints | None = None,
        bbox: tuple[int, int, int, int] = (0, 0, 1, 1),
    ) -> None:
        self._person_count = person_count
        self._confidence = confidence
        self._keypoints = keypoints
        self._bbox = bbox

    def detect(self, frame: object) -> list[Detection]:
        return [
            Detection(
                label="person",
                confidence=self._confidence,
                bbox=self._bbox,
                keypoints=self._keypoints,
            )
            for _ in range(self._person_count)
        ]
```

- [ ] **Step 4: Verificar**

Run: `pytest -q && ruff check src tests && mypy`
Expected: todo en verde.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/models.py src/vitahub/inference/stub.py tests/test_models.py tests/test_stub_detector.py
git commit -m "feat: Detection.keypoints opcional y StubDetector con pose sintética"
```

---

### Task 2: `PosePersonDetector`

**Files:**
- Create: `src/vitahub/inference/person_pose_yolo.py`
- Test: `tests/test_pose_detector.py`

**Interfaces:**
- Consumes: `Detection`, `Keypoints` (Task 1); `_to_float`, `_to_bbox`, `_PERSON_CLASS_ID` de `vitahub.inference.person_yolo`.
- Produces: `PosePersonDetector(model, confidence=0.4)` con `detect(frame) -> list[Detection]` y `PosePersonDetector.from_weights(weights_path, confidence=0.4)`.

Ultralytics (modelos `*-pose`) devuelve en `results[0].keypoints.data` un tensor `(n, 17, 3)` alineado con `results[0].boxes`. Si el modelo no es de pose, `results[0].keypoints` es `None`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/test_pose_detector.py`:

```python
from types import SimpleNamespace

from vitahub.inference.person_pose_yolo import PosePersonDetector


class _FakeBoxes:
    def __init__(self, rows):
        self.cls = [r[0] for r in rows]
        self.conf = [r[1] for r in rows]
        self.xyxy = [r[2] for r in rows]

    def __len__(self):
        return len(self.cls)


class _FakeKeypoints:
    """Imita ultralytics Results[0].keypoints: .data es (n, 17, 3)."""

    def __init__(self, per_person):
        self.data = per_person


def _kps(value: float, conf: float = 0.9):
    return [[value, value, conf] for _ in range(17)]


def _fake_model(rows, keypoints=None):
    result = SimpleNamespace(
        boxes=_FakeBoxes(rows),
        keypoints=None if keypoints is None else _FakeKeypoints(keypoints),
    )
    return lambda frame, verbose=False: [result]


def test_returns_persons_with_keypoints_aligned_to_boxes():
    model = _fake_model(
        [(0, 0.8, (0, 0, 10, 20)), (2, 0.9, (5, 5, 15, 25)), (0, 0.7, (1, 1, 2, 2))],
        keypoints=[_kps(1.0), _kps(2.0), _kps(3.0)],
    )
    det = PosePersonDetector(model=model, confidence=0.4)
    result = det.detect(frame=None)
    assert [d.bbox for d in result] == [(0, 0, 10, 20), (1, 1, 2, 2)]
    assert result[0].keypoints is not None and result[0].keypoints[0] == (1.0, 1.0, 0.9)
    assert result[1].keypoints is not None and result[1].keypoints[0] == (3.0, 3.0, 0.9)
    assert len(result[0].keypoints) == 17


def test_filters_below_confidence():
    model = _fake_model([(0, 0.2, (0, 0, 1, 1))], keypoints=[_kps(1.0)])
    assert PosePersonDetector(model=model, confidence=0.4).detect(frame=None) == []


def test_model_without_keypoints_yields_none():
    model = _fake_model([(0, 0.8, (0, 0, 10, 20))], keypoints=None)
    result = PosePersonDetector(model=model, confidence=0.4).detect(frame=None)
    assert len(result) == 1
    assert result[0].keypoints is None


def test_empty_results_returns_empty():
    det = PosePersonDetector(model=lambda frame, verbose=False: [], confidence=0.4)
    assert det.detect(frame=None) == []
```

- [ ] **Step 2: Comprobar que fallan**

Run: `pytest tests/test_pose_detector.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implementar**

Crear `src/vitahub/inference/person_pose_yolo.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vitahub.inference.base import Detector
from vitahub.inference.person_yolo import _PERSON_CLASS_ID, _to_bbox, _to_float
from vitahub.models import Detection, Keypoints


def _to_keypoints(value: object) -> Keypoints:
    tolist = getattr(value, "tolist", None)
    rows = tolist() if callable(tolist) else list(value)  # type: ignore[call-overload]
    return tuple((float(x), float(y), float(c)) for x, y, c in rows)


class PosePersonDetector(Detector):
    """Detector de persona con pose (yolo11n-pose).

    Devuelve exactamente las mismas cajas que PersonDetector (misma clase,
    mismo umbral) y además los 17 keypoints COCO de cada persona. Un solo
    modelo sirve a la presencia y a la analítica de caídas.
    """

    def __init__(self, model: Callable[..., Any], confidence: float = 0.4) -> None:
        self._model = model
        self._confidence = confidence

    def detect(self, frame: object) -> list[Detection]:
        results = self._model(frame, verbose=False)
        if not results:
            return []
        boxes = results[0].boxes
        # Un modelo sin cabeza de pose (p. ej. yolo11n.pt cargado por error)
        # devuelve keypoints=None: se degrada a cajas sin pose en vez de fallar.
        kp_obj = getattr(results[0], "keypoints", None)
        kp_rows = list(kp_obj.data) if kp_obj is not None else None
        detections: list[Detection] = []
        for idx, (cls, conf, xyxy) in enumerate(
            zip(boxes.cls, boxes.conf, boxes.xyxy, strict=True)
        ):
            if int(_to_float(cls)) != _PERSON_CLASS_ID:
                continue
            score = _to_float(conf)
            if score < self._confidence:
                continue
            keypoints = _to_keypoints(kp_rows[idx]) if kp_rows is not None else None
            detections.append(
                Detection(
                    label="person",
                    confidence=round(score, 3),
                    bbox=_to_bbox(xyxy),
                    keypoints=keypoints,
                )
            )
        return detections

    @classmethod
    def from_weights(cls, weights_path: str, confidence: float = 0.4) -> PosePersonDetector:
        from ultralytics import YOLO  # import perezoso: torch solo en runtime real

        return cls(model=YOLO(weights_path), confidence=confidence)
```

- [ ] **Step 4: Verificar**

Run: `pytest -q && ruff check src tests && mypy`
Expected: verde.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/inference/person_pose_yolo.py tests/test_pose_detector.py
git commit -m "feat: PosePersonDetector (yolo11n-pose) con keypoints alineados a las cajas"
```

---

### Task 3: Señales y score (`fall_signals.py`)

**Files:**
- Create: `src/vitahub/analytics/fall_signals.py`
- Test: `tests/test_fall_signals.py`

**Interfaces:**
- Consumes: `Keypoints` (Task 1).
- Produces (todo en `vitahub.analytics.fall_signals`):
  - `Sample = tuple[float, float, float]` — `(t, ref_y, box_h)`: instante, altura de referencia (medio de caderas o centro de caja) y alto de caja.
  - `reference_y(keypoints, bbox) -> float`
  - `torso_angle(keypoints) -> float | None`
  - `head_low(keypoints, bbox) -> bool | None`
  - `keypoint_conf(keypoints) -> float | None`
  - `drop_speed(history: list[Sample], now: float) -> float | None`
  - `@dataclass(frozen=True) Signals(torso_angle, bbox_ratio, drop_speed, floor_time_s, head_low, keypoint_conf)` con `to_payload() -> dict[str, object]`
  - `compute_signals(keypoints, bbox, history, now, floor_time_s) -> Signals`
  - `is_horizontal(signals) -> bool`, `is_upright(signals) -> bool`
  - `score(signals) -> float`
  - Constantes: `KEYPOINT_MIN_CONF = 0.3`, `HORIZONTAL_ANGLE = 60.0`, `UPRIGHT_ANGLE = 45.0`, `HORIZONTAL_RATIO = 1.2`, `DROP_WINDOW_S = 1.5`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/test_fall_signals.py`:

```python
import math

import pytest

from vitahub.analytics.fall_signals import (
    Signals,
    compute_signals,
    drop_speed,
    head_low,
    is_horizontal,
    is_upright,
    keypoint_conf,
    reference_y,
    score,
    torso_angle,
)

NOSE, L_SH, R_SH, L_HIP, R_HIP = 0, 5, 6, 11, 12


def _pose(nose, shoulders, hips, conf=0.9):
    """Pose sintética: solo nariz, hombros y caderas; el resto a conf 0."""
    kps = [(0.0, 0.0, 0.0)] * 17
    kps[NOSE] = (*nose, conf)
    kps[L_SH] = (shoulders[0] - 10, shoulders[1], conf)
    kps[R_SH] = (shoulders[0] + 10, shoulders[1], conf)
    kps[L_HIP] = (hips[0] - 10, hips[1], conf)
    kps[R_HIP] = (hips[0] + 10, hips[1], conf)
    return tuple(kps)


STANDING = _pose(nose=(50, 10), shoulders=(50, 30), hips=(50, 80))   # torso vertical
LYING = _pose(nose=(10, 90), shoulders=(30, 90), hips=(90, 90))      # torso horizontal
BOX_STANDING = (30, 0, 70, 120)
BOX_LYING = (0, 80, 120, 110)


def test_torso_angle_vertical_and_horizontal():
    assert torso_angle(STANDING) == pytest.approx(0.0)
    assert torso_angle(LYING) == pytest.approx(90.0)


def test_torso_angle_diagonal():
    diag = _pose(nose=(0, 0), shoulders=(0, 0), hips=(50, 50))
    assert torso_angle(diag) == pytest.approx(45.0)


def test_torso_angle_none_without_confident_points():
    low = _pose(nose=(50, 10), shoulders=(50, 30), hips=(50, 80), conf=0.1)
    assert torso_angle(low) is None
    assert torso_angle(None) is None


def test_head_low_uses_hips_when_available():
    assert head_low(STANDING, BOX_STANDING) is False
    assert head_low(LYING, BOX_LYING) is False  # nariz a la misma altura que cadera
    below = _pose(nose=(50, 100), shoulders=(50, 30), hips=(50, 80))
    assert head_low(below, BOX_STANDING) is True


def test_head_low_falls_back_to_box_thirds_without_hips():
    only_nose = [(0.0, 0.0, 0.0)] * 17
    only_nose[NOSE] = (50.0, 100.0, 0.9)
    assert head_low(tuple(only_nose), (0, 0, 100, 120)) is True  # 100 > 80 (2/3 de 120)
    only_nose[NOSE] = (50.0, 10.0, 0.9)
    assert head_low(tuple(only_nose), (0, 0, 100, 120)) is False


def test_head_low_none_without_nose():
    assert head_low(None, BOX_STANDING) is None
    no_nose = list(STANDING)
    no_nose[NOSE] = (0.0, 0.0, 0.0)
    assert head_low(tuple(no_nose), BOX_STANDING) is None


def test_keypoint_conf_is_mean_of_used_points():
    assert keypoint_conf(STANDING) == pytest.approx(0.9)
    assert keypoint_conf(None) is None


def test_reference_y_prefers_hips_then_box_center():
    assert reference_y(STANDING, BOX_STANDING) == pytest.approx(80.0)
    assert reference_y(None, (0, 0, 100, 120)) == pytest.approx(60.0)


def test_drop_speed_max_within_window():
    # caderas bajan de 50 a 100 en 0.5 s con caja de 100 de alto -> 1.0 alturas/s
    history = [(0.0, 50.0, 100.0), (0.5, 100.0, 100.0), (1.0, 100.0, 100.0)]
    assert drop_speed(history, now=1.0) == pytest.approx(1.0)


def test_drop_speed_ignores_samples_outside_window_and_rising():
    history = [(0.0, 50.0, 100.0), (0.5, 100.0, 100.0), (3.0, 90.0, 100.0), (3.5, 80.0, 100.0)]
    assert drop_speed(history, now=3.5) == pytest.approx(0.0)  # solo sube en la ventana


def test_drop_speed_none_with_fewer_than_two_samples():
    assert drop_speed([], now=0.0) is None
    assert drop_speed([(0.0, 50.0, 100.0)], now=0.0) is None


def test_drop_speed_tolerates_degenerate_box_and_dt():
    history = [(0.0, 50.0, 0.0), (0.0, 90.0, 0.0)]
    assert drop_speed(history, now=0.0) is None


def test_is_horizontal_and_upright_from_angle():
    lying = Signals(90.0, 0.5, None, 0.0, None, None)
    standing = Signals(10.0, 3.0, None, 0.0, None, None)
    assert is_horizontal(lying) and not is_upright(lying)
    assert is_upright(standing) and not is_horizontal(standing)


def test_is_horizontal_and_upright_fall_back_to_bbox_ratio():
    wide = Signals(None, 1.5, None, 0.0, None, None)
    tall = Signals(None, 0.5, None, 0.0, None, None)
    assert is_horizontal(wide) and not is_upright(wide)
    assert is_upright(tall) and not is_horizontal(tall)


def test_score_full_fall_is_high():
    s = Signals(torso_angle=85.0, bbox_ratio=2.0, drop_speed=1.2, floor_time_s=6.0,
                head_low=True, keypoint_conf=0.8)
    assert score(s) == pytest.approx(1.0)


def test_score_slow_lie_down_is_low():
    s = Signals(torso_angle=85.0, bbox_ratio=2.0, drop_speed=0.1, floor_time_s=0.0,
                head_low=False, keypoint_conf=0.8)
    assert score(s) == pytest.approx(0.35)


def test_score_renormalizes_when_signals_missing():
    # Solo permanencia (0.25) y horizontalidad (0.35) disponibles, ambas al máximo -> 1.0
    s = Signals(torso_angle=85.0, bbox_ratio=2.0, drop_speed=None, floor_time_s=6.0,
                head_low=None, keypoint_conf=None)
    assert score(s) == pytest.approx(1.0)


def test_score_linear_mapping_midpoints():
    s = Signals(torso_angle=62.5, bbox_ratio=1.0, drop_speed=0.65, floor_time_s=2.5,
                head_low=False, keypoint_conf=0.5)
    # todos los términos a 0.5 salvo head_low=0: 0.35*0.5 + 0.30*0.5 + 0.25*0.5 + 0.10*0 = 0.45
    assert score(s) == pytest.approx(0.45)


def test_compute_signals_integrates_everything():
    history = [(0.0, 50.0, 100.0), (0.5, 100.0, 100.0)]
    s = compute_signals(LYING, BOX_LYING, history, now=0.5, floor_time_s=1.0)
    assert s.torso_angle == pytest.approx(90.0)
    assert s.bbox_ratio == pytest.approx(4.0)
    assert s.drop_speed == pytest.approx(1.0)
    assert s.floor_time_s == 1.0
    assert s.head_low is False
    assert s.keypoint_conf == pytest.approx(0.9)


def test_compute_signals_degenerate_box_does_not_raise():
    s = compute_signals(None, (10, 10, 10, 10), [], now=0.0, floor_time_s=0.0)
    assert s.bbox_ratio == 0.0
    assert math.isfinite(score(s))


def test_to_payload_rounds_and_keeps_nulls():
    s = Signals(torso_angle=74.123456, bbox_ratio=1.9, drop_speed=None, floor_time_s=2.5,
                head_low=True, keypoint_conf=0.6149)
    assert s.to_payload() == {
        "torso_angle": 74.123, "bbox_ratio": 1.9, "drop_speed": None,
        "floor_time_s": 2.5, "head_low": True, "keypoint_conf": 0.615,
    }
```

- [ ] **Step 2: Comprobar que fallan**

Run: `pytest tests/test_fall_signals.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implementar**

Crear `src/vitahub/analytics/fall_signals.py`:

```python
"""Señales geométricas de caída a partir de una pose y su historial reciente.

Funciones puras, sin estado: todo lo temporal (historial, tiempo en el
suelo) entra como argumento. El FallEngine se encarga del estado.

Pesos y umbrales son constantes a propósito (no config): se calibran una
vez con scripts/replay_video.py sobre vídeos reales. Si en campo hiciera
falta ajustarlos por hogar, se pasarán a config entonces.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from vitahub.models import Keypoints

# Índices COCO-17 que usa la analítica.
_NOSE, _L_SHOULDER, _R_SHOULDER, _L_HIP, _R_HIP = 0, 5, 6, 11, 12
_USED_POINTS = (_NOSE, _L_SHOULDER, _R_SHOULDER, _L_HIP, _R_HIP)

# Un keypoint por debajo de esta confianza se trata como ausente.
KEYPOINT_MIN_CONF = 0.3
# Torso a ≥ 60° de la vertical = tumbado; < 45° = de pie. El hueco entre
# ambos evita oscilar en posturas intermedias (sentado, agachado).
HORIZONTAL_ANGLE = 60.0
UPRIGHT_ANGLE = 45.0
# Sin keypoints: una caja más ancha que alta (×1.2) cuenta como tumbado.
HORIZONTAL_RATIO = 1.2
# Ventana sobre la que se busca la bajada más rápida de las caderas.
DROP_WINDOW_S = 1.5

# Score: pesos y mapeos lineales (valor en el que el término vale 0 → 1).
_W_HORIZONTAL, _ANGLE_0, _ANGLE_1 = 0.35, 45.0, 80.0
_W_DROP, _DROP_0, _DROP_1 = 0.30, 0.3, 1.0
_W_FLOOR, _FLOOR_0, _FLOOR_1 = 0.25, 0.0, 5.0
_W_HEAD = 0.10

# (t, ref_y, box_h): instante, altura de referencia (caderas o centro de
# caja) y alto de la caja en ese instante.
Sample = tuple[float, float, float]


def _point(kps: Keypoints | None, idx: int) -> tuple[float, float] | None:
    if kps is None or idx >= len(kps):
        return None
    x, y, conf = kps[idx]
    if conf < KEYPOINT_MIN_CONF:
        return None
    return (x, y)


def _mid(kps: Keypoints | None, a: int, b: int) -> tuple[float, float] | None:
    pa, pb = _point(kps, a), _point(kps, b)
    if pa is None or pb is None:
        return None
    return ((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0)


def torso_angle(kps: Keypoints | None) -> float | None:
    """Ángulo en grados del torso (hombros→caderas) respecto a la vertical."""
    shoulders = _mid(kps, _L_SHOULDER, _R_SHOULDER)
    hips = _mid(kps, _L_HIP, _R_HIP)
    if shoulders is None or hips is None:
        return None
    dx = abs(hips[0] - shoulders[0])
    dy = abs(hips[1] - shoulders[1])
    if dx == 0.0 and dy == 0.0:
        return None
    return math.degrees(math.atan2(dx, dy))


def head_low(kps: Keypoints | None, bbox: tuple[int, int, int, int]) -> bool | None:
    """Nariz por debajo de las caderas; sin caderas, en el tercio inferior de la caja."""
    nose = _point(kps, _NOSE)
    if nose is None:
        return None
    hips = _mid(kps, _L_HIP, _R_HIP)
    if hips is not None:
        return nose[1] > hips[1]
    _, y1, _, y2 = bbox
    return nose[1] > y1 + (y2 - y1) * 2.0 / 3.0


def keypoint_conf(kps: Keypoints | None) -> float | None:
    """Confianza media de los puntos que usa la analítica (nariz, hombros, caderas)."""
    if kps is None:
        return None
    confs = [kps[i][2] for i in _USED_POINTS if i < len(kps)]
    if not confs:
        return None
    return sum(confs) / len(confs)


def reference_y(kps: Keypoints | None, bbox: tuple[int, int, int, int]) -> float:
    """Altura que se sigue en el tiempo: medio de caderas, o centro de la caja."""
    hips = _mid(kps, _L_HIP, _R_HIP)
    if hips is not None:
        return hips[1]
    return (bbox[1] + bbox[3]) / 2.0


def drop_speed(history: list[Sample], now: float) -> float | None:
    """Máxima velocidad de bajada en la ventana, en alturas de caja por segundo.

    Positivo = baja (y crece). Subidas cuentan como 0. None si no hay dos
    muestras válidas en la ventana.
    """
    window = [s for s in history if s[0] >= now - DROP_WINDOW_S]
    best: float | None = None
    for (t0, y0, h0), (t1, y1, _) in zip(window, window[1:], strict=False):
        dt = t1 - t0
        if dt <= 0.0 or h0 <= 0.0:
            continue
        speed = max(0.0, (y1 - y0) / h0 / dt)
        best = speed if best is None else max(best, speed)
    return best


@dataclass(frozen=True)
class Signals:
    torso_angle: float | None
    bbox_ratio: float
    drop_speed: float | None
    floor_time_s: float
    head_low: bool | None
    keypoint_conf: float | None

    def to_payload(self) -> dict[str, object]:
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 3)

        return {
            "torso_angle": r(self.torso_angle),
            "bbox_ratio": round(self.bbox_ratio, 3),
            "drop_speed": r(self.drop_speed),
            "floor_time_s": round(self.floor_time_s, 3),
            "head_low": self.head_low,
            "keypoint_conf": r(self.keypoint_conf),
        }


def compute_signals(
    kps: Keypoints | None,
    bbox: tuple[int, int, int, int],
    history: list[Sample],
    now: float,
    floor_time_s: float,
) -> Signals:
    x1, y1, x2, y2 = bbox
    w, h = max(0, x2 - x1), max(0, y2 - y1)
    return Signals(
        torso_angle=torso_angle(kps),
        bbox_ratio=(w / h) if h > 0 else 0.0,
        drop_speed=drop_speed(history, now),
        floor_time_s=floor_time_s,
        head_low=head_low(kps, bbox),
        keypoint_conf=keypoint_conf(kps),
    )


def is_horizontal(s: Signals) -> bool:
    if s.torso_angle is not None:
        return s.torso_angle >= HORIZONTAL_ANGLE
    return s.bbox_ratio >= HORIZONTAL_RATIO


def is_upright(s: Signals) -> bool:
    if s.torso_angle is not None:
        return s.torso_angle < UPRIGHT_ANGLE
    return s.bbox_ratio < HORIZONTAL_RATIO


def _ramp(value: float, at_zero: float, at_one: float) -> float:
    if at_one == at_zero:
        return 1.0 if value >= at_one else 0.0
    return min(1.0, max(0.0, (value - at_zero) / (at_one - at_zero)))


def score(s: Signals) -> float:
    """Suma ponderada en [0, 1]; los términos sin señal no cuentan y el resto se renormaliza."""
    terms: list[tuple[float, float]] = []
    if s.torso_angle is not None:
        terms.append((_W_HORIZONTAL, _ramp(s.torso_angle, _ANGLE_0, _ANGLE_1)))
    if s.drop_speed is not None:
        terms.append((_W_DROP, _ramp(s.drop_speed, _DROP_0, _DROP_1)))
    terms.append((_W_FLOOR, _ramp(s.floor_time_s, _FLOOR_0, _FLOOR_1)))
    if s.head_low is not None:
        terms.append((_W_HEAD, 1.0 if s.head_low else 0.0))
    total_weight = sum(w for w, _ in terms)
    if total_weight <= 0.0:
        return 0.0
    return sum(w * v for w, v in terms) / total_weight
```

- [ ] **Step 4: Verificar**

Run: `pytest -q && ruff check src tests && mypy`
Expected: verde. Si `test_score_slow_lie_down_is_low` no da 0.35: horizontalidad 1.0·0.35 + brusquedad 0 + permanencia 0 + cabeza 0 = 0.35 sobre peso total 1.0 — comprobar que los cuatro términos están presentes (ninguno `None`).

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/analytics/fall_signals.py tests/test_fall_signals.py
git commit -m "feat: señales geométricas y score de caída (fall_signals)"
```

---

### Task 4: `FallEngine` — pistas y máquina de estados

**Files:**
- Create: `src/vitahub/analytics/fall_engine.py`
- Test: `tests/test_fall_engine.py`

**Interfaces:**
- Consumes: `fall_signals` (Task 3), `Detection`/`Keypoints` (Task 1), `Camera`, `Event`.
- Produces: `FallEngine(hub_id: str, min_score: float = 0.3, clock: Callable[[], datetime] = _utcnow)` con `observe(camera: Camera, detections: list[Detection], now: float) -> list[Event]`.
- Constantes: `IOU_MATCH = 0.3`, `TRACK_TTL_S = 3.0`, `HISTORY_S = 3.0`, `CONFIRM_FLOOR_S = 2.0`, `CONFIRM_UPRIGHT_S = 2.0`, `UPDATE_EVERY_S = 10.0`.
- `episode_id` = `f"{camera.id}-{int(clock().timestamp())}"` en el instante del `fall_detected`.

- [ ] **Step 1: Tests que fallan**

Crear `tests/test_fall_engine.py`:

```python
from datetime import UTC, datetime, timedelta

from vitahub.analytics.fall_engine import FallEngine
from vitahub.models import Camera, Detection

CAM = Camera(id="onvif-abc", name="salon", last_ip="10.0.0.5")
NOSE, L_SH, R_SH, L_HIP, R_HIP = 0, 5, 6, 11, 12


def _pose(nose, shoulders, hips, conf=0.9):
    kps = [(0.0, 0.0, 0.0)] * 17
    kps[NOSE] = (*nose, conf)
    kps[L_SH] = (shoulders[0] - 10, shoulders[1], conf)
    kps[R_SH] = (shoulders[0] + 10, shoulders[1], conf)
    kps[L_HIP] = (hips[0] - 10, hips[1], conf)
    kps[R_HIP] = (hips[0] + 10, hips[1], conf)
    return tuple(kps)


# Geometría elegida para que (a) la caja de pie y la tumbada solapen con
# IoU ≥ 0.3 (misma pista) y (b) la bajada de caderas 50 → 115 en 0.5 s sea
# brusca (> 1 altura de caja por segundo).
def standing(x=50):
    return Detection("person", 0.9, (x - 20, 0, x + 20, 120),
                     _pose(nose=(x, 10), shoulders=(x, 30), hips=(x, 50)))


def lying(x=50):
    return Detection("person", 0.9, (x - 50, 50, x + 50, 120),
                     _pose(nose=(x - 40, 115), shoulders=(x - 20, 115), hips=(x + 40, 115)))


class _Clock:
    def __init__(self):
        self.t = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


def _engine(min_score=0.3):
    clock = _Clock()
    return FallEngine(hub_id="hub-1", min_score=min_score, clock=clock), clock


def _feed(engine, clock, seq, step=0.5, start=0.0):
    """seq: lista de listas de detecciones, una por frame. Devuelve todos los eventos."""
    events = []
    now = start
    for dets in seq:
        events += engine.observe(CAM, dets, now)
        now += step
        clock.advance(step)
    return events


def test_sudden_fall_emits_detected_after_two_seconds_on_floor():
    eng, clock = _engine()
    # 1 s de pie, caída en 0.5 s, luego en el suelo
    seq = [[standing()]] * 2 + [[lying()]] * 6
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected"]
    ev = events[0]
    assert ev.severity == "high"
    assert ev.camera_id == "onvif-abc"
    assert ev.payload["score"] >= 0.7
    assert ev.payload["signals"]["floor_time_s"] >= 2.0
    assert ev.payload["signals"]["drop_speed"] is not None
    assert ev.payload["person_count"] == 1
    assert ev.payload["episode_id"].startswith("onvif-abc-")


def test_no_event_before_two_seconds_on_floor():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 4  # 1.5 s en el suelo (frames a 0, .5, 1, 1.5)
    assert _feed(eng, clock, seq) == []


def test_slow_lie_down_scores_low_and_respects_min_score():
    eng, clock = _engine(min_score=0.5)
    # Sin transición brusca: aparece ya tumbada y no se mueve (drop_speed = 0),
    # así que el score es 0.35 + 0.05·s de suelo. La permanencia sola acaba
    # superando 0.5 a los 3 s (por diseño: seguir en el suelo es cada vez más
    # sospechoso), así que esta rama se limita a 2.5 s de suelo (score 0.475).
    seq_short = [[lying()]] * 6
    assert _feed(eng, clock, seq_short) == []
    seq = [[lying()]] * 8
    eng2, clock2 = _engine(min_score=0.3)
    events = _feed(eng2, clock2, seq)
    assert [e.type for e in events] == ["fall_detected"]
    assert events[0].payload["score"] < 0.5


def test_crouch_and_stand_up_emits_nothing():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 3 + [[standing()]] * 8
    assert _feed(eng, clock, seq) == []


def test_updates_every_ten_seconds_while_on_floor():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 60  # 30 s en el suelo
    events = _feed(eng, clock, seq)
    types = [e.type for e in events]
    assert types[0] == "fall_detected"
    assert types.count("fall_update") == 2  # a +10 s y +20 s del detected
    assert all(e.payload["episode_id"] == events[0].payload["episode_id"] for e in events)
    assert all(e.severity == "high" for e in events)


def test_resolved_when_person_stands_up():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 6 + [[standing()]] * 5
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected", "fall_resolved"]
    res = events[1]
    assert res.severity == "info"
    assert res.payload["episode_id"] == events[0].payload["episode_id"]
    assert res.payload["max_score"] >= events[0].payload["score"]
    assert res.payload["duration_s"] > 0


def test_resolved_when_track_disappears():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 6 + [[]] * 7  # 3 s sin verla
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected", "fall_resolved"]


def test_missing_keypoints_uses_bbox_and_never_raises():
    # Sin pose solo puntúan brusquedad y permanencia: el score es bajo; el
    # umbral se baja para comprobar el camino de caja, no la calibración.
    eng, clock = _engine(min_score=0.1)
    tall = Detection("person", 0.9, (30, 0, 70, 120), None)
    wide = Detection("person", 0.9, (0, 50, 100, 120), None)
    seq = [[tall]] * 2 + [[wide]] * 6
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected"]
    assert events[0].payload["signals"]["torso_angle"] is None
    assert events[0].payload["signals"]["keypoint_conf"] is None


def test_degenerate_detections_do_not_raise():
    eng, clock = _engine()
    bad = Detection("person", 0.9, (10, 10, 10, 10), ())
    assert _feed(eng, clock, [[bad]] * 10) == []


def test_two_people_are_tracked_separately():
    eng, clock = _engine()
    # Persona A de pie a la izquierda; persona B cae a la derecha
    seq = [[standing(60), standing(300)]] * 2 + [[standing(60), lying(300)]] * 6
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected"]
    assert events[0].payload["person_count"] == 2


def test_per_camera_state_is_independent():
    eng, clock = _engine()
    other = Camera(id="onvif-xyz", name="cocina", last_ip="10.0.0.6")
    now = 0.0
    for _ in range(2):
        eng.observe(CAM, [standing()], now)
        eng.observe(other, [standing()], now)
        now += 0.5
    events = []
    for _ in range(6):
        events += eng.observe(CAM, [lying()], now)
        events += eng.observe(other, [standing()], now)
        now += 0.5
    assert [(e.type, e.camera_id) for e in events] == [("fall_detected", "onvif-abc")]
```

- [ ] **Step 2: Comprobar que fallan**

Run: `pytest tests/test_fall_engine.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implementar**

Crear `src/vitahub/analytics/fall_engine.py`:

```python
"""Analítica de caídas: pistas por persona y máquina de estados de episodio.

    upright ──tumbado──► candidate ──score ≥ min y ≥ 2 s en el suelo──► reported
       ▲                    │                                             │
       └──de pie 2 s────────┘         de pie 2 s, o pista perdida 3 s ────┘ → fall_resolved

Una pista es una persona seguida entre frames por solapamiento de cajas
(IoU). Todo el estado es en memoria; un reinicio del hub a mitad de episodio
pierde el fall_resolved (igual que el ConnectionMonitor, ver docs/backlog.md).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vitahub.analytics.fall_signals import (
    Sample,
    Signals,
    compute_signals,
    is_horizontal,
    is_upright,
    reference_y,
    score,
)
from vitahub.models import Camera, Detection, Event

# Solapamiento mínimo para decir "es la misma persona que en el frame anterior".
IOU_MATCH = 0.3
# Una pista sin observación durante este tiempo se da por perdida.
TRACK_TTL_S = 3.0
# Historial de alturas que se conserva por pista (cubre la ventana de drop_speed).
HISTORY_S = 3.0
# Tiempo mínimo en el suelo antes de emitir: evita disparar por un frame ruidoso.
CONFIRM_FLOOR_S = 2.0
# Tiempo de pie necesario para cerrar un episodio (o volver de candidate).
CONFIRM_UPRIGHT_S = 2.0
# Cadencia máxima de fall_update mientras sigue en el suelo.
UPDATE_EVERY_S = 10.0

_SEV_HIGH = "high"
_SEV_INFO = "info"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


@dataclass
class _Track:
    bbox: tuple[int, int, int, int]
    last_seen: float
    history: list[Sample] = field(default_factory=list)
    state: str = "upright"  # upright | candidate | reported
    floor_since: float | None = None
    upright_since: float | None = None
    # Pico de drop_speed desde que dejó de estar de pie. La ventana de
    # drop_speed (1.5 s) es más corta que la confirmación (2 s): sin
    # conservar el pico, la brusquedad de la caída nunca llegaría al score.
    peak_drop: float | None = None
    # Solo con state == "reported":
    episode_id: str = ""
    episode_start: float = 0.0
    last_event_at: float = 0.0
    max_score: float = 0.0


@dataclass
class _CameraState:
    tracks: list[_Track] = field(default_factory=list)


class FallEngine:
    def __init__(
        self,
        hub_id: str,
        min_score: float = 0.3,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._hub_id = hub_id
        self._min_score = min_score
        self._clock = clock
        self._cameras: dict[str, _CameraState] = {}

    def observe(self, camera: Camera, detections: list[Detection], now: float) -> list[Event]:
        cam = self._cameras.setdefault(camera.id, _CameraState())
        persons = [d for d in detections if d.label == "person"]
        events: list[Event] = []

        matched = self._match(cam.tracks, persons)
        for track, det in matched:
            events += self._update_track(camera, track, det, now, len(persons))

        # Pistas no vistas: caducan a los TRACK_TTL_S; si estaban en episodio, se resuelve.
        alive: list[_Track] = []
        for track in cam.tracks:
            if now - track.last_seen < TRACK_TTL_S:
                alive.append(track)
            elif track.state == "reported":
                events.append(self._resolved(camera, track, now))
        cam.tracks = alive
        return events

    # --- emparejamiento -------------------------------------------------

    def _match(
        self, tracks: list[_Track], persons: list[Detection]
    ) -> list[tuple[_Track, Detection]]:
        """Greedy por mayor IoU; las detecciones sin pista abren una nueva."""
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
        result: list[tuple[_Track, Detection]] = []
        for iou, ti, di in pairs:
            if iou < IOU_MATCH or ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            result.append((tracks[ti], persons[di]))
        for di, d in enumerate(persons):
            if di not in used_d:
                track = _Track(bbox=d.bbox, last_seen=-1.0)
                tracks.append(track)
                result.append((track, d))
        return result

    # --- estado por pista -----------------------------------------------

    def _update_track(
        self, camera: Camera, track: _Track, det: Detection, now: float, person_count: int
    ) -> list[Event]:
        track.bbox = det.bbox
        track.last_seen = now
        h = max(0, det.bbox[3] - det.bbox[1])
        track.history.append((now, reference_y(det.keypoints, det.bbox), float(h)))
        track.history = [s for s in track.history if s[0] >= now - HISTORY_S]

        floor_time = 0.0 if track.floor_since is None else now - track.floor_since
        signals = compute_signals(det.keypoints, det.bbox, track.history, now, floor_time)

        if is_horizontal(signals):
            if track.floor_since is None:
                track.floor_since = now
            track.upright_since = None
        else:
            track.floor_since = None
            if is_upright(signals):
                if track.upright_since is None:
                    track.upright_since = now
            else:
                track.upright_since = None
        if track.state == "upright" and track.floor_since is None:
            track.peak_drop = None
        elif signals.drop_speed is not None:
            track.peak_drop = max(track.peak_drop or 0.0, signals.drop_speed)

        # Recalcular con el floor_since recién fijado (primer frame tumbado = 0 s)
        # y con el pico de bajada en lugar del valor instantáneo.
        floor_time = 0.0 if track.floor_since is None else now - track.floor_since
        signals = Signals(
            signals.torso_angle, signals.bbox_ratio, track.peak_drop,
            floor_time, signals.head_low, signals.keypoint_conf,
        )
        upright_for = 0.0 if track.upright_since is None else now - track.upright_since

        if track.state == "upright":
            if track.floor_since is not None:
                track.state = "candidate"
            return []

        if track.state == "candidate":
            if upright_for >= CONFIRM_UPRIGHT_S:
                track.state = "upright"
                track.peak_drop = None
                return []
            current = score(signals)
            confirmed = track.floor_since is not None and floor_time >= CONFIRM_FLOOR_S
            if confirmed and current >= self._min_score:
                track.state = "reported"
                track.episode_id = f"{camera.id}-{int(self._clock().timestamp())}"
                track.episode_start = now
                track.last_event_at = now
                track.max_score = current
                return [self._fall_event(camera, track, "fall_detected", current, signals, person_count)]
            return []

        # reported
        if upright_for >= CONFIRM_UPRIGHT_S:
            ev = self._resolved(camera, track, now)
            track.state = "upright"
            track.peak_drop = None
            return [ev]
        if now - track.last_event_at >= UPDATE_EVERY_S:
            current = score(signals)
            track.last_event_at = now
            track.max_score = max(track.max_score, current)
            return [self._fall_event(camera, track, "fall_update", current, signals, person_count)]
        return []

    # --- eventos ----------------------------------------------------------

    def _fall_event(
        self,
        camera: Camera,
        track: _Track,
        event_type: str,
        current: float,
        signals: Signals,
        person_count: int,
    ) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type=event_type,
            severity=_SEV_HIGH,
            timestamp=self._clock().isoformat(),
            payload={
                "episode_id": track.episode_id,
                "score": round(current, 3),
                "signals": signals.to_payload(),
                "person_count": person_count,
            },
        )

    def _resolved(self, camera: Camera, track: _Track, now: float) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type="fall_resolved",
            severity=_SEV_INFO,
            timestamp=self._clock().isoformat(),
            payload={
                "episode_id": track.episode_id,
                "duration_s": round(now - track.episode_start, 3),
                "max_score": round(track.max_score, 3),
            },
        )
```

- [ ] **Step 4: Verificar**

Run: `pytest tests/test_fall_engine.py -q`
Expected: verde. Cronología de referencia para `test_sudden_fall...` (frames cada 0.5 s):
de pie en t = 0 y 0.5; tumbada desde t = 1.0 (`floor_since` = 1.0, estado `candidate`); el pico de
bajada se mide en el par 0.5 → 1.0: caderas 50 → 115 con caja de 120 de alto → 65/120/0.5 = 1.08
alturas/s → término de brusquedad 1.0; a t = 3.0 `floor_time` = 2.0 → `fall_detected` con score
0.35·1 + 0.30·1 + 0.25·0.4 + 0.10·0 = 0.75. En `test_slow_lie_down...` no hay bajada (pico 0.0) y
el score a los 2 s es 0.45: por debajo de 0.5 y por encima de 0.3.

- [ ] **Step 5: Verificación completa**

Run: `pytest -q && ruff check src tests && mypy`
Expected: verde. Si ruff se queja de línea > 100 en el `if` largo de `candidate`, partirlo en dos variables.

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/analytics/fall_engine.py tests/test_fall_engine.py
git commit -m "feat: FallEngine con pistas IoU y episodios fall_detected/update/resolved"
```

---

### Task 5: Config `inference.fall`

**Files:**
- Modify: `src/vitahub/config.py:33-39` (dataclasses) y `:190-240` (parseo)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `FallConfig(enabled: bool = False, min_score: float = 0.3)`; `InferenceConfig.fall: FallConfig` (default `FallConfig()`).
- `load_config` lanza `ConfigError` si: `inference.fall` no es mapping; `min_score` no numérico o fuera de `[0, 1]`; `enabled` y `detector != "person_pose"`.

- [ ] **Step 1: Tests que fallan**

Añadir a `tests/test_config.py`:

```python
def test_fall_defaults_disabled(tmp_path):
    cfg = load_config(_write(tmp_path, "hub_id: hub-x\n"), ENV)
    assert cfg.inference.fall.enabled is False
    assert cfg.inference.fall.min_score == 0.3


def test_fall_enabled_requires_pose_detector(tmp_path):
    path = _write(tmp_path, "hub_id: hub-x\ninference:\n  detector: person_yolo\n  fall:\n    enabled: true\n")
    with pytest.raises(ConfigError, match="person_pose"):
        load_config(path, ENV)


def test_fall_enabled_with_pose_detector(tmp_path):
    path = _write(
        tmp_path,
        "hub_id: hub-x\ninference:\n  detector: person_pose\n  fall:\n    enabled: true\n    min_score: 0.5\n",
    )
    cfg = load_config(path, ENV)
    assert cfg.inference.detector == "person_pose"
    assert cfg.inference.fall.enabled is True
    assert cfg.inference.fall.min_score == 0.5


def test_fall_min_score_not_numeric_raises(tmp_path):
    path = _write(tmp_path, "hub_id: hub-x\ninference:\n  fall:\n    min_score: alto\n")
    with pytest.raises(ConfigError, match="min_score"):
        load_config(path, ENV)


def test_fall_min_score_out_of_range_raises(tmp_path):
    path = _write(tmp_path, "hub_id: hub-x\ninference:\n  fall:\n    min_score: 1.5\n")
    with pytest.raises(ConfigError, match="min_score"):
        load_config(path, ENV)


def test_fall_section_must_be_mapping(tmp_path):
    path = _write(tmp_path, "hub_id: hub-x\ninference:\n  fall: si\n")
    with pytest.raises(ConfigError, match="inference.fall"):
        load_config(path, ENV)
```

- [ ] **Step 2: Comprobar que fallan**

Run: `pytest tests/test_config.py -q`
Expected: FAIL (`AttributeError: 'InferenceConfig' object has no attribute 'fall'`).

- [ ] **Step 3: Implementar**

En `src/vitahub/config.py`, sustituir el dataclass `InferenceConfig` por:

```python
@dataclass
class FallConfig:
    enabled: bool = False
    # Score mínimo para emitir fall_detected. Bajo a propósito: quien filtra
    # es el consumidor en AWS; el hub emite candidatos con su fiabilidad.
    min_score: float = 0.3


@dataclass
class InferenceConfig:
    detector: str = "person_yolo"
    sample_fps: float = 2.0
    confidence: float = 0.4
    stream: str = "substream"
    fall: FallConfig = field(default_factory=FallConfig)
```

Añadir una función de parseo (junto a `_uplink_from_raw`):

```python
def _fall_from_raw(raw: object, detector: str) -> FallConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("Sección 'inference.fall' debe ser un mapping YAML")
    enabled = bool(raw.get("enabled", False))
    try:
        min_score = float(raw.get("min_score", 0.3))
    except (ValueError, TypeError) as exc:
        raise ConfigError("Campo 'inference.fall.min_score' no es numérico") from exc
    if not 0.0 <= min_score <= 1.0:
        raise ConfigError("Campo 'inference.fall.min_score' debe estar entre 0 y 1")
    if enabled and detector != "person_pose":
        # Sin keypoints la analítica solo vería cajas: funcionaría a medias y
        # en silencio. Mejor negarse a arrancar con un mensaje que diga qué cambiar.
        raise ConfigError(
            "inference.fall.enabled requiere 'inference.detector: person_pose' "
            f"(recibido: {detector!r})"
        )
    return FallConfig(enabled=enabled, min_score=min_score)
```

En `load_config`, construir `InferenceConfig` así:

```python
    detector = str(inf_raw.get("detector", "person_yolo"))
    return HubConfig(
        hub_id=hub_id,
        discovery=DiscoveryConfig(interval_seconds=interval_seconds),
        inference=InferenceConfig(
            detector=detector,
            sample_fps=sample_fps,
            confidence=confidence,
            stream=stream,
            fall=_fall_from_raw(inf_raw.get("fall"), detector),
        ),
        cameras=_cameras_from_raw(raw.get("cameras") or []),
        credentials=credentials,
        uplink=_uplink_from_raw(up_raw, env),
    )
```

- [ ] **Step 4: Verificar**

Run: `pytest -q && ruff check src tests && mypy`
Expected: verde.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/config.py tests/test_config.py
git commit -m "feat: config inference.fall (apagada por defecto, exige person_pose)"
```

---

### Task 6: Factory — `person_pose` y comprobación de pesos

**Files:**
- Modify: `src/vitahub/factory.py:18-24`
- Test: `tests/test_factory.py`

**Interfaces:**
- Consumes: `PosePersonDetector` (Task 2), `InferenceConfig` (Task 5).
- Produces: `build_detector(cfg: InferenceConfig, weights_path: str) -> Detector` acepta `detector == "person_pose"`; para `person_yolo` y `person_pose` lanza `ConfigError` si `weights_path` no existe.

- [ ] **Step 1: Tests que fallan**

Añadir a `tests/test_factory.py`:

```python
from vitahub.inference.person_pose_yolo import PosePersonDetector
from vitahub.inference.person_yolo import PersonDetector


def test_missing_weights_raises_config_error_for_person_yolo(tmp_path):
    missing = tmp_path / "nope.pt"
    with pytest.raises(ConfigError, match=str(missing)):
        build_detector(InferenceConfig(detector="person_yolo"), weights_path=str(missing))


def test_missing_weights_raises_config_error_for_person_pose(tmp_path):
    missing = tmp_path / "nope-pose.pt"
    with pytest.raises(ConfigError, match=str(missing)):
        build_detector(InferenceConfig(detector="person_pose"), weights_path=str(missing))


def test_build_person_pose_uses_from_weights(tmp_path, monkeypatch):
    weights = tmp_path / "yolo11n-pose.pt"
    weights.write_bytes(b"fake")
    calls = {}

    def _fake_from_weights(path, confidence):
        calls["path"], calls["confidence"] = path, confidence
        return PosePersonDetector(model=lambda f, verbose=False: [], confidence=confidence)

    monkeypatch.setattr(PosePersonDetector, "from_weights", staticmethod(_fake_from_weights))
    det = build_detector(
        InferenceConfig(detector="person_pose", confidence=0.6), weights_path=str(weights)
    )
    assert isinstance(det, PosePersonDetector)
    assert calls == {"path": str(weights), "confidence": 0.6}


def test_build_person_yolo_uses_from_weights(tmp_path, monkeypatch):
    weights = tmp_path / "yolo11n.pt"
    weights.write_bytes(b"fake")
    monkeypatch.setattr(
        PersonDetector,
        "from_weights",
        staticmethod(lambda path, confidence: PersonDetector(model=lambda f, verbose=False: [])),
    )
    det = build_detector(InferenceConfig(detector="person_yolo"), weights_path=str(weights))
    assert isinstance(det, PersonDetector)
```

- [ ] **Step 2: Comprobar que fallan**

Run: `pytest tests/test_factory.py -q`
Expected: FAIL (`ValueError: Detector desconocido: person_pose`; los de pesos fallan porque no lanza `ConfigError`).

- [ ] **Step 3: Implementar**

En `src/vitahub/factory.py`, añadir el import `from vitahub.inference.person_pose_yolo import PosePersonDetector` y sustituir `build_detector`:

```python
def _require_weights(weights_path: str) -> None:
    # Si el fichero no existe, Ultralytics intenta descargarlo al directorio
    # padre de esa ruta — que puede ser de solo lectura (/app/models en el
    # contenedor) — y revienta con un traceback confuso. Mejor decir qué falta.
    if not Path(weights_path).is_file():
        raise ConfigError(
            f"pesos del modelo no encontrados en {weights_path} — en el contenedor los "
            "embebe el Dockerfile; en local, descárgalos con "
            f"'python scripts/download_model.py {weights_path}' o usa detector: stub"
        )


def build_detector(cfg: InferenceConfig, weights_path: str) -> Detector:
    if cfg.detector == "stub":
        return StubDetector()
    if cfg.detector == "person_yolo":
        _require_weights(weights_path)
        return PersonDetector.from_weights(weights_path, confidence=cfg.confidence)
    if cfg.detector == "person_pose":
        _require_weights(weights_path)
        return PosePersonDetector.from_weights(weights_path, confidence=cfg.confidence)
    raise ValueError(f"Detector desconocido: {cfg.detector}")
```

- [ ] **Step 4: Verificar**

Run: `pytest -q && ruff check src tests && mypy`
Expected: verde.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/factory.py tests/test_factory.py
git commit -m "feat: detector person_pose en factory y error claro si faltan los pesos"
```

---

### Task 7: `process_frame` con `FallEngine`

**Files:**
- Modify: `src/vitahub/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `FallEngine.observe` (Task 4), `StubDetector(keypoints=..., bbox=...)` (Task 1).
- Produces: `process_frame(camera, frame, detector, engine, sink, now, fall_engine: FallEngine | None = None) -> list[Event]`. Los eventos de caída se emiten por el sink tras los de presencia y se incluyen en el retorno. Una excepción en `fall_engine.observe` se loguea (`_log.exception`, una vez por cámara) y no afecta a los eventos de presencia.

- [ ] **Step 1: Tests que fallan**

Añadir a `tests/test_worker.py`:

```python
from vitahub.analytics.fall_engine import FallEngine

NOSE, L_SH, R_SH, L_HIP, R_HIP = 0, 5, 6, 11, 12


def _pose(nose, shoulders, hips, conf=0.9):
    kps = [(0.0, 0.0, 0.0)] * 17
    kps[NOSE] = (*nose, conf)
    kps[L_SH] = (shoulders[0] - 10, shoulders[1], conf)
    kps[R_SH] = (shoulders[0] + 10, shoulders[1], conf)
    kps[L_HIP] = (hips[0] - 10, hips[1], conf)
    kps[R_HIP] = (hips[0] + 10, hips[1], conf)
    return tuple(kps)


LYING_KPS = _pose(nose=(10, 110), shoulders=(30, 110), hips=(90, 110))
LYING_BOX = (0, 90, 120, 120)


def test_process_frame_without_fall_engine_is_unchanged():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1, keypoints=LYING_KPS, bbox=LYING_BOX)
    process_frame(CAM, None, detector, engine, sink, now=0.0, fall_engine=None)
    emitted = process_frame(CAM, None, detector, engine, sink, now=2.0, fall_engine=None)
    assert [e.type for e in emitted] == ["person_detected"]


def test_process_frame_emits_presence_and_fall_through_same_sink():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    fall = FallEngine(hub_id="hub-1", min_score=0.3)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1, keypoints=LYING_KPS, bbox=LYING_BOX)
    all_events = []
    now = 0.0
    for _ in range(8):
        all_events += process_frame(CAM, None, detector, engine, sink, now, fall_engine=fall)
        now += 0.5
    types = [e.type for e in all_events]
    assert "person_detected" in types
    assert "fall_detected" in types
    assert sink.events == all_events


def test_fall_engine_exception_does_not_break_presence(caplog):
    class _Boom:
        def observe(self, camera, detections, now):
            raise RuntimeError("boom")

    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1)
    process_frame(CAM, None, detector, engine, sink, now=0.0, fall_engine=_Boom())
    emitted = process_frame(CAM, None, detector, engine, sink, now=2.0, fall_engine=_Boom())
    assert [e.type for e in emitted] == ["person_detected"]
    assert sink.events == emitted
    assert sum("analítica de caídas" in r.message for r in caplog.records) == 1
```

- [ ] **Step 2: Comprobar que fallan**

Run: `pytest tests/test_worker.py -q`
Expected: FAIL (`TypeError: unexpected keyword argument 'fall_engine'`).

- [ ] **Step 3: Implementar**

Sustituir `src/vitahub/worker.py`:

```python
from __future__ import annotations

from vitahub.analytics.event_engine import EventEngine
from vitahub.analytics.fall_engine import FallEngine
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
) -> list[Event]:
    detections = detector.detect(frame)
    person_count = sum(1 for d in detections if d.label == "person")
    confidence = max((d.confidence for d in detections), default=0.0)
    events = engine.observe(camera, person_count, confidence, now)
    if fall_engine is not None:
        try:
            events = events + fall_engine.observe(camera, detections, now)
        except Exception:  # noqa: BLE001 — la caída no debe tumbar la presencia
            if camera.id not in _fall_failure_logged:
                _fall_failure_logged.add(camera.id)
                _log.exception("cam %s: error en la analítica de caídas (se silencia a partir de ahora)", camera.id)
    for event in events:
        sink.emit(event)
    return events
```

(Partir la línea del `_log.exception` si supera 100 columnas.)

- [ ] **Step 4: Verificar**

Run: `pytest -q && ruff check src tests && mypy`
Expected: verde. Si `caplog` no captura el logger de `vitahub`, revisar `logging_setup.get_logger` (propagación) y, si hace falta, `caplog.set_level("ERROR", logger="vitahub")` en el test.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/worker.py tests/test_worker.py
git commit -m "feat: process_frame emite eventos de caída junto a la presencia"
```

---

### Task 8: Cableado en `app.py`, pesos de pose y `download_model.py`

**Files:**
- Modify: `src/vitahub/app.py:41-70` (`run`), `:164-200` (`_camera_loop`), `:225-236` (`main`)
- Modify: `scripts/download_model.py`
- Modify: `Dockerfile`
- Test: `tests/test_app_loop.py` (solo si ya prueba `_camera_loop`; si no, sin test nuevo — es código de integración, ver backlog)

**Interfaces:**
- Consumes: `FallEngine` (Task 4), `build_detector` (Task 6), `cfg.inference.fall` (Task 5).
- Produces: `run(config_path, weights_path, env)` mantiene la firma; elige internamente `env["VITAHUB_POSE_WEIGHTS"]` (default `/app/models/yolo11n-pose.pt`) cuando `detector == "person_pose"`. `_camera_loop` recibe `fall_engine` como argumento adicional.

- [ ] **Step 1: `run` y `_camera_loop`**

En `src/vitahub/app.py`, añadir `from vitahub.analytics.fall_engine import FallEngine` y, en `run`, sustituir el bloque que construye detector/engine/sink:

```python
    if cfg.inference.detector == "person_pose":
        weights_path = env.get("VITAHUB_POSE_WEIGHTS", _DEFAULT_POSE_WEIGHTS)
    detector = build_detector(cfg.inference, weights_path)
    engine = EventEngine(cfg.hub_id)
    fall_engine = (
        FallEngine(cfg.hub_id, min_score=cfg.inference.fall.min_score)
        if cfg.inference.fall.enabled
        else None
    )
    if fall_engine is not None:
        _log.info("analítica de caídas activada (min_score=%.2f)", cfg.inference.fall.min_score)
    monitor = ConnectionMonitor(cfg.hub_id)
    sink = build_sink(cfg)

    def worker(camera: Camera, rtsp_url: str, cam_stop: threading.Event) -> None:
        _camera_loop(  # type: ignore[no-untyped-call]
            camera, rtsp_url, detector, engine, sink, cfg, cam_stop, monitor, fall_engine
        )
```

Añadir la constante junto a `_HEARTBEAT_FILE`:

```python
_DEFAULT_POSE_WEIGHTS = "/app/models/yolo11n-pose.pt"
```

Cambiar la firma de `_camera_loop` a `_camera_loop(camera, rtsp_url, detector, engine, sink, cfg, stop, monitor, fall_engine=None)` y la llamada interna a:

```python
                        process_frame(
                            camera, frame, detector, engine, sink, now, fall_engine=fall_engine
                        )
```

- [ ] **Step 2: `download_model.py` descarga el modelo que diga el destino**

Sustituir el cuerpo de `main` en `scripts/download_model.py`:

```python
def main() -> int:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("yolo11n.pt")
    # El nombre del fichero destino es el nombre del modelo en ultralytics
    # (yolo11n.pt, yolo11n-pose.pt...): un solo script para todos los pesos.
    model = YOLO(dest.name)  # descarga a la cache de ultralytics
    src = Path(getattr(model, "ckpt_path", "") or dest.name)
    if not src.exists():
        print(f"error: no se encontró el peso descargado ({src})", file=sys.stderr)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy(src, dest)
    print(f"modelo en {dest}")
    return 0
```

- [ ] **Step 3: `Dockerfile`**

Sustituir la línea `RUN python scripts/download_model.py /app/models/yolo11n.pt` y el bloque `ENV` por:

```dockerfile
RUN python scripts/download_model.py /app/models/yolo11n.pt \
 && python scripts/download_model.py /app/models/yolo11n-pose.pt

ENV VITAHUB_CONFIG=/data/hub.yaml \
    VITAHUB_WEIGHTS=/app/models/yolo11n.pt \
    VITAHUB_POSE_WEIGHTS=/app/models/yolo11n-pose.pt \
    PYTHONUNBUFFERED=1
```

- [ ] **Step 4: Verificar**

Run: `pytest -q && ruff check src tests scripts && mypy`
Expected: verde. Además, arranque local con stub para comprobar que el cableado no rompe nada:

```bash
cat > /tmp/hub-stub.yaml <<'EOF'
hub_id: hub-dev
inference:
  detector: stub
EOF
VITAHUB_ONVIF_USER=a VITAHUB_ONVIF_PASSWORD=b VITAHUB_CONFIG=/tmp/hub-stub.yaml timeout 5 python -m vitahub.app; echo "exit $?"
```
Expected: arranca, "0 cámaras encontradas", sale por timeout (exit 124) sin traceback. Y con `fall.enabled: true` y `detector: stub` debe fallar con `config inválida: inference.fall.enabled requiere 'inference.detector: person_pose'`.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/app.py scripts/download_model.py Dockerfile
git commit -m "feat: cablear FallEngine en app y embeber yolo11n-pose en la imagen"
```

---

### Task 9: Script de calibración `scripts/replay_video.py`

**Files:**
- Create: `scripts/replay_video.py`
- Test: `tests/test_replay_video.py` (la lógica de muestreo; el bucle de OpenCV no se prueba)

**Interfaces:**
- Consumes: `build_detector`, `FallEngine`, `EventEngine`, `StdoutJsonSink`, `process_frame`, `should_sample` de `vitahub.ingest.rtsp`.
- Produces: CLI `python scripts/replay_video.py <video.mp4> [--weights PATH] [--fps 2] [--min-score 0.3] [--presence]`. Imprime por stdout los eventos JSON (caídas siempre; presencia solo con `--presence`) y por stderr un resumen final: frames procesados, eventos por tipo, score máximo.

- [ ] **Step 1: Test de la función de resumen**

Crear `tests/test_replay_video.py`:

```python
import importlib.util
import sys
from pathlib import Path

from vitahub.models import Event

_spec = importlib.util.spec_from_file_location(
    "replay_video", Path(__file__).resolve().parents[1] / "scripts" / "replay_video.py"
)
replay_video = importlib.util.module_from_spec(_spec)
sys.modules["replay_video"] = replay_video
_spec.loader.exec_module(replay_video)


def _ev(type_, score=None):
    payload = {} if score is None else {"score": score}
    return Event("h", "c", "n", type_, "high", "t", payload)


def test_summary_counts_types_and_max_score():
    events = [_ev("fall_detected", 0.4), _ev("fall_update", 0.8), _ev("fall_resolved"),
              _ev("person_detected")]
    summary = replay_video.summarize(events, frames=120)
    assert summary == {
        "frames": 120,
        "events": {"fall_detected": 1, "fall_update": 1, "fall_resolved": 1, "person_detected": 1},
        "max_score": 0.8,
    }


def test_summary_without_scores():
    assert replay_video.summarize([], frames=0) == {"frames": 0, "events": {}, "max_score": None}
```

- [ ] **Step 2: Comprobar que falla**

Run: `pytest tests/test_replay_video.py -q`
Expected: FAIL (fichero no existe).

- [ ] **Step 3: Implementar**

Crear `scripts/replay_video.py`:

```python
"""Pasa un vídeo grabado por el pipeline de caídas e imprime los eventos.

Para calibrar pesos y umbrales con caídas simuladas antes de ir a un hogar:

    python scripts/replay_video.py caida-lateral.mp4 --weights yolo11n-pose.pt

Eventos por stdout (JSON-lines, igual que el hub); resumen por stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

import cv2

from vitahub.analytics.event_engine import EventEngine
from vitahub.analytics.fall_engine import FallEngine
from vitahub.config import InferenceConfig
from vitahub.factory import build_detector
from vitahub.ingest.rtsp import should_sample
from vitahub.models import Camera, Event
from vitahub.sinks.base import EventSink
from vitahub.sinks.stdout_json import StdoutJsonSink
from vitahub.worker import process_frame


class _FilteredSink(EventSink):
    """Deja pasar solo los eventos de caída salvo que se pidan también los de presencia."""

    def __init__(self, inner: EventSink, presence: bool) -> None:
        self._inner = inner
        self._presence = presence
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)
        if self._presence or event.type.startswith("fall_"):
            self._inner.emit(event)

    def close(self) -> None:
        self._inner.close()


def summarize(events: list[Event], frames: int) -> dict[str, object]:
    scores = [float(e.payload["score"]) for e in events if "score" in e.payload]  # type: ignore[arg-type]
    return {
        "frames": frames,
        "events": dict(Counter(e.type for e in events)),
        "max_score": max(scores) if scores else None,
    }


def replay(path: str, weights: str, fps: float, min_score: float, presence: bool) -> dict[str, object]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"no se puede abrir {path}")
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    detector = build_detector(InferenceConfig(detector="person_pose"), weights)
    engine = EventEngine("replay")
    fall = FallEngine("replay", min_score=min_score)
    sink = _FilteredSink(StdoutJsonSink(), presence)
    camera = Camera(id="video", name=path, last_ip="")
    frames = 0
    last_sample = -1e9
    index = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            now = index / video_fps  # reloj del vídeo, no el de pared
            index += 1
            if should_sample(last_sample, now, fps):
                last_sample = now
                frames += 1
                process_frame(camera, frame, detector, engine, sink, now, fall_engine=fall)
    finally:
        cap.release()
        sink.close()
    return summarize(sink.events, frames)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--weights", default="yolo11n-pose.pt")
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--min-score", type=float, default=0.3)
    parser.add_argument("--presence", action="store_true", help="imprime también person_*")
    args = parser.parse_args()
    summary = replay(args.video, args.weights, args.fps, args.min_score, args.presence)
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Comprobar la firma real de `should_sample` en `src/vitahub/ingest/rtsp.py` (se usa en `app.py` como `should_sample(last_sample, now, cfg.inference.sample_fps)`) y que `StdoutJsonSink.close()` existe; ajustar si difiere.

- [ ] **Step 4: Verificar**

Run: `pytest -q && ruff check src tests scripts && mypy`
Expected: verde. (mypy solo analiza el paquete `vitahub`; el script queda fuera por configuración, igual que `healthcheck.py`.)

- [ ] **Step 5: Commit**

```bash
git add scripts/replay_video.py tests/test_replay_video.py
git commit -m "feat: scripts/replay_video.py para calibrar caídas sobre un vídeo"
```

---

### Task 10: Documentación y backlog

**Files:**
- Modify: `config/hub.example.yaml`, `README.md`, `docs/como-funciona.md`, `docs/como-probar.md`, `docs/backlog.md`

- [ ] **Step 1: `config/hub.example.yaml`**

Dentro de `inference:`, tras `stream:`, añadir:

```yaml
  fall:
    enabled: false          # true para emitir fall_detected/update/resolved (requiere detector: person_pose)
    min_score: 0.3          # score mínimo para emitir; bajo a propósito, quien filtra es AWS
```

Y cambiar el comentario de `detector:` a `# "person_yolo", "person_pose" (cajas + pose, necesario para caídas) o "stub"`.

- [ ] **Step 2: `docs/como-funciona.md`**

- En «Alcance de esta versión» añadir: «y **detección de caídas por pose** (opcional, `inference.fall`)».
- En la tabla de módulos añadir `inference/person_pose_yolo.py` (cajas + keypoints con `yolo11n-pose`), `analytics/fall_signals.py` (señales y score, puro) y `analytics/fall_engine.py` (pistas y episodios).
- Nueva sección «Caídas» con: el diagrama de estados de la docstring de `fall_engine.py`, la tabla de señales y pesos (copiar de §4.2–4.3 del spec) y un ejemplo del JSON de `fall_detected` (§5 del spec).
- En la frase «mañana entra la detección de caídas … sin tocar el resto» cambiar a «la detección de caídas entró por esa costura (`PosePersonDetector`) sin tocar la presencia».

- [ ] **Step 3: `docs/como-probar.md`**

- Junto a la nota de `VITAHUB_WEIGHTS` en local: «para `person_pose`, `VITAHUB_POSE_WEIGHTS=yolo11n-pose.pt` y descárgalo antes con `python scripts/download_model.py yolo11n-pose.pt` (desde este slice, si el fichero no existe el hub no arranca y lo dice)».
- Nueva subsección «Calibrar caídas con un vídeo»: grabar 3-4 clips (caída frontal, caída lateral, tumbarse en el sofá, agacharse), correr `python scripts/replay_video.py <clip> --weights yolo11n-pose.pt` y comprobar que el orden de scores es caída > tumbarse > agacharse (que no debe emitir).
- En la tabla de «Qué cubren y qué no»: añadir `test_fall_signals.py`, `test_fall_engine.py`, `test_pose_detector.py`, `test_replay_video.py`; fuera: rendimiento real de `yolo11n-pose` en Orin (medir en campo y anotar aquí).
- En la tabla de errores: `pesos del modelo no encontrados en X` → descargar con `scripts/download_model.py X` o usar `detector: stub`.

- [ ] **Step 4: `README.md`**

Tras la sección «Uplink a AWS (opcional)», añadir «Caídas (opcional)»: tres líneas — poner `detector: person_pose` y `fall.enabled: true` en `hub.yaml`; salen `fall_detected`/`fall_update`/`fall_resolved` con `score` y `signals` por stdout y AWS; el hub no avisa a nadie, lo hará un sistema en AWS.

- [ ] **Step 5: `docs/backlog.md`**

- Marcar como HECHO el punto «`person_yolo.from_weights`: si el fichero de pesos no existe…» de «Robustez / calidad» (resuelto en factory).
- Añadir sección «Diferidos del slice de caídas (2026-08-22)» con los cinco puntos de §8 del spec (zonas de exclusión, tracker real, clasificador entrenado, pesos a config, persistencia de episodios) y: «medir coste de `yolo11n-pose` en Orin con 4 cámaras; ajustar `--start-period` si la carga del modelo de pose lo requiere».
- En la nota del `ConnectionMonitor` en memoria, añadir «lo mismo aplica a los episodios del `FallEngine`».

- [ ] **Step 6: Verificar y commit**

Run: `pytest -q && ruff check src tests scripts && mypy`
Expected: verde.

```bash
git add config/hub.example.yaml README.md docs/como-funciona.md docs/como-probar.md docs/backlog.md
git commit -m "docs: detección de caídas — config, cómo funciona, cómo probar y backlog"
```

---

## Verificación final del slice

- [ ] `pytest -q && ruff check src tests scripts && mypy` en verde.
- [ ] `git diff 745ff7c -- src/vitahub/analytics/event_engine.py src/vitahub/inference/person_yolo.py` vacío (presencia intacta).
- [ ] Arranque local con `detector: stub` sin `fall` → comportamiento idéntico al anterior (mismos logs).
- [ ] `docker compose build` termina (descarga ambos pesos). Si no hay red en la máquina de build, anotarlo y verificar en el Jetson.
- [ ] Con pesos de pose descargados, `python scripts/replay_video.py <clip>` sobre un vídeo cualquiera con una persona imprime al menos el resumen por stderr sin traceback.
- [ ] Pendiente en hardware real (no bloqueante): coste de CPU/GPU de `yolo11n-pose` con 4 cámaras en Orin; orden de scores sobre los clips de calibración.
