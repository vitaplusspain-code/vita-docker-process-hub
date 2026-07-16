import json
import logging

from event_engine.mqtt_client import FrigateMqttClient


class _FakeMessage:
    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


async def test_dispatch_valid_frigate_event_calls_on_event():
    received = []

    async def on_event(payload):
        received.append(payload)

    async def on_availability(available):
        pass

    client = FrigateMqttClient("mosquitto", 1883, on_event, on_availability)
    message = _FakeMessage("frigate/events", json.dumps({"type": "new", "after": {}}).encode())
    await client._dispatch(message)
    assert received == [{"type": "new", "after": {}}]


async def test_dispatch_malformed_json_is_discarded(caplog):
    async def on_event(payload):
        raise AssertionError("should not be called")

    async def on_availability(available):
        pass

    client = FrigateMqttClient("mosquitto", 1883, on_event, on_availability)
    message = _FakeMessage("frigate/events", b"{not valid json")
    with caplog.at_level(logging.WARNING):
        await client._dispatch(message)
    assert "malformed" in caplog.text


async def test_dispatch_availability_online_and_offline():
    seen = []

    async def on_event(payload):
        pass

    async def on_availability(available):
        seen.append(available)

    client = FrigateMqttClient("mosquitto", 1883, on_event, on_availability)
    await client._dispatch(_FakeMessage("frigate/available", b"online"))
    await client._dispatch(_FakeMessage("frigate/available", b"offline"))
    assert seen == [True, False]
