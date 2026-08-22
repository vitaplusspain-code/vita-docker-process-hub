from vitahub.inference.stub import StubDetector


def test_stub_returns_configured_number_of_persons():
    det = StubDetector(person_count=3)
    result = det.detect(frame=None)
    assert len(result) == 3
    assert all(d.label == "person" for d in result)


def test_stub_zero_by_default():
    assert StubDetector().detect(frame=None) == []


def test_stub_without_keypoints_returns_none():
    det = StubDetector(person_count=1)
    assert det.detect(frame=None)[0].keypoints is None


def test_stub_with_keypoints_and_bbox():
    kps = tuple((1.0, 2.0, 0.9) for _ in range(17))
    det = StubDetector(person_count=2, keypoints=kps, bbox=(10, 10, 50, 100))
    result = det.detect(frame=None)
    assert len(result) == 2
    assert all(d.keypoints == kps for d in result)
    assert all(d.bbox == (10, 10, 50, 100) for d in result)
