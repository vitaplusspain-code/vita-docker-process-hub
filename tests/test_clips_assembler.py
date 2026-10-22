from __future__ import annotations

import hashlib

import av
import pytest

from tests.clip_media import snapshot_from_file, write_test_video
from vitahub.clips.assembler import assemble_mp4
from vitahub.clips.buffer import BufferedPacket, Snapshot


def test_assemble_produces_playable_mp4(tmp_path):
    src = tmp_path / "src.mkv"
    write_test_video(src)
    snap = snapshot_from_file(src)
    out = tmp_path / "clip.mp4"

    clip = assemble_mp4(snap, out)

    assert clip.path == out
    assert clip.sha256 == hashlib.sha256(out.read_bytes()).hexdigest()
    assert clip.duration_s == pytest.approx(4.9)
    with av.open(str(out)) as c:
        assert len(c.streams.video) == 1
        assert sum(1 for _ in c.decode(video=0)) == 50


def test_assemble_does_not_mutate_snapshot_packets(tmp_path):
    src = tmp_path / "src.mkv"
    write_test_video(src, frames=20)
    # Recorte que no empieza en pts 0: hay que rebasar sin tocar el original.
    snap = snapshot_from_file(src)
    snap = Snapshot(snap.template, snap.packets[10:])
    before = [(p.packet.pts, p.packet.dts) for p in snap.packets]

    assemble_mp4(snap, tmp_path / "clip.mp4")

    assert [(p.packet.pts, p.packet.dts) for p in snap.packets] == before
    with av.open(str(tmp_path / "clip.mp4")) as c:
        assert sum(1 for _ in c.decode(video=0)) == 10
        assert c.streams.video[0].start_time in (0, None)


def test_assemble_rejects_empty_snapshot(tmp_path):
    with pytest.raises(ValueError):
        assemble_mp4(Snapshot(template=object(), packets=[]), tmp_path / "x.mp4")
    assert not (tmp_path / "x.mp4").exists()


def test_assemble_cleans_partial_file_on_error(tmp_path):
    src = tmp_path / "src.mkv"
    write_test_video(src, frames=10)
    snap = snapshot_from_file(src)
    bad = Snapshot(snap.template, [BufferedPacket(0.0, True, object())])
    out = tmp_path / "clip.mp4"

    with pytest.raises(Exception):  # noqa: B017 — cualquier fallo de PyAV/tipos
        assemble_mp4(bad, out)
    assert not out.exists()
