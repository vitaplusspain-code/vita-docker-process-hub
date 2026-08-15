from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

from vitahub.analytics.event_engine import EventEngine
from vitahub.config import ConfigError, load_config, save_cameras
from vitahub.discovery.onvif import discover
from vitahub.factory import build_detector
from vitahub.ingest.rtsp import (
    backoff_delay,
    is_stalled,
    open_capture,
    should_sample,
    with_credentials,
)
from vitahub.logging_setup import configure_logging, get_logger, register_secret
from vitahub.registry import reconcile
from vitahub.sinks.stdout_json import StdoutJsonSink
from vitahub.worker import process_frame

_log = get_logger("app")
_HEARTBEAT_FILE = Path("/data/heartbeat")


def run(config_path: Path, weights_path: str, env: dict[str, str]) -> None:
    cfg = load_config(config_path, env)
    register_secret(cfg.credentials.onvif_password)
    _log.info("hub %s arrancando", cfg.hub_id)

    # Se instalan los manejadores de señal lo primero, antes de descubrimiento/
    # carga del detector, para que una señal recibida durante el arranque se
    # atienda igualmente (en vez de perderse mientras esas fases bloquean).
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    detector = build_detector(cfg.inference, weights_path)
    engine = EventEngine(cfg.hub_id)
    sink = StdoutJsonSink()

    # Descubrimiento inicial + reconciliación persistida.
    # Nota: cfg.discovery.interval_seconds es la cadencia de un futuro bucle de
    # redescubrimiento periódico (no cableado en este slice); no es el timeout
    # del socket de probe ONVIF, por eso aquí se usa el timeout por defecto de
    # discover().
    discovered = discover(cfg.credentials)
    if discovered:
        _log.info("descubrimiento: %d cámaras", len(discovered))
    else:
        _log.warning(
            "descubrimiento: 0 cámaras encontradas — revisa ONVIF/credencial/red"
        )
    cfg.cameras, changes = reconcile(cfg.cameras, discovered)
    for ch in changes:
        _log.info("registro: %s %s", ch.kind, ch.camera_id)
    save_cameras(config_path, cfg.cameras)

    uris = {
        dc.id: with_credentials(
            dc.rtsp_sub if cfg.inference.stream == "substream" else dc.rtsp_main,
            cfg.credentials.onvif_user,
            cfg.credentials.onvif_password,
        )
        for dc in discovered
    }

    threads = [
        threading.Thread(
            target=_camera_loop,
            args=(cam, uris.get(cam.id), detector, engine, sink, cfg, stop),
            name=f"cam-{cam.id}",
            daemon=True,
        )
        for cam in cfg.cameras
        if cam.enabled and uris.get(cam.id)
    ]
    for t in threads:
        t.start()

    while not stop.wait(timeout=5.0):
        _touch_heartbeat()
    _log.info("apagando (SIGTERM)")
    sink.close()


def _camera_loop(camera, rtsp_url, detector, engine, sink, cfg, stop):  # type: ignore[no-untyped-def]
    attempt = 0
    while not stop.is_set():
        cap = None
        try:
            cap = open_capture(rtsp_url)
            if not cap.isOpened():
                delay = backoff_delay(attempt)
                _log.warning("cam %s no abre, reintento en %.0fs", camera.id, delay)
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
                if should_sample(last_sample, now, cfg.inference.sample_fps):
                    last_sample = now
                    try:
                        process_frame(camera, frame, detector, engine, sink, now)
                    except Exception:  # noqa: BLE001 — un frame malo no tumba el worker
                        _log.exception("cam %s error procesando frame", camera.id)
            _log.info("cam %s desconectada", camera.id)
        except Exception:  # noqa: BLE001 — un fallo de conexión no tumba el worker
            _log.exception("cam %s error de conexión, reconecto", camera.id)
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
