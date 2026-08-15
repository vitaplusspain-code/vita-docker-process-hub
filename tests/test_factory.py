from vitahub.config import InferenceConfig
from vitahub.factory import build_detector
from vitahub.inference.stub import StubDetector


def test_build_stub_detector():
    det = build_detector(InferenceConfig(detector="stub"), weights_path="unused.pt")
    assert isinstance(det, StubDetector)


def test_unknown_detector_raises():
    import pytest

    with pytest.raises(ValueError, match="desconocido"):
        build_detector(InferenceConfig(detector="nope"), weights_path="unused.pt")
