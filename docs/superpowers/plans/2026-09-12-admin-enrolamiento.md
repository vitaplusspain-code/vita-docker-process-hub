# Administración local (enrolamiento + config) — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Página de administración local servida por el hub (puerto 8787) con la que el técnico enrola personas con fotos validadas al vuelo, ajusta `fall.enabled` / `identity.enabled` / `match_threshold`, y aplica los cambios con un reinicio limpio.

**Architecture:** Dos módulos de lógica pura (`admin/enrollment.py`, `admin/config_edit.py`) sin HTTP; `control.py` crece con las rutas `/api/*` (mismo token Bearer, mismo `compare_digest`) y sirve una página estática en `GET /`. `POST /api/apply` valida y dispara el mismo apagado limpio que SIGTERM; `restart: unless-stopped` relevanta el hub.

**Tech Stack:** Python 3.12 stdlib (`http.server` ya en uso), OpenCV para decodificar fotos, el `FaceEngine` existente para validar caras, YAML con el patrón atómico tmp+fsync+replace de `config.py`. Página en HTML+JS vanilla, sin build.

**Spec:** `docs/superpowers/specs/2026-09-12-admin-enrolamiento-design.md`

## Global Constraints

- Tests con `.venv/bin/python -m pytest` (el repo usa `pythonpath = ["src"]`; no hay que instalar).
- mypy strict y ruff (línea 100) están activos: anota tipos en todo lo nuevo.
- Comentarios y mensajes en español, estilo del repo: el comentario explica la restricción, no la línea.
- `person_id` válido: regex `^[a-z0-9-]{1,32}$`. Nombre de foto válido: regex `^\d{3}\.jpg$`.
- Límite de subida de foto: 10 MB (constante `_MAX_PHOTO_BYTES = 10 * 1024 * 1024`).
- Los errores HTTP con cuerpo devuelven `{"error": "<mensaje legible>"}`.
- Ninguna ruta nueva abre puerto sin `VITAHUB_ADMIN_TOKEN` (comportamiento actual intacto).
- `GET /` es público (página estática sin datos); todo `/api/*` exige el token.
- Commits: mensaje en español estilo repo, terminando con `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `admin/enrollment.py` — lógica pura de personas y fotos

**Files:**
- Create: `src/vitahub/admin/__init__.py` (vacío)
- Create: `src/vitahub/admin/enrollment.py`
- Test: `tests/test_enrollment.py`

**Interfaces:**
- Consumes: `FaceEngine` / `FaceObservation` de `vitahub.identity.base`; `StubFaceEngine` de `vitahub.identity.stub` (tests).
- Produces (Task 3 y 4 dependen de estas firmas exactas):
  - `@dataclass(frozen=True) PersonSummary: id: str; photos: list[str]`
  - `class EnrollmentError(Exception)` con atributos `message: str` y `status: int` (default 400)
  - `list_people(faces_dir: Path) -> list[PersonSummary]`
  - `photo_bytes(faces_dir: Path, person_id: str, photo: str) -> bytes`
  - `save_photo(faces_dir: Path, person_id: str, data: bytes, engine: FaceEngine) -> str` (devuelve el nombre de fichero guardado)
  - `delete_photo(faces_dir: Path, person_id: str, photo: str) -> None`
  - `delete_person(faces_dir: Path, person_id: str) -> None`

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_enrollment.py`:

```python
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from vitahub.admin.enrollment import (
    EnrollmentError,
    PersonSummary,
    delete_person,
    delete_photo,
    list_people,
    photo_bytes,
    save_photo,
)
from vitahub.identity.base import FaceObservation
from vitahub.identity.stub import StubFaceEngine


def _jpeg_bytes() -> bytes:
    image = np.full((32, 32, 3), 128, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", image)
    assert ok
    return bytes(buf)


def _face() -> FaceObservation:
    emb = np.zeros(512, dtype=np.float32)
    emb[0] = 1.0
    return FaceObservation(bbox=(0, 0, 10, 10), embedding=emb)


def test_list_people_empty_dir(tmp_path: Path) -> None:
    assert list_people(tmp_path) == []


def test_save_photo_creates_person_and_sequential_names(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face()], [_face()]])
    first = save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    second = save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    assert (first, second) == ("001.jpg", "002.jpg")
    assert list_people(tmp_path) == [
        PersonSummary(id="maria", photos=["001.jpg", "002.jpg"])
    ]


def test_save_photo_rejects_zero_faces(tmp_path: Path) -> None:
    engine = StubFaceEngine([[]])
    with pytest.raises(EnrollmentError) as exc:
        save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    assert "0 caras" in exc.value.message
    assert not (tmp_path / "maria").exists()


def test_save_photo_rejects_two_faces(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face(), _face()]])
    with pytest.raises(EnrollmentError) as exc:
        save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    assert "2 caras" in exc.value.message


def test_save_photo_rejects_unreadable_image(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face()]])
    with pytest.raises(EnrollmentError) as exc:
        save_photo(tmp_path, "maria", b"no soy una imagen", engine)
    assert "ilegible" in exc.value.message
    assert engine.calls == 0


@pytest.mark.parametrize("bad_id", ["", "María", "a b", "../x", "x" * 33, "A-1"])
def test_save_photo_rejects_invalid_person_id(tmp_path: Path, bad_id: str) -> None:
    engine = StubFaceEngine([[_face()]])
    with pytest.raises(EnrollmentError):
        save_photo(tmp_path, bad_id, _jpeg_bytes(), engine)


def test_photo_bytes_roundtrip(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face()]])
    name = save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    data = photo_bytes(tmp_path, "maria", name)
    assert data[:2] == b"\xff\xd8"  # cabecera JPEG


def test_photo_bytes_missing_is_404(tmp_path: Path) -> None:
    with pytest.raises(EnrollmentError) as exc:
        photo_bytes(tmp_path, "maria", "001.jpg")
    assert exc.value.status == 404


@pytest.mark.parametrize("bad_name", ["../../etc/passwd", "1.jpg", "001.png", "001"])
def test_photo_name_is_validated(tmp_path: Path, bad_name: str) -> None:
    with pytest.raises(EnrollmentError):
        photo_bytes(tmp_path, "maria", bad_name)


def test_delete_photo_and_person(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face()], [_face()]])
    save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    delete_photo(tmp_path, "maria", "001.jpg")
    assert list_people(tmp_path)[0].photos == ["002.jpg"]
    delete_person(tmp_path, "maria")
    assert list_people(tmp_path) == []


def test_delete_missing_is_404(tmp_path: Path) -> None:
    with pytest.raises(EnrollmentError) as exc:
        delete_person(tmp_path, "maria")
    assert exc.value.status == 404
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_enrollment.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.admin'`

