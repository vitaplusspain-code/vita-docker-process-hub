import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from vitahub.app import _rescan_loop, _shutdown, _start_control_server_safe
from vitahub.control import AdminContext
from vitahub.rescan import RescanResult


def _fake_admin_context() -> AdminContext:
    # No lo usan estos tests (patchean start_control_server entero); solo
    # hace falta que satisfaga la firma de _start_control_server_safe.
    return AdminContext(
        faces_dir=Path("/data/faces"),
        config_path=Path("/data/hub.yaml"),
        env={},
        engine_factory=lambda: (_ for _ in ()).throw(NotImplementedError()),
        request_shutdown=lambda: None,
        hub_id="hub-test",
    )


def _cfg():
    return MagicMock(clips=MagicMock(enabled=False, cameras=[]), cameras=[])


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
    _rescan_loop(service, _cfg(), 0.01, stop)
    assert service.calls == 1


def test_rescan_loop_exits_immediately_when_already_stopped():
    """Espera sobre el event, no con sleep: si durmiera, este test colgaría."""
    stop = threading.Event()
    stop.set()
    service = _CountingService(stop)
    _rescan_loop(service, _cfg(), 3600.0, stop)
    assert service.calls == 0


def test_start_control_server_safe_swallows_os_error_when_port_busy():
    """Un puerto ocupado por otro servicio del Jetson no debe tumbar el arranque."""
    with patch("vitahub.app.start_control_server", side_effect=OSError("Address already in use")):
        result = _start_control_server_safe(
            service=object(), token="x", port=8787, admin=_fake_admin_context()
        )
    assert result is None


def test_start_control_server_safe_logs_warning_without_leaking_token(caplog):
    """El fallo debe quedar explicado en el log (no tragado en silencio) y sin
    filtrar el token de admin en el mensaje."""
    secret_token = "el-token-secreto-9f3"
    with (
        patch(
            "vitahub.app.start_control_server",
            side_effect=OSError("Address already in use"),
        ),
        caplog.at_level(logging.WARNING, logger="app"),
    ):
        result = _start_control_server_safe(
            service=object(), token=secret_token, port=8787, admin=_fake_admin_context()
        )

    assert result is None
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "control HTTP" in message
    assert "no disponible" in message
    assert "8787" in message
    assert secret_token not in message


def test_emit_all_survives_a_failing_sink():
    """Un fallo al emitir no puede tumbar el hilo de la cámara."""
    from vitahub.app import _emit_all

    class _BoomSink:
        def __init__(self):
            self.seen = []

        def emit(self, event):
            self.seen.append(event)
            raise OSError("stdout roto")

    sink = _BoomSink()
    _emit_all(sink, ["evento-1", "evento-2"])
    assert len(sink.seen) == 2  # siguió con el segundo pese al fallo del primero


def test_shutdown_stops_live_before_supervisor_and_sink():
    # Los live_view_ended de los viewers que se cortan salen por el sink:
    # cerrarlo antes perdería la auditoría del apagado.
    order: list[str] = []
    live = MagicMock(stop=lambda **_: order.append("live"))
    supervisor = MagicMock(stop_all=lambda **_: order.append("supervisor"))
    sink = MagicMock(close=lambda: order.append("sink"))
    _shutdown(None, live, supervisor, sink)
    assert order == ["live", "supervisor", "sink"]


def test_shutdown_stops_clips_after_supervisor_and_before_sink():
    # Los workers dejan de emitir antes de parar el tick del disparador, y el
    # tick para antes de sink.close(): close() no puede competir con un tick
    # que esté cerrando la misma ventana.
    order: list[str] = []
    supervisor = MagicMock(stop_all=lambda **_: order.append("supervisor"))
    clips = MagicMock(stop=lambda **_: order.append("clips"))
    sink = MagicMock(close=lambda: order.append("sink"))
    _shutdown(None, None, supervisor, sink, clips)
    assert order == ["supervisor", "clips", "sink"]


def test_shutdown_closes_sink_even_if_clips_stop_fails():
    sink = MagicMock()
    clips = MagicMock()
    clips.stop.side_effect = RuntimeError("boom")
    try:
        _shutdown(None, None, MagicMock(), sink, clips)
    except RuntimeError:
        pass
    sink.close.assert_called_once()


def test_rescan_loop_warns_missing_clip_cameras():
    stop = threading.Event()
    service = _CountingService(stop)
    cfg = _cfg()
    with patch("vitahub.app.warn_missing_clip_cameras") as warn:
        _rescan_loop(service, cfg, 0.01, stop)
    warn.assert_called_once_with(cfg)


def test_shutdown_continues_even_if_live_stop_fails():
    sink = MagicMock()
    supervisor = MagicMock()
    live = MagicMock()
    live.stop.side_effect = RuntimeError("boom")
    try:
        _shutdown(None, live, supervisor, sink)
    except RuntimeError:
        pass
    supervisor.stop_all.assert_called_once()
    sink.close.assert_called_once()


def test_shutdown_without_live():
    sink = MagicMock()
    _shutdown(None, None, MagicMock(), sink)
    sink.close.assert_called_once()
