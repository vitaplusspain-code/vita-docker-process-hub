"""Galería de personas enroladas: /data/faces/<person_id>/*.jpg → embedding medio.

Sin caché a propósito: recalcular al arrancar tarda segundos y hace que
añadir o quitar fotos sea solo reiniciar el hub. Los embeddings viven solo
en memoria; del hogar no sale ninguno.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from vitahub.config import ConfigError
from vitahub.identity.base import FaceEngine

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
    """Falla con ConfigError legible: es un error de instalación y el técnico está delante."""
    if not faces_dir.is_dir():
        raise ConfigError(
            f"carpeta de caras no existe: {faces_dir} — crea /data/faces/<person_id>/ "
            "con 3-5 fotos de la cara (distintas luces y ángulos)"
        )
    people: dict[str, npt.NDArray[np.float32]] = {}
    for person_dir in sorted(p for p in faces_dir.iterdir() if p.is_dir()):
        embeddings: list[npt.NDArray[np.float32]] = []
        for photo in sorted(person_dir.iterdir()):
            if photo.suffix.lower() not in _PHOTO_SUFFIXES:
                continue
            image = cv2.imread(str(photo))
            if image is None:
                raise ConfigError(f"foto de enrolamiento ilegible: {photo}")
            faces = engine.extract(image)
            if len(faces) != 1:
                # 0 caras = foto inútil; 2+ = ambigua (¿cuál es la persona?).
                raise ConfigError(
                    f"la foto de enrolamiento {photo} tiene {len(faces)} caras; "
                    "cada foto debe tener exactamente una"
                )
            embeddings.append(faces[0].embedding)
        if not embeddings:
            raise ConfigError(
                f"la carpeta {person_dir} no tiene fotos válidas (.jpg/.jpeg/.png)"
            )
        people[person_dir.name] = mean_embedding(embeddings)
    if not people:
        raise ConfigError(
            f"ninguna persona enrolada en {faces_dir} — crea /data/faces/<person_id>/ "
            "con 3-5 fotos de la cara (distintas luces y ángulos)"
        )
    return Gallery(people=people)
