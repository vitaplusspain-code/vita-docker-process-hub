"""Aplica un manifiesto de la nube a /data/faces con sustitución por carpeta.

Todo se descarga y valida antes de tocar `faces_dir`. Staging y old viven dentro
de `faces_dir` (os.replace exige el mismo sistema de ficheros) y empiezan por
punto: `load_gallery` los ignora, así que un corte a medias nunca los carga.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vitahub.admin.enrollment import (
    PhotoRejected,
    decode_photo,
    encode_jpeg,
    validate_photo,
    write_atomic,
)
from vitahub.gallery.manifest import PERSON_KEY_RE, Manifest
from vitahub.gallery.shadow import PhotoStatus
from vitahub.identity.base import FaceEngine

logger = logging.getLogger(__name__)

STAGING_DIR = ".cloud-staging"
OLD_DIR = ".cloud-old"

# (person_key, photo_id) -> bytes, o None si la foto no está en el listado de S3.
# Cualquier otro fallo lanza.
FetchPhoto = Callable[[str, str], bytes | None]


@dataclass(frozen=True)
class ApplyResult:
    statuses: dict[str, PhotoStatus]
    changed: bool


def _snapshot(faces_dir: Path, keys: set[str]) -> dict[str, frozenset[str]]:
    return {
        key: frozenset(p.name for p in (faces_dir / key).iterdir())
        for key in keys
        if (faces_dir / key).is_dir()
    }


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Algunos sistemas de ficheros no admiten fsync de directorios.
        pass
    finally:
        os.close(fd)


def apply_manifest(
    faces_dir: Path, manifest: Manifest, fetch: FetchPhoto, engine: FaceEngine
) -> ApplyResult:
    faces_dir.mkdir(parents=True, exist_ok=True)
    staging = faces_dir / STAGING_DIR
    old = faces_dir / OLD_DIR
    # Restos de un corte anterior.
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)

    # Fase 1: descargar y validar todo. Si fetch lanza, el disco queda igual.
    statuses: dict[str, PhotoStatus] = {}
    accepted: dict[str, dict[str, bytes]] = {}
    for key, photo_ids in manifest.persons.items():
        for photo_id in photo_ids:
            data = fetch(key, photo_id)
            if data is None:
                continue  # pendiente: ni se escribe ni se reporta
            try:
                # Se valida el JPEG reencodado, que es lo que se escribe y lo que
                # load_gallery leerá al arrancar, no la foto original.
                jpeg = encode_jpeg(decode_photo(data))
                validate_photo(jpeg, engine)
            except PhotoRejected as exc:
                statuses[photo_id] = ("rejected", exc.reason)
                continue
            statuses[photo_id] = ("accepted", None)
            accepted.setdefault(key, {})[photo_id] = jpeg

    existing = {
        p.name for p in faces_dir.iterdir() if p.is_dir() and PERSON_KEY_RE.fullmatch(p.name)
    }
    targets = set(manifest.persons) | existing
    before = _snapshot(faces_dir, targets)

    # Fase 2: staging fuera de las carpetas vivas.
    for key, photos in accepted.items():
        folder = staging / key
        folder.mkdir(parents=True)
        for photo_id, jpeg in photos.items():
            write_atomic(folder / f"{photo_id}.jpg", jpeg)
        # Las entradas de la carpeta deben estar en disco antes de moverla a su
        # sitio: si no, un corte tras el rename puede dejar una persona vacía.
        _fsync_dir(folder)

    # Fase 3: sustitución carpeta a carpeta.
    old.mkdir(exist_ok=True)
    for key in sorted(targets):
        live = faces_dir / key
        if key in accepted:
            if live.exists():
                os.replace(live, old / key)
            os.replace(staging / key, live)
        elif live.exists():
            os.replace(live, old / key)

    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    _fsync_dir(faces_dir)

    after = _snapshot(faces_dir, targets)
    changed = before != after
    logger.info(
        "manifiesto %d aplicado: %d personas, %d fotos aceptadas, cambios=%s",
        manifest.version,
        len(accepted),
        sum(len(p) for p in accepted.values()),
        changed,
    )
    return ApplyResult(statuses=statuses, changed=changed)
