import numpy as np
import pytest

from vitahub.config import ConfigError
from vitahub.identity.base import FaceObservation
from vitahub.identity.gallery import Gallery, best_match, load_gallery, mean_embedding
from vitahub.identity.stub import StubFaceEngine


def _unit(v):
    arr = np.asarray(v, dtype=np.float32)
    return arr / np.linalg.norm(arr)


def test_mean_embedding_is_renormalized():
    mean = mean_embedding([_unit([1, 0, 0]), _unit([0, 1, 0])])
    assert np.linalg.norm(mean) == pytest.approx(1.0)


def test_best_match_returns_most_similar_above_threshold():
    gallery = Gallery(people={"maria": _unit([1, 0, 0]), "pepe": _unit([0, 1, 0])})
    got = best_match(_unit([0.9, 0.1, 0]), gallery, threshold=0.4)
    assert got is not None
    person, sim = got
    assert person == "maria" and sim > 0.9


def test_best_match_below_threshold_is_none():
    gallery = Gallery(people={"maria": _unit([1, 0, 0])})
    assert best_match(_unit([0, 0, 1]), gallery, threshold=0.4) is None


def _photo(tmp_path, person, name):
    # Un JPEG válido de 8x8 escrito con cv2 basta: el StubFaceEngine ignora
    # los píxeles, pero load_gallery debe poder leer el fichero.
    import cv2

    d = tmp_path / person
    d.mkdir(exist_ok=True)
    path = d / name
    cv2.imwrite(str(path), np.zeros((8, 8, 3), dtype=np.uint8))
    return path


def test_load_gallery_averages_per_person(tmp_path):
    _photo(tmp_path, "maria", "a.jpg")
    _photo(tmp_path, "maria", "b.jpg")
    engine = StubFaceEngine([
        [FaceObservation((0, 0, 8, 8), _unit([1, 0, 0]))],
        [FaceObservation((0, 0, 8, 8), _unit([0, 1, 0]))],
    ])
    gallery = load_gallery(tmp_path, engine)
    assert set(gallery.people) == {"maria"}
    assert np.linalg.norm(gallery.people["maria"]) == pytest.approx(1.0)


def test_load_gallery_empty_dir_raises(tmp_path):
    with pytest.raises(ConfigError, match="ninguna persona enrolada"):
        load_gallery(tmp_path, StubFaceEngine([]))


def test_load_gallery_photo_without_face_raises(tmp_path):
    _photo(tmp_path, "maria", "a.jpg")
    with pytest.raises(ConfigError, match="a.jpg"):
        load_gallery(tmp_path, StubFaceEngine([[]]))


def test_load_gallery_photo_with_two_faces_raises(tmp_path):
    _photo(tmp_path, "maria", "a.jpg")
    two = [FaceObservation((0, 0, 4, 8), _unit([1, 0, 0])),
           FaceObservation((4, 0, 8, 8), _unit([0, 1, 0]))]
    with pytest.raises(ConfigError, match="a.jpg"):
        load_gallery(tmp_path, StubFaceEngine([two]))


def test_load_gallery_faces_dir_not_exist_raises(tmp_path):
    nonexistent = tmp_path / "nonexistent"
    with pytest.raises(ConfigError, match="carpeta de caras no existe"):
        load_gallery(nonexistent, StubFaceEngine([]))


def test_load_gallery_faces_dir_is_file_raises(tmp_path):
    faces_file = tmp_path / "faces"
    faces_file.write_text("not a directory")
    with pytest.raises(ConfigError, match="carpeta de caras no existe"):
        load_gallery(faces_file, StubFaceEngine([]))


def test_load_gallery_photo_unreadable_raises(tmp_path):
    # Crea un fichero .jpg con bytes basura que cv2 no puede leer
    d = tmp_path / "maria"
    d.mkdir(exist_ok=True)
    path = d / "garbage.jpg"
    path.write_bytes(b"\xff\xff\xff\xff")
    with pytest.raises(ConfigError, match="foto de enrolamiento ilegible"):
        load_gallery(tmp_path, StubFaceEngine([]))


def test_load_gallery_photo_folder_no_valid_photos_raises(tmp_path):
    # Carpeta con solo ficheros no-foto (.txt, .json, etc.) que se saltan por sufijo
    d = tmp_path / "maria"
    d.mkdir(exist_ok=True)
    (d / "notes.txt").write_text("not a photo")
    (d / "data.json").write_text("{}")
    with pytest.raises(ConfigError, match="no tiene fotos válidas"):
        load_gallery(tmp_path, StubFaceEngine([]))