- [ ] **Step 3: Implementar `src/vitahub/admin/enrollment.py`** (y `__init__.py` vacío)

```python
"""Enrolamiento por API: personas y fotos en /data/faces/<person_id>/.

Lógica pura, sin HTTP. Las reglas de validación son las mismas que aplica
load_gallery al arrancar: una foto aceptada aquí jamás rompe el arranque.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from vitahub.identity.base import FaceEngine

_PERSON_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_PHOTO_NAME_RE = re.compile(r"^\d{3}\.jpg$")


class EnrollmentError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class PersonSummary:
    id: str
    photos: list[str]


def _check_person_id(person_id: str) -> None:
    if not _PERSON_ID_RE.match(person_id):
        raise EnrollmentError(
            "person_id inválido: solo minúsculas, dígitos y guiones (máx. 32)"
        )


def _check_photo_name(photo: str) -> None:
    # El nombre viene de la URL: sin este patrón sería una ruta arbitraria.
    if not _PHOTO_NAME_RE.match(photo):
        raise EnrollmentError("nombre de foto inválido")


def list_people(faces_dir: Path) -> list[PersonSummary]:
    if not faces_dir.is_dir():
        return []
    people = []
    for person_dir in sorted(p for p in faces_dir.iterdir() if p.is_dir()):
        photos = sorted(
            p.name for p in person_dir.iterdir() if _PHOTO_NAME_RE.match(p.name)
        )
        people.append(PersonSummary(id=person_dir.name, photos=photos))
    return people


def photo_bytes(faces_dir: Path, person_id: str, photo: str) -> bytes:
    _check_person_id(person_id)
    _check_photo_name(photo)
    path = faces_dir / person_id / photo
    if not path.is_file():
        raise EnrollmentError("foto no encontrada", status=404)
    return path.read_bytes()


def save_photo(
    faces_dir: Path, person_id: str, data: bytes, engine: FaceEngine
) -> str:
    _check_person_id(person_id)
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise EnrollmentError("imagen ilegible: sube un JPEG o PNG válido")
    faces = engine.extract(image)
    if len(faces) != 1:
        raise EnrollmentError(
            f"la foto tiene {len(faces)} caras; debe tener exactamente una"
        )
    person_dir = faces_dir / person_id
    person_dir.mkdir(parents=True, exist_ok=True)
    existing = [
        int(p.stem) for p in person_dir.iterdir() if _PHOTO_NAME_RE.match(p.name)
    ]
    name = f"{max(existing, default=0) + 1:03d}.jpg"
    # Se reencoda siempre a JPEG: normaliza el formato (aunque llegue PNG),
    # descarta EXIF y garantiza que lo guardado es exactamente lo validado.
    ok, buf = cv2.imencode(".jpg", image)
    if not ok:
        raise EnrollmentError("no se pudo codificar la imagen")
    (person_dir / name).write_bytes(bytes(buf))
    return name


def delete_photo(faces_dir: Path, person_id: str, photo: str) -> None:
    _check_person_id(person_id)
    _check_photo_name(photo)
    path = faces_dir / person_id / photo
    if not path.is_file():
        raise EnrollmentError("foto no encontrada", status=404)
    path.unlink()


def delete_person(faces_dir: Path, person_id: str) -> None:
    _check_person_id(person_id)
    path = faces_dir / person_id
    if not path.is_dir():
        raise EnrollmentError("persona no encontrada", status=404)
    shutil.rmtree(path)
```

- [ ] **Step 4: Verificar que pasan**

Run: `.venv/bin/python -m pytest tests/test_enrollment.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Lint + tipos + commit**

Run: `.venv/bin/python -m ruff check src/vitahub/admin tests/test_enrollment.py && .venv/bin/python -m mypy`
Expected: sin errores

```bash
git add src/vitahub/admin tests/test_enrollment.py
git commit -m "feat: enrolamiento por API — lógica pura de personas y fotos

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `admin/config_edit.py` — subconjunto curado de config

**Files:**
- Create: `src/vitahub/admin/config_edit.py`
- Test: `tests/test_config_edit.py`

**Interfaces:**
- Consumes: `load_config`, `ConfigError` de `vitahub.config`; `PersonSummary` de `vitahub.admin.enrollment` (Task 1).
- Produces (Task 4 depende de estas firmas exactas):
  - `@dataclass(frozen=True) AdminSettings: fall_enabled: bool; identity_enabled: bool; match_threshold: float`
  - `read_settings(config_path: Path) -> AdminSettings`
  - `write_settings(config_path: Path, settings: AdminSettings, env: Mapping[str, str]) -> None` (lanza `ConfigError` si la candidata no valida; en ese caso no escribe nada)
  - `validate_apply(settings: AdminSettings, people: list[PersonSummary]) -> None` (lanza `ConfigError`)

- [ ] **Step 1: Escribir los tests que fallan**

