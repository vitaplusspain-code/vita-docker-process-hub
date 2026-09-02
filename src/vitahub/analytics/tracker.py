"""Pistas de persona por cámara: emparejamiento entre frames e identidad.

Una pista es una persona seguida entre frames por solapamiento de cajas
(IoU) y, cuando no hay solape, por cercanía de centros (una caída hacia
delante mueve la caja entera). Las pistas caducan antes de emparejar: quien
reaparece tras un hueco largo abre pista nueva, no hereda la anterior.

La identidad (FaceIdentifier) viaja aquí, colgada de la pista: se etiqueta
una vez y acompaña a la persona hasta que la pista se pierde.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from vitahub.models import Detection

# Solapamiento mínimo para decir "es la misma persona que en el frame anterior".
IOU_MATCH = 0.3
# Respaldo cuando no hay solape: distancia entre centros normalizada por el
# lado mayor de las dos cajas. Una caída hacia delante desplaza la caja hasta
# una altura de cuerpo, y a 2 fps eso puede dejar IoU = 0 entre frames
# consecutivos. Puede cruzar personas en escenas concurridas: el tracker real
# está en el backlog.
CENTER_MATCH = 1.0
# Una pista sin observación durante este tiempo se da por perdida.
TRACK_TTL_S = 3.0


@dataclass(frozen=True)
class TrackIdentity:
    person_id: str
    confidence: float


@dataclass
class Track:
    track_id: int
    bbox: tuple[int, int, int, int]
    last_seen: float
    # La rellena el FaceIdentifier; None = pista anónima (persona no enrolada,
    # cara nunca vista, o identidad apagada).
    identity: TrackIdentity | None = None
    # Primera coincidencia a la espera de confirmación (regla de 2 frames del
    # FaceIdentifier): un solo frame ruidoso no etiqueta.
    pending_identity: TrackIdentity | None = None


@dataclass(frozen=True)
class Match:
    track: Track
    detection: Detection
    # Segundos desde la muestra anterior de esta pista; 0.0 si es nueva. El
    # consumidor decide su tope (FallEngine aplica MAX_STEP_S).
    step_s: float


@dataclass(frozen=True)
class TrackerUpdate:
    matches: list[Match]
    lost: list[Track]


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _center_distance(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> float:
    """Distancia entre centros normalizada por el lado mayor de ambas cajas."""
    ax, ay = (a[0] + a[2]) / 2.0, (a[1] + a[3]) / 2.0
    bx, by = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    scale = max(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1])
    if scale <= 0:
        return float("inf")
    return math.hypot(ax - bx, ay - by) / scale


@dataclass
class _CameraTracks:
    tracks: list[Track] = field(default_factory=list)


class Tracker:
    """Estado en memoria por cámara; track_id único en todo el proceso."""

    def __init__(self) -> None:
        self._cameras: dict[str, _CameraTracks] = {}
        self._next_id = 1

    def observe(
        self, camera_id: str, detections: list[Detection], now: float
    ) -> TrackerUpdate:
        cam = self._cameras.setdefault(camera_id, _CameraTracks())
        persons = [d for d in detections if d.label == "person"]

        # Caducar ANTES de emparejar: si la pista lleva TRACK_TTL_S sin verse,
        # la persona que aparece ahora no es "la misma de antes" aunque su
        # caja solape.
        lost = [t for t in cam.tracks if now - t.last_seen >= TRACK_TTL_S]
        cam.tracks = [t for t in cam.tracks if now - t.last_seen < TRACK_TTL_S]

        matches: list[Match] = []
        for track, det in self._match(cam.tracks, persons):
            step = max(0.0, now - track.last_seen)
            track.bbox = det.bbox
            track.last_seen = now
            matches.append(Match(track=track, detection=det, step_s=step))

        matched_dets = {id(m.detection) for m in matches}
        for det in persons:
            if id(det) in matched_dets:
                continue
            track = Track(track_id=self._next_id, bbox=det.bbox, last_seen=now)
            self._next_id += 1
            cam.tracks.append(track)
            matches.append(Match(track=track, detection=det, step_s=0.0))
        return TrackerUpdate(matches=matches, lost=lost)

    def _match(
        self, tracks: list[Track], persons: list[Detection]
    ) -> list[tuple[Track, Detection]]:
        """Greedy por mayor IoU, luego por cercanía de centros."""
        pairs = sorted(
            (
                (_iou(t.bbox, d.bbox), ti, di)
                for ti, t in enumerate(tracks)
                for di, d in enumerate(persons)
            ),
            reverse=True,
        )
        used_t: set[int] = set()
        used_d: set[int] = set()
        result: list[tuple[Track, Detection]] = []
        for iou, ti, di in pairs:
            if iou < IOU_MATCH or ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            result.append((tracks[ti], persons[di]))

        # Segunda pasada: pistas y detecciones aún sueltas, por cercanía de
        # centros (menor distancia primero). Rescata la caída sin solape.
        near = sorted(
            (
                (_center_distance(t.bbox, d.bbox), ti, di)
                for ti, t in enumerate(tracks)
                if ti not in used_t
                for di, d in enumerate(persons)
                if di not in used_d
            ),
        )
        for dist, ti, di in near:
            if dist > CENTER_MATCH or ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            result.append((tracks[ti], persons[di]))
        return result
