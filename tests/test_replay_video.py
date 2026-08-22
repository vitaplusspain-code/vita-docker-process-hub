import importlib.util
import sys
from pathlib import Path

from vitahub.models import Event

_spec = importlib.util.spec_from_file_location(
    "replay_video", Path(__file__).resolve().parents[1] / "scripts" / "replay_video.py"
)
replay_video = importlib.util.module_from_spec(_spec)
sys.modules["replay_video"] = replay_video
_spec.loader.exec_module(replay_video)


def _ev(type_, score=None):
    payload = {} if score is None else {"score": score}
    return Event("h", "c", "n", type_, "high", "t", payload)


def test_summary_counts_types_and_max_score():
    events = [_ev("fall_detected", 0.4), _ev("fall_update", 0.8), _ev("fall_resolved"),
              _ev("person_detected")]
    summary = replay_video.summarize(events, frames=120)
    assert summary == {
        "frames": 120,
        "events": {"fall_detected": 1, "fall_update": 1, "fall_resolved": 1, "person_detected": 1},
        "max_score": 0.8,
    }


def test_summary_without_scores():
    assert replay_video.summarize([], frames=0) == {"frames": 0, "events": {}, "max_score": None}
