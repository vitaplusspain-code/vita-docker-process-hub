from __future__ import annotations

import ssl
from pathlib import Path
from typing import Protocol

from vitahub.logging_setup import get_logger
from vitahub.models import Event
from vitahub.sinks.base import EventSink

_log = get_logger("sinks.aws_iot")

# QoS 0 a propósito: este slice no tiene cola (ver el spec, §2). Un QoS 1 sin
# cola persistente solo añadiría reintentos en memoria que un reinicio del
# contenedor —justo lo que provoca un corte de luz— se lleva igual.
_QOS = 0
_PORT = 8883
_KEEPALIVE_S = 60


class MqttClient(Protocol):
    """Lo mínimo que el sink usa de paho.

    Existe para que los tests inyecten un doble: sin esto, probar el sink
    exigiría un broker, y la suite corre en CI sin red.
    """

    def publish(self, topic: str, payload: str, qos: int) -> object: ...
    # `-> object`, no `-> None`: paho devuelve `MQTTErrorCode` en los tres
    # métodos y no lo comprobamos (ver `emit`/`close`, que atrapan la
    # excepción en vez de mirar el código de retorno). `object` es lo mínimo
    # que describe correctamente lo que paho ofrece de verdad.
    def loop_stop(self) -> object: ...
    def disconnect(self) -> object: ...


def topic_for(prefix: str, hub_id: str) -> str:
    return f"{prefix}/{hub_id}/events"


class AwsIotSink(EventSink):
    def __init__(self, client: MqttClient, topic: str) -> None:
        self._client = client
        self._topic = topic

    def emit(self, event: Event) -> None:
        try:
            self._client.publish(self._topic, event.to_json(), _QOS)
        except Exception:  # noqa: BLE001 — el uplink caído no puede tumbar la cámara
            _log.exception("no se pudo publicar en %s", self._topic)

    def close(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001 — el apagado no falla por el uplink
            _log.exception("fallo cerrando el uplink")


def warn_if_key_is_exposed(key: Path) -> None:
    """Avisa si la clave privada la puede leer alguien más que su dueño.

    Solo avisa, no aborta: bloquear el arranque por un `chmod` dejaría una
    vivienda entera sin vigilancia por un problema de permisos de fichero.
    """
    try:
        mode = key.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        _log.warning(
            "la clave privada %s es legible por otros (modo %o) — arréglalo con: chmod 600 %s",
            key,
            mode,
            key,
        )


def build_client(hub_id: str, endpoint: str, ca: Path, cert: Path, key: Path) -> MqttClient:
    """Cliente MQTT con TLS mutuo, conectando en segundo plano.

    `connect_async` + `loop_start`: el arranque del hub NO se bloquea si el
    enlace está caído — un hogar sin internet tiene que seguir vigilando. La
    reconexión con backoff la lleva el propio bucle de paho.

    El import va dentro de la función y no arriba: los tests inyectan un doble
    y nunca llegan aquí, así que la suite no paga el import ni depende de que
    paho esté instalado para recolectar los tests.
    """
    import paho.mqtt.client as mqtt

    # `paho.mqtt.client` no re-exporta `CallbackAPIVersion` explícitamente
    # (vive en `paho.mqtt.enums`), y con `strict` mypy no lo acepta como
    # `mqtt.CallbackAPIVersion` por `no-implicit-reexport`. Se importa de su
    # módulo real.
    from paho.mqtt.enums import CallbackAPIVersion

    warn_if_key_is_exposed(key)
    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        # La policy de IoT exige clientId == nombre del thing == hub_id. Con
        # otro valor, el broker rechaza la conexión sin más explicación.
        client_id=hub_id,
        protocol=mqtt.MQTTv311,
    )
    client.tls_set(
        ca_certs=str(ca),
        certfile=str(cert),
        keyfile=str(key),
        tls_version=ssl.PROTOCOL_TLSv1_2,
    )
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.connect_async(endpoint, _PORT, keepalive=_KEEPALIVE_S)
    client.loop_start()
    return client


def _on_connect(
    client: object,
    userdata: object,
    flags: object,
    reason_code: object,
    properties: object = None,
) -> None:
    # reason_code 0 (o Success en paho 2) es el único caso bueno; cualquier
    # otro suele ser certificado no adjunto al thing o policy mal acotada.
    if str(reason_code) in ("0", "Success"):
        _log.info("uplink conectado a AWS IoT")
    else:
        _log.warning("uplink rechazado por el broker: %s", reason_code)


def _on_disconnect(
    client: object,
    userdata: object,
    flags: object,
    reason_code: object,
    properties: object = None,
) -> None:
    _log.warning("uplink desconectado (%s), reintentando en segundo plano", reason_code)
