import json

import pytest

from vitahub.live.credentials import AwsCredentials, IotCredentialsProvider, parse_credentials


def _body(exp: str = "2026-10-15T10:00:00Z", key: str = "AK") -> bytes:
    return json.dumps(
        {"credentials": {"accessKeyId": key, "secretAccessKey": "SK",
                         "sessionToken": "TK", "expiration": exp}}
    ).encode()


def test_parse_credentials():
    creds = parse_credentials(_body())
    assert creds == AwsCredentials("AK", "SK", "TK", 1792058400.0)


@pytest.mark.parametrize("body", [b"no json", b"{}", b'{"credentials": {"accessKeyId": "AK"}}'])
def test_parse_rejects_incomplete(body):
    with pytest.raises(ValueError):
        parse_credentials(body)


def test_provider_caches_until_margin():
    calls: list[int] = []
    now = [1792058400.0 - 3600]

    def fetch() -> bytes:
        calls.append(1)
        return _body(key=f"AK{len(calls)}")

    provider = IotCredentialsProvider(fetch, clock=lambda: now[0], refresh_margin_s=300)
    assert provider.get().access_key == "AK1"
    now[0] = 1792058400.0 - 301
    assert provider.get().access_key == "AK1"
    # Dentro del margen se renueva: las credenciales que se firman ahora deben
    # seguir vivas mientras dura la conexión WSS que abren.
    now[0] = 1792058400.0 - 299
    assert provider.get().access_key == "AK2"
    assert len(calls) == 2