`tests/test_config_edit.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vitahub.admin.config_edit import (
    AdminSettings,
    read_settings,
    validate_apply,
    write_settings,
)
from vitahub.admin.enrollment import PersonSummary
from vitahub.config import ConfigError

_ENV = {"VITAHUB_ONVIF_USER": "admin", "VITAHUB_ONVIF_PASSWORD": "secreto"}

_BASE_YAML = """\
hub_id: hub-test
inference:
  detector: person_pose
  fall:
    enabled: true
  identity:
    enabled: false
cameras: []
"""


def _write(tmp_path: Path, text: str = _BASE_YAML) -> Path:
    path = tmp_path / "hub.yaml"
    path.write_text(text)
    return path


def test_read_settings_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path)
    assert read_settings(path) == AdminSettings(
        fall_enabled=True, identity_enabled=False, match_threshold=0.4
    )


def test_write_settings_roundtrip_preserves_rest(tmp_path: Path) -> None:
    path = _write(tmp_path)
    write_settings(
        path,
        AdminSettings(fall_enabled=True, identity_enabled=True, match_threshold=0.55),
        _ENV,
    )
    assert read_settings(path) == AdminSettings(
        fall_enabled=True, identity_enabled=True, match_threshold=0.55
    )
    raw = yaml.safe_load(path.read_text())
    # El resto del YAML sobrevive intacto.
    assert raw["hub_id"] == "hub-test"
    assert raw["inference"]["detector"] == "person_pose"
    assert raw["cameras"] == []


def test_invalid_candidate_writes_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path)
    before = path.read_text()
    # identity sin fall: la regla vive en load_config y se hereda de allí.
    with pytest.raises(ConfigError):
        write_settings(
            path,
            AdminSettings(
                fall_enabled=False, identity_enabled=True, match_threshold=0.4
            ),
            _ENV,
        )
    assert path.read_text() == before
    assert not list(tmp_path.glob(".hub-*"))  # sin temporales huérfanos


def test_corrupt_yaml_refuses_write(tmp_path: Path) -> None:
    path = _write(tmp_path, "solo texto")
    with pytest.raises(ConfigError):
        write_settings(
            path,
            AdminSettings(fall_enabled=True, identity_enabled=False, match_threshold=0.4),
            _ENV,
        )


def test_validate_apply_requires_people_when_identity_on() -> None:
    settings = AdminSettings(
        fall_enabled=True, identity_enabled=True, match_threshold=0.4
    )
    with pytest.raises(ConfigError):
        validate_apply(settings, [])
    with pytest.raises(ConfigError):
        validate_apply(settings, [PersonSummary(id="maria", photos=[])])
    validate_apply(settings, [PersonSummary(id="maria", photos=["001.jpg"])])


def test_validate_apply_identity_off_needs_nothing() -> None:
    settings = AdminSettings(
        fall_enabled=False, identity_enabled=False, match_threshold=0.4
    )
    validate_apply(settings, [])
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_config_edit.py -v`
Expected: FAIL con `ModuleNotFoundError` (config_edit no existe)

- [ ] **Step 3: Implementar `src/vitahub/admin/config_edit.py`**

```python
"""Subconjunto curado de hub.yaml editable por el técnico.

Solo tres campos; el resto del fichero se preserva byte a byte en estructura.
La candidata pasa por el load_config real antes de escribir: si no valida,
el hub.yaml queda como estaba (nunca cementamos una config que impida arrancar).
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from vitahub.admin.enrollment import PersonSummary
from vitahub.config import ConfigError, load_config


@dataclass(frozen=True)
class AdminSettings:
    fall_enabled: bool
    identity_enabled: bool
    match_threshold: float


def read_settings(config_path: Path) -> AdminSettings:
    raw = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} no es un mapping YAML")
    inference = raw.get("inference") or {}
    fall = inference.get("fall") or {}
    identity = inference.get("identity") or {}
    return AdminSettings(
        fall_enabled=bool(fall.get("enabled", False)),
        identity_enabled=bool(identity.get("enabled", False)),
        match_threshold=float(identity.get("match_threshold", 0.4)),
    )


def write_settings(
    config_path: Path, settings: AdminSettings, env: Mapping[str, str]
) -> None:
    raw = yaml.safe_load(config_path.read_text()) or {}
    # Mismo guardarraíl que save_cameras: sobre un fichero corrupto no se
    # escribe (con restart: unless-stopped sería un bucle de reinicio).
    if not isinstance(raw, dict) or not raw.get("hub_id"):
        raise ConfigError(
            f"No se guarda la config: {config_path} está vacío, corrupto o sin 'hub_id'"
        )
    inference = raw.setdefault("inference", {})
    inference.setdefault("fall", {})["enabled"] = settings.fall_enabled
    identity = inference.setdefault("identity", {})
    identity["enabled"] = settings.identity_enabled
    identity["match_threshold"] = settings.match_threshold

    fd, tmp = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=".hub-", suffix=".yaml.tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
            # flush + fsync antes del replace, como en save_cameras: un corte
            # de luz no debe dejar un hub.yaml truncado en la eMMC.
            f.flush()
            os.fsync(f.fileno())
        # La validación corre sobre el MISMO fichero que se va a promocionar.
        load_config(Path(tmp), env)
        os.replace(tmp, config_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_apply(settings: AdminSettings, people: list[PersonSummary]) -> None:
    if not settings.identity_enabled:
        return
    if not any(p.photos for p in people):
        raise ConfigError(
            "identity.enabled requiere al menos una persona enrolada con una foto"
        )
```

- [ ] **Step 4: Verificar que pasan**

Run: `.venv/bin/python -m pytest tests/test_config_edit.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint + tipos + commit**

Run: `.venv/bin/python -m ruff check src/vitahub/admin tests/test_config_edit.py && .venv/bin/python -m mypy`
Expected: sin errores

```bash
git add src/vitahub/admin/config_edit.py tests/test_config_edit.py
git commit -m "feat: edición validada del subconjunto de config del técnico

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: rutas de enrolamiento en `control.py`

**Files:**
- Modify: `src/vitahub/control.py`
- Test: `tests/test_control.py` (ampliar)

