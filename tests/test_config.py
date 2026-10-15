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


def test_hub_id_with_invalid_characters_is_rejected(tmp_path):
    # hub_id viaja al topic MQTT, al clientId y al nombre del thing; un '/'
    # rompe el topic tal y como lo compone topic_for(). scripts/provision-hub.sh
    # (repo vitaplus-aws-architecture) valida la misma expresión del otro lado.
    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub/casa lopez\n")
    with pytest.raises(ConfigError, match="hub_id"):
        load_config(path, ENV)


def test_hub_id_with_colon_and_dash_is_accepted(tmp_path):
    cfg = load_config(_write(tmp_path, "hub_id: hub:casa-lopez_1\n"), ENV)
    assert cfg.hub_id == "hub:casa-lopez_1"


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


def test_load_config_strips_credentials_from_hand_edited_uris(tmp_path):
    """Un hub.yaml editado a mano con credenciales en la URI debe quedar limpio.

    La invariante "el YAML no contiene secretos" se aplicaba solo a lo que
    venía de ONVIF (`registry.strip_credentials`). Un operador puede pegar
    `rtsp://admin:clave@...` a mano (el propio proyecto lo documenta e
    invita a hacerlo) y `load_config` debe limpiarlo, para que la siguiente
    reescritura (`save_cameras`) no cemente la contraseña en disco.
    """
    path = tmp_path / "hub.yaml"
    path.write_text(
        "hub_id: hub-x\n"
        "cameras:\n"
        "- id: onvif-a\n"
        "  name: camera-1\n"
        "  last_ip: 10.0.0.5\n"
        "  rtsp_main: rtsp://admin:secreto@10.0.0.5:554/main\n"
        "  rtsp_sub: rtsp://admin:secreto@10.0.0.5:554/sub\n"
    )
    cfg = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert cfg.cameras[0].rtsp_main == "rtsp://10.0.0.5:554/main"
    assert cfg.cameras[0].rtsp_sub == "rtsp://10.0.0.5:554/sub"


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
    """Invariante del proyecto: el fichero de config no guarda secretos.

    `save_cameras` es la última línea de defensa: se le pasa una `Camera`
    con credenciales SIN limpiar (a diferencia de antes, que limpiaba la URI
    antes de llamar y por tanto solo probaba `strip_credentials`, no la
    defensa de `save_cameras`), y lo escrito en disco no debe contenerlas.
    """
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
                rtsp_main="rtsp://admin:secreto@10.0.0.5:554/V_ENC_000",
                rtsp_sub="",
            )
        ],
    )
    assert "secreto" not in path.read_text()


UPLINK_YAML = VALID_YAML + """
uplink:
  enabled: true
  topic_prefix: vita/hub
"""


def _certs(tmp_path: Path) -> dict[str, str]:
    """Crea los tres ficheros de certificado y devuelve el entorno que los apunta."""
    env = dict(ENV)
    for var, name in (
        ("VITAHUB_IOT_CA", "AmazonRootCA1.pem"),
        ("VITAHUB_IOT_CERT", "certificate.pem.crt"),
        ("VITAHUB_IOT_KEY", "private.pem.key"),
    ):
        path = tmp_path / name
        path.write_text("x")
        env[var] = str(path)
    env["VITAHUB_IOT_ENDPOINT"] = "abc123-ats.iot.eu-west-1.amazonaws.com"
    return env


def test_config_without_uplink_section_keeps_it_disabled(tmp_path):
    # Un hub ya instalado, con un hub.yaml anterior a este slice, arranca igual.
    cfg = load_config(_write(tmp_path, VALID_YAML), ENV)
    assert cfg.uplink.enabled is False


def test_enabled_uplink_without_endpoint_raises(tmp_path):
    with pytest.raises(ConfigError, match="VITAHUB_IOT_ENDPOINT"):
        load_config(_write(tmp_path, UPLINK_YAML), ENV)


