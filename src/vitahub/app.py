from __future__ import annotations

import os
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from vitahub.analytics.connection_monitor import ConnectionMonitor
from vitahub.analytics.event_engine import EventEngine
from vitahub.analytics.fall_engine import FallEngine
from vitahub.config import ConfigError, load_config
from vitahub.control import admin_port, start_control_server
from vitahub.discovery.onvif import discover
from vitahub.factory import build_detector, build_sink
from vitahub.ingest.rtsp import (
    backoff_delay,
    is_stalled,
    open_capture,
    should_sample,
)
from vitahub.logging_setup import configure_logging, get_logger, register_secret
from vitahub.models import Camera, Event
from vitahub.rescan import RescanService
from vitahub.sinks.base import EventSink
from vitahub.supervisor import CameraSupervisor
from vitahub.worker import process_frame

_log = get_logger("app")
_HEARTBEAT_FILE = Path("/data/heartbeat")
_DEFAULT_POSE_WEIGHTS = "/app/models/yolo11n-pose.pt"

# Plazo global (no por cámara, y cubre también la espera por el lock interno
# del supervisor, no solo los joins) para que stop_all() no exceda el
# stop_grace_period de Docker (ver docker-compose.yml) por muchas cámaras
# atascadas en cap.read(). El tiempo TOTAL de stop_all() queda acotado por
# este valor; debe quedar con margen real por debajo de stop_grace_period.
_CAMERA_SHUTDOWN_DEADLINE_S = 20.0


def run(config_path: Path, weights_path: str, env: dict[str, str]) -> None:
    cfg = load_config(config_path, env)
    register_secret(cfg.credentials.onvif_password)
    _log.info("hub %s arrancando", cfg.hub_id)

    # Los manejadores de señal van lo primero, antes de descubrimiento y carga
    # del detector, para que una señal recibida durante el arranque se atienda.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # Toque de latido temprano, antes de cargar el detector y del rescan
    # inicial. /data/heartbeat vive en un bind mount: tras un corte de luz
    # sobrevive con la marca de tiempo de ANTES del apagón. Sin este toque
    # aquí, el healthcheck vería ese latido caducado mientras el hub todavía
    # está cargando YOLO o sondeando ONVIF (nada de eso tiene tope global) y
    # el contenedor quedaría marcado `unhealthy` pese a estar perfectamente
    # vivo. (El HEALTHCHECK solo señala el estado; no lo remedia — ver
    # docs/backlog.md.)
    _touch_heartbeat()

    if cfg.inference.detector == "person_pose":
        weights_path = env.get("VITAHUB_POSE_WEIGHTS", _DEFAULT_POSE_WEIGHTS)
    detector = build_detector(cfg.inference, weights_path)
    engine = EventEngine(cfg.hub_id)
    fall_engine = (
        FallEngine(cfg.hub_id, min_score=cfg.inference.fall.min_score)
        if cfg.inference.fall.enabled
        else None
    )
    if fall_engine is not None:
        _log.info("analítica de caídas activada (min_score=%.2f)", cfg.inference.fall.min_score)
    monitor = ConnectionMonitor(cfg.hub_id)
    sink = build_sink(cfg)

    def worker(camera: Camera, rtsp_url: str, cam_stop: threading.Event) -> None:
        _camera_loop(  # type: ignore[no-untyped-call]
            camera, rtsp_url, detector, engine, sink, cfg, cam_stop, monitor, fall_engine
        )

    supervisor = CameraSupervisor(worker)
    service = RescanService(cfg, config_path, supervisor, discover)

    # El rescan inicial usa exactamente el mismo camino que los periódicos.
    result = service.run_once()
    if result.status == "ok" and result.found == 0:
        _log.warning("descubrimiento: 0 cámaras encontradas — revisa ONVIF/credencial/red")

    httpd = _start_control_server_safe(service, env.get("VITAHUB_ADMIN_TOKEN", ""), admin_port(env))

    # En su propio hilo: si el rescan compartiera hilo con el heartbeat, un
    # discover() lento dejaría de latir y el contenedor quedaría marcado
    # `unhealthy` pese a estar sano (el HEALTHCHECK señala, no remedia).
    threading.Thread(
        target=_rescan_loop,
        args=(service, cfg.discovery.interval_seconds, stop),
        name="rescan",
        daemon=True,
    ).start()

    # Segundo toque justo antes del bucle de latido: cubre el hueco entre el
    # toque de arriba y el primer `stop.wait(timeout=5.0)` de abajo, que de
    # otro modo tardaría hasta 5s en tocar el latido por primera vez.
    _touch_heartbeat()

    try:
        while not stop.wait(timeout=5.0):
            _touch_heartbeat()
    finally:
        _log.info("apagando (SIGTERM)")
        _shutdown(httpd, supervisor, sink)


