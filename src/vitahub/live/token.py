from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_public_key

_REQUIRED = ["sid", "sub", "hub", "iat", "exp"]


class SessionKeyError(ValueError):
    pass


@dataclass(frozen=True)
class SessionClaims:
    sid: str
    sub: str
    hub: str
    exp: float


@dataclass(frozen=True)
class TokenRejected:
    # malformed | unknown_kid | bad_signature | expired | wrong_hub. Solo para
    # logs: al viewer le llega siempre `closed:invalid`, sin pistas de por qué.
    reason: str


def load_session_keys(raw: Mapping[str, str]) -> dict[str, ec.EllipticCurvePublicKey]:
    """PEM por `kid` → claves P-256. Falla con el `kid` en el mensaje.

    Se llama desde `load_config`: una clave mal pegada en hub.yaml durante la
    instalación debe parar el arranque con un mensaje que diga cuál, no
    descubrirse cuando el primer familiar intente ver y reciba `invalid`.
    """
    keys: dict[str, ec.EllipticCurvePublicKey] = {}
    for kid, pem in raw.items():
        try:
            key = load_pem_public_key(pem.encode())
        except ValueError as exc:
            raise SessionKeyError(f"clave de sesión {kid!r}: PEM ilegible ({exc})") from exc
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise SessionKeyError(f"clave de sesión {kid!r}: no es una clave pública P-256")
        keys[kid] = key
    return keys


def verify_token(
    token: str,
    keys: Mapping[str, ec.EllipticCurvePublicKey],
    hub_id: str,
    now: float,
) -> SessionClaims | TokenRejected:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return TokenRejected("malformed")
    key = keys.get(str(header.get("kid", "")))
    if key is None:
        return TokenRejected("unknown_kid")
    try:
        # exp se comprueba a mano contra `now` inyectado: PyJWT usaría el reloj
        # del sistema y el test de caducidad dependería de la hora real.
        payload = jwt.decode(
            token,
            key,
            algorithms=["ES256"],
            options={"verify_exp": False, "verify_iat": False, "require": _REQUIRED},
        )
    except jwt.InvalidSignatureError:
        return TokenRejected("bad_signature")
    except jwt.InvalidTokenError:
        return TokenRejected("malformed")
    if payload["hub"] != hub_id:
        return TokenRejected("wrong_hub")
    exp = float(payload["exp"])
    if exp <= now:
        return TokenRejected("expired")
    return SessionClaims(
        sid=str(payload["sid"]), sub=str(payload["sub"]), hub=str(payload["hub"]), exp=exp
    )
