"""Etiquetado oportunista de pistas: la cara se busca mientras la pista es anónima.

No hace falta ver la cara durante la caída: si el hub se la vio al entrar o
al sentarse, la etiqueta viaja con la pista y el fall_detected sale ya
identificado. El coste se controla: solo se extraen caras si hay alguna
pista sin identidad, y como mucho una vez por segundo y por cámara (una
extracción sirve a todas las pistas del frame). Ese ahorro no es gratis para
siempre: una persona no enrolada es anónima para siempre, así que mientras
esté en cámara la extracción sigue corriendo 1 vez/s por cámara
indefinidamente — no hay backoff para "esta pista nunca va a matchear".
"""
from __future__ import annotations

from vitahub.analytics.tracker import Match, Track, TrackIdentity
from vitahub.identity.base import FaceEngine, FaceObservation
from vitahub.identity.gallery import Gallery, best_match
from vitahub.logging_setup import get_logger

_log = get_logger("identity")

# Alto mínimo de cara en píxeles para intentar el match: por debajo (persona
# lejos, substream) el embedding es ruido y produce falsos matches.
MIN_FACE_PX = 40
# Cadencia máxima de extracción por cámara mientras haya pistas anónimas.
ATTEMPT_EVERY_S = 1.0


def _contains(bbox: tuple[int, int, int, int], x: float, y: float) -> bool:
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _area(bbox: tuple[int, int, int, int]) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def _owner(face: FaceObservation, matches: list[Match]) -> Track | None:
    """La pista cuyo bbox contiene el centro de la cara; la más ajustada si hay varias."""
    cx = (face.bbox[0] + face.bbox[2]) / 2.0
    cy = (face.bbox[1] + face.bbox[3]) / 2.0
    candidates = [m.track for m in matches if _contains(m.track.bbox, cx, cy)]
    if not candidates:
        return None
    return min(candidates, key=lambda t: _area(t.bbox))


class FaceIdentifier:
    def __init__(self, engine: FaceEngine, gallery: Gallery, match_threshold: float) -> None:
        self._engine = engine
        self._gallery = gallery
        self._threshold = match_threshold
        self._last_attempt: dict[str, float] = {}

    def identify(self, frame: object, camera_id: str, matches: list[Match], now: float) -> None:
        if not any(m.track.identity is None for m in matches):
            return
        last = self._last_attempt.get(camera_id)
        if last is not None and now - last < ATTEMPT_EVERY_S:
            return
        self._last_attempt[camera_id] = now

        for face in self._engine.extract(frame):
            if face.bbox[3] - face.bbox[1] < MIN_FACE_PX:
                continue
            track = _owner(face, matches)
            if track is None:
                continue
            matched = best_match(face.embedding, self._gallery, self._threshold)
            if matched is None:
                continue
            person_id, sim = matched
            self._apply(camera_id, track, person_id, sim)

    def _apply(self, camera_id: str, track: Track, person_id: str, sim: float) -> None:
        if track.identity is not None:
            # Corrección de cruce de pistas: solo si otra persona aparece con
            # una similitud claramente mejor que la que etiquetó la pista.
            if person_id != track.identity.person_id and sim > track.identity.confidence:
                _log.warning(
                    "cam %s pista %d: identidad corregida %s → %s (sim %.2f > %.2f)",
                    camera_id, track.track_id, track.identity.person_id, person_id,
                    sim, track.identity.confidence,
                )
                track.identity = TrackIdentity(person_id, sim)
            return
        # Regla de 2 coincidencias: una primera vista deja la identidad en
        # pendiente y solo se confirma si la siguiente extracción coincide
        # con la misma persona. Una sola coincidencia puede ser un frame
        # ruidoso; dos consecutivas ya bastan.
        pending = track.pending_identity
        if pending is not None and pending.person_id == person_id:
            track.identity = TrackIdentity(person_id, max(sim, pending.confidence))
            track.pending_identity = None
            return
        track.pending_identity = TrackIdentity(person_id, sim)
