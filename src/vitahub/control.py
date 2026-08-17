from __future__ import annotations

import hmac
import json
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from vitahub.logging_setup import get_logger, register_secret
from vitahub.rescan import RescanResult

_log = get_logger("control")

_DEFAULT_PORT = 8787


class _Rescannable(Protocol):
    def run_once(self) -> RescanResult: ...


def admin_port(env: Mapping[str, str]) -> int:
    raw = env.get("VITAHUB_ADMIN_PORT")
    if not raw:
        return _DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        _log.warning("VITAHUB_ADMIN_PORT no es numérico (%s), uso %d", raw, _DEFAULT_PORT)
        return _DEFAULT_PORT
    if not 1 <= port <= 65535:
        _log.warning("VITAHUB_ADMIN_PORT fuera de rango (%d), uso %d", port, _DEFAULT_PORT)
        return _DEFAULT_PORT
    return port


def _build_handler(
    service: _Rescannable, token: str
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        # Sin body ni parámetros: no hay nada que parsear, luego no hay
        # superficie de inyección.
        def do_POST(self) -> None:
            if self.path != "/rescan":
                self._respond(404)
                return
            if not self._authorized():
                _log.warning("control: petición rechazada desde %s",
                             self.client_address[0])
                self._respond(401)
                return
            result = service.run_once()
            if result.status == "busy":
                self._respond(409)
                return
            if result.status == "error":
                self._respond(500)
                return
            self._respond(
                200,
                {
                    "found": result.found,
                    "added": result.added,
                    "ip_changed": result.ip_changed,
                    "started": result.started,
                    "cameras": result.cameras,
                },
            )

        def do_GET(self) -> None:
            self._respond(404)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                return False
            # compare_digest evita filtrar el token por tiempo de comparación.
            return hmac.compare_digest(header[len(prefix):], token)

        def _respond(
            self, code: int, payload: dict[str, object] | None = None
        ) -> None:
            body = (
                b"" if payload is None
                else json.dumps(payload, ensure_ascii=False).encode()
            )
            self.send_response(code)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # Por defecto BaseHTTPRequestHandler escribe a stderr crudo; se
            # redirige al logger JSON para no ensuciar el formato.
            _log.info("control %s", format % args)

    return _Handler


def start_control_server(
    service: _Rescannable, token: str, port: int, host: str = "0.0.0.0"
) -> ThreadingHTTPServer | None:
    """Arranca el servidor de control. Sin token no se abre ningún puerto."""
    if not token:
        _log.warning("control HTTP deshabilitado: define VITAHUB_ADMIN_TOKEN")
        return None
    register_secret(token)
    httpd = ThreadingHTTPServer((host, port), _build_handler(service, token))
    threading.Thread(
        target=httpd.serve_forever, name="control-http", daemon=True
    ).start()
    _log.info("control HTTP escuchando en %s:%d", host, httpd.server_address[1])
    return httpd
