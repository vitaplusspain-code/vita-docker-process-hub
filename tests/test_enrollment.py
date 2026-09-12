from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from vitahub.admin.enrollment import (
    EnrollmentError,
    PersonSummary,
    delete_person,
    delete_photo,
    list_people,
    photo_bytes,
    save_photo,
)
from vitahub.identity.base import FaceObservation
from vitahub.identity.stub import StubFaceEngine


def _jpeg_bytes() -> bytes:
    image = np.full((32, 32, 3), 128, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", image)
    assert ok
    return bytes(buf)


def _face() -> FaceObservation:
    emb = np.zeros(512, dtype=np.float32)
    emb[0] = 1.0
    return FaceObservation(bbox=(0, 0, 10, 10), embedding=emb)


def test_list_people_empty_dir(tmp_path: Path) -> None:
    assert list_people(tmp_path) == []


def test_list_people_skips_invalid_dir_names(tmp_path: Path) -> None:
    # Una carpeta creada a mano (o con mayúsculas, espacios, etc.) que no
    # cumple _PERSON_ID_RE no debe aparecer en la API ni en la página: nunca
    # se podría enrolar ni borrar por esa ruta (_check_person_id la rechaza).
    (tmp_path / "María").mkdir()
    (tmp_path / "a b").mkdir()
    (tmp_path / "maria").mkdir()
    (tmp_path / "maria" / "001.jpg").write_bytes(b"x")
    assert list_people(tmp_path) == [PersonSummary(id="maria", photos=["001.jpg"])]


def test_save_photo_creates_person_and_sequential_names(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face()], [_face()]])
    first = save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    second = save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    assert (first, second) == ("001.jpg", "002.jpg")
    assert list_people(tmp_path) == [
        PersonSummary(id="maria", photos=["001.jpg", "002.jpg"])
    ]


def test_save_photo_rejects_zero_faces(tmp_path: Path) -> None:
    engine = StubFaceEngine([[]])
    with pytest.raises(EnrollmentError) as exc:
        save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    assert "0 caras" in exc.value.message
    assert not (tmp_path / "maria").exists()


def test_save_photo_rejects_two_faces(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face(), _face()]])
    with pytest.raises(EnrollmentError) as exc:
        save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    assert "2 caras" in exc.value.message


def test_save_photo_rejects_unreadable_image(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face()]])
    with pytest.raises(EnrollmentError) as exc:
        save_photo(tmp_path, "maria", b"no soy una imagen", engine)
    assert "ilegible" in exc.value.message
    assert engine.calls == 0


@pytest.mark.parametrize("bad_id", ["", "María", "a b", "../x", "x" * 33, "A-1"])
def test_save_photo_rejects_invalid_person_id(tmp_path: Path, bad_id: str) -> None:
    engine = StubFaceEngine([[_face()]])
    with pytest.raises(EnrollmentError):
        save_photo(tmp_path, bad_id, _jpeg_bytes(), engine)


def test_save_photo_leaves_no_tmp_files(tmp_path: Path) -> None:
    # save_photo escribe vía fichero temporal + fsync + os.replace (mismo
    # patrón que write_settings): un JPEG truncado por un corte de luz o una
    # conexión perdida nunca debe llegar a aparecer en la galería.
    engine = StubFaceEngine([[_face()]])
    save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    leftovers = list((tmp_path / "maria").glob("*.tmp")) + list(
        (tmp_path / "maria").glob(".*")
    )
    assert leftovers == []


def test_photo_bytes_roundtrip(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face()]])
    name = save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    data = photo_bytes(tmp_path, "maria", name)
    assert data[:2] == b"\xff\xd8"  # cabecera JPEG


def test_photo_bytes_missing_is_404(tmp_path: Path) -> None:
    with pytest.raises(EnrollmentError) as exc:
        photo_bytes(tmp_path, "maria", "001.jpg")
    assert exc.value.status == 404


@pytest.mark.parametrize("bad_name", ["../../etc/passwd", "1.jpg", "001.png", "001"])
def test_photo_name_is_validated(tmp_path: Path, bad_name: str) -> None:
    with pytest.raises(EnrollmentError):
        photo_bytes(tmp_path, "maria", bad_name)


def test_delete_photo_and_person(tmp_path: Path) -> None:
    engine = StubFaceEngine([[_face()], [_face()]])
    save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    save_photo(tmp_path, "maria", _jpeg_bytes(), engine)
    delete_photo(tmp_path, "maria", "001.jpg")
    assert list_people(tmp_path)[0].photos == ["002.jpg"]
    delete_person(tmp_path, "maria")
    assert list_people(tmp_path) == []


def test_delete_missing_is_404(tmp_path: Path) -> None:
    with pytest.raises(EnrollmentError) as exc:
        delete_person(tmp_path, "maria")
    assert exc.value.status == 404
