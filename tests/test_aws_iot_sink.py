import json
import os
import stat

from vitahub.models import Event
from vitahub.sinks.aws_iot import (
    _WARN_EVERY_N_FAILURES,
    AwsIotSink,
    _make_connect_callbacks,
    topic_for,
    warn_if_key_is_exposed,
)


def _event() -> Event:
    return Event(
        hub_id="hub-1",
        camera_id="onvif-abc",
        camera_name="salon",
        type="person_detected",
        severity="info",
        timestamp="2026-08-21T10:00:00+00:00",
        payload={"person_count": 1},
    )


class _FakeClient:
    def __init__(
        self,
        fail: bool = False,
        fail_disconnect: bool = False,
        fail_loop_stop: bool = False,
    ) -> None:
        self.published: list[tuple[str, str, int]] = []
        self.calls: list[str] = []
        self.stopped = False
        self.disconnected = False
        self._fail = fail
        self._fail_disconnect = fail_disconnect
        self._fail_loop_stop = fail_loop_stop

    def publish(self, topic: str, payload: str, qos: int) -> object:
        if self._fail:
            raise RuntimeError("broker caído")
        self.published.append((topic, payload, qos))
        return None

    def loop_stop(self) -> None:
        self.calls.append("loop_stop")
        if self._fail_loop_stop:
            raise RuntimeError("loop_stop caído")
        self.stopped = True

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        if self._fail_disconnect:
            raise RuntimeError("disconnect caído")
        self.disconnected = True


def test_topic_for_composes_the_contract_topic():
    assert topic_for("vita/hub", "hub-casa-lopez") == "vita/hub/hub-casa-lopez/events"


def test_emit_publishes_the_exact_event_json():
    client = _FakeClient()
    event = _event()
    AwsIotSink(client, "vita/hub/hub-1/events").emit(event)
    topic, payload, qos = client.published[0]
    assert topic == "vita/hub/hub-1/events"
    # El contrato del spec: por MQTT sale byte a byte lo mismo que por stdout.
    assert payload == event.to_json()
    assert json.loads(payload)["type"] == "person_detected"
    assert qos == 0


def test_a_publish_that_raises_does_not_propagate():
    # Un broker caído no puede tumbar el hilo de una cámara.
    AwsIotSink(_FakeClient(fail=True), "vita/hub/hub-1/events").emit(_event())


def test_close_stops_the_loop_and_disconnects():
    client = _FakeClient()
    AwsIotSink(client, "vita/hub/hub-1/events").close()
    assert client.stopped
    assert client.disconnected


def test_close_disconnects_before_stopping_the_loop():
    # Orden documentado de paho: disconnect() antes que loop_stop(). Hoy
    # funciona al revés por accidente de la implementación de paho (ver el
    # comentario en AwsIotSink.close) — sin un DISCONNECT limpio, un apagado
    # ordenado es indistinguible de un corte de luz hasta que vence el
    # keepalive.
    client = _FakeClient()
    AwsIotSink(client, "vita/hub/hub-1/events").close()
    assert client.calls == ["disconnect", "loop_stop"]


def test_close_stops_the_loop_even_if_disconnect_raises():
    client = _FakeClient(fail_disconnect=True)
    AwsIotSink(client, "vita/hub/hub-1/events").close()
    assert client.stopped  # loop_stop se intenta igual, cada uno en su try/except


def test_close_disconnect_already_happened_even_if_loop_stop_raises():
    client = _FakeClient(fail_loop_stop=True)
    AwsIotSink(client, "vita/hub/hub-1/events").close()
    assert client.disconnected  # disconnect va primero y no depende de loop_stop


def test_on_connect_fail_logs_the_first_failure_of_an_episode(caplog):
    caplog.set_level("WARNING")
    _on_connect, on_connect_fail = _make_connect_callbacks()
    on_connect_fail(_FakeClient(), None)
    assert "no logra conectar" in caplog.text


def test_on_connect_fail_throttles_repeated_failures(caplog):
    # Sin atenuar, con paho reintentando cada 120 s en régimen, meses de
    # hogar sin uplink llenarían el log con un warning cada dos minutos.
    caplog.set_level("WARNING")
    _on_connect, on_connect_fail = _make_connect_callbacks()

    on_connect_fail(_FakeClient(), None)  # intento 1: logueado
    assert "no logra conectar" in caplog.text

    caplog.clear()
    for _ in range(_WARN_EVERY_N_FAILURES - 2):  # intentos 2..N-1: atenuados
        on_connect_fail(_FakeClient(), None)
    assert caplog.text == ""

    on_connect_fail(_FakeClient(), None)  # intento N: logueado de nuevo
    assert "no logra conectar" in caplog.text


