from types import SimpleNamespace

from vitahub.inference.person_yolo import PersonDetector


class _FakeBoxes:
    """Imita ultralytics Results[0].boxes: .cls, .conf, .xyxy como listas."""

    def __init__(self, rows):
        self.cls = [r[0] for r in rows]
        self.conf = [r[1] for r in rows]
        self.xyxy = [r[2] for r in rows]

    def __len__(self):
        return len(self.cls)


def _fake_model(rows):
    result = SimpleNamespace(boxes=_FakeBoxes(rows))
    return lambda frame, verbose=False: [result]


def test_filters_persons_above_threshold():
    # (clase, conf, bbox): persona=0. Una persona a 0.8, un coche(2) a 0.9, persona a 0.2
    model = _fake_model([
        (0, 0.8, (0, 0, 10, 20)),
        (2, 0.9, (5, 5, 15, 25)),
        (0, 0.2, (1, 1, 2, 2)),
    ])
    det = PersonDetector(model=model, confidence=0.4)
    result = det.detect(frame=None)
    assert len(result) == 1
    assert result[0].label == "person"
    assert result[0].confidence == 0.8
    assert result[0].bbox == (0, 0, 10, 20)


def test_no_boxes_returns_empty():
    det = PersonDetector(model=_fake_model([]), confidence=0.4)
    assert det.detect(frame=None) == []
