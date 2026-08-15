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


def test_non_mapping_camera_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "hub_id: h\ncameras:\n  - oops\n"), ENV)


def test_non_mapping_section_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "hub_id: h\ndiscovery: [1, 2]\n"), ENV)


def test_non_numeric_interval_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(
            _write(tmp_path, "hub_id: h\ndiscovery:\n  interval_seconds: abc\n"), ENV
        )
