from __future__ import annotations

import os
from urllib.parse import quote, urlsplit, urlunsplit

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


def with_credentials(rtsp_url: str, user: str, password: str) -> str:
    """Inserta credenciales en la netinfo de una URL RTSP si no las tiene ya.

    El resultado de ONVIF GetStreamUri normalmente no lleva userinfo, así que
    hay que inyectar user:password@ en el netloc (URL-encoded) para que el
    stream abra. Si la URL ya trae userinfo o no hay usuario, se devuelve tal cual.
    """
    if not user:
        return rtsp_url
    parts = urlsplit(rtsp_url)
    if "@" in parts.netloc:
        return rtsp_url
    encoded_user = quote(user, safe="")
    encoded_password = quote(password, safe="")
    netloc = f"{encoded_user}:{encoded_password}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def strip_credentials(rtsp_url: str) -> str:
    """Quita el userinfo (usuario:clave@) de una URL RTSP. Inverso de with_credentials.

    El fichero de config no contiene secretos: las credenciales se inyectan en
    memoria al conectar. Si una URI llegara de ONVIF con userinfo y se
    persistiera tal cual, la contraseña del hogar acabaría escrita en el YAML.
    """
    parts = urlsplit(rtsp_url)
    if "@" not in parts.netloc:
        return rtsp_url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def open_capture(rtsp_url: str):  # type: ignore[no-untyped-def]
    """Runtime only. No cubierto por unit tests (requiere stream real)."""
    import cv2

    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap
