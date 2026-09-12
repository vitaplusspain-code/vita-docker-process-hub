"""Enrolamiento por API: personas y fotos en /data/faces/<person_id>/.

Lógica pura, sin HTTP. Las reglas de validación son las mismas que aplica
load_gallery al arrancar: una foto aceptada aquí jamás rompe el arranque.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from vitahub.identity.base import FaceEngine

_PERSON_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_PHOTO_NAME_RE = re.compile(r"^\d{3}\.jpg$")


class EnrollmentError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class PersonSummary:
    id: str
    photos: list[str]


def _check_person_id(person_id: str) -> None:
    if not _PERSON_ID_RE.match(person_id):
        raise EnrollmentError(
            "person_id inválido: solo minúsculas, dígitos y guiones (máx. 32)"
        )


def _check_photo_name(photo: str) -> None:
    # El nombre viene de la URL: sin este patrón sería una ruta arbitraria.
    if not _PHOTO_NAME_RE.match(photo):
        raise EnrollmentError("nombre de foto inválido")


def list_people(faces_dir: Path) -> list[PersonSummary]:
    if not faces_dir.is_dir():
        return []
    people = []
    for person_dir in sorted(
        p for p in faces_dir.iterdir()
        if p.is_dir() and _PERSON_ID_RE.match(p.name)
    ):
        photos = sorted(
            p.name for p in person_dir.iterdir() if _PHOTO_NAME_RE.match(p.name)
        )
        people.append(PersonSummary(id=person_dir.name, photos=photos))
    return people


def photo_bytes(faces_dir: Path, person_id: str, photo: str) -> bytes:
    _check_person_id(person_id)
    _check_photo_name(photo)
    path = faces_dir / person_id / photo
    if not path.is_file():
        raise EnrollmentError("foto no encontrada", status=404)
    return path.read_bytes()


def save_photo(
    faces_dir: Path, person_id: str, data: bytes, engine: FaceEngine
) -> str:
    _check_person_id(person_id)
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise EnrollmentError("imagen ilegible: sube un JPEG o PNG válido")
    faces = engine.extract(image)
    if len(faces) != 1:
        raise EnrollmentError(
            f"la foto tiene {len(faces)} caras; debe tener exactamente una"
        )
    person_dir = faces_dir / person_id
    person_dir.mkdir(parents=True, exist_ok=True)
    existing = [
        int(p.stem) for p in person_dir.iterdir() if _PHOTO_NAME_RE.match(p.name)
    ]
    name = f"{max(existing, default=0) + 1:03d}.jpg"
    # Se reencoda siempre a JPEG: normaliza el formato (aunque llegue PNG),
    # descarta EXIF y garantiza que lo guardado es exactamente lo validado.
    ok, buf = cv2.imencode(".jpg", image)
    if not ok:
        raise EnrollmentError("no se pudo codificar la imagen")
    # Mismo patrón que write_settings: tempfile en el propio directorio +
    # fsync + os.replace, para que un corte de luz o una conexión perdida a
    # mitad de escritura jamás deje un JPEG truncado en la galería.
    fd, tmp = tempfile.mkstemp(dir=str(person_dir), prefix=".photo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(buf))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, person_dir / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return name


def delete_photo(faces_dir: Path, person_id: str, photo: str) -> None:
    _check_person_id(person_id)
    _check_photo_name(photo)
    path = faces_dir / person_id / photo
    if not path.is_file():
        raise EnrollmentError("foto no encontrada", status=404)
    path.unlink()


def delete_person(faces_dir: Path, person_id: str) -> None:
    _check_person_id(person_id)
    path = faces_dir / person_id
    if not path.is_dir():
        raise EnrollmentError("persona no encontrada", status=404)
    shutil.rmtree(path)
