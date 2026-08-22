"""Pasa un vídeo grabado por el pipeline de caídas e imprime los eventos.

Para calibrar pesos y umbrales con caídas simuladas antes de ir a un hogar:

    python scripts/replay_video.py caida-lateral.mp4 --weights yolo11n-pose.pt

Eventos por stdout (JSON-lines, igual que el hub); resumen por stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

import cv2

from vitahub.analytics.event_engine import EventEngine
from vitahub.analytics.fall_engine import FallEngine
from vitahub.config import InferenceConfig
from vitahub.factory import build_detector
from vitahub.ingest.rtsp import should_sample
from vitahub.models import Camera, Event
from vitahub.sinks.base import EventSink
from vitahub.sinks.stdout_json import StdoutJsonSink
from vitahub.worker import process_frame


class _FilteredSink(EventSink):
    """Deja pasar solo los eventos de caída salvo que se pidan también los de presencia."""

    def __init__(self, inner: EventSink, presence: bool) -> None:
        self._inner = inner
        self._presence = presence
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)
        if self._presence or event.type.startswith("fall_"):
            self._inner.emit(event)

    def close(self) -> None:
        self._inner.close()


def summarize(events: list[Event], frames: int) -> dict[str, object]:
    scores = [
        float(e.payload["score"])  # type: ignore[arg-type]
        for e in events
        if "score" in e.payload
    ]
    return {
        "frames": frames,
        "events": dict(Counter(e.type for e in events)),
        "max_score": max(scores) if scores else None,
    }


def replay(
    path: str, weights: str, fps: float, min_score: float, presence: bool
) -> dict[str, object]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"no se puede abrir {path}")
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    detector = build_detector(InferenceConfig(detector="person_pose"), weights)
    engine = EventEngine("replay")
    fall = FallEngine("replay", min_score=min_score)
    sink = _FilteredSink(StdoutJsonSink(), presence)
    camera = Camera(id="video", name=path, last_ip="")
    frames = 0
    last_sample = -1e9
    index = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            now = index / video_fps  # reloj del vídeo, no el de pared
            index += 1
            if should_sample(last_sample, now, fps):
                last_sample = now
                frames += 1
                process_frame(camera, frame, detector, engine, sink, now, fall_engine=fall)
    finally:
        cap.release()
        sink.close()
    return summarize(sink.events, frames)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--weights", default="yolo11n-pose.pt")
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--min-score", type=float, default=0.3)
    parser.add_argument("--presence", action="store_true", help="imprime también person_*")
    args = parser.parse_args()
    summary = replay(args.video, args.weights, args.fps, args.min_score, args.presence)
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
