from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.clip_media import snapshot_from_file, write_test_video
from vitahub.clips.trigger import ClipRequest
from vitahub.clips.uploader import ClipUploader, PendingQueue, UploadRejected
from vitahub.models import Event
from vitahub.sinks.base import EventSink


class _Sink(EventSink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


class _Put:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, Path, dict[str, str]]] = []

    def __call__(self, key: str, path: Path, metadata: dict[str, str]) -> None:
        self.calls.append((key, path, metadata))
        if self.error is not None:
            raise self.error


def _request(tmp_path: Path, episode: str = "e1", name: str = "v") -> ClipRequest:
    src = tmp_path / f"{name}-{episode}.mp4"
    write_test_video(src)
    return ClipRequest(
        camera_id="cam1",
        camera_name="Salón",
        episode_ids=[episode],
        clip_start=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
        snapshot=snapshot_from_file(src),
    )


def _clock() -> datetime:
    return datetime(2026, 10, 22, 9, 5, tzinfo=UTC)


def _make(
    tmp_path: Path, put: _Put, **kw: object
) -> tuple[ClipUploader, PendingQueue, _Sink, Path]:
    root = tmp_path / "pending"
    queue = PendingQueue(root, **kw)  # type: ignore[arg-type]
    sink = _Sink()
    return ClipUploader(queue, "hub-1", sink, put, _clock), queue, sink, root


def test_upload_success_removes_and_emits_ready(tmp_path: Path) -> None:
    put = _Put()
    up, _, sink, root = _make(tmp_path, put)
    up.submit(_request(tmp_path))
    assert up.run_once() is True
    key, _, meta = put.calls[0]
    assert key == "hub-1/cam1/e1.mp4"
    assert meta["episode-ids"] == "e1" and meta["camera-id"] == "cam1"
    ev = sink.events[-1]
    assert ev.type == "fall_clip_ready" and ev.severity == "info"
    assert ev.payload["s3_key"] == "hub-1/cam1/e1.mp4"
    assert ev.payload["sha256"] == meta["sha256"]
    assert ev.payload["episode_ids"] == ["e1"]
    assert ev.payload["clip_start"] == "2026-10-22T09:00:00+00:00"
    assert list(root.iterdir()) == []


def test_transient_failure_keeps_clip_and_retries(tmp_path: Path) -> None:
    put = _Put(OSError("sin red"))
    up, queue, sink, _ = _make(tmp_path, put)
    up.submit(_request(tmp_path))
    with pytest.raises(OSError):
        up.run_once()
    assert queue.oldest() is not None and sink.events == []
    put.error = None
    assert up.run_once() is True
    assert queue.oldest() is None


def test_rejected_emits_once_keeps_clip_and_retries(tmp_path: Path) -> None:
    put = _Put(UploadRejected("403"))
    up, queue, sink, _ = _make(tmp_path, put)
    up.submit(_request(tmp_path))
    assert up.run_once() is False
    assert up.run_once() is False
    assert len(put.calls) == 2
    failed = [e for e in sink.events if e.type == "fall_clip_failed"]
    assert len(failed) == 1 and failed[0].payload["reason"] == "upload_rejected"
    assert queue.oldest() is not None
    put.error = None
    assert up.run_once() is True
    types = [e.type for e in sink.events]
    assert types == ["fall_clip_failed", "fall_clip_ready"]


def test_submit_never_raises(tmp_path: Path) -> None:
    up, _, _, _ = _make(tmp_path, _Put())

    class _Boom(_Sink):
        def emit(self, event: Event) -> None:
            raise RuntimeError("sink roto")

    up._sink = _Boom()
    req = _request(tmp_path)
    broken = ClipRequest(
        camera_id="cam1",
        camera_name="n",
        episode_ids=["e"],
        clip_start=req.clip_start,
        snapshot=type(req.snapshot)(template=req.snapshot.template, packets=[]),
    )
    up.submit(broken)  # mux_error -> emit lanza -> no se propaga


def test_ignores_wrong_field_types(tmp_path: Path) -> None:
    root = tmp_path / "pending"
    root.mkdir()
    (root / "a.json").write_text(
        '{"key":"k","camera_id":"c","camera_name":"n","episode_ids":["e"],'
        '"clip_start":"s","sha256":"h","duration_s":1.0,"created_at":"ayer"}'
    )
    (root / "a.mp4").write_bytes(b"x")
    assert PendingQueue(root).oldest() is None


def test_queue_full_evicts_oldest(tmp_path: Path) -> None:
    now = [100.0]
    put = _Put()
    up, queue, sink, root = _make(tmp_path, put, max_bytes=1, clock=lambda: now[0])
    up.submit(_request(tmp_path, "e1"))
    now[0] = 200.0
    up.submit(_request(tmp_path, "e2"))
    failed = [e for e in sink.events if e.type == "fall_clip_failed"]
    assert [e.payload["episode_ids"] for e in failed] == [["e1"]]
    assert failed[0].payload["reason"] == "queue_full"
    survivor = queue.oldest()
    assert survivor is not None and survivor.episode_ids == ["e2"]
    assert len(list(root.glob("*.mp4"))) == 1


def test_expired_after_3_days(tmp_path: Path) -> None:
    now = [0.0]
    put = _Put()
    up, queue, sink, root = _make(tmp_path, put, clock=lambda: now[0])
    up.submit(_request(tmp_path))
    now[0] = 3 * 24 * 3600 + 1
    assert up.run_once() is False
    assert [e.payload["reason"] for e in sink.events] == ["expired"]
    assert queue.oldest() is None and list(root.iterdir()) == []
    assert put.calls == []


def test_resume_after_restart(tmp_path: Path) -> None:
    up, _, _, root = _make(tmp_path, _Put())
    up.submit(_request(tmp_path))
    again = PendingQueue(root)
    clip = again.oldest()
    assert clip is not None and clip.key == "hub-1/cam1/e1.mp4"
    assert clip.mp4.exists()


def test_deletes_orphans_and_partials(tmp_path: Path) -> None:
    root = tmp_path / "pending"
    root.mkdir()
    (root / "a.mp4.part").write_bytes(b"x")
    (root / "b.json.part").write_text("{")
    (root / "c.json").write_text("{no json")
    (root / "c.mp4").write_bytes(b"x")
    (root / "d.json").write_text(
        '{"key":"k","camera_id":"c","camera_name":"n","episode_ids":["e"],'
        '"clip_start":"s","sha256":"h","duration_s":1.0,"created_at":1.0}'
    )
    (root / "e.mp4").write_bytes(b"x")
    queue = PendingQueue(root)
    assert queue.oldest() is None
    # Nada de esto se subirá nunca ni lo alcanzaría expire(): se borra para
    # que no pase de 3 días en el hogar.
    assert list(root.iterdir()) == []


def test_mux_error_emits_failed(tmp_path: Path) -> None:
    up, queue, sink, root = _make(tmp_path, _Put())
    req = _request(tmp_path)
    broken = ClipRequest(
        camera_id=req.camera_id,
        camera_name=req.camera_name,
        episode_ids=req.episode_ids,
        clip_start=req.clip_start,
        snapshot=type(req.snapshot)(template=req.snapshot.template, packets=[]),
    )
    up.submit(broken)
    assert [e.payload["reason"] for e in sink.events] == ["mux_error"]
    assert queue.oldest() is None and list(root.iterdir()) == []
