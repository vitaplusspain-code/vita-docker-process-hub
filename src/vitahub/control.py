from __future__ import annotations

import hmac
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from vitahub.admin.config_edit import AdminSettings, read_settings, validate_apply, write_settings
from vitahub.admin.enrollment import (
    EnrollmentError,
    delete_person,
    delete_photo,
    list_people,
    photo_bytes,
    save_photo,
)
from vitahub.config import ConfigError
from vitahub.identity.base import FaceEngine
from vitahub.logging_setup import get_logger, register_secret
from vitahub.rescan import RescanResult

_log = get_logger("control")

_DEFAULT_PORT = 8787

_MAX_PHOTO_BYTES = 10 * 1024 * 1024

_PEOPLE_RE = re.compile(r"^/api/people$")
_PHOTOS_RE = re.compile(r"^/api/people/([^/]+)/photos$")
_PHOTO_RE = re.compile(r"^/api/people/([^/]+)/photos/([^/]+)$")
_PERSON_RE = re.compile(r"^/api/people/([^/]+)$")


class _Rescannable(Protocol):
    def run_once(self) -> RescanResult: ...


@dataclass
class AdminContext:
    """Dependencias de las rutas /api/*. Sin él, el servidor es solo /rescan."""

    faces_dir: Path
    config_path: Path
    env: Mapping[str, str]
    engine_factory: Callable[[], FaceEngine]
    request_shutdown: Callable[[], None]


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
    service: _Rescannable, token: str, admin: AdminContext | None = None
) -> type[BaseHTTPRequestHandler]:
    # Perezoso: los pesos del motor solo se cargan si el técnico enrola de
    # verdad, no en cada arranque del servidor de control.
    engine_lock = threading.Lock()
    engine_cache: list[FaceEngine] = []

    def _engine() -> FaceEngine:
        assert admin is not None
        with engine_lock:
            if not engine_cache:
                engine_cache.append(admin.engine_factory())
            return engine_cache[0]

    class _Handler(BaseHTTPRequestHandler):
        # Socket timeout para evitar slow-loris: conexiones lentas o inertes
        # no deben agotar hilos ni descriptores del proceso. Crítico porque
        # el servidor es accesible desde la WiFi del cliente.
        timeout = 10

        def do_GET(self) -> None:
            if admin is None or not self.path.startswith("/api"):
                self._respond(404)
                return
            if not self._authorized():
                self._respond(401)
                return
            if _PEOPLE_RE.match(self.path):
                people = list_people(admin.faces_dir)
                self._respond(
                    200, {"people": [{"id": p.id, "photos": p.photos} for p in people]}
                )
                return
            if m := _PHOTO_RE.match(self.path):
                try:
                    data = photo_bytes(admin.faces_dir, m.group(1), m.group(2))
                except EnrollmentError as err:
                    self._respond(err.status, {"error": err.message})
                    return
                self._respond_bytes(200, data, "image/jpeg")
                return
            if self.path == "/api/config":
                try:
                    s = read_settings(admin.config_path)
                except ConfigError as err:
                    self._respond(400, {"error": str(err)})
                    return
                self._respond(200, {
                    "fall_enabled": s.fall_enabled,
                    "identity_enabled": s.identity_enabled,
                    "match_threshold": s.match_threshold,
                })
                return
            self._respond(404)

        def do_PUT(self) -> None:
            if admin is None or self.path != "/api/config":
                self._respond(404)
                return
            if not self._authorized():
                self._respond(401)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                raw = json.loads(self.rfile.read(length))
                settings = AdminSettings(
                    fall_enabled=bool(raw["fall_enabled"]),
                    identity_enabled=bool(raw["identity_enabled"]),
                    match_threshold=float(raw["match_threshold"]),
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                self._respond(400, {"error": "cuerpo JSON inválido: se esperan "
                                    "fall_enabled, identity_enabled y match_threshold"})
                return
            try:
                write_settings(admin.config_path, settings, admin.env)
            except ConfigError as err:
                self._respond(400, {"error": str(err)})
                return
            self._respond(200, {})

        def do_POST(self) -> None:
            if self.path == "/rescan":
                self._handle_rescan()
                return
            if admin is None or not self.path.startswith("/api"):
                self._respond(404)
                return
            if not self._authorized():
                self._respond(401)
                return
            if m := _PHOTOS_RE.match(self.path):
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    self._respond(400, {"error": "cuerpo vacío"})
                    return
                if length > _MAX_PHOTO_BYTES:
                    # Drenamos el cuerpo antes de responder: si cerramos con
                    # datos aún pendientes de escribir, el cliente ve un
                    # broken pipe en vez del 413.
                    self.rfile.read(length)
                    self._respond(413, {"error": "foto demasiado grande (máx. 10 MB)"})
                    return
                body = self.rfile.read(length)
                try:
                    saved = save_photo(admin.faces_dir, m.group(1), body, _engine())
                except EnrollmentError as err:
                    self._respond(err.status, {"error": err.message})
                    return
                self._respond(200, {"saved": saved})
                return
            if self.path == "/api/apply":
                try:
                    settings = read_settings(admin.config_path)
                    validate_apply(settings, list_people(admin.faces_dir))
                except ConfigError as err:
                    self._respond(400, {"error": str(err)})
                    return
                # Responder ANTES de apagar: la página necesita el 200 para
                # empezar su polling de "el hub ha vuelto".
                self._respond(200, {"status": "reiniciando"})
                admin.request_shutdown()
                return
            self._respond(404)

        def do_DELETE(self) -> None:
            if admin is None or not self.path.startswith("/api"):
                self._respond(404)
                return
            if not self._authorized():
                self._respond(401)
                return
            if m := _PHOTO_RE.match(self.path):
                try:
                    delete_photo(admin.faces_dir, m.group(1), m.group(2))
                except EnrollmentError as err:
                    self._respond(err.status, {"error": err.message})
                    return
                self._respond(200, {})
                return
            if m := _PERSON_RE.match(self.path):
                try:
                    delete_person(admin.faces_dir, m.group(1))
                except EnrollmentError as err:
                    self._respond(err.status, {"error": err.message})
                    return
                self._respond(200, {})
                return
            self._respond(404)

        # Sin body ni parámetros: no hay nada que parsear, luego no hay
        # superficie de inyección.
        def _handle_rescan(self) -> None:
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

        def send_error(
            self, code: int, message: str | None = None, explain: str | None = None
        ) -> None:
            # Intercepta el camino de error POR DEFECTO de BaseHTTPRequestHandler
            # (p. ej. un método sin do_<METODO>, que por defecto contesta 501
            # con un cuerpo HTML que revela detalles). El diseño exige 404
            # vacío para cualquier ruta o método no contemplado, igual que el
            # resto de rutas no válidas — sin filtrar nada a un cliente no
            # autenticado en la WiFi del hogar.
            self._respond(404)

        def version_string(self) -> str:
            # Por defecto expone "BaseHTTP/x.y Python/a.b.c" en la cabecera
            # Server de TODA respuesta (incluida un 401): revela la versión
            # exacta de Python a cualquiera en la WiFi del hogar. No hay nada
            # que ganar exponiéndolo aquí.
            return ""

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                return False
            # compare_digest evita filtrar el token por tiempo de comparación.
            # Capturamos errores de encoding (p. ej. bytes no ASCII) como
            # credenciales inválidas, sin excepción.
            try:
                return hmac.compare_digest(header[len(prefix):], token)
            except (TypeError, UnicodeDecodeError):
                return False

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

        def _respond_bytes(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # Por defecto BaseHTTPRequestHandler escribe a stderr crudo; se
            # redirige al logger JSON para no ensuciar el formato.
            _log.info("control %s", format % args)

    return _Handler


def start_control_server(
    service: _Rescannable,
    token: str,
    port: int,
    host: str = "0.0.0.0",
    admin: AdminContext | None = None,
) -> ThreadingHTTPServer | None:
    """Arranca el servidor de control. Sin token no se abre ningún puerto."""
    if not token:
        _log.warning("control HTTP deshabilitado: define VITAHUB_ADMIN_TOKEN")
        return None
    register_secret(token)
    httpd = ThreadingHTTPServer((host, port), _build_handler(service, token, admin))
    # Cada petición corre en su propio hilo (ThreadingMixIn); sin esto no son
    # daemon, así que una petición en vuelo en el momento del shutdown()
    # retrasaría la salida del proceso (o la impediría si se cuelga).
    httpd.daemon_threads = True
    threading.Thread(
        target=httpd.serve_forever, name="control-http", daemon=True
    ).start()
    _log.info("control HTTP escuchando en %s:%d", host, httpd.server_address[1])
    return httpd
