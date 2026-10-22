from __future__ import annotations

import threading
import time
from pathlib import Path

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
