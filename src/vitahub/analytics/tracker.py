"""Pistas de persona por cámara: emparejamiento entre frames e identidad.

Una pista es una persona seguida entre frames por solapamiento de cajas
(IoU) y, cuando no hay solape, por cercanía de centros (una caída hacia
delante mueve la caja entera). Las pistas caducan antes de emparejar: quien
reaparece tras un hueco largo abre pista nueva, no hereda la anterior.

La identidad (FaceIdentifier) viaja aquí, colgada de la pista: se etiqueta
una vez y acompaña a la persona hasta que la pista se pierde.

Aparcado: una pista identificada que llevaba quieta un rato cuando se pierde
(p.ej. alguien dormido o inmóvil que el detector deja de ver) no se descarta:
su identidad se aparca durante PARK_TTL_S. Si una pista nueva aparece en esa
misma caja, hereda la identidad como provisional (el FaceIdentifier la
reconfirma). El tracker no sabe nada de inactividad: solo informa de qué se
aparcó, reanudó o caducó.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

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
# Cuánto vive una identidad aparcada esperando que reaparezca en su caja.
PARK_TTL_S = 120.0
# Valores iniciales: se calibran con replay_video.py (spec §8).
# Tiempo mínimo que la pista llevaba quieta al perderse para aparcarla.
PARK_STILL_S = 4.0
# IoU contra la caja de referencia que aún cuenta como "el mismo sitio".
STILL_IOU = 0.7
# IoU mínimo de una pista nueva con una aparcada para heredarla; más estricto
# que IOU_MATCH porque aquí no hay continuidad temporal que lo respalde.
RESUME_IOU = 0.5


@dataclass(frozen=True)
class TrackIdentity:
    person_id: str
    confidence: float
    # Heredada de una pista aparcada: pendiente de que el FaceIdentifier la
    # reconfirme con la cara.
    provisional: bool = False


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
    # Contabilidad de quietud, privada del tracker: caja de referencia y desde
    # cuándo la pista no sale de ella.
    still_bbox: tuple[int, int, int, int] | None = None
    still_since: float | None = None


@dataclass(frozen=True)
class Match:
    track: Track
    detection: Detection
    # Segundos desde la muestra anterior de esta pista; 0.0 si es nueva. El
    # consumidor decide su tope (FallEngine aplica MAX_STEP_S).
    step_s: float


@dataclass(frozen=True)
class ParkedIdentity:
    track_id: int
    identity: TrackIdentity
    bbox: tuple[int, int, int, int]
    # `last_seen` de la pista perdida.
    parked_at: float


@dataclass(frozen=True)
class Resume:
    track: Track
    previous_track_id: int


@dataclass(frozen=True)
class TrackerUpdate:
    matches: list[Match]
    lost: list[Track]
    # Aparcadas, reanudadas y caducadas en esta llamada.
    parked: list[ParkedIdentity] = field(default_factory=list)
    resumed: list[Resume] = field(default_factory=list)
    expired: list[ParkedIdentity] = field(default_factory=list)


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
    parked: list[ParkedIdentity] = field(default_factory=list)


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

        # Aparcar la identidad de las pistas quietas perdidas, y soltar las
        # aparcadas que llevan demasiado: caducan ANTES de intentar reanudar.
        parked: list[ParkedIdentity] = []
        for t in lost:
            if (
                t.identity is not None
                and t.still_since is not None
                and t.last_seen - t.still_since >= PARK_STILL_S
            ):
                parked.append(
                    ParkedIdentity(
                        track_id=t.track_id,
                        identity=t.identity,
                        bbox=t.bbox,
                        parked_at=t.last_seen,
                    )
                )
        cam.parked.extend(parked)
        expired = [p for p in cam.parked if now - p.parked_at >= PARK_TTL_S]
        cam.parked = [p for p in cam.parked if now - p.parked_at < PARK_TTL_S]

        matches: list[Match] = []
        matched_idx: set[int] = set()
        for track, di in self._match(cam.tracks, persons):
            det = persons[di]
            step = max(0.0, now - track.last_seen)
            track.bbox = det.bbox
            track.last_seen = now
            if (
                track.still_bbox is None
                or _iou(track.still_bbox, det.bbox) < STILL_IOU
            ):
                track.still_bbox = det.bbox
                track.still_since = now
            matches.append(Match(track=track, detection=det, step_s=step))
            matched_idx.add(di)

        # Dedup por índice, no por id(det): dos detecciones idénticas
        # (mismo objeto Detection, p.ej. duplicado por el detector) deben
        # abrir dos pistas, no una — id() las confundiría en una sola.
        resumed: list[Resume] = []
        for di, det in enumerate(persons):
            if di in matched_idx:
                continue
            track = Track(
                track_id=self._next_id,
                bbox=det.bbox,
                last_seen=now,
                still_bbox=det.bbox,
                still_since=now,
            )
            self._next_id += 1
            # Una persona ya presente en una pista viva de la cámara no hereda
            # su identidad aparcada (sería un segundo "ella"): la entrada
            # aparcada caduca por su cuenta.
            live_ids = {
                t.identity.person_id for t in cam.tracks if t.identity is not None
            }
            best = max(
                (p for p in cam.parked if p.identity.person_id not in live_ids),
                key=lambda p: _iou(p.bbox, det.bbox),
                default=None,
            )
            if best is not None and _iou(best.bbox, det.bbox) >= RESUME_IOU:
                track.identity = replace(best.identity, provisional=True)
                cam.parked.remove(best)
                resumed.append(Resume(track=track, previous_track_id=best.track_id))
            cam.tracks.append(track)
            matches.append(Match(track=track, detection=det, step_s=0.0))
        return TrackerUpdate(
            matches=matches,
            lost=lost,
            parked=parked,
            resumed=resumed,
            expired=expired,
        )

    def _match(
        self, tracks: list[Track], persons: list[Detection]
    ) -> list[tuple[Track, int]]:
        """Greedy por mayor IoU, luego por cercanía de centros.

        Devuelve (Track, índice en `persons`) — no la Detection directamente,
        para que el emparejamiento y el dedup en `observe` funcionen por
        posición y no por identidad de objeto.
        """
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
        result: list[tuple[Track, int]] = []
        for iou, ti, di in pairs:
            if iou < IOU_MATCH or ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            result.append((tracks[ti], di))

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
            result.append((tracks[ti], di))
        return result
