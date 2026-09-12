import json
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import pytest

from vitahub.control import AdminContext, admin_port, start_control_server
from vitahub.identity.base import FaceObservation
from vitahub.identity.stub import StubFaceEngine
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


def test_put_is_404_empty(server_factory):
    """Método sin do_<METODO>: por defecto BaseHTTPRequestHandler contesta 501
    con cuerpo HTML (revela detalles). El diseño exige el mismo 404 vacío que
    el resto de rutas/métodos no contemplados."""
    base = server_factory(_FakeService(_ok_result()))
    request = urllib.request.Request(f"{base}/rescan", method="PUT", data=b"")

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)

    assert exc.value.code == 404
    assert exc.value.read() == b""


def test_delete_is_404_empty(server_factory):
    base = server_factory(_FakeService(_ok_result()))
    request = urllib.request.Request(f"{base}/rescan", method="DELETE")

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)

    assert exc.value.code == 404
    assert exc.value.read() == b""


def test_no_response_leaks_a_server_header(server_factory):
    """Ninguna respuesta (200, 401 o 404) debe llevar una cabecera Server que
    revele BaseHTTPServer o la versión de Python a la WiFi del hogar."""
    service = _FakeService(_ok_result())
    base = server_factory(service)

    with _post(f"{base}/rescan", token="secreto") as response:
        header = response.headers.get("Server") or ""
    assert "Python" not in header
    assert "BaseHTTP" not in header

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/rescan", token="incorrecto")
    header = exc.value.headers.get("Server") or ""
    assert "Python" not in header
    assert "BaseHTTP" not in header

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            urllib.request.Request(f"{base}/rescan", method="PUT", data=b""), timeout=5
        )
    header = exc.value.headers.get("Server") or ""
    assert "Python" not in header
    assert "BaseHTTP" not in header


def test_server_uses_daemon_threads():
    """Sin daemon_threads, una petición en vuelo en el momento del shutdown()
    (hilo por request de ThreadingMixIn) retrasaría o impediría la salida
    del proceso."""
    httpd = start_control_server(_FakeService(_ok_result()), "secreto", port=0, host="127.0.0.1")
    assert httpd is not None
    try:
        assert httpd.daemon_threads is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def _jpeg_bytes() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((32, 32, 3), 128, dtype=np.uint8))
    assert ok
    return bytes(buf)


def _face() -> FaceObservation:
    emb = np.zeros(512, dtype=np.float32)
    emb[0] = 1.0
    return FaceObservation(bbox=(0, 0, 10, 10), embedding=emb)


def _request(url, method="GET", token=None, data=None, content_type=None):
    req = urllib.request.Request(url, method=method, data=data)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    if content_type is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


@pytest.fixture()
def admin_server(tmp_path: Path):
    """Servidor con AdminContext real sobre tmp_path y engine stub."""
    servers = []
    engine = StubFaceEngine([[_face()] for _ in range(10)])
    shutdowns: list[bool] = []
    ctx = AdminContext(
        faces_dir=tmp_path / "faces",
        config_path=tmp_path / "hub.yaml",
        env={"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"},
        engine_factory=lambda: engine,
        request_shutdown=lambda: shutdowns.append(True),
    )
    (tmp_path / "hub.yaml").write_text(
        "hub_id: hub-test\ninference:\n  detector: person_pose\n"
        "  fall:\n    enabled: true\ncameras: []\n"
    )

    def _start():
        httpd = start_control_server(
            _FakeService(_ok_result()), "secreto", port=0, host="127.0.0.1", admin=ctx
        )
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", shutdowns

    yield _start
    for s in servers:
        s.shutdown()


def test_api_requires_token(admin_server):
    base, _ = admin_server()
    status, _body = _request(f"{base}/api/people")
    assert status == 401