**Interfaces:**
- Consumes: Task 1 (`list_people`, `photo_bytes`, `save_photo`, `delete_photo`, `delete_person`, `EnrollmentError`); `FaceEngine` de `identity.base`.
- Produces (Task 4, 5 y 6 dependen de esto):
  - `@dataclass AdminContext: faces_dir: Path; config_path: Path; env: Mapping[str, str]; engine_factory: Callable[[], FaceEngine]; request_shutdown: Callable[[], None]`
  - `start_control_server(service, token, port, host="0.0.0.0", admin: AdminContext | None = None)` — firma actual + parámetro opcional; con `admin=None` todo `/api/*` es 404 (los tests existentes siguen pasando sin tocar).
  - `_build_handler(service, token, admin: AdminContext | None = None)`

- [ ] **Step 1: Escribir los tests que fallan**

Añadir a `tests/test_control.py` (junto a los fixtures existentes; reutiliza el patrón `server_factory` con `port=0, host="127.0.0.1"` y el helper `_post`; añade helpers análogos `_get`, `_delete`, `_post_bytes` con `urllib.request` y método explícito):

```python
import json as _json
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import pytest

from vitahub.control import AdminContext, start_control_server
from vitahub.identity.base import FaceObservation
from vitahub.identity.stub import StubFaceEngine


def _jpeg_bytes() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((32, 32, 3), 128, dtype=np.uint8))
    assert ok
    return bytes(buf)


def _face() -> FaceObservation:
    emb = np.zeros(512, dtype=np.float32)
    emb[0] = 1.0
    return FaceObservation(bbox=(0, 0, 10, 10), embedding=emb)


def _request(url, method="GET", token=None, data=None, content_type=None):
    req = urllib.request.Request(url, method=method, data=data)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    if content_type is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


@pytest.fixture()
def admin_server(tmp_path: Path):
    """Servidor con AdminContext real sobre tmp_path y engine stub."""
    servers = []
    engine = StubFaceEngine([[_face()] for _ in range(10)])
    shutdowns: list[bool] = []
    ctx = AdminContext(
        faces_dir=tmp_path / "faces",
        config_path=tmp_path / "hub.yaml",
        env={"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"},
        engine_factory=lambda: engine,
        request_shutdown=lambda: shutdowns.append(True),
    )
    (tmp_path / "hub.yaml").write_text(
        "hub_id: hub-test\ninference:\n  detector: person_pose\n"
        "  fall:\n    enabled: true\ncameras: []\n"
    )

    def _start():
        httpd = start_control_server(
            _FakeService(_ok_result()), "secreto", port=0, host="127.0.0.1", admin=ctx
        )
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", shutdowns

    yield _start
    for s in servers:
        s.shutdown()


def test_api_requires_token(admin_server):
    base, _ = admin_server()
    status, _body = _request(f"{base}/api/people")
    assert status == 401


def test_people_lifecycle_over_http(admin_server):
    base, _ = admin_server()
    status, body = _request(f"{base}/api/people", token="secreto")
    assert (status, _json.loads(body)) == (200, {"people": []})

    status, body = _request(
        f"{base}/api/people/maria/photos", method="POST", token="secreto",
        data=_jpeg_bytes(), content_type="image/jpeg",
    )
    assert status == 200
    assert _json.loads(body) == {"saved": "001.jpg"}

    status, body = _request(f"{base}/api/people", token="secreto")
    assert _json.loads(body) == {"people": [{"id": "maria", "photos": ["001.jpg"]}]}

    status, body = _request(
        f"{base}/api/people/maria/photos/001.jpg", token="secreto"
    )
    assert status == 200 and body[:2] == b"\xff\xd8"

    status, _body = _request(
        f"{base}/api/people/maria/photos/001.jpg", method="DELETE", token="secreto"
    )
    assert status == 200
    status, _body = _request(f"{base}/api/people/maria", method="DELETE", token="secreto")
    assert status == 200


def test_upload_invalid_image_is_400_with_message(admin_server):
    base, _ = admin_server()
    status, body = _request(
        f"{base}/api/people/maria/photos", method="POST", token="secreto",
        data=b"garbage", content_type="image/jpeg",
    )
    assert status == 400
    assert "ilegible" in _json.loads(body)["error"]


def test_upload_too_large_is_413(admin_server):
    base, _ = admin_server()
    status, _body = _request(
        f"{base}/api/people/maria/photos", method="POST", token="secreto",
        data=b"x" * (10 * 1024 * 1024 + 1), content_type="image/jpeg",
    )
    assert status == 413


def test_api_without_admin_context_is_404(server_factory):
    # El fixture existente arranca sin AdminContext: nada de /api existe.
    base = server_factory(_FakeService(_ok_result()))
    status, _body = _request(f"{base}/api/people", token="secreto")
    assert status == 404
```

Nota: adapta la llamada a `server_factory` al retorno real del fixture existente (hoy devuelve la URL con el server dentro; míralo antes de escribir).

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_control.py -v`
Expected: los tests nuevos FAIL con `ImportError: cannot import name 'AdminContext'`; los antiguos PASS.

- [ ] **Step 3: Implementar en `control.py`**

Añadir imports (`re`, `dataclass`, `Callable`, `Mapping`, `Path`, los símbolos de `admin.enrollment`) y:

```python
@dataclass
class AdminContext:
    """Dependencias de las rutas /api/*. Sin él, el servidor es solo /rescan."""

    faces_dir: Path
    config_path: Path
    env: Mapping[str, str]
    engine_factory: Callable[[], FaceEngine]
    request_shutdown: Callable[[], None]


_MAX_PHOTO_BYTES = 10 * 1024 * 1024

