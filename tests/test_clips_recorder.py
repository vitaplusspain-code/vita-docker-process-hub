from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

import pytest

from tests.clip_media import write_test_video
from vitahub.clips.buffer import PacketBuffer
from vitahub.clips.recorder import record


def test_record_llena_el_buffer_y_para_con_stop(tmp_path: Path) -> None:
    path = tmp_path / "v.mkv"
    write_test_video(path)
    buffer = PacketBuffer()
    stop = threading.Event()
    thread = threading.Thread(
        target=record, args=(str(path), buffer, stop), kwargs={"format": None, "options": {}}, daemon=True
    )
    thread.start()

    # Tras el EOF el bucle reabre el fichero y hace reset() del buffer, así que
    # el recuento fluctúa: se espera a verlo completo en lugar de a una hora fija.
    deadline = time.monotonic() + 5.0
    snap = None
    while time.monotonic() < deadline:
        snap = buffer.snapshot(0.0, float("inf"))
        if snap is not None and len(snap.packets) == 50:
            break
        time.sleep(0.02)
    assert snap is not None and len(snap.packets) == 50
    assert snap.packets[0].keyframe

    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_record_no_filtra_credenciales_en_logs(caplog: pytest.LogCaptureFixture) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    uri = f"rtsp://user:secretpw@127.0.0.1:{port}/x"
    buffer = PacketBuffer()
    stop = threading.Event()
    with caplog.at_level(logging.DEBUG):
        thread = threading.Thread(target=record, args=(uri, buffer, stop), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not caplog.records:
            time.sleep(0.02)
        assert caplog.records  # falló al menos una vez
        stop.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()
    for r in caplog.records:
        assert "secretpw" not in r.getMessage()
        assert "user:" not in r.getMessage()
        assert "secretpw" not in (r.exc_text or "")
        assert "user:" not in (r.exc_text or "")
