"""Galería de personas enroladas: /data/faces/<person_id>/*.jpg → embedding medio.

Sin caché a propósito: recalcular al arrancar tarda segundos y hace que
añadir o quitar fotos sea solo reiniciar el hub. Los embeddings viven solo
en memoria; del hogar no sale ninguno.

Sin personas la galería sale vacía: el hub arranca sin identidad en vez de
fallar (la nube puede vaciar la galería). Los directorios que empiezan por "."
son temporales de la sincronización y se ignoran.

Las carpetas con forma de `person_key` son de la nube: una foto mala ahí se
salta con un aviso, porque nadie está delante para arreglarla y el hub no puede
dejar de arrancar por eso. Las del técnico siguen siendo estrictas.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from vitahub.config import ConfigError
from vitahub.gallery.manifest import PERSON_KEY_RE
from vitahub.identity.base import FaceEngine

logger = logging.getLogger(__name__)

_PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png")


@dataclass(frozen=True)
class Gallery:
    people: dict[str, npt.NDArray[np.float32]]


def cosine_similarity(
    a: npt.NDArray[np.float32], b: npt.NDArray[np.float32]
) -> float:
    # Ambos llegan L2-normalizados (contrato de FaceObservation y de Gallery):
    # el coseno es el producto escalar, sin división que pueda ser por cero.
    return float(np.dot(a, b))


def mean_embedding(
    embeddings: list[npt.NDArray[np.float32]],
) -> npt.NDArray[np.float32]:
    mean = np.mean(np.stack(embeddings), axis=0)
    norm = float(np.linalg.norm(mean))
    if norm == 0.0:
        raise ConfigError("embeddings de enrolamiento degenerados (media nula)")
    return np.asarray(mean / norm, dtype=np.float32)


def best_match(
    embedding: npt.NDArray[np.float32], gallery: Gallery, threshold: float
) -> tuple[str, float] | None:
    best: tuple[str, float] | None = None
    for person_id, ref in gallery.people.items():
        sim = cosine_similarity(embedding, ref)
        if best is None or sim > best[1]:
            best = (person_id, sim)
    if best is None or best[1] < threshold:
        return None
    return best


def load_gallery(faces_dir: Path, engine: FaceEngine) -> Gallery:
    """Carpeta inexistente, foto ilegible o persona sin fotos válidas: ConfigError legible
    (error de instalación, el técnico está delante). Sin personas: galería vacía.
    En carpetas de la nube (`person_key`) la foto o la persona inválida se salta con aviso."""
    if not faces_dir.is_dir():
        raise ConfigError(
            f"carpeta de caras no existe: {faces_dir} — crea /data/faces/<person_id>/ "
            "con 3-5 fotos de la cara (distintas luces y ángulos)"
        )
    people: dict[str, npt.NDArray[np.float32]] = {}
    for person_dir in sorted(p for p in faces_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        cloud = PERSON_KEY_RE.fullmatch(person_dir.name) is not None
        embeddings: list[npt.NDArray[np.float32]] = []
        for photo in sorted(person_dir.iterdir()):
            if photo.suffix.lower() not in _PHOTO_SUFFIXES:
                continue
            image = cv2.imread(str(photo))
            if image is None:
                if cloud:
                    logger.warning("galería: se ignora la foto ilegible %s de %s", photo.name, person_dir.name)
                    continue
                raise ConfigError(f"foto de enrolamiento ilegible: {photo}")
            faces = engine.extract(image)
            if len(faces) != 1:
                if cloud:
                    logger.warning(
                        "galería: se ignora la foto %s de %s: tiene %d caras",
                        photo.name,
                        person_dir.name,
                        len(faces),
                    )
                    continue
                # 0 caras = foto inútil; 2+ = ambigua (¿cuál es la persona?).
                raise ConfigError(
                    f"la foto de enrolamiento {photo} tiene {len(faces)} caras; "
                    "cada foto debe tener exactamente una"
                )
            embeddings.append(faces[0].embedding)
        if not embeddings:
            if cloud:
                logger.warning("galería: se ignora %s: no tiene fotos válidas", person_dir.name)
                continue
            raise ConfigError(
                f"la carpeta {person_dir} no tiene fotos válidas (.jpg/.jpeg/.png)"
            )
        people[person_dir.name] = mean_embedding(embeddings)
    return Gallery(people=people)