_PEOPLE_RE = re.compile(r"^/api/people$")
_PHOTOS_RE = re.compile(r"^/api/people/([^/]+)/photos$")
_PHOTO_RE = re.compile(r"^/api/people/([^/]+)/photos/([^/]+)$")
_PERSON_RE = re.compile(r"^/api/people/([^/]+)$")
```

En `_build_handler(service, token, admin=None)`, dentro del closure, un engine perezoso compartido entre peticiones:

```python
engine_lock = threading.Lock()
engine_cache: list[FaceEngine] = []

def _engine() -> FaceEngine:
    # Perezoso: los pesos solo se cargan si el técnico enrola de verdad.
    with engine_lock:
        if not engine_cache:
            engine_cache.append(admin.engine_factory())
        return engine_cache[0]
```

Dispatch en el handler — patrón: cada `do_<METODO>` comprueba primero `/api` + auth, delega y mapea errores. Ejemplo para GET y POST (DELETE análogo a GET):

```python
def do_GET(self) -> None:
    if admin is None or not self.path.startswith("/api"):
        self._respond(404)
        return
    if not self._authorized():
        self._respond(401)
        return
    if _PEOPLE_RE.match(self.path):
        people = list_people(admin.faces_dir)
        self._respond(200, {"people": [
            {"id": p.id, "photos": p.photos} for p in people
        ]})
        return
    if m := _PHOTO_RE.match(self.path):
        try:
            data = photo_bytes(admin.faces_dir, m.group(1), m.group(2))
        except EnrollmentError as err:
            self._respond(err.status, {"error": err.message})
            return
        self._respond_bytes(200, data, "image/jpeg")
        return
    self._respond(404)

def do_POST(self) -> None:
    # /rescan conserva su camino actual, intacto.
    if self.path == "/rescan":
        ...  # código existente sin cambios
        return
    if admin is None or not self.path.startswith("/api"):
        self._respond(404)
        return
    if not self._authorized():
        self._respond(401)
        return
    if m := _PHOTOS_RE.match(self.path):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._respond(400, {"error": "cuerpo vacío"})
            return
        if length > _MAX_PHOTO_BYTES:
            self._respond(413, {"error": "foto demasiado grande (máx. 10 MB)"})
            return
        body = self.rfile.read(length)
        try:
            saved = save_photo(admin.faces_dir, m.group(1), body, _engine())
        except EnrollmentError as err:
            self._respond(err.status, {"error": err.message})
            return
        self._respond(200, {"saved": saved})
        return
    self._respond(404)
```

`_respond_bytes` es un helper nuevo junto a `_respond` (mismo shape, con `Content-Type` parametrizado y cuerpo binario). `do_DELETE` usa `_PHOTO_RE` → `delete_photo` y `_PERSON_RE` → `delete_person`, respondiendo `200 {}` o el error mapeado. Reestructura el `do_POST` existente moviendo el cuerpo del `/rescan` actual a un método privado `_handle_rescan()` si queda más legible — sin cambiar su comportamiento ni sus tests.

`start_control_server` gana `admin: AdminContext | None = None` y lo pasa a `_build_handler`.

Cuidado con `send_error`: sigue devolviendo 404 vacío para métodos/rutas no contemplados (no tocar).

- [ ] **Step 4: Verificar que pasan (todos, viejos y nuevos)**

Run: `.venv/bin/python -m pytest tests/test_control.py -v`
Expected: PASS

- [ ] **Step 5: Lint + tipos + commit**

Run: `.venv/bin/python -m ruff check src/vitahub/control.py tests/test_control.py && .venv/bin/python -m mypy`
Expected: sin errores

```bash
git add src/vitahub/control.py tests/test_control.py
git commit -m "feat: API de enrolamiento en el servidor de control

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: rutas de config y apply en `control.py`

**Files:**
- Modify: `src/vitahub/control.py`
- Test: `tests/test_control.py` (ampliar)

**Interfaces:**
- Consumes: Task 2 (`read_settings`, `write_settings`, `validate_apply`, `AdminSettings`), Task 3 (`AdminContext`, dispatch, `admin_server` fixture); `ConfigError` de `vitahub.config`.
- Produces: rutas `GET/PUT /api/config` y `POST /api/apply` que Task 5 (UI) consume.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir a `tests/test_control.py` (reutiliza `admin_server` y `_request` de Task 3):

```python
def test_config_roundtrip_over_http(admin_server):
    base, _ = admin_server()
    status, body = _request(f"{base}/api/config", token="secreto")
    assert status == 200
    assert _json.loads(body) == {
        "fall_enabled": True, "identity_enabled": False, "match_threshold": 0.4,
    }

    payload = _json.dumps({
        "fall_enabled": True, "identity_enabled": True, "match_threshold": 0.5,
    }).encode()
    status, _body = _request(
        f"{base}/api/config", method="PUT", token="secreto",
        data=payload, content_type="application/json",
    )
    assert status == 200

    status, body = _request(f"{base}/api/config", token="secreto")
    assert _json.loads(body)["match_threshold"] == 0.5


def test_config_invalid_combo_is_400(admin_server):
    base, _ = admin_server()
    payload = _json.dumps({
        "fall_enabled": False, "identity_enabled": True, "match_threshold": 0.4,
    }).encode()
    status, body = _request(
        f"{base}/api/config", method="PUT", token="secreto",
        data=payload, content_type="application/json",
    )
    assert status == 400
    assert "fall" in _json.loads(body)["error"]


def test_config_bad_json_is_400(admin_server):
    base, _ = admin_server()
    status, _body = _request(
        f"{base}/api/config", method="PUT", token="secreto",
        data=b"{no json", content_type="application/json",
    )
    assert status == 400


def test_apply_triggers_shutdown(admin_server):
    base, shutdowns = admin_server()
    status, body = _request(f"{base}/api/apply", method="POST", token="secreto")
    assert status == 200
    assert _json.loads(body) == {"status": "reiniciando"}
    assert shutdowns == [True]


def test_apply_identity_without_people_is_400(admin_server):
    base, shutdowns = admin_server()
    payload = _json.dumps({
        "fall_enabled": True, "identity_enabled": True, "match_threshold": 0.4,
    }).encode()
    _request(f"{base}/api/config", method="PUT", token="secreto",
             data=payload, content_type="application/json")
    status, body = _request(f"{base}/api/apply", method="POST", token="secreto")
    assert status == 400
    assert shutdowns == []
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_control.py -k "config or apply" -v`
Expected: FAIL con 404 en `/api/config` y `/api/apply`

