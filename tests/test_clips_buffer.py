from __future__ import annotations

from vitahub.clips.buffer import BufferedPacket, PacketBuffer


def _stream(
    start: float, end: float, *, first_keyframe_at: float | None = None
) -> list[BufferedPacket]:
    """Paquetes sintéticos: uno cada 0.5 s, keyframe cada 2 s."""
    out: list[BufferedPacket] = []
    n = round(start / 0.5)
    while n * 0.5 < end:
        t = n * 0.5
        key = t % 2.0 == 0.0 and (first_keyframe_at is None or t >= first_keyframe_at)
        out.append(BufferedPacket(t=t, keyframe=key, packet=object()))
        n += 1
    return out


def _filled(max_age_s: float, end: float) -> PacketBuffer:
    buf = PacketBuffer(max_age_s=max_age_s)
    buf.reset(object())
    for p in _stream(0.0, end):
        buf.append(p)
    return buf


def test_ignores_until_first_keyframe() -> None:
    buf = PacketBuffer()
    buf.reset(object())
    # Los paquetes a 0.5, 1.0 y 1.5 no son keyframe y llegan antes del primero.
    for p in _stream(0.5, 4.0):
        buf.append(p)
    snap = buf.snapshot(0.0, 1e9)
    assert snap is not None
    assert snap.packets[0].t == 2.0
    assert snap.packets[0].keyframe


def test_trims_to_max_age_starting_on_keyframe() -> None:
    buf = _filled(45.0, 60.0)
    snap = buf.snapshot(0.0, 1e9)
    assert snap is not None
    assert snap.packets[0].keyframe
    assert snap.packets[0].t >= 60 - 45 - 2
    assert snap.packets[0].t <= 60 - 45


def test_trim_keeps_everything_when_no_keyframe_within_limit() -> None:
    buf = PacketBuffer(max_age_s=1.0)
    buf.reset(object())
    for p in _stream(0.0, 5.0):
        buf.append(p)
    snap = buf.snapshot(0.0, 1e9)
    assert snap is not None
    assert snap.packets[0].keyframe


def test_snapshot_starts_on_keyframe_before_start() -> None:
    buf = _filled(45.0, 60.0)
    snap = buf.snapshot(31.0, 41.0)
    assert snap is not None
    assert snap.packets[0].t == 30.0
    assert snap.packets[0].keyframe
    assert snap.packets[-1].t <= 41.0


def test_snapshot_none_when_empty_or_after_end() -> None:
    buf = PacketBuffer()
    assert buf.snapshot(0.0, 10.0) is None
    buf.append(BufferedPacket(t=0.0, keyframe=True, packet=object()))
    assert buf.snapshot(0.0, 10.0) is None  # sin plantilla
    buf.reset(object())
    assert buf.snapshot(0.0, 10.0) is None  # vacío tras reset
    for p in _stream(10.0, 14.0):
        buf.append(p)
    assert buf.snapshot(0.0, 5.0) is None  # primer paquete posterior a end


def test_snapshot_without_earlier_keyframe_starts_at_first() -> None:
    buf = PacketBuffer()
    buf.reset(object())
    for p in _stream(10.0, 20.0):
        buf.append(p)
    snap = buf.snapshot(5.0, 15.0)
    assert snap is not None
    assert snap.packets[0].t == 10.0


def test_reset_drops_old_stream() -> None:
    t1, t2 = object(), object()
    buf = PacketBuffer()
    buf.reset(t1)
    old = _stream(0.0, 10.0)
    for p in old:
        buf.append(p)
    buf.reset(t2)
    for p in _stream(100.0, 110.0):
        buf.append(p)
    snap = buf.snapshot(0.0, 1e9)
    assert snap is not None
    assert snap.template is t2
    old_ids = {id(p.packet) for p in old}
    assert all(id(p.packet) not in old_ids for p in snap.packets)
    assert snap.packets[0].t == 100.0
