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


def test_read_settings_missing_file_raises_config_error(tmp_path: Path) -> None:
    # GET /api/config no debe tirar un traceback si hub.yaml desaparece
    # (borrado a mano, montaje que aún no ha aparecido, etc.).
    with pytest.raises(ConfigError):
        read_settings(tmp_path / "no-existe.yaml")


def test_read_settings_unparseable_yaml_raises_config_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "a: [1, 2\n")  # YAML mal formado (sin cerrar)
    with pytest.raises(ConfigError):
        read_settings(path)


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


def test_validate_apply_rejects_any_person_without_photos() -> None:
    # load_gallery revienta al arrancar con CUALQUIER carpeta de persona sin
    # fotos válidas, no solo cuando no hay ninguna persona con fotos: si se
    # borra la única foto de una persona, su carpeta vacía queda y el
    # siguiente arranque cae en bucle de reinicio.
    settings = AdminSettings(
        fall_enabled=True, identity_enabled=True, match_threshold=0.4
    )
    with pytest.raises(ConfigError):
        validate_apply(
            settings,
            [
                PersonSummary(id="maria", photos=["001.jpg"]),
                PersonSummary(id="juan", photos=[]),
            ],
        )


def test_validate_apply_identity_off_needs_nothing() -> None:
    settings = AdminSettings(
        fall_enabled=False, identity_enabled=False, match_threshold=0.4
    )
    validate_apply(settings, [])