- [ ] **Step 3: Implementar**

En `do_GET`, rama nueva:

```python
if self.path == "/api/config":
    try:
        s = read_settings(admin.config_path)
    except ConfigError as err:
        self._respond(400, {"error": str(err)})
        return
    self._respond(200, {
        "fall_enabled": s.fall_enabled,
        "identity_enabled": s.identity_enabled,
        "match_threshold": s.match_threshold,
    })
    return
```

`do_PUT` nuevo (mismo preámbulo admin/auth que `do_GET`):

```python
def do_PUT(self) -> None:
    if admin is None or self.path != "/api/config":
        self._respond(404)
        return
    if not self._authorized():
        self._respond(401)
        return
    length = int(self.headers.get("Content-Length") or 0)
    try:
        raw = json.loads(self.rfile.read(length))
        settings = AdminSettings(
            fall_enabled=bool(raw["fall_enabled"]),
            identity_enabled=bool(raw["identity_enabled"]),
            match_threshold=float(raw["match_threshold"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        self._respond(400, {"error": "cuerpo JSON inválido: se esperan "
                            "fall_enabled, identity_enabled y match_threshold"})
        return
    try:
        write_settings(admin.config_path, settings, admin.env)
    except ConfigError as err:
        self._respond(400, {"error": str(err)})
        return
    self._respond(200, {})
```

En `do_POST`, rama `/api/apply`:

```python
if self.path == "/api/apply":
    try:
        settings = read_settings(admin.config_path)
        validate_apply(settings, list_people(admin.faces_dir))
    except ConfigError as err:
        self._respond(400, {"error": str(err)})
        return
    # Responder ANTES de apagar: la página necesita el 200 para empezar
    # su polling de "el hub ha vuelto".
    self._respond(200, {"status": "reiniciando"})
    admin.request_shutdown()
    return
```

Ojo: `send_error` intercepta métodos sin `do_<METODO>`; al añadir `do_PUT` real, el test existente `test_put_is_404_empty` debe seguir pasando (PUT a ruta no contemplada → 404 vacío; el camino nuevo solo responde a `/api/config` con admin).

- [ ] **Step 4: Verificar que pasan todos**

Run: `.venv/bin/python -m pytest tests/test_control.py -v`
Expected: PASS

- [ ] **Step 5: Lint + tipos + commit**

Run: `.venv/bin/python -m ruff check src/vitahub/control.py tests/test_control.py && .venv/bin/python -m mypy`
Expected: sin errores

```bash
git add src/vitahub/control.py tests/test_control.py
git commit -m "feat: config curada y apply con reinicio limpio por API

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: página estática del técnico

**Files:**
- Create: `src/vitahub/admin/static/index.html`
- Modify: `src/vitahub/control.py` (servir `GET /`)
- Modify: `pyproject.toml` (package-data)
- Test: `tests/test_control.py` (ampliar)

**Interfaces:**
- Consumes: la API completa de Tasks 3–4.
- Produces: `GET /` devuelve la página (200, `text/html`, sin auth) cuando hay `AdminContext`; 404 sin él.

- [ ] **Step 1: Escribir los tests que fallan**

```python
def test_root_serves_page_without_token(admin_server):
    base, _ = admin_server()
    status, body = _request(f"{base}/")
    assert status == 200
    assert b"vitahub" in body.lower()


