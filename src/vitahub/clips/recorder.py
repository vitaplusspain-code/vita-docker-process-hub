"""Grabación en memoria del vídeo de una cámara, sin recodificar."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import av

from vitahub.clips.buffer import BufferedPacket, PacketBuffer
from vitahub.ingest.rtsp import backoff_delay, next_attempt, strip_credentials
from vitahub.live.source import _RTSP_OPTIONS
from vitahub.logging_setup import get_logger

_log = get_logger("clips.recorder")


def _safe_error(exc: BaseException, uri: str) -> str:
    """Error sin credenciales: PyAV mete la URI completa en el mensaje."""
    return f"{type(exc).__name__}: {str(exc).replace(uri, strip_credentials(uri))}"


def record(
    uri: str,
    buffer: PacketBuffer,
    stop: threading.Event,
    format: str | None = "rtsp",
    options: dict[str, str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Demuxa la cámara al buffer hasta `stop`, reconectando con backoff.

    Corre en su propio hilo durante toda la vida del worker de la cámara, así
    que nunca deja escapar una excepción. Los errores se loguean sin traceback y
    con `uri` sin credenciales: PyAV la incluye en el mensaje y el formateador
    no redacta la excepción formateada.
    `format` y `options` existen para probarlo con un fichero local.
    """
    attempt = 0
    while not stop.is_set():
        if _session(uri, buffer, stop, format, options, monotonic):
            attempt = 0  # conectó y entregó vídeo: el siguiente fallo parte de cero
        stop.wait(backoff_delay(attempt))
        attempt = next_attempt(attempt)


def _session(
    uri: str,
    buffer: PacketBuffer,
    stop: threading.Event,
    format: str | None,
    options: dict[str, str] | None,
    monotonic: Callable[[], float],
) -> bool:
    """Una conexión, hasta su fin por EOF, error o `stop`; devuelve si entregó vídeo.

    El contenedor no se cierra a mano: el stream es la plantilla del buffer y
    un Snapshot en curso puede seguir montando un MP4 con ella, y montar con la
    plantilla de un contenedor cerrado es un segfault de PyAV. Al salir se vacía
    el buffer y, al morir este marco, se sueltan contenedor, stream y
    generador de demux; el contenedor se libera (y cierra) en cuanto el último
    Snapshot que lo usa desaparece. Por el ciclo contenedor-stream de PyAV eso
    lo hace el recolector cíclico de Python, no el recuento de referencias.
    """
    delivered = False
    video = None
    try:
        container = av.open(uri, format=format, options=_RTSP_OPTIONS if options is None else options)
        video = container.streams.video[0]
        if stop.is_set():
            # Al supervisor le dio tiempo a arrancar otro grabador mientras
            # este abría: el buffer ya es del nuevo y no se le cambia la plantilla.
            return False
        # El stream nuevo tiene otros parámetros y base de tiempos: nada del
        # anterior se puede mezclar con él.
        buffer.reset(video)
        for packet in container.demux(video):
            if stop.is_set():
                return delivered
            if packet.size > 0:
                buffer.append(BufferedPacket(monotonic(), packet.is_keyframe, packet))
                delivered = True
        _log.warning("fin del stream de la cámara; se reintenta")
    except Exception as exc:  # noqa: BLE001 — el hilo grabador no puede morir por un fallo de red o de códec
        _log.warning("fallo grabando la cámara; se reintenta: %s", _safe_error(exc, uri))
    finally:
        if video is not None:
            buffer.clear(video)
    return delivered
