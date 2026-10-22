from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import vitahub.clips.recorder as recorder_mod
from tests.clip_media import write_test_video
from vitahub.clips.buffer import PacketBuffer, Snapshot
from vitahub.clips.recorder import record

_REPO = Path(__file__).resolve().parent.parent


class _SpyBuffer(PacketBuffer):
    """Guarda lo que había en el buffer justo antes de cada clear()."""

    def __init__(self) -> None:
        super().__init__()
        self.before_clear: list[Snapshot | None] = []

    def clear(self, template: object) -> None:
        self.before_clear.append(self.snapshot(0.0, float("inf")))
        super().clear(template)


def test_record_llena_el_buffer_y_lo_vacia_al_desconectar(tmp_path: Path) -> None:
    path = tmp_path / "v.mkv"
    write_test_video(path)
    buffer = _SpyBuffer()
    stop = threading.Event()
    thread = threading.Thread(
        target=record, args=(str(path), buffer, stop), kwargs={"format": None, "options": {}}, daemon=True
    )
    thread.start()

    # El EOF del fichero es una desconexión: el grabador vacía el buffer, que
    # el spy fotografía justo antes.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not buffer.before_clear:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    snap = buffer.before_clear[0]
    assert snap is not None and len(snap.packets) == 50
    assert snap.packets[0].keyframe
    assert buffer.snapshot(0.0, float("inf")) is None


def test_grabador_parado_durante_open_no_sustituye_la_plantilla(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v.mkv"
    write_test_video(path)
    stop = threading.Event()
    real_open = recorder_mod.av.open

    def slow_open(*args: object, **kwargs: object) -> object:
        # El supervisor para este grabador (y arranca otro) mientras abre.
        container = real_open(*args, **kwargs)  # type: ignore[call-overload]
        stop.set()
        return container

    monkeypatch.setattr(recorder_mod.av, "open", slow_open)
    current = object()
    buffer = PacketBuffer()
    buffer.reset(current)
    record(str(path), buffer, stop, format=None, options={})
    snap = buffer.snapshot(0.0, float("inf"))
    assert snap is None  # sin paquetes, pero la plantilla sigue siendo la del nuevo
    buffer.append(recorder_mod.BufferedPacket(t=0.0, keyframe=True, packet=object()))
    snap = buffer.snapshot(0.0, float("inf"))
    assert snap is not None and snap.template is current


# Un segfault no es una excepción: el montaje se ejecuta en un subproceso.
_ASSEMBLE_AFTER_DISCONNECT = """
import gc, sys, threading
from pathlib import Path
from tests.clip_media import write_test_video
from vitahub.clips.assembler import assemble_mp4
from vitahub.clips.buffer import PacketBuffer
from vitahub.clips.recorder import record

d = Path(sys.argv[1])
write_test_video(d / "v.mkv")
stop = threading.Event()
taken = []

class Buf(PacketBuffer):
    def append(self, p):
        super().append(p)
        if not taken:
            snap = self.snapshot(0.0, float("inf"))
            if snap is not None and len(snap.packets) == 50:
                taken.append(snap)
                stop.set()

buf = Buf()
t = threading.Thread(target=record, args=(str(d / "v.mkv"), buf, stop),
                     kwargs={"format": None, "options": {}})
t.start()
t.join(timeout=5.0)
assert not t.is_alive() and taken
gc.collect()
clip = assemble_mp4(taken[0], d / "o.mp4")
print("ok", clip.duration_s)
"""


def test_snapshot_sigue_montable_tras_desconectar_el_grabador(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _ASSEMBLE_AFTER_DISCONNECT, str(tmp_path)],
        check=False,
        cwd=_REPO,
        env={**os.environ, "PYTHONPATH": str(_REPO / "src")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr[-2000:])
    assert proc.stdout.startswith("ok")


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