def test_root_without_admin_context_is_404(server_factory):
    base = server_factory(_FakeService(_ok_result()))
    status, _body = _request(f"{base}/")
    assert status == 404
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_control.py -k root -v`
Expected: FAIL (404 en `/`)

- [ ] **Step 3: Crear `src/vitahub/admin/static/index.html`**

Página completa, vanilla, móvil primero. Contenido íntegro:

```html
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>vitahub — administración</title>
<style>
  :root { font-family: system-ui, sans-serif; }
  body { margin: 0 auto; max-width: 640px; padding: 1rem; background: #fafafa; color: #222; }
  h1 { font-size: 1.3rem; } h2 { font-size: 1.05rem; margin-top: 1.5rem; }
  section { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
  button { padding: .5rem 1rem; border-radius: 6px; border: 1px solid #888; background: #fff; cursor: pointer; }
  button.primary { background: #1a6b3c; color: #fff; border-color: #1a6b3c; }
  input[type=text], input[type=password], input[type=range] { width: 100%; box-sizing: border-box; padding: .4rem; }
  .person { display: flex; align-items: center; gap: .6rem; margin: .5rem 0; flex-wrap: wrap; }
  .person img { width: 56px; height: 56px; object-fit: cover; border-radius: 6px; }
  .msg { padding: .5rem; border-radius: 6px; margin: .5rem 0; }
  .msg.ok { background: #e6f4ea; } .msg.error { background: #fdecea; }
  .row { display: flex; gap: .6rem; align-items: center; margin: .5rem 0; }
  #login[hidden], #app[hidden] { display: none; }
</style>
</head>
<body>
<h1>vitahub — administración</h1>
<div id="msg"></div>

<section id="login" hidden>
  <h2>Token de acceso</h2>
  <input type="password" id="token-input" placeholder="token del hub" autocomplete="off">
  <div class="row"><button class="primary" id="token-save">Entrar</button></div>
</section>

<div id="app" hidden>
<section>
  <h2>Personas enroladas</h2>
  <div id="people"></div>
  <div class="row">
    <input type="text" id="new-person" placeholder="nombre (p. ej. maria)">
    <input type="file" id="photo-file" accept="image/*" capture="environment" hidden>
    <button id="add-photo">Añadir foto…</button>
  </div>
  <p>3–5 fotos por persona, con distintas luces y ángulos. Cada foto debe tener exactamente una cara.</p>
</section>

<section>
  <h2>Configuración</h2>
  <label class="row"><input type="checkbox" id="fall"> Detección de caídas</label>
  <label class="row"><input type="checkbox" id="identity"> Identidad de persona (requiere caídas)</label>
  <label>Umbral de coincidencia (<span id="thr-val">0.40</span> — 0.4 = sin calibrar)
    <input type="range" id="thr" min="0.05" max="1" step="0.05">
  </label>
  <div class="row"><button id="save-config">Guardar configuración</button></div>
</section>

<section>
  <h2>Aplicar</h2>
  <p>Valida todo y reinicia el hub (unos segundos sin detección).</p>
  <div class="row"><button class="primary" id="apply">Aplicar y reiniciar</button></div>
</section>
</div>

<script>
const $ = (id) => document.getElementById(id);
let token = localStorage.getItem("vitahub_token") || "";

function msg(text, cls) {
  $("msg").innerHTML = text ? `<div class="msg ${cls}">${text}</div>` : "";
}

async function api(path, opts = {}) {
  opts.headers = Object.assign({ "Authorization": "Bearer " + token }, opts.headers);
  const resp = await fetch(path, opts);
  if (resp.status === 401) { showLogin(); throw new Error("token inválido"); }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).error; } catch (e) {}
    throw new Error(detail);
  }
  return resp;
}

function showLogin() { $("login").hidden = false; $("app").hidden = true; }
function showApp() { $("login").hidden = true; $("app").hidden = false; }

async function refreshPeople() {
  const data = await (await api("/api/people")).json();
  const box = $("people");
  box.innerHTML = "";
  for (const p of data.people) {
    const div = document.createElement("div");
    div.className = "person";
    div.innerHTML = `<strong>${p.id}</strong> (${p.photos.length} fotos)`;
    for (const name of p.photos) {
      const img = document.createElement("img");
      api(`/api/people/${p.id}/photos/${name}`)
        .then(r => r.blob()).then(b => { img.src = URL.createObjectURL(b); });
      img.title = name + " — toca para borrar";
      img.onclick = async () => {
        if (!confirm(`¿Borrar ${name} de ${p.id}?`)) return;
        await api(`/api/people/${p.id}/photos/${name}`, { method: "DELETE" });
        refreshPeople();
      };
      div.appendChild(img);
    }
    const del = document.createElement("button");
    del.textContent = "Borrar persona";
    del.onclick = async () => {
      if (!confirm(`¿Borrar a ${p.id} y todas sus fotos?`)) return;
      await api(`/api/people/${p.id}`, { method: "DELETE" });
      refreshPeople();
    };
    div.appendChild(del);
    box.appendChild(div);
  }
}

async function refreshConfig() {
  const c = await (await api("/api/config")).json();
  $("fall").checked = c.fall_enabled;
  $("identity").checked = c.identity_enabled;
  $("thr").value = c.match_threshold;
  $("thr-val").textContent = Number(c.match_threshold).toFixed(2);
}

$("thr").oninput = () => { $("thr-val").textContent = Number($("thr").value).toFixed(2); };
$("identity").onchange = () => { if ($("identity").checked) $("fall").checked = true; };

$("add-photo").onclick = () => {
  const id = $("new-person").value.trim().toLowerCase().replace(/[^a-z0-9-]/g, "-");
  if (!id) { msg("Escribe primero el nombre de la persona", "error"); return; }
  $("new-person").value = id;
  $("photo-file").click();
};

$("photo-file").onchange = async () => {
  const file = $("photo-file").files[0];
  if (!file) return;
  const id = $("new-person").value.trim();
  try {
    await api(`/api/people/${id}/photos`, {
      method: "POST", body: file, headers: { "Content-Type": file.type },
    });
    msg(`Foto guardada para ${id} ✓`, "ok");
    refreshPeople();
  } catch (err) { msg("Foto rechazada: " + err.message, "error"); }
  $("photo-file").value = "";
};

$("save-config").onclick = async () => {
  try {
    await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fall_enabled: $("fall").checked,
        identity_enabled: $("identity").checked,
        match_threshold: Number($("thr").value),
      }),
    });
    msg("Configuración guardada ✓ (se aplica al reiniciar)", "ok");
  } catch (err) { msg("Config rechazada: " + err.message, "error"); }
};

$("apply").onclick = async () => {
  if (!confirm("El hub validará todo y se reiniciará (unos segundos sin detección). ¿Continuar?")) return;
  try {
    await api("/api/apply", { method: "POST" });
  } catch (err) { msg("No se puede aplicar: " + err.message, "error"); return; }
  msg("Reiniciando… esperando a que el hub vuelva", "ok");
  const poll = setInterval(async () => {
    try {
      const data = await (await api("/api/people")).json();
      clearInterval(poll);
      msg(`Hub reiniciado con ${data.people.length} personas enroladas ✓`, "ok");
      refreshConfig();
    } catch (e) { /* aún reiniciando */ }
  }, 2000);
};

$("token-save").onclick = async () => {
  token = $("token-input").value.trim();
  localStorage.setItem("vitahub_token", token);
  try { await api("/api/people"); showApp(); msg("", ""); await refreshPeople(); await refreshConfig(); }
  catch (err) { msg("Token inválido", "error"); }
};

(async () => {
  if (!token) { showLogin(); return; }
  try { showApp(); await refreshPeople(); await refreshConfig(); }
  catch (err) { /* api() ya mostró el login si era 401 */ }
})();
</script>
</body>
</html>
```

- [ ] **Step 4: Servir la página desde `control.py` y empaquetarla**

En `do_GET`, ANTES del preámbulo `/api` (esta ruta es pública):

```python
if self.path == "/" and admin is not None:
    page = (
        resources.files("vitahub.admin") / "static" / "index.html"
    ).read_bytes()
    self._respond_bytes(200, page, "text/html; charset=utf-8")
    return
