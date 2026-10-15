from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class AwsCredentials:
    access_key: str
    secret_key: str
    token: str
    expiration: float


def parse_credentials(body: bytes) -> AwsCredentials:
    try:
        creds = json.loads(body)["credentials"]
        return AwsCredentials(
            access_key=str(creds["accessKeyId"]),
            secret_key=str(creds["secretAccessKey"]),
            token=str(creds["sessionToken"]),
            expiration=datetime.fromisoformat(str(creds["expiration"])).timestamp(),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"respuesta del credentials provider inválida: {exc}") from exc


class IotCredentialsProvider:
    """Credenciales temporales del rol `hub-live`, en caché hasta cerca de caducar.

    Sin secretos nuevos en el hogar: se piden con el mismo certificado X.509
    que ya usa el uplink (spec §3.1). Thread-safe porque las llamadas de
    botocore corren en el executor, no en el loop.
    """

    def __init__(
        self,
        fetch: Callable[[], bytes],
        clock: Callable[[], float] = time.time,
        refresh_margin_s: float = 300.0,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._margin = refresh_margin_s
        self._cached: AwsCredentials | None = None
        self._lock = threading.Lock()

    def get(self) -> AwsCredentials:
        with self._lock:
            if self._cached is None or self._cached.expiration - self._clock() <= self._margin:
                self._cached = parse_credentials(self._fetch())
            return self._cached


def https_fetcher(
    endpoint: str, role_alias: str, thing_name: str, ca: Path, cert: Path, key: Path
) -> Callable[[], bytes]:
    """GET al credentials provider con mTLS. Runtime only: requiere la cuenta real."""
    ctx = ssl.create_default_context(cafile=str(ca))
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    url = f"https://{endpoint}/role-aliases/{role_alias}/credentials"

    def _fetch() -> bytes:
        req = urllib.request.Request(url, headers={"x-amzn-iot-thingname": thing_name})
        with urllib.request.urlopen(req, context=ctx, timeout=_TIMEOUT_S) as resp:
            body: bytes = resp.read()
            return body

    return _fetch
