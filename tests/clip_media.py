"""Helpers de test compartidos por los tests del subsistema de clips.

Solo vídeo, sin audio: lo que el buffer de clips guarda de una cámara.
"""
from __future__ import annotations

from pathlib import Path

from vitahub.clips.buffer import BufferedPacket, Snapshot


def write_test_video(
    path: Path,
    frames: int = 50,
    fps: int = 10,
    gop: int = 10,
    codec: str = "mpeg4",
    options: dict[str, str] | None = None,
) -> None:
    """Escribe un vídeo 64x48 con un keyframe cada `gop` frames.

    Con `codec="libx264"` y `options={"bf": "2"}` genera B-frames (dts < pts);
    puede lanzar si el build de PyAV no trae libx264.
    """
    import av
    import numpy as np
    from av import VideoFrame

    out = av.open(str(path), "w")
    video = out.add_stream(codec, rate=fps, options=options)
    video.width, video.height, video.pix_fmt = 64, 48, "yuv420p"
    video.gop_size = gop
    for i in range(frames):
        vf = VideoFrame.from_ndarray(np.zeros((48, 64, 3), dtype=np.uint8), format="bgr24")
        vf.pts = i
        for packet in video.encode(vf):
            out.mux(packet)
    for packet in video.encode():
        out.mux(packet)
    out.close()


def snapshot_from_file(path: Path, fps: int = 10) -> Snapshot:
    """Demux de `path` como lo haría el grabador: `t = i / fps` por paquete."""
    import av

    container = av.open(str(path))
    stream = container.streams.video[0]
    packets = [
        BufferedPacket(t=i / fps, keyframe=bool(p.is_keyframe), packet=p)
        for i, p in enumerate(p for p in container.demux(stream) if p.size > 0)
    ]
    # El contenedor se mantiene abierto: los paquetes y el stream lo referencian.
    return Snapshot(template=stream, packets=packets)
