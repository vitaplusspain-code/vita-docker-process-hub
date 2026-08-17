import json
import urllib.error
import urllib.request

import pytest

from vitahub.control import admin_port, start_control_server
from vitahub.rescan import RescanResult


class _FakeService:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def run_once(self):
        self.calls += 1
        return self.result


def _ok_result():
    return RescanResult(
        status="ok",
        found=2,
        added=[{"id": "onvif-a", "name": "camera-2", "ip": "10.0.0.9"}],
        ip_changed=[],
        started=["onvif-a"],
        cameras=2,
    )


@pytest.fixture
def server_factory():
    servers = []

    def _start(service, token="secreto"):
        httpd = start_control_server(service, token, port=0, host="127.0.0.1")
        assert httpd is not None
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _post(url, token=None):
    request = urllib.request.Request(url, method="POST", data=b"")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(request, timeout=5)


def test_valid_token_runs_rescan_and_returns_json(server_factory):
    service = _FakeService(_ok_result())
    base = server_factory(service)

    with _post(f"{base}/rescan", token="secreto") as response:
        assert response.status == 200
        body = json.loads(response.read())

    assert service.calls == 1
    assert body == {
        "found": 2,
        "added": [{"id": "onvif-a", "name": "camera-2", "ip": "10.0.0.9"}],
        "ip_changed": [],
        "started": ["onvif-a"],
        "cameras": 2,
    }


def test_wrong_token_is_rejected(server_factory):
    service = _FakeService(_ok_result())
    base = server_factory(service)

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/rescan", token="incorrecto")

    assert exc.value.code == 401
    assert service.calls == 0  # no llegó a escanear


def test_missing_token_is_rejected(server_factory):
    service = _FakeService(_ok_result())
    base = server_factory(service)

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/rescan")

    assert exc.value.code == 401
    assert service.calls == 0


def test_unknown_path_is_404(server_factory):
    base = server_factory(_FakeService(_ok_result()))

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/otra-cosa", token="secreto")

    assert exc.value.code == 404


def test_get_is_404(server_factory):
    base = server_factory(_FakeService(_ok_result()))

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base}/rescan", timeout=5)

    assert exc.value.code == 404


def test_busy_returns_409(server_factory):
    base = server_factory(_FakeService(RescanResult(status="busy")))

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/rescan", token="secreto")

    assert exc.value.code == 409


def test_discovery_error_returns_500(server_factory):
    base = server_factory(_FakeService(RescanResult(status="error")))

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/rescan", token="secreto")

    assert exc.value.code == 500


def test_no_token_configured_does_not_open_a_port():
    assert start_control_server(_FakeService(_ok_result()), "", port=0) is None


def test_token_is_registered_for_log_redaction(monkeypatch, server_factory):
    registered = []
    monkeypatch.setattr("vitahub.control.register_secret", registered.append)
    server_factory(_FakeService(_ok_result()), token="secreto")
    assert registered == ["secreto"]


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, 8787),
        ({"VITAHUB_ADMIN_PORT": "9000"}, 9000),
        ({"VITAHUB_ADMIN_PORT": "no-es-un-puerto"}, 8787),
        ({"VITAHUB_ADMIN_PORT": "0"}, 8787),
        ({"VITAHUB_ADMIN_PORT": "70000"}, 8787),
    ],
)
def test_admin_port_parsing(env, expected):
    assert admin_port(env) == expected


def test_malformed_authorization_header_is_rejected(server_factory):
    """Cabecera Authorization con bytes no ASCII se trata como credencial
    inválida (401), sin excepción que cierre la conexión silenciosamente."""
    service = _FakeService(_ok_result())
    base = server_factory(service)

    request = urllib.request.Request(f"{base}/rescan", method="POST", data=b"")
    # Simulamos bytes no ASCII que causarían TypeError en compare_digest
    request.add_header("Authorization", "Bearer \xff\xfe")

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)

    assert exc.value.code == 401
    assert service.calls == 0  # no llegó a escanear


def test_handler_has_socket_timeout():
    """El handler debe tener timeout para evitar slow-loris attacks."""
    from vitahub.control import _build_handler

    handler_class = _build_handler(_FakeService(_ok_result()), "token")
    assert hasattr(handler_class, "timeout")
    assert handler_class.timeout == 10
