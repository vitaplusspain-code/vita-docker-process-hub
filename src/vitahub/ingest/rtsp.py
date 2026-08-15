from __future__ import annotations

import os

# Fuerza RTSP sobre TCP en el backend FFmpeg de OpenCV (debe fijarse antes de importar cv2).
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    return float(min(cap, base * (2**attempt)))


def should_sample(last_sample_t: float, now: float, sample_fps: float) -> bool:
    if sample_fps <= 0:
        return True
    return (now - last_sample_t) >= (1.0 / sample_fps)


def is_stalled(last_frame_t: float, now: float, max_stale_s: float) -> bool:
    return (now - last_frame_t) > max_stale_s


def open_capture(rtsp_url: str):  # type: ignore[no-untyped-def]
    """Runtime only. No cubierto por unit tests (requiere stream real)."""
    import cv2

    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap
