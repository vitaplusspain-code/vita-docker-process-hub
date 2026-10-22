from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vitahub.clips.buffer import PacketBuffer
from vitahub.clips.trigger import ClipTriggerSink
from vitahub.clips.uploader import ClipUploader, PendingQueue, s3_put
from vitahub.config import ConfigError, HubConfig, InferenceConfig
from vitahub.identity.base import FaceEngine
from vitahub.identity.face_id import FaceIdentifier
from vitahub.identity.gallery import load_gallery
from vitahub.identity.insightface_engine import InsightFaceEngine
from vitahub.inference.base import Detector
from vitahub.inference.person_pose_yolo import PosePersonDetector
from vitahub.inference.person_yolo import PersonDetector
from vitahub.inference.stub import StubDetector
from vitahub.live.credentials import IotCredentialsProvider, https_fetcher
from vitahub.live.service import LiveService
from vitahub.live.signaling import KvsSignaling, channel_name, resolve_endpoints
from vitahub.live.source import SourcePool, live_targets, open_rtsp_track
from vitahub.live.token import load_session_keys
from vitahub.logging_setup import get_logger
from vitahub.sinks.aws_iot import AwsIotSink, build_client, topic_for
from vitahub.sinks.base import EventSink
from vitahub.sinks.fanout import FanoutSink
from vitahub.sinks.stdout_json import StdoutJsonSink

_log = get_logger("factory")

_DEFAULT_FACE_WEIGHTS = "/app/models/insightface"
_DEFAULT_FACES_DIR = "/data/faces"


def _require_weights(weights_path: str) -> None:
    # Si el fichero no existe, Ultralytics intenta descargarlo al directorio
    # padre de esa ruta — que puede ser de solo lectura (/app/models en el
    # contenedor) — y revienta con un traceback confuso. Mejor decir qué falta.
    if not Path(weights_path).is_file():
        raise ConfigError(
            f"pesos del modelo no encontrados en {weights_path} — en el contenedor los "
            "embebe el Dockerfile; en local, descárgalos con "
            f"'python scripts/download_model.py {weights_path}' o usa detector: stub"
        )


def build_detector(cfg: InferenceConfig, weights_path: str) -> Detector:
    if cfg.detector == "stub":
        return StubDetector()
    if cfg.detector == "person_yolo":
        _require_weights(weights_path)
        return PersonDetector.from_weights(weights_path, confidence=cfg.confidence)
    if cfg.detector == "person_pose":
        _require_weights(weights_path)
        return PosePersonDetector.from_weights(weights_path, confidence=cfg.confidence)
    raise ValueError(f"Detector desconocido: {cfg.detector}")


def build_sink(cfg: HubConfig) -> EventSink:
    """El sink de stdout siempre; el de AWS solo si el uplink está habilitado.

    Con el uplink apagado devuelve el StdoutJsonSink pelado, sin envolverlo en
    un fanout de un solo elemento: el comportamiento de un hub sin uplink es
    exactamente el de antes de este slice, sin una capa de más en medio.
    """
    stdout: EventSink = StdoutJsonSink()
    if not cfg.uplink.enabled:
        return stdout

    # `load_config` solo comprueba que los tres ficheros existen
    # (`Path.is_file()`); no que sean PEM válidos. Un fichero truncado o
    # ilegible pasa esa validación y revienta aquí, en `client.tls_set()`,
    # con un `ssl.SSLError` que de otro modo escaparía de `build_sink` → `run`
    # → el `except Exception` de `main`: "fallo no controlado" + `exit(1)` +
    # bucle de reinicio, un traceback en vez de un mensaje de instalación. Se
    # relanza rápido — según el spec §4.4 es lo correcto, el técnico está
    # delante — pero como `ConfigError`, con la ruta y qué revisar.
    try:
        client = build_client(
            hub_id=cfg.hub_id,
            endpoint=cfg.uplink.endpoint,
            ca=Path(cfg.uplink.ca_path),
            cert=Path(cfg.uplink.cert_path),
            key=Path(cfg.uplink.key_path),
        )
    except Exception as exc:  # se traduce a ConfigError abajo (con la causa encadenada)
        raise ConfigError(
            "uplink.enabled es true pero no se pudo construir el cliente MQTT "
            f"({exc}) — revisa que los certificados en {cfg.uplink.ca_path}, "
            f"{cfg.uplink.cert_path} y {cfg.uplink.key_path} sean PEM válidos "
            "y correspondan al mismo hogar dado de alta con scripts/provision-hub.sh"
        ) from exc
    topic = topic_for(cfg.uplink.topic_prefix, cfg.hub_id)
    _log.info("uplink habilitado hacia %s", topic)
    return FanoutSink([stdout, AwsIotSink(client, topic)])


