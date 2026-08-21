from __future__ import annotations

import itertools
import ssl
from collections.abc import Callable
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
        # Orden documentado de paho: disconnect() antes que loop_stop(). Hoy
        # funciona al revés por accidente de la implementación (loop_stop
        # deja self._thread a None y entonces _packet_queue escribe el
        # DISCONNECT de forma síncrona), pero sin un DISCONNECT limpio un
        # apagado ordenado es indistinguible de un corte de luz hasta que
        # vence el keepalive — y los eventos de presencia de IoT están
        # planificados para más adelante. Cada llamada en su propio
        # try/except, como FanoutSink con cada sink: si una lanza, la otra
        # tiene que intentarse igual.
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001 — el apagado no falla por el uplink
            _log.exception("fallo desconectando el uplink")
        try:
            self._client.loop_stop()
        except Exception:  # noqa: BLE001 — el apagado no falla por el uplink
            _log.exception("fallo parando el bucle del uplink")


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
    on_connect, on_connect_fail = _make_connect_callbacks()
    client.on_connect = on_connect
    client.on_disconnect = _on_disconnect
    client.on_connect_fail = on_connect_fail
    client.connect_async(endpoint, _PORT, keepalive=_KEEPALIVE_S)
    client.loop_start()
    return client


def _on_disconnect(
    client: object,
    userdata: object,
    flags: object,
    reason_code: object,
    properties: object = None,
) -> None:
    _log.warning("uplink desconectado (%s), reintentando en segundo plano", reason_code)


# Cuántos intentos fallidos deben pasar, tras el primero de cada episodio,
# antes del siguiente warning. `reconnect_delay_set(max_delay=120)` hace que
# paho reintente cada 120 s en régimen, así que 30 son ~1 hora entre avisos:
# suficiente para seguir viendo el problema en los logs sin que meses de hogar
# desconectado llenen el tope de 50 MB (ver docker-compose.yml) con un
# warning cada dos minutos y entrenen a cualquiera a ignorarlo.
_WARN_EVERY_N_FAILURES = 30


def _make_connect_callbacks() -> tuple[
    Callable[[object, object, object, object, object], None],
    Callable[[object, object], None],
]:
    """Fábrica de `on_connect` y `on_connect_fail`, compartiendo un contador.

    Sin un callback de fallo, un `reconnect()` que no cuaja —DNS que no
    resuelve, 8883 filtrado por el router del hogar, TLS rechazado por un
    certificado revocado, endpoint mal copiado— no deja ningún rastro:
    `_handle_on_connect_fail` de paho solo loguea en `MQTT_LOG_DEBUG`, a un
    logger que nadie ha habilitado, y `_on_disconnect` no aplica aquí porque
    solo salta si hubo conexión previa. Tras "uplink habilitado hacia ..." el
    hub se queda en silencio absoluto para siempre, indistinguible desde el
    log de un hogar tranquilo.

    El aviso se atenúa (ver `_WARN_EVERY_N_FAILURES`): se loguea el primer
    fallo del episodio —para que se note en cuanto pasa— y luego solo uno de
    cada `_WARN_EVERY_N_FAILURES`, para que meses de hogar desconectado no
    llenen el tope de 50 MB del log con un warning cada dos minutos.

    El contador es de episodio, no de por vida del cliente: `on_connect` lo
    resetea cada vez que la conexión tiene éxito, así que una desconexión
    nueva —aunque el cliente ya arrastre fallos de episodios anteriores—
    vuelve a avisar en su primer intento fallido, no a mitad de un ciclo de
    30 heredado. Por eso ambos callbacks nacen de la misma fábrica: comparten
    el contador por closure, y con `nonlocal` `on_connect` puede reiniciarlo.

    Se usa una fábrica (closure) y no funciones a nivel de módulo para que
    cada cliente lleve su propio contador: el estado no se comparte entre
    clientes (y un test no deja contaminado el contador del siguiente). Hay
    un test (`test_on_connect_fail_counters_are_independent_per_client`) que
    lo comprueba.
    """
    failures = itertools.count(1)

    def _on_connect(
        client: object,
        userdata: object,
        flags: object,
        reason_code: object,
        properties: object = None,
    ) -> None:
        nonlocal failures
        # reason_code 0 (o Success en paho 2) es el único caso bueno;
        # cualquier otro suele ser certificado no adjunto al thing o policy
        # mal acotada, y no es una conexión real: no reinicia el episodio.
        if str(reason_code) in ("0", "Success"):
            _log.info("uplink conectado a AWS IoT")
            failures = itertools.count(1)
        else:
            _log.warning("uplink rechazado por el broker: %s", reason_code)

    def _on_connect_fail(client: object, userdata: object) -> None:
        n = next(failures)
        if n == 1 or n % _WARN_EVERY_N_FAILURES == 0:
            _log.warning(
                "uplink no logra conectar a AWS IoT (intento nº %d de este "
                "episodio) — revisa endpoint/certificados/conectividad",
                n,
            )

    return _on_connect, _on_connect_fail