def test_on_connect_fail_counters_are_independent_per_client(caplog):
    # Cada llamada a _make_connect_callbacks (una por cliente, en
    # build_client) arranca su propio contador: un cliente no hereda el
    # episodio de otro.
    caplog.set_level("WARNING")
    _first_on_connect, first_on_connect_fail = _make_connect_callbacks()
    _second_on_connect, second_on_connect_fail = _make_connect_callbacks()

    first_on_connect_fail(_FakeClient(), None)
    caplog.clear()

    second_on_connect_fail(_FakeClient(), None)
    assert "no logra conectar" in caplog.text


def test_on_connect_fail_logs_again_after_a_successful_connection(caplog):
    # El contador es de episodio, no de por vida del cliente: si el hub falló
    # unas veces al arrancar, conectó bien y meses después pierde el enlace,
    # ese corte nuevo tiene que avisar en su primer fallo — no a mitad de un
    # ciclo de _WARN_EVERY_N_FAILURES heredado del episodio anterior.
    caplog.set_level("WARNING")
    on_connect, on_connect_fail = _make_connect_callbacks()

    # Episodio 1: agota más de un ciclo de atenuación.
    for _ in range(_WARN_EVERY_N_FAILURES + 5):
        on_connect_fail(_FakeClient(), None)

    on_connect(_FakeClient(), None, None, 0)  # conexión con éxito: resetea

    caplog.clear()
    on_connect_fail(_FakeClient(), None)  # primer fallo del episodio 2
    assert "no logra conectar" in caplog.text


def test_a_broker_rejection_does_not_reset_the_failure_counter(caplog):
    # reason_code distinto de éxito (p.ej. certificado no adjunto al thing,
    # policy mal acotada) no es una conexión buena: no debe reiniciar el
    # episodio ni volver a destapar el primer aviso.
    caplog.set_level("WARNING")
    on_connect, on_connect_fail = _make_connect_callbacks()

    on_connect_fail(_FakeClient(), None)  # intento 1: logueado
    assert "no logra conectar" in caplog.text

    on_connect(_FakeClient(), None, None, 5)  # rechazo del broker, no reinicia

    caplog.clear()
    on_connect_fail(_FakeClient(), None)  # intento 2: sigue atenuado
    assert caplog.text == ""


def test_event_json_has_exactly_the_eight_fields_the_iot_rule_expects():
    """Alambrada del contrato con el SQL de la regla de IoT.

    `lib/hub-ingest-stack.ts` (repo `vitaplus-aws-architecture`) NO usa
    `SELECT *`: enumera los campos de primer nivel a propósito (ver el
    comentario junto al SQL de `HubEventsRule`), para que un `hub_id`
    falsificado en el payload no pueda colarse en la tabla de otro hogar. Si
    este test se pone rojo, alguien ha añadido o quitado un campo de primer
    nivel a `Event` sin tocar el SQL de la regla en el otro repositorio: el
    campo nuevo se publicaría igual por MQTT, pero la regla lo descartaría en
    silencio antes de llegar a DynamoDB, sin error en ningún sitio. Actualiza
    el `SELECT` de `HubEventsRule` en `lib/hub-ingest-stack.ts` a la vez que
    cambies esto.
    """
    keys = set(json.loads(_event().to_json()))
    assert keys == {
        "schema_version",
        "hub_id",
        "camera_id",
        "camera_name",
        "type",
        "severity",
        "timestamp",
        "payload",
    }


def test_warns_when_the_private_key_is_readable_by_others(tmp_path, caplog):
    key = tmp_path / "private.pem.key"
    key.write_text("clave")
    os.chmod(key, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    warn_if_key_is_exposed(key)
    assert "legible por otros" in caplog.text


def test_does_not_warn_when_the_private_key_is_locked_down(tmp_path, caplog):
    key = tmp_path / "private.pem.key"
    key.write_text("clave")
    os.chmod(key, stat.S_IRUSR | stat.S_IWUSR)
    warn_if_key_is_exposed(key)
    assert caplog.text == ""
