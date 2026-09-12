import io
import json
import sys

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


def test_emit_uses_stream_captured_at_construction(monkeypatch, capsys):
    # El engine de identidad carga sus pesos de forma perezosa (primera foto
    # subida, hilos de cámara ya vivos) y redirige sys.stdout a stderr
    # mientras dura la carga. Si el sink resolviera sys.stdout en emit() en
    # vez de en __init__, un evento emitido durante esa carga se perdería
    # (iría al stderr redirigido, no al flujo JSON-lines real).
    sink = StdoutJsonSink()
    hijacked = io.StringIO()
    monkeypatch.setattr(sys, "stdout", hijacked)
    sink.emit(_event())
    assert hijacked.getvalue() == ""
    out = capsys.readouterr().out
    assert json.loads(out.strip())["type"] == "person_detected"
