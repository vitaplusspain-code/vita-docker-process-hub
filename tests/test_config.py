import os
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


def test_save_cameras_refuses_when_file_is_empty(tmp_path):
    path = tmp_path / "hub.yaml"
    path.write_text("")
    with pytest.raises(ConfigError, match="hub_id"):
        save_cameras(path, [])
    assert path.read_text() == ""  # no se cementó una config inválida


def test_save_cameras_refuses_when_hub_id_missing(tmp_path):
    path = tmp_path / "hub.yaml"
    path.write_text("cameras: []\n")
    with pytest.raises(ConfigError, match="hub_id"):
        save_cameras(path, [])
    assert path.read_text() == "cameras: []\n"  # sin tocar


def test_save_cameras_fsyncs_temp_file_before_replace(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    fsynced_fds = []
    real_fsync = os.fsync

    def _spy_fsync(fd):
        fsynced_fds.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("vitahub.config.os.fsync", _spy_fsync)
    save_cameras(path, [])
    assert fsynced_fds  # se llamó a fsync antes del os.replace


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


def test_non_positive_interval_is_rejected(tmp_path):
    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-x\ndiscovery:\n  interval_seconds: 0\n")
    with pytest.raises(ConfigError, match="interval_seconds"):
        load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})


def test_invalid_stream_value_is_rejected(tmp_path):
    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-x\ninference:\n  stream: sub_stream\n")
    with pytest.raises(ConfigError, match="stream"):
        load_config(path, ENV)


def test_valid_stream_values_are_accepted(tmp_path):
    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-x\ninference:\n  stream: main\n")
    cfg = load_config(path, ENV)
    assert cfg.inference.stream == "main"


def test_config_without_uri_fields_still_loads(tmp_path):
    """Retrocompatibilidad: un hub.yaml de la versión anterior debe cargar."""
    path = tmp_path / "hub.yaml"
    path.write_text(
        "hub_id: hub-x\n"
        "cameras:\n"
        "- id: onvif-a\n"
        "  name: camera-1\n"
        "  last_ip: 10.0.0.5\n"
    )
    cfg = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert cfg.cameras[0].rtsp_main == ""
    assert cfg.cameras[0].rtsp_sub == ""


def test_config_reads_uri_fields(tmp_path):
    path = tmp_path / "hub.yaml"
    path.write_text(
        "hub_id: hub-x\n"
        "cameras:\n"
        "- id: onvif-a\n"
        "  name: camera-1\n"
        "  last_ip: 10.0.0.5\n"
        "  rtsp_main: rtsp://10.0.0.5:554/V_ENC_000\n"
        "  rtsp_sub: rtsp://10.0.0.5:554/V_ENC_001\n"
    )
    cfg = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert cfg.cameras[0].rtsp_main == "rtsp://10.0.0.5:554/V_ENC_000"
    assert cfg.cameras[0].rtsp_sub == "rtsp://10.0.0.5:554/V_ENC_001"


def test_config_handles_null_uri_fields(tmp_path):
    """Null URIs en config se convierten a cadena vacía, no a 'None'."""
    path = tmp_path / "hub.yaml"
    path.write_text(
        "hub_id: hub-x\n"
        "cameras:\n"
        "- id: onvif-a\n"
        "  name: camera-1\n"
        "  last_ip: 10.0.0.5\n"
        "  rtsp_main: null\n"
        "  rtsp_sub:\n"
    )
    cfg = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert cfg.cameras[0].rtsp_main == ""
    assert cfg.cameras[0].rtsp_sub == ""


def test_save_cameras_persists_uri_fields(tmp_path):
    from vitahub.models import Camera

    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-x\ncameras: []\n")
    save_cameras(
        path,
        [
            Camera(
                id="onvif-a",
                name="camera-1",
                last_ip="10.0.0.5",
                rtsp_main="rtsp://10.0.0.5:554/V_ENC_000",
                rtsp_sub="rtsp://10.0.0.5:554/V_ENC_001",
            )
        ],
    )
    reloaded = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert reloaded.cameras[0].rtsp_main == "rtsp://10.0.0.5:554/V_ENC_000"
    assert reloaded.cameras[0].rtsp_sub == "rtsp://10.0.0.5:554/V_ENC_001"


def test_saved_yaml_never_contains_credentials(tmp_path):
    """Invariante del proyecto: el fichero de config no guarda secretos."""
    from vitahub.ingest.rtsp import strip_credentials
    from vitahub.models import Camera

    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-x\ncameras: []\n")
    sucia = "rtsp://admin:secreto@10.0.0.5:554/V_ENC_000"
    save_cameras(
        path,
        [
            Camera(
                id="onvif-a",
                name="camera-1",
                last_ip="10.0.0.5",
                rtsp_main=strip_credentials(sucia),
                rtsp_sub="",
            )
        ],
    )
    assert "secreto" not in path.read_text()
