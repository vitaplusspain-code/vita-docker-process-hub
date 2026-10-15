from datetime import UTC, datetime

from vitahub.live.events import live_view_ended, live_view_started
from vitahub.live.token import SessionClaims

_CLAIMS = SessionClaims(sid="s-1", sub="acc-1", hub="hub-1", exp=1600.0)
_NOW = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)


def test_started_event():
    ev = live_view_started("hub-1", _CLAIMS, "onvif-a", "salón", _NOW)
    assert ev.type == "live_view_started"
    assert ev.severity == "info"
    assert ev.camera_id == "onvif-a"
    assert ev.camera_name == "salón"
    assert ev.timestamp == _NOW.isoformat()
    assert ev.payload == {"sid": "s-1", "account_id": "acc-1", "camera_id": "onvif-a"}


def test_ended_event_without_camera():
    # Un viewer que se autentica y se va sin elegir cámara: camera_id vacío (spec §4.5).
    ev = live_view_ended("hub-1", _CLAIMS, "", "", "expired", 12.34, _NOW)
    assert ev.type == "live_view_ended"
    assert ev.camera_id == ""
    assert ev.payload == {"sid": "s-1", "account_id": "acc-1", "reason": "expired", "seconds": 12}