def _shutdown(
    httpd: ThreadingHTTPServer | None, supervisor: CameraSupervisor, sink: EventSink
) -> None:
    """Cierra en orden y con cada paso garantizado pase lo que pase con el anterior.

    Primero el control HTTP: si se cerrara después de parar los workers, una
    petición POST /rescan que ya estuviera en vuelo podría llegar a
    `supervisor.apply()` tras `stop_all()` y arrancar workers huérfanos que ya
    nadie señalará. Luego los workers, con un plazo global acotado (no por
    cámara, ver `_CAMERA_SHUTDOWN_DEADLINE_S`). El sink se cierra siempre,
    incluso si algo de lo anterior falla.
    """
    try:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
    finally:
        try:
            supervisor.stop_all(deadline_s=_CAMERA_SHUTDOWN_DEADLINE_S)
        finally:
            sink.close()


def _start_control_server_safe(
    service: RescanService, token: str, port: int
) -> ThreadingHTTPServer | None:
    """Arranca el servidor de control sin tumbar el hub si el puerto está ocupado.

    El control HTTP es opcional (el rescan periódico sigue funcionando sin
    él); un puerto ya tomado por otro servicio del Jetson no puede impedir
    el arranque.
    """
    try:
        return start_control_server(service, token, port)
    except OSError as exc:
        _log.warning(
            "control HTTP no disponible (puerto %d ocupado o inaccesible): %s — "
            "el hub sigue arrancando, el rescan periódico sí funcionará",
            port,
            exc,
        )
        return None


def _rescan_loop(service: RescanService, interval_seconds: float, stop: threading.Event) -> None:
    """Espera sobre el event de parada (no duerme): el apagado es inmediato."""
    while not stop.wait(timeout=interval_seconds):
        service.run_once()


def _emit_all(sink: EventSink, events: list[Event]) -> None:
    """Vuelca eventos por el sink. Un fallo de emisión no tumba el worker."""
    for event in events:
        try:
            sink.emit(event)
        except Exception:  # noqa: BLE001 — emitir no debe matar el hilo de cámara
            _log.exception("no se pudo emitir un evento")


def _camera_loop(  # type: ignore[no-untyped-def]
    camera, rtsp_url, detector, engine, sink, cfg, stop, monitor, fall_engine=None
):
    attempt = 0
    while not stop.is_set():
        cap = None
        try:
            cap = open_capture(rtsp_url)
            if not cap.isOpened():
                delay = backoff_delay(attempt)
                _log.warning("cam %s no abre, reintento en %.0fs", camera.id, delay)
                _emit_all(sink, monitor.on_failed(camera, time.monotonic()))
                attempt += 1
                stop.wait(delay)
                continue
            _log.info("cam %s conectada", camera.id)
            attempt = 0
            last_sample = 0.0
            last_frame = time.monotonic()
            while not stop.is_set():
                ok, frame = cap.read()
                now = time.monotonic()
                if not ok:
                    if is_stalled(last_frame, now, max_stale_s=10.0):
                        _log.warning("cam %s estancada, reconecto", camera.id)
                        break
                    stop.wait(0.1)
                    continue
                last_frame = now
                # La evidencia de que la cámara funciona es el fotograma, no
                # el `open` de arriba: abrir el socket sin llegar a entregar
                # vídeo (slot agotado, RTP filtrado) no debe contar como
                # "conectada" para el monitor.
                _emit_all(sink, monitor.on_frame(camera, now))
                if should_sample(last_sample, now, cfg.inference.sample_fps):
                    last_sample = now
                    try:
                        process_frame(
                            camera, frame, detector, engine, sink, now, fall_engine=fall_engine
                        )
                    except Exception:  # noqa: BLE001 — un frame malo no tumba el worker
                        _log.exception("cam %s error procesando frame", camera.id)
            _log.info("cam %s desconectada", camera.id)
        except Exception:  # noqa: BLE001 — un fallo de conexión no tumba el worker
            _log.exception("cam %s error de conexión, reconecto", camera.id)
            _emit_all(sink, monitor.on_failed(camera, time.monotonic()))
            delay = backoff_delay(attempt)
            attempt += 1
            stop.wait(delay)
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001 — liberar no debe tumbar el worker
                    _log.exception("cam %s error liberando captura", camera.id)


def _touch_heartbeat() -> None:
    try:
        _HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HEARTBEAT_FILE.write_text(str(time.time()))
    except OSError:
        pass


def main() -> None:
    configure_logging(os.environ.get("VITAHUB_LOG_LEVEL", "INFO"))
    config_path = Path(os.environ.get("VITAHUB_CONFIG", "/data/hub.yaml"))
    weights_path = os.environ.get("VITAHUB_WEIGHTS", "/app/models/yolo11n.pt")
    try:
        run(config_path, weights_path, dict(os.environ))
    except ConfigError as exc:
        _log.error("config inválida: %s", exc)
        sys.exit(1)
    except Exception:  # noqa: BLE001 — última red de seguridad
        _log.exception("fallo no controlado")
        sys.exit(1)


if __name__ == "__main__":
    main()