def test_people_lifecycle_over_http(admin_server):
    base, _ = admin_server()
    status, body = _request(f"{base}/api/people", token="secreto")
    assert (status, json.loads(body)) == (200, {"people": []})

    status, body = _request(
        f"{base}/api/people/maria/photos", method="POST", token="secreto",
        data=_jpeg_bytes(), content_type="image/jpeg",
    )
    assert status == 200
    assert json.loads(body) == {"saved": "001.jpg"}

    status, body = _request(f"{base}/api/people", token="secreto")
    assert json.loads(body) == {"people": [{"id": "maria", "photos": ["001.jpg"]}]}

    status, body = _request(
        f"{base}/api/people/maria/photos/001.jpg", token="secreto"
    )
    assert status == 200 and body[:2] == b"\xff\xd8"

    status, _body = _request(
        f"{base}/api/people/maria/photos/001.jpg", method="DELETE", token="secreto"
    )
    assert status == 200
    status, _body = _request(f"{base}/api/people/maria", method="DELETE", token="secreto")
    assert status == 200


def test_upload_invalid_image_is_400_with_message(admin_server):
    base, _ = admin_server()
    status, body = _request(
        f"{base}/api/people/maria/photos", method="POST", token="secreto",
        data=b"garbage", content_type="image/jpeg",
    )
    assert status == 400
    assert "ilegible" in json.loads(body)["error"]


def test_upload_too_large_is_413(admin_server):
    base, _ = admin_server()
    status, _body = _request(
        f"{base}/api/people/maria/photos", method="POST", token="secreto",
        data=b"x" * (10 * 1024 * 1024 + 1), content_type="image/jpeg",
    )
    assert status == 413


def test_api_without_admin_context_is_404(server_factory):
    # El fixture existente arranca sin AdminContext: nada de /api existe.
    base = server_factory(_FakeService(_ok_result()))
    status, _body = _request(f"{base}/api/people", token="secreto")
    assert status == 404


def test_config_roundtrip_over_http(admin_server):
    base, _ = admin_server()
    status, body = _request(f"{base}/api/config", token="secreto")
    assert status == 200
    assert json.loads(body) == {
        "fall_enabled": True, "identity_enabled": False, "match_threshold": 0.4,
    }

    payload = json.dumps({
        "fall_enabled": True, "identity_enabled": True, "match_threshold": 0.5,
    }).encode()
    status, _body = _request(
        f"{base}/api/config", method="PUT", token="secreto",
        data=payload, content_type="application/json",
    )
    assert status == 200

    status, body = _request(f"{base}/api/config", token="secreto")
    assert json.loads(body)["match_threshold"] == 0.5


def test_config_invalid_combo_is_400(admin_server):
    base, _ = admin_server()
    payload = json.dumps({
        "fall_enabled": False, "identity_enabled": True, "match_threshold": 0.4,
    }).encode()
    status, body = _request(
        f"{base}/api/config", method="PUT", token="secreto",
        data=payload, content_type="application/json",
    )
    assert status == 400
    assert "fall" in json.loads(body)["error"]


def test_config_bad_json_is_400(admin_server):
    base, _ = admin_server()
    status, _body = _request(
        f"{base}/api/config", method="PUT", token="secreto",
        data=b"{no json", content_type="application/json",
    )
    assert status == 400


def test_apply_triggers_shutdown(admin_server):
    base, shutdowns = admin_server()
    status, body = _request(f"{base}/api/apply", method="POST", token="secreto")
    assert status == 200
    assert json.loads(body) == {"status": "reiniciando"}
    assert shutdowns == [True]


def test_apply_identity_without_people_is_400(admin_server):
    base, shutdowns = admin_server()
    payload = json.dumps({
        "fall_enabled": True, "identity_enabled": True, "match_threshold": 0.4,
    }).encode()
    _request(f"{base}/api/config", method="PUT", token="secreto",
             data=payload, content_type="application/json")
    status, _body = _request(f"{base}/api/apply", method="POST", token="secreto")
    assert status == 400
    assert shutdowns == []


def test_root_serves_page_without_token(admin_server):
    base, _ = admin_server()
    status, body = _request(f"{base}/")
    assert status == 200
    assert b"vitahub" in body.lower()


def test_root_without_admin_context_is_404(server_factory):
    base = server_factory(_FakeService(_ok_result()))
    status, _body = _request(f"{base}/")
    assert status == 404
