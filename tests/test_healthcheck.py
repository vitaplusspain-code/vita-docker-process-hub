import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "healthcheck", Path(__file__).resolve().parents[1] / "scripts" / "healthcheck.py"
)
healthcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthcheck)  # type: ignore[union-attr]


def test_fresh_heartbeat_is_healthy(tmp_path):
    hb = tmp_path / "heartbeat"
    hb.write_text("0")
    assert healthcheck.is_healthy(hb, now=30.0, max_age_s=60.0) is True


def test_stale_heartbeat_is_unhealthy(tmp_path):
    hb = tmp_path / "heartbeat"
    hb.write_text("0")
    assert healthcheck.is_healthy(hb, now=120.0, max_age_s=60.0) is False


def test_missing_heartbeat_is_unhealthy(tmp_path):
    assert healthcheck.is_healthy(tmp_path / "nope", now=1.0, max_age_s=60.0) is False
