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