```

con `from importlib import resources` arriba.

En `pyproject.toml`, junto a `[tool.setuptools.packages.find]`:

```toml
[tool.setuptools.package-data]
vitahub = ["admin/static/*.html"]
```

- [ ] **Step 5: Verificar que pasan**

Run: `.venv/bin/python -m pytest tests/test_control.py -v`
Expected: PASS

- [ ] **Step 6: Lint + commit**

Run: `.venv/bin/python -m ruff check src/vitahub/control.py && .venv/bin/python -m mypy`
Expected: sin errores

```bash
git add src/vitahub/admin/static/index.html src/vitahub/control.py pyproject.toml tests/test_control.py
git commit -m "feat: página de administración del técnico servida por el hub

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: cableado en `app.py`, factory y documentación

**Files:**
- Modify: `src/vitahub/factory.py` (helper `build_admin_engine_factory`)
- Modify: `src/vitahub/app.py` (construir y pasar `AdminContext`)
- Modify: `docs/como-funciona.md`, `docs/como-probar.md`
- Test: `tests/test_factory.py` (ampliar)

**Interfaces:**
- Consumes: `AdminContext` y `start_control_server(..., admin=)` de Task 3; `_DEFAULT_FACES_DIR`, `_DEFAULT_FACE_WEIGHTS`, `_require_face_weights` ya existentes en `factory.py`.
- Produces: `build_admin_engine_factory(env: Mapping[str, str]) -> Callable[[], FaceEngine]` en `factory.py` — valida los pesos EN LA LLAMADA (no al construir la factory), para que un hub sin pesos siga arrancando y el error salga legible en la subida.

- [ ] **Step 1: Escribir el test que falla**

Añadir a `tests/test_factory.py`:

```python
def test_admin_engine_factory_missing_weights_raises_config_error(tmp_path):
    from vitahub.factory import build_admin_engine_factory

    factory = build_admin_engine_factory(
        {"VITAHUB_FACE_WEIGHTS": str(tmp_path / "no-existe")}
    )
    # Construir la factory no toca disco; llamar sí, y falla legible.
    with pytest.raises(ConfigError):
        factory()
```

(usa los imports de `ConfigError`/`pytest` que el fichero ya tiene).

- [ ] **Step 2: Verificar que falla**

Run: `.venv/bin/python -m pytest tests/test_factory.py -v`
Expected: FAIL con `ImportError` de `build_admin_engine_factory`

- [ ] **Step 3: Implementar en `factory.py`**

```python
def build_admin_engine_factory(
    env: Mapping[str, str],
) -> Callable[[], FaceEngine]:
    """Factory perezosa del engine del servidor de control.

    La validación de pesos corre al LLAMARLA, no al construirla: un hub sin
    pesos faciales debe arrancar igual (la identidad puede estar apagada) y
    el error debe salir legible en la primera subida de foto.
    """
    def _factory() -> FaceEngine:
        root = env.get("VITAHUB_FACE_WEIGHTS", _DEFAULT_FACE_WEIGHTS)
        _require_face_weights(root)
        return InsightFaceEngine.from_weights(root)

    return _factory
```

En `app.py::run`, justo antes de `_start_control_server_safe`:

```python
admin_ctx = AdminContext(
    faces_dir=Path(env.get("VITAHUB_FACES_DIR", _DEFAULT_FACES_DIR)),
    config_path=config_path,
    env=env,
    engine_factory=build_admin_engine_factory(env),
    request_shutdown=stop.set,
)
httpd = _start_control_server_safe(
    service, env.get("VITAHUB_ADMIN_TOKEN", ""), admin_port(env), admin_ctx
)
```

`_start_control_server_safe` gana el parámetro `admin: AdminContext` y lo pasa a `start_control_server`. Exporta `_DEFAULT_FACES_DIR` desde `factory.py` (quitarle el guion bajo o importarlo tal cual, siguiendo el criterio del import de `_DEFAULT_POSE_WEIGHTS` que `app.py` ya hace — mira cómo está resuelto allí y copia el patrón).

Nota sobre `EnrollmentError` en subidas cuando la factory lanza `ConfigError`: en la rama de subida de Task 3, envolver la llamada `_engine()` igual que `save_photo`:

```python
try:
    engine = _engine()
except ConfigError as err:
    self._respond(500, {"error": str(err)})
    return
```

(añadir este try en Task 6 al cablear, con un test en `test_control.py` que use una factory que lance `ConfigError` y espere 500 con mensaje).

- [ ] **Step 4: Suite completa**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, todos

- [ ] **Step 5: Documentación**

- `docs/como-funciona.md`: en la tabla de módulos añadir `admin/enrollment.py` y `admin/config_edit.py`; en la sección del control HTTP, describir las rutas `/api/*`, la página en `GET /` (pública, la API con token) y el ciclo aplicar→reinicio limpio→Docker relevanta.
- `docs/como-probar.md`: sección nueva "Administración del técnico": abrir `http://<ip>:8787` desde el móvil en la WiFi del hogar, meter el token, enrolar 3–5 fotos, ajustar umbral, Aplicar; y cómo probarlo en local con curl (`curl -H "Authorization: Bearer $TOKEN" http://localhost:8787/api/people`).

- [ ] **Step 6: Lint + tipos + commit final**

Run: `.venv/bin/python -m ruff check src tests && .venv/bin/python -m mypy && .venv/bin/python -m pytest -q`
Expected: sin errores, suite verde

```bash
git add src/vitahub/factory.py src/vitahub/app.py src/vitahub/control.py tests docs/como-funciona.md docs/como-probar.md
git commit -m "feat: administración local cableada — página, API y reinicio limpio

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
