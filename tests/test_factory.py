import ssl

import pytest

from vitahub.config import (
    ConfigError,
    Credentials,
    DiscoveryConfig,
    HubConfig,
    IdentityConfig,
    InferenceConfig,
    UplinkConfig,
)
from vitahub.factory import build_detector, build_face_identifier, build_sink
from vitahub.inference.person_pose_yolo import PosePersonDetector
from vitahub.inference.person_yolo import PersonDetector
from vitahub.inference.stub import StubDetector
from vitahub.sinks.aws_iot import AwsIotSink
from vitahub.sinks.fanout import FanoutSink
from vitahub.sinks.stdout_json import StdoutJsonSink


def test_build_stub_detector():
    det = build_detector(InferenceConfig(detector="stub"), weights_path="unused.pt")
    assert isinstance(det, StubDetector)


def test_unknown_detector_raises():
    import pytest

    with pytest.raises(ValueError, match="desconocido"):
        build_detector(InferenceConfig(detector="nope"), weights_path="unused.pt")


def _hub_config(
    uplink: UplinkConfig | None = None, identity: IdentityConfig | None = None
) -> HubConfig:
    return HubConfig(
        hub_id="hub-casa-lopez",
        discovery=DiscoveryConfig(),
        inference=InferenceConfig(identity=identity or IdentityConfig()),
        cameras=[],
        credentials=Credentials(onvif_user="admin", onvif_password="x"),
        uplink=uplink or UplinkConfig(enabled=False),
    )


def test_build_sink_without_uplink_is_stdout_only():
    sink = build_sink(_hub_config(UplinkConfig(enabled=False)))
    assert isinstance(sink, StdoutJsonSink)


def test_build_sink_with_uplink_fans_out_to_stdout_and_aws(monkeypatch):
    built: dict[str, object] = {}

    def _fake_build_client(hub_id, endpoint, ca, cert, key):
        built["hub_id"] = hub_id
        built["endpoint"] = endpoint
        return object()

    monkeypatch.setattr("vitahub.factory.build_client", _fake_build_client)
    sink = build_sink(
        _hub_config(
            UplinkConfig(
                enabled=True,
                topic_prefix="vita/hub",
                endpoint="abc-ats.iot.eu-west-1.amazonaws.com",
                ca_path="/data/certs/AmazonRootCA1.pem",
                cert_path="/data/certs/certificate.pem.crt",
                key_path="/data/certs/private.pem.key",
            )
        )
    )
    assert isinstance(sink, FanoutSink)
    kinds = [type(s) for s in sink._sinks]
    assert StdoutJsonSink in kinds
    assert AwsIotSink in kinds
    assert built["hub_id"] == "hub-casa-lopez"


def test_build_sink_wraps_a_broken_client_as_config_error(monkeypatch):
    # load_config solo comprueba que los ficheros existen, no que sean PEM
    # válidos: un truncado revienta en client.tls_set() con ssl.SSLError.
    # build_sink debe traducirlo a ConfigError con un mensaje de instalación,
    # no dejarlo escapar como traceback crudo.
    def _broken_build_client(hub_id, endpoint, ca, cert, key):
        raise ssl.SSLError("certificado corrupto")

    monkeypatch.setattr("vitahub.factory.build_client", _broken_build_client)
    with pytest.raises(ConfigError, match="certificado corrupto"):
        build_sink(
            _hub_config(
                UplinkConfig(
                    enabled=True,
                    topic_prefix="vita/hub",
                    endpoint="abc-ats.iot.eu-west-1.amazonaws.com",
                    ca_path="/data/certs/AmazonRootCA1.pem",
                    cert_path="/data/certs/certificate.pem.crt",
                    key_path="/data/certs/private.pem.key",
                )
            )
        )


def test_build_sink_uses_the_contract_topic(monkeypatch):
    monkeypatch.setattr("vitahub.factory.build_client", lambda **kw: object())
    sink = build_sink(
        _hub_config(
            UplinkConfig(
                enabled=True,
                topic_prefix="vita/hub",
                endpoint="abc-ats.iot.eu-west-1.amazonaws.com",
                ca_path="a",
                cert_path="b",
                key_path="c",
            )
        )
    )
    aws = next(s for s in sink._sinks if isinstance(s, AwsIotSink))
    assert aws._topic == "vita/hub/hub-casa-lopez/events"


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


def test_build_face_identifier_disabled_returns_none(tmp_path):
    cfg = _hub_config(identity=IdentityConfig(enabled=False))
    assert build_face_identifier(cfg, {}) is None


def test_build_face_identifier_missing_weights_raises(tmp_path):
    cfg = _hub_config(identity=IdentityConfig(enabled=True))
    env = {
        "VITAHUB_FACE_WEIGHTS": str(tmp_path / "no-existe"),
        "VITAHUB_FACES_DIR": str(tmp_path),
    }
    with pytest.raises(ConfigError, match="modelos de reconocimiento facial"):
        build_face_identifier(cfg, env)


def test_build_face_identifier_missing_faces_dir_raises(tmp_path):
    weights = tmp_path / "insightface" / "models" / "buffalo_s"
    weights.mkdir(parents=True)
    (weights / "det.onnx").touch()
    cfg = _hub_config(identity=IdentityConfig(enabled=True))
    env = {
        "VITAHUB_FACE_WEIGHTS": str(tmp_path / "insightface"),
        "VITAHUB_FACES_DIR": str(tmp_path / "faces-no-existe"),
    }
    with pytest.raises(ConfigError, match="no existe"):
        build_face_identifier(cfg, env)


def test_admin_engine_factory_missing_weights_raises_config_error(tmp_path):
    from vitahub.factory import build_admin_engine_factory

    factory = build_admin_engine_factory(
        {"VITAHUB_FACE_WEIGHTS": str(tmp_path / "no-existe")}
    )
    # Construir la factory no toca disco; llamar sí, y falla legible.
    with pytest.raises(ConfigError):
        factory()
