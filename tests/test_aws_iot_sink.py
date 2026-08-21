import json
import os
import stat

from vitahub.models import Event
from vitahub.sinks.aws_iot import AwsIotSink, topic_for, warn_if_key_is_exposed


def _event() -> Event:
    return Event(
        hub_id="hub-1",
        camera_id="onvif-abc",
        camera_name="salon",
        type="person_detected",
        severity="info",
        timestamp="2026-08-21T10:00:00+00:00",
        payload={"person_count": 1},
    )


class _FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[tuple[str, str, int]] = []
        self.stopped = False
        self.disconnected = False
        self._fail = fail

    def publish(self, topic: str, payload: str, qos: int) -> object:
        if self._fail:
            raise RuntimeError("broker caído")
        self.published.append((topic, payload, qos))
        return None

    def loop_stop(self) -> None:
        self.stopped = True

    def disconnect(self) -> None:
        self.disconnected = True


def test_topic_for_composes_the_contract_topic():
    assert topic_for("vita/hub", "hub-casa-lopez") == "vita/hub/hub-casa-lopez/events"


def test_emit_publishes_the_exact_event_json():
    client = _FakeClient()
    event = _event()
    AwsIotSink(client, "vita/hub/hub-1/events").emit(event)
    topic, payload, qos = client.published[0]
    assert topic == "vita/hub/hub-1/events"
    # El contrato del spec: por MQTT sale byte a byte lo mismo que por stdout.
    assert payload == event.to_json()
    assert json.loads(payload)["type"] == "person_detected"
    assert qos == 0


def test_a_publish_that_raises_does_not_propagate():
    # Un broker caído no puede tumbar el hilo de una cámara.
    AwsIotSink(_FakeClient(fail=True), "vita/hub/hub-1/events").emit(_event())


def test_close_stops_the_loop_and_disconnects():
    client = _FakeClient()
    AwsIotSink(client, "vita/hub/hub-1/events").close()
    assert client.stopped
    assert client.disconnected


def test_warns_when_the_private_key_is_readable_by_others(tmp_path, caplog):
    key = tmp_path / "private.pem.key"
    key.write_text("clave")
    os.chmod(key, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    warn_if_key_is_exposed(key)
    assert "legible por otros" in caplog.text


def test_does_not_warn_when_the_private_key_is_locked_down(tmp_path, caplog):
    key = tmp_path / "private.pem.key"
    key.write_text("clave")
    os.chmod(key, stat.S_IRUSR | stat.S_IWUSR)
    warn_if_key_is_exposed(key)
    assert caplog.text == ""
