"""Lectura de S3 para las fotos de identidad (`hubs/<hub_id>/faces/`).

Solo lectura y solo runtime, como `s3_put` de los clips: requiere la cuenta
real y botocore se importa dentro de las funciones para que los tests no lo
necesiten. Las credenciales se piden en cada llamada porque rotan.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vitahub.live.credentials import IotCredentialsProvider


def gallery_key(hub_id: str, *parts: str) -> str:
    """Clave S3 bajo la carpeta de caras del hub. Sin `parts` es el prefijo de listado."""
    base = f"hubs/{hub_id}/faces/"
    return base + "/".join(parts)


def _client(region: str, creds: IotCredentialsProvider) -> Any:
    import botocore.session

    c = creds.get()  # en cada llamada: las credenciales rotan
    return botocore.session.get_session().create_client(
        "s3",
        region_name=region,
        aws_access_key_id=c.access_key,
        aws_secret_access_key=c.secret_key,
        aws_session_token=c.token,
    )


def s3_list(
    bucket: str, region: str, creds: IotCredentialsProvider
) -> Callable[[str], frozenset[str]]:
    """`ListObjectsV2` paginado: claves completas bajo un prefijo.

    El prefijo debe acabar en `/`: la política del rol solo concede
    `s3:ListBucket` con la condición `s3:prefix` y S3 deniega el listado sin la
    barra final. Se lista antes de leer porque un `GetObject` de una clave
    inexistente devuelve `403`, no `404`.
    """

    def _list(prefix: str) -> frozenset[str]:
        if not prefix.endswith("/"):
            raise ValueError(f"el prefijo de listado debe acabar en '/': {prefix!r}")
        client = _client(region, creds)
        keys: set[str] = set()
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            keys.update(obj["Key"] for obj in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return frozenset(keys)
            token = page["NextContinuationToken"]

    return _list


def s3_get(
    bucket: str, region: str, creds: IotCredentialsProvider
) -> Callable[[str], bytes]:
    """`GetObject`. Cualquier error se propaga: solo se piden claves ya listadas."""

    def _get(key: str) -> bytes:
        client = _client(region, creds)
        body: bytes = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return body

    return _get
