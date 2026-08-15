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
