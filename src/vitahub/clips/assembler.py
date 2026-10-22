"""Montaje del MP4 de un clip a partir de paquetes ya codificados."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import av

from vitahub.clips.buffer import Snapshot
from vitahub.logging_setup import get_logger

_log = get_logger("clips.assembler")


@dataclass(frozen=True)
class ClipFile:
    path: Path
    sha256: str
    duration_s: float


def detach_template(stream: object) -> object:
    """Copia de la plantilla de un stream que no depende de su contenedor.

    Montar con la plantilla de un contenedor ya cerrado es un segfault de PyAV,
    pero el grabador tiene que cerrar el suyo en cuanto termina la sesión: si
    no, la conexión RTSP sigue ocupando una plaza en la cámara hasta que pase
    el recolector cíclico de Python. La copia vive en un contenedor de salida
    en memoria que nunca se escribe; no tiene socket, así que da igual cuándo
    se libere, y vive mientras algún Snapshot la referencie.
    """
    holder = av.open(io.BytesIO(), "w", format="mp4")
    return holder.add_stream_from_template(stream)


def assemble_mp4(snapshot: Snapshot, out_path: Path) -> ClipFile:
    """Remuxa el snapshot a MP4 sin decodificar ni recodificar.

    Los paquetes se copian antes de reasignarles stream y marcas de tiempo: el
    grabador sigue demuxeando en otro hilo y otro clip puede estar montándose
    sobre los mismos paquetes, así que los originales no se tocan. Si algo
    falla se borra el fichero parcial para no dejar un MP4 truncado.
    """
    if not snapshot.packets:
        raise ValueError("snapshot sin paquetes: no se escribe un clip vacío")
    try:
        _remux(snapshot, out_path)
        digest = hashlib.sha256()
        with out_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise
    duration = round(snapshot.packets[-1].t - snapshot.packets[0].t, 3)
    return ClipFile(path=out_path, sha256=digest.hexdigest(), duration_s=duration)


def _remux(snapshot: Snapshot, out_path: Path) -> None:
    packets: list[av.Packet] = []
    for bp in snapshot.packets:
        if not isinstance(bp.packet, av.Packet):
            raise TypeError(f"se esperaba av.Packet, llegó {type(bp.packet).__name__}")
        packets.append(bp.packet)
    # Un único desfase para pts y dts: con B-frames el primer dts es negativo
    # (retardo de reordenado) y rebasarlos por separado dejaría pts < dts, que
    # el muxer rechaza. Sin dts en el primer paquete se toma el pts.
    first = packets[0]
    base = first.dts if first.dts is not None else first.pts
    with av.open(str(out_path), "w", format="mp4", options={"movflags": "+faststart"}) as out:
        stream = out.add_stream_from_template(snapshot.template)
        for src in packets:
            pkt = av.Packet(bytes(src))
            pkt.time_base = src.time_base
            pkt.duration = src.duration
            pkt.is_keyframe = src.is_keyframe
            pkt.pts = None if src.pts is None or base is None else src.pts - base
            pkt.dts = None if src.dts is None or base is None else src.dts - base
            pkt.stream = stream
            out.mux(pkt)
