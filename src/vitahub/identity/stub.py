from __future__ import annotations

from vitahub.identity.base import FaceEngine, FaceObservation


class StubFaceEngine(FaceEngine):
    """Motor sin modelo: devuelve respuestas programadas, una por llamada.

    Agotada la lista, devuelve [] (ninguna cara): permite simular "la cara se
    vio dos veces y luego dejó de verse" sin tocar el reloj.
    """

    def __init__(self, responses: list[list[FaceObservation]]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def extract(self, image: object) -> list[FaceObservation]:
        self.calls += 1
        if not self._responses:
            return []
        return self._responses.pop(0)