def build_live_service(cfg: HubConfig, sink: EventSink) -> LiveService | None:
    """El servicio de directo, o None si `live.enabled` es false.

    No conecta nada aquí: la primera llamada a AWS ocurre en el hilo del
    servicio, con reintento. Un hogar sin internet arranca igual y vigila.
    """
    if not cfg.live.enabled:
        return None
    live = cfg.live
    # `https_fetcher` construye el contexto ssl al instante y `load_config` solo
    # comprueba que los ficheros existen: un PEM truncado o ilegible revienta
    # aquí con `ssl.SSLError`/`FileNotFoundError`. Como en `build_sink`, se
    # relanza como `ConfigError` (el técnico está delante) con rutas y causa.
    try:
        fetch = https_fetcher(
            live.credentials_endpoint,
            live.role_alias,
            cfg.hub_id,
            Path(cfg.uplink.ca_path),
            Path(cfg.uplink.cert_path),
            Path(cfg.uplink.key_path),
        )
    except Exception as exc:  # se traduce a ConfigError abajo (con la causa encadenada)
        raise ConfigError(
            "live.enabled es true pero no se pudo preparar la conexión al credentials "
            f"provider ({exc}) — revisa que los certificados en {cfg.uplink.ca_path}, "
            f"{cfg.uplink.cert_path} y {cfg.uplink.key_path} existan y sean PEM válidos"
        ) from exc
    creds = IotCredentialsProvider(fetch)
    channel = channel_name(live.channel_prefix, cfg.hub_id)
    _log.info("vídeo en directo habilitado (canal %s, máx. %d visores)", channel, live.max_viewers)
    return LiveService(
        hub_id=cfg.hub_id,
        max_viewers=live.max_viewers,
        keys=load_session_keys(live.session_keys),
        signaling_factory=lambda: KvsSignaling(
            resolve=lambda: resolve_endpoints(channel, live.region, creds.get()),
            creds=creds,
            region=live.region,
        ),
        sources=SourcePool(open_rtsp_track, max_fps=live.max_fps),
        # Se lee cfg.cameras en cada llamada: RescanService la sustituye en
        # cada ciclo, y así el directo ve las cámaras nuevas sin reiniciar.
        targets=lambda: live_targets(
            cfg.cameras, cfg.credentials.onvif_user, cfg.credentials.onvif_password
        ),
        sink=sink,
    )


@dataclass
class ClipService:
    """Las piezas de clips que `app.run` necesita ver: el sink decorado, los
    buffers que llenan los grabadores de cada cámara y los dos hilos propios."""

    sink: ClipTriggerSink
    buffers: dict[str, PacketBuffer]
    uploader: ClipUploader
    _stop: threading.Event | None = field(default=None, init=False, repr=False)
    _threads: list[threading.Thread] = field(default_factory=list, init=False, repr=False)

    def start(self, stop: threading.Event) -> None:
        self._stop = stop
        self._threads = [
            threading.Thread(
                target=self._tick_loop, args=(stop,), name="clips-trigger", daemon=True
            ),
            threading.Thread(
                target=self.uploader.run, args=(stop,), name="clips-upload", daemon=True
            ),
        ]
        for t in self._threads:
            t.start()

    def _tick_loop(self, stop: threading.Event) -> None:
        while not stop.wait(0.5):
            try:
                self.sink.tick()
            except Exception:  # noqa: BLE001 — sin este hilo ninguna ventana se cerraría
                _log.exception("fallo en el tick del disparador de clips")

    def stop(self, timeout_s: float) -> None:
        """Para y espera los dos hilos con un plazo global.

        El tick debe haber terminado antes de `sink.close()`: si no, close() y
        un tick tardío competirían por la misma ventana. La subida en curso
        puede quedarse colgada en la red: por eso el plazo, y por eso el hilo
        es daemon; el clip sigue en la cola y se sube en el siguiente arranque.
        """
        if self._stop is None:
            return
        self._stop.set()
        deadline = time.monotonic() + timeout_s
        for t in self._threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                _log.warning("el hilo %s no terminó a tiempo en el apagado", t.name)


