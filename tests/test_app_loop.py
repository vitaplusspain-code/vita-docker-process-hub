import threading
from unittest.mock import patch

from vitahub.app import _rescan_loop, _start_control_server_safe
from vitahub.rescan import RescanResult


class _CountingService:
    def __init__(self, stop):
        self.calls = 0
        self._stop = stop

    def run_once(self):
        self.calls += 1
        self._stop.set()  # un solo ciclo y salimos
        return RescanResult(status="ok")


def test_rescan_loop_runs_until_stopped():
    stop = threading.Event()
    service = _CountingService(stop)
    _rescan_loop(service, 0.01, stop)
    assert service.calls == 1


def test_rescan_loop_exits_immediately_when_already_stopped():
    """Espera sobre el event, no con sleep: si durmiera, este test colgaría."""
    stop = threading.Event()
    stop.set()
    service = _CountingService(stop)
    _rescan_loop(service, 3600.0, stop)
    assert service.calls == 0


def test_start_control_server_safe_swallows_os_error_when_port_busy():
    """Un puerto ocupado por otro servicio del Jetson no debe tumbar el arranque."""
    with patch("vitahub.app.start_control_server", side_effect=OSError("Address already in use")):
        result = _start_control_server_safe(service=object(), token="x", port=8787)
    assert result is None