def test_enabled_uplink_without_certificate_file_raises(tmp_path):
    # Las tres VITAHUB_IOT_* deben apuntar explícitamente a rutas inexistentes
    # bajo tmp_path: sin esto, el test caía en los defaults de /data/certs/ y
    # dependía de que ese directorio NO existiera en la máquina que corre el
    # test. En un Jetson —el hardware objetivo— sí existe, porque ahí es
    # donde provision-hub.sh siembra los certificados de verdad.
    env = dict(ENV)
    env["VITAHUB_IOT_ENDPOINT"] = "abc123-ats.iot.eu-west-1.amazonaws.com"
    missing = tmp_path / "no-such-certs"
    env["VITAHUB_IOT_CA"] = str(missing / "AmazonRootCA1.pem")
    env["VITAHUB_IOT_CERT"] = str(missing / "certificate.pem.crt")
    env["VITAHUB_IOT_KEY"] = str(missing / "private.pem.key")
    with pytest.raises(ConfigError, match="no existe el fichero de certificado"):
        load_config(_write(tmp_path, UPLINK_YAML), env)


def test_enabled_uplink_loads_endpoint_and_paths(tmp_path):
    env = _certs(tmp_path)
    cfg = load_config(_write(tmp_path, UPLINK_YAML), env)
    assert cfg.uplink.enabled is True
    assert cfg.uplink.topic_prefix == "vita/hub"
    assert cfg.uplink.endpoint == env["VITAHUB_IOT_ENDPOINT"]
    assert cfg.uplink.key_path == env["VITAHUB_IOT_KEY"]


def test_empty_topic_prefix_is_rejected(tmp_path):
    yaml_text = VALID_YAML + "\nuplink:\n  enabled: true\n  topic_prefix: ''\n"
    with pytest.raises(ConfigError, match="topic_prefix"):
        load_config(_write(tmp_path, yaml_text), _certs(tmp_path))


def test_null_topic_prefix_is_rejected(tmp_path):
    # `topic_prefix:` sin valor (errata trivial en YAML) es `None`, y antes
    # `str(None)` producía la cadena "None" — no vacía, así que colaba la
    # guarda de "no vacío" y el topic quedaba `None/<hub_id>/events`.
    yaml_text = VALID_YAML + "\nuplink:\n  topic_prefix:\n"
    with pytest.raises(ConfigError, match="topic_prefix"):
        load_config(_write(tmp_path, yaml_text), ENV)


def test_save_cameras_preserves_the_uplink_section(tmp_path):
    # El hub reescribe hub.yaml en cada descubrimiento. Si save_cameras se
    # comiera la sección uplink, el hogar dejaría de reportar tras el primer
    # rescan y nadie relacionaría una cosa con la otra.
    path = _write(tmp_path, UPLINK_YAML)
    save_cameras(path, [])
    assert "uplink:" in path.read_text()
    assert "enabled: true" in path.read_text()


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


def test_identity_defaults_off(tmp_path):
    cfg = load_config(_write(tmp_path, "hub_id: hub-1\n"), ENV)
    assert cfg.inference.identity.enabled is False
    assert cfg.inference.identity.match_threshold == 0.4


def test_identity_requires_fall_enabled(tmp_path):
    path = _write(
        tmp_path,
        "hub_id: hub-1\ninference:\n  detector: person_pose\n  identity:\n    enabled: true\n",
    )
    with pytest.raises(ConfigError, match="inference.identity.enabled requiere"):
        load_config(path, ENV)


def test_identity_valid_when_fall_enabled(tmp_path):
    path = _write(
        tmp_path,
        "hub_id: hub-1\ninference:\n  detector: person_pose\n  fall:\n    enabled: true\n  identity:\n    enabled: true\n    match_threshold: 0.5\n",
    )
    cfg = load_config(path, ENV)
    assert cfg.inference.identity.enabled is True
    assert cfg.inference.identity.match_threshold == 0.5


def test_identity_threshold_out_of_range(tmp_path):
    path = _write(
        tmp_path,
        "hub_id: hub-1\ninference:\n  identity:\n    match_threshold: 1.5\n",
    )
    with pytest.raises(ConfigError, match="match_threshold"):
        load_config(path, ENV)


def test_identity_threshold_not_numeric(tmp_path):
    path = _write(
        tmp_path,
        "hub_id: hub-1\ninference:\n  identity:\n    match_threshold: alto\n",
    )
    with pytest.raises(ConfigError, match="match_threshold"):
        load_config(path, ENV)