def build_clip_service(
    cfg: HubConfig, inner: EventSink, pending_dir: Path = Path("/data/clips-pending")
) -> ClipService | None:
    """Los clips de caída, o None si `clips.enabled` es false.

    Como el directo, no conecta nada aquí: la primera llamada a AWS ocurre en
    el hilo de subida, con reintento.
    """
    if not cfg.clips.enabled:
        return None
    clips = cfg.clips
    # Mismo caso que en `build_live_service`: un PEM ilegible revienta aquí y
    # se relanza como `ConfigError` con las rutas y la causa.
    try:
        fetch = https_fetcher(
            clips.credentials_endpoint,
            clips.role_alias,
            cfg.hub_id,
            Path(cfg.uplink.ca_path),
            Path(cfg.uplink.cert_path),
            Path(cfg.uplink.key_path),
        )
    except Exception as exc:  # se traduce a ConfigError abajo (con la causa encadenada)
        raise ConfigError(
            "clips.enabled es true pero no se pudo preparar la conexión al credentials "
            f"provider ({exc}) — revisa que los certificados en {cfg.uplink.ca_path}, "
            f"{cfg.uplink.cert_path} y {cfg.uplink.key_path} existan y sean PEM válidos"
        ) from exc
    creds = IotCredentialsProvider(fetch)
    # El uploader emite por el sink interno, no por el disparador: sus eventos
    # no son caídas y no tienen por qué pasar por él.
    uploader = ClipUploader(
        PendingQueue(pending_dir), cfg.hub_id, inner, s3_put(clips.bucket, clips.region, creds)
    )
    buffers = {camera_id: PacketBuffer() for camera_id in clips.cameras}
    trigger = ClipTriggerSink(
        inner, cfg.hub_id, set(clips.cameras), buffers.get, on_clip=uploader.submit
    )
    _log.info(
        "clips de caída habilitados para %d cámaras (bucket %s)", len(buffers), clips.bucket
    )
    if cfg.inference.stream == "main":
        # El recorder graba el mismo stream que analiza el hub: con el principal
        # el buffer y cada clip pesan unas 8 veces más (spec §5.5).
        _log.warning(
            "clips habilitados con inference.stream: main — buffers de 22-45 MB por "
            "cámara y clips de 12-25 MB; el substream basta para ver la caída"
        )
    return ClipService(sink=trigger, buffers=buffers, uploader=uploader)


def warn_missing_clip_cameras(cfg: HubConfig) -> None:
    """Avisa de cada cámara de `clips.cameras` que el hub no conoce.

    Suele ser un id mal escrito en hub.yaml: esa cámara no grabaría nunca, y
    el fallo no se notaría hasta la primera caída sin clip.
    """
    if not cfg.clips.enabled:
        return
    known = {camera.id for camera in cfg.cameras}
    for camera_id in cfg.clips.cameras:
        if camera_id not in known:
            _log.warning(
                "clips.cameras incluye %s, que no es ninguna cámara conocida — "
                "revisa el id en hub.yaml",
                camera_id,
            )


def _require_face_weights(root: str) -> None:
    # FaceAnalysis descarga el pack de internet si falta — inaceptable en un
    # hogar sin conexión y en /app de solo lectura. Mejor decir qué falta.
    pack = Path(root) / "models" / "buffalo_s"
    if not pack.is_dir() or not any(pack.iterdir()):
        raise ConfigError(
            f"modelos de reconocimiento facial no encontrados en {pack} — en el "
            "contenedor los embebe el Dockerfile; en local, descárgalos con "
            "'python scripts/download_face_models.py'"
        )


def build_face_identifier(cfg: HubConfig, env: Mapping[str, str]) -> FaceIdentifier | None:
    if not cfg.inference.identity.enabled:
        return None
    root = env.get("VITAHUB_FACE_WEIGHTS", _DEFAULT_FACE_WEIGHTS)
    faces_dir = Path(env.get("VITAHUB_FACES_DIR", _DEFAULT_FACES_DIR))
    _require_face_weights(root)
    if not faces_dir.is_dir():
        raise ConfigError(
            f"inference.identity.enabled es true pero no existe {faces_dir} — "
            "crea /data/faces/<person_id>/ con 3-5 fotos de la persona"
        )
    engine = InsightFaceEngine.from_weights(root)
    gallery = load_gallery(faces_dir, engine)
    _log.info(
        "identidad activada: %d personas enroladas (umbral %.2f)",
        len(gallery.people), cfg.inference.identity.match_threshold,
    )
    return FaceIdentifier(
        engine, gallery, match_threshold=cfg.inference.identity.match_threshold
    )


def build_admin_engine_factory(
    env: Mapping[str, str],
) -> Callable[[], FaceEngine]:
    """Factory perezosa del engine del servidor de control.

    La validación de pesos corre al LLAMARLA, no al construirla: un hub sin
    pesos faciales debe arrancar igual (la identidad puede estar apagada) y
    el error debe salir legible en la primera subida de foto.
    """

    def _factory() -> FaceEngine:
        root = env.get("VITAHUB_FACE_WEIGHTS", _DEFAULT_FACE_WEIGHTS)
        _require_face_weights(root)
        return InsightFaceEngine.from_weights(root)

    return _factory
