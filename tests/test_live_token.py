import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from vitahub.live.token import (
    SessionClaims,
    SessionKeyError,
    TokenRejected,
    load_session_keys,
    verify_token,
)

_PRIV = ec.generate_private_key(ec.SECP256R1())
_OTHER = ec.generate_private_key(ec.SECP256R1())


def _pem(key: ec.EllipticCurvePrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


_KEYS = load_session_keys({"k1": _pem(_PRIV)})


def _token(key: ec.EllipticCurvePrivateKey = _PRIV, kid: str = "k1", **over: object) -> str:
    claims: dict[str, object] = {
        "sid": "s-1", "sub": "acc-1", "hub": "hub-1", "iat": 1000, "exp": 1600,
    }
    claims.update(over)
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid})


def test_valid_token_returns_claims():
    assert verify_token(_token(), _KEYS, "hub-1", now=1100) == SessionClaims(
        sid="s-1", sub="acc-1", hub="hub-1", exp=1600.0
    )


def test_unknown_kid_is_rejected():
    assert verify_token(_token(kid="k9"), _KEYS, "hub-1", 1100) == TokenRejected("unknown_kid")


def test_signature_from_another_key_is_rejected():
    # Mismo kid, otra clave: el caso de un token fabricado por quien no tiene KMS.
    assert verify_token(_token(key=_OTHER), _KEYS, "hub-1", 1100) == TokenRejected(
        "bad_signature"
    )


def test_expired_token_is_rejected():
    assert verify_token(_token(), _KEYS, "hub-1", now=1600) == TokenRejected("expired")


def test_token_for_another_hub_is_rejected():
    # Un token válido de otra casa no abre esta: credenciales de viewer y token
    # van acotados al mismo hub, pero el hub no se fía y lo comprueba.
    assert verify_token(_token(), _KEYS, "hub-2", 1100) == TokenRejected("wrong_hub")


@pytest.mark.parametrize("raw", ["", "abc", "a.b.c"])
def test_garbage_is_malformed(raw: str):
    assert verify_token(raw, _KEYS, "hub-1", 1100) == TokenRejected("malformed")


def test_missing_claim_is_malformed():
    token = jwt.encode(
        {"sub": "acc-1", "hub": "hub-1", "iat": 1000, "exp": 1600},
        _PRIV, algorithm="ES256", headers={"kid": "k1"},
    )
    assert verify_token(token, _KEYS, "hub-1", 1100) == TokenRejected("malformed")


def test_hs256_with_known_kid_is_malformed():
    # Confusión de algoritmo: un HS256 firmado con el PEM público como secreto
    # no debe pasar nunca. algorithms=["ES256"] lo cierra.
    token = jwt.encode(
        {"sid": "s", "sub": "a", "hub": "hub-1", "iat": 1, "exp": 9999},
        "secreto", algorithm="HS256", headers={"kid": "k1"},
    )
    assert verify_token(token, _KEYS, "hub-1", 1100) == TokenRejected("malformed")


def test_load_rejects_unreadable_pem():
    with pytest.raises(SessionKeyError, match="k1"):
        load_session_keys({"k1": "-----BEGIN PUBLIC KEY-----\nbasura\n-----END PUBLIC KEY-----"})


def test_load_rejects_non_p256_key():
    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    with pytest.raises(SessionKeyError, match="P-256"):
        load_session_keys({"k1": rsa_pem})
