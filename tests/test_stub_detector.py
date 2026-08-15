from vitahub.inference.stub import StubDetector


def test_stub_returns_configured_number_of_persons():
    det = StubDetector(person_count=3)
    result = det.detect(frame=None)
    assert len(result) == 3
    assert all(d.label == "person" for d in result)


def test_stub_zero_by_default():
    assert StubDetector().detect(frame=None) == []
