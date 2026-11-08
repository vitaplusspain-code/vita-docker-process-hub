"""Manifiesto de fotos de identidad en S3 (`hubs/<hub_id>/faces/manifest.json`)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from vitahub.admin.enrollment import CLOUD_PHOTO_ID_RE

PERSON_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class Manifest:
    version: int
    persons: dict[str, tuple[str, ...]]


class ManifestError(Exception):
    """El manifiesto está roto o no cumple el contrato."""


def parse_manifest(raw: bytes) -> Manifest:
    """Valida el manifiesto entero: claves y photoId acaban en rutas de disco y S3."""
    try:
        data = json.loads(raw)
    except ValueError as exc:  # incluye UnicodeDecodeError
        raise ManifestError(f"JSON inválido: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("el manifiesto no es un objeto")
    version = data.get("version")
    # bool es subclase de int: se descarta a mano.
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ManifestError("version no es un entero >= 0")
    persons_raw = data.get("persons")
    if not isinstance(persons_raw, dict):
        raise ManifestError("persons no es un objeto")
    persons: dict[str, tuple[str, ...]] = {}
    for key, photos in persons_raw.items():
        if not PERSON_KEY_RE.fullmatch(key):
            raise ManifestError(f"person_key inválida: {key!r}")
        if not isinstance(photos, list):
            raise ManifestError(f"las fotos de {key} no son una lista")
        ids: list[str] = []
        for photo_id in photos:
            if not isinstance(photo_id, str) or not CLOUD_PHOTO_ID_RE.fullmatch(photo_id):
                raise ManifestError(f"photoId inválido en {key}: {photo_id!r}")
            if photo_id not in ids:
                ids.append(photo_id)
        persons[key] = tuple(ids)
    return Manifest(version=version, persons=persons)
