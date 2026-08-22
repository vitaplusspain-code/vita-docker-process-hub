from types import SimpleNamespace

from vitahub.inference.person_pose_yolo import PosePersonDetector


class _FakeBoxes:
    def __init__(self, rows):
        self.cls = [r[0] for r in rows]
        self.conf = [r[1] for r in rows]
        self.xyxy = [r[2] for r in rows]

    def __len__(self):
        return len(self.cls)


class _FakeKeypoints:
    """Imita ultralytics Results[0].keypoints: .data es (n, 17, 3)."""

    def __init__(self, per_person):
        self.data = per_person


def _kps(value: float, conf: float = 0.9):
    return [[value, value, conf] for _ in range(17)]


def _fake_model(rows, keypoints=None):
    result = SimpleNamespace(
        boxes=_FakeBoxes(rows),
        keypoints=None if keypoints is None else _FakeKeypoints(keypoints),
    )
    return lambda frame, verbose=False: [result]


def test_returns_persons_with_keypoints_aligned_to_boxes():
    model = _fake_model(
        [(0, 0.8, (0, 0, 10, 20)), (2, 0.9, (5, 5, 15, 25)), (0, 0.7, (1, 1, 2, 2))],
        keypoints=[_kps(1.0), _kps(2.0), _kps(3.0)],
    )
    det = PosePersonDetector(model=model, confidence=0.4)
    result = det.detect(frame=None)
    assert [d.bbox for d in result] == [(0, 0, 10, 20), (1, 1, 2, 2)]
    assert result[0].keypoints is not None and result[0].keypoints[0] == (1.0, 1.0, 0.9)
    assert result[1].keypoints is not None and result[1].keypoints[0] == (3.0, 3.0, 0.9)
    assert len(result[0].keypoints) == 17


def test_filters_below_confidence():
    model = _fake_model([(0, 0.2, (0, 0, 1, 1))], keypoints=[_kps(1.0)])
    assert PosePersonDetector(model=model, confidence=0.4).detect(frame=None) == []


def test_model_without_keypoints_yields_none():
    model = _fake_model([(0, 0.8, (0, 0, 10, 20))], keypoints=None)
    result = PosePersonDetector(model=model, confidence=0.4).detect(frame=None)
    assert len(result) == 1
    assert result[0].keypoints is None


def test_empty_results_returns_empty():
    det = PosePersonDetector(model=lambda frame, verbose=False: [], confidence=0.4)
    assert det.detect(frame=None) == []
