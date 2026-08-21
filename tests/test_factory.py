import ssl

import pytest

from vitahub.config import (
    ConfigError,
    Credentials,
    DiscoveryConfig,
    HubConfig,
    InferenceConfig,
    UplinkConfig,
)
from vitahub.factory import build_detector, build_sink
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


def _hub_config(uplink: UplinkConfig) -> HubConfig:
    return HubConfig(
        hub_id="hub-casa-lopez",
        discovery=DiscoveryConfig(),
        inference=InferenceConfig(),
        cameras=[],
        credentials=Credentials(onvif_user="admin", onvif_password="x"),
        uplink=uplink,
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
