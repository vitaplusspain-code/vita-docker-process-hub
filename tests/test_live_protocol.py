import json

import pytest

from vitahub.live.protocol import (
    AuthMsg,
    SelectMsg,
    closed_message,
    parse_client_message,
    ready_message,
)


def test_parses_auth():
    assert parse_client_message('{"type":"auth","token":"t"}') == AuthMsg("t")


def test_parses_select_from_bytes():
    assert parse_client_message(b'{"type":"select","camera_id":"onvif-a"}') == SelectMsg("onvif-a")


@pytest.mark.parametrize(
    "raw",
    [
        "no es json",
        "[]",
        '{"type":"auth"}',
        '{"type":"auth","token":5}',
        '{"type":"select","camera_id":""}',
        '{"type":"desconocido"}',
        b"\xff\xfe",
    ],
)
def test_garbage_returns_none(raw):
    # Un viewer con un bug (o malicioso) no puede tumbar el peer con un mensaje raro.
    assert parse_client_message(raw) is None


def test_ready_lists_cameras():
    msg = json.loads(ready_message([("onvif-a", "salón")]))
    assert msg == {"type": "ready", "cameras": [{"id": "onvif-a", "name": "salón"}]}


def test_closed_message():
    assert json.loads(closed_message("replaced")) == {"type": "closed", "reason": "replaced"}


def test_closed_rejects_unknown_reason():
    # El frontend traduce `reason` a texto: una errata aquí sería un mensaje vacío en la app.
    with pytest.raises(ValueError):
        closed_message("expirado")