from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_LIVE_PEM = (
    ec.generate_private_key(ec.SECP256R1())
    .public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)


def _live_yaml(live_block: str, uplink: bool = True) -> str:
    up = "uplink:\n  enabled: true\n" if uplink else ""
    return VALID_YAML + up + live_block


def _live_block(**over: str) -> str:
    pem = "\n".join("      " + line for line in _LIVE_PEM.strip().splitlines())
    fields = {"enabled": "true", "max_viewers": "3", "max_fps": "15"} | over
    body = "".join(f"  {k}: {v}\n" for k, v in fields.items())
    return f"live:\n{body}  session_keys:\n    k1: |\n{pem}\n"


def _live_env(tmp_path: Path) -> dict[str, str]:
    env = _certs(tmp_path)
    env["VITAHUB_IOT_CREDENTIALS_ENDPOINT"] = "abc.credentials.iot.eu-west-1.amazonaws.com"
    env["VITAHUB_LIVE_ROLE_ALIAS"] = "vita-dev-hub-live"
    env["VITAHUB_LIVE_CHANNEL_PREFIX"] = "vita-dev-live"
    return env


def test_live_disabled_by_default(tmp_path):
    cfg = load_config(_write(tmp_path, VALID_YAML), ENV)
    assert cfg.live.enabled is False


def test_live_enabled_loads(tmp_path):
    cfg = load_config(_write(tmp_path, _live_yaml(_live_block())), _live_env(tmp_path))
    assert cfg.live.enabled is True
    assert cfg.live.max_viewers == 3
    assert cfg.live.max_fps == 15.0
    assert cfg.live.region == "eu-west-1"
    assert list(cfg.live.session_keys) == ["k1"]
    assert cfg.live.channel_prefix == "vita-dev-live"


def test_live_requires_uplink(tmp_path):
    # El hub saca las credenciales AWS del certificado del uplink: sin uplink
    # no hay certificado sembrado y el directo no podría autenticarse.
    with pytest.raises(ConfigError, match="uplink"):
        load_config(_write(tmp_path, _live_yaml(_live_block(), uplink=False)), _live_env(tmp_path))


@pytest.mark.parametrize(
    "var",
    ["VITAHUB_IOT_CREDENTIALS_ENDPOINT", "VITAHUB_LIVE_ROLE_ALIAS", "VITAHUB_LIVE_CHANNEL_PREFIX"],
)
def test_live_requires_env(tmp_path, var):
    env = _live_env(tmp_path)
    del env[var]
    with pytest.raises(ConfigError, match=var):
        load_config(_write(tmp_path, _live_yaml(_live_block())), env)


def test_live_requires_session_keys(tmp_path):
    block = "live:\n  enabled: true\n"
    with pytest.raises(ConfigError, match="session_keys"):
        load_config(_write(tmp_path, _live_yaml(block)), _live_env(tmp_path))


def test_live_rejects_bad_pem(tmp_path):
    block = "live:\n  enabled: true\n  session_keys:\n    k1: basura\n"
    with pytest.raises(ConfigError, match="k1"):
        load_config(_write(tmp_path, _live_yaml(block)), _live_env(tmp_path))


@pytest.mark.parametrize(
    "field,value", [("max_viewers", "0"), ("max_viewers", "11"), ("max_fps", "0"), ("max_fps", "31"),
                    ("max_viewers", "tres")],
)
def test_live_rejects_out_of_range(tmp_path, field, value):
    with pytest.raises(ConfigError, match=field):
        load_config(_write(tmp_path, _live_yaml(_live_block(**{field: value}))), _live_env(tmp_path))


def test_live_rejects_colon_in_hub_id(tmp_path):
    # IoT admite ':' en el nombre del thing, KVS no en el del canal: un hub así
    # nunca podría abrir su canal. Mejor saberlo en la instalación.
    text = _live_yaml(_live_block()).replace("hub_id: hub-3f9a", "hub_id: hub:3f9a")
    with pytest.raises(ConfigError, match="':'"):
        load_config(_write(tmp_path, text), _live_env(tmp_path))


def test_live_section_must_be_mapping(tmp_path):
    with pytest.raises(ConfigError, match="live"):
        load_config(_write(tmp_path, VALID_YAML + "live: [1]\n"), ENV)
