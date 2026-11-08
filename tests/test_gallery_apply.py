from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from vitahub.gallery.apply import apply_manifest
from vitahub.gallery.manifest import Manifest
from vitahub.identity.base import FaceObservation
from vitahub.identity.gallery import load_gallery
from vitahub.identity.stub import StubFaceEngine

K1 = "a" * 32
K2 = "b" * 32
P1 = "11111111-1111-1111-1111-111111111111"
P2 = "22222222-2222-2222-2222-222222222222"
P3 = "33333333-3333-3333-3333-333333333333"


def _jpeg_bytes() -> bytes:
    image = np.full((32, 32, 3), 128, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", image)
    assert ok
    return bytes(buf)


def _face() -> FaceObservation:
    emb = np.zeros(512, dtype=np.float32)
    emb[0] = 1.0
    return FaceObservation(bbox=(0, 0, 10, 10), embedding=emb)


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def _fetch_from(photos: dict[str, bytes | None]):  # type: ignore[no-untyped-def]
    return lambda key, pid: photos.get(pid)


def test_writes_accepted_and_reports_rejected(tmp_path: Path) -> None:
    f = _face()
    engine = StubFaceEngine([[f], [], [f, f]])
    manifest = Manifest(version=1, persons={K1: (P1, P2, P3)})
    result = apply_manifest(
        tmp_path, manifest, _fetch_from({P1: _jpeg_bytes(), P2: _jpeg_bytes(), P3: _jpeg_bytes()}), engine
    )
    assert sorted(p.name for p in (tmp_path / K1).iterdir()) == [f"{P1}.jpg"]
    assert result.statuses == {
        P1: ("accepted", None),
        P2: ("rejected", "no_face"),
        P3: ("rejected", "multiple_faces"),
    }
    assert result.changed is True


def test_decode_error(tmp_path: Path) -> None:
    manifest = Manifest(version=1, persons={K1: (P1,)})
    result = apply_manifest(tmp_path, manifest, _fetch_from({P1: b"basura"}), StubFaceEngine([]))
    assert result.statuses == {P1: ("rejected", "decode_error")}
    assert not (tmp_path / K1).exists()


def test_missing_photo_is_pending(tmp_path: Path) -> None:
    manifest = Manifest(version=1, persons={K1: (P1,)})
    result = apply_manifest(tmp_path, manifest, _fetch_from({P1: None}), StubFaceEngine([]))
    assert result.statuses == {}
    assert not (tmp_path / K1).exists()


def test_person_with_no_valid_photos_folder_removed(tmp_path: Path) -> None:
    (tmp_path / K1).mkdir()
    (tmp_path / K1 / "001.jpg").write_bytes(b"x")
    manifest = Manifest(version=1, persons={K1: (P1,)})
    result = apply_manifest(tmp_path, manifest, _fetch_from({P1: _jpeg_bytes()}), StubFaceEngine([[]]))
    assert not (tmp_path / K1).exists()
    assert result.changed is True


def test_removes_person_keys_not_in_manifest_but_keeps_technician_dirs(tmp_path: Path) -> None:
    (tmp_path / "ana").mkdir()
    (tmp_path / "ana" / "001.jpg").write_bytes(b"x")
    (tmp_path / K2).mkdir()
    (tmp_path / K2 / "x.jpg").write_bytes(b"x")
    result = apply_manifest(tmp_path, Manifest(version=1, persons={}), _fetch_from({}), StubFaceEngine([]))
    assert (tmp_path / "ana" / "001.jpg").exists()
    assert not (tmp_path / K2).exists()
    assert result.changed is True


def test_replaces_technician_photos_in_cloud_folder(tmp_path: Path) -> None:
    (tmp_path / K1).mkdir()
    (tmp_path / K1 / "001.jpg").write_bytes(b"x")
    manifest = Manifest(version=1, persons={K1: (P1,)})
    apply_manifest(tmp_path, manifest, _fetch_from({P1: _jpeg_bytes()}), StubFaceEngine([[_face()]]))
    assert sorted(p.name for p in (tmp_path / K1).iterdir()) == [f"{P1}.jpg"]


def test_second_apply_same_manifest_is_unchanged(tmp_path: Path) -> None:
    manifest = Manifest(version=1, persons={K1: (P1,)})
    fetch = _fetch_from({P1: _jpeg_bytes()})
    engine = StubFaceEngine([[_face()], [_face()]])
    assert apply_manifest(tmp_path, manifest, fetch, engine).changed is True
    assert apply_manifest(tmp_path, manifest, fetch, engine).changed is False


def test_creates_missing_faces_dir(tmp_path: Path) -> None:
    faces = tmp_path / "faces"
    manifest = Manifest(version=1, persons={K1: (P1,)})
    apply_manifest(faces, manifest, _fetch_from({P1: _jpeg_bytes()}), StubFaceEngine([[_face()]]))
    assert (faces / K1 / f"{P1}.jpg").is_file()


def test_fetch_error_leaves_disk_untouched(tmp_path: Path) -> None:
    (tmp_path / K1).mkdir()
    (tmp_path / K1 / "001.jpg").write_bytes(b"x")
    (tmp_path / "ana").mkdir()
    (tmp_path / "ana" / "001.jpg").write_bytes(b"y")
    before = _tree(tmp_path)
    calls = []

    def fetch(key: str, pid: str) -> bytes | None:
        calls.append(pid)
        if len(calls) == 2:
            raise RuntimeError("red caída")
        return _jpeg_bytes()

    manifest = Manifest(version=1, persons={K1: (P1, P2)})
    with pytest.raises(RuntimeError):
        apply_manifest(tmp_path, manifest, fetch, StubFaceEngine([[_face()]]))
    assert _tree(tmp_path) == before


def test_leftover_staging_dirs_are_ignored_and_cleaned(tmp_path: Path) -> None:
    (tmp_path / ".cloud-staging" / K1).mkdir(parents=True)
    (tmp_path / ".cloud-staging" / K1 / "x.jpg").write_bytes(b"x")
    (tmp_path / ".cloud-old" / K1).mkdir(parents=True)
    # load_gallery ignora los directorios con punto aunque estén ahí.
    assert load_gallery(tmp_path, StubFaceEngine([])).people == {}
    apply_manifest(tmp_path, Manifest(version=1, persons={}), _fetch_from({}), StubFaceEngine([]))
    assert not (tmp_path / ".cloud-staging").exists()
    assert not (tmp_path / ".cloud-old").exists()
