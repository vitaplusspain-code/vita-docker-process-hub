from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiomqtt

logger = logging.getLogger("mqtt_client")

FrigateEventHandler = Callable[[dict], Awaitable[None]]
AvailabilityHandler = Callable[[bool], Awaitable[None]]

_MIN_BACKOFF = 1.0
_MAX_BACKOFF = 60.0


class FrigateMqttClient:
    def __init__(
        self,
        host: str,
        port: int,
        on_event: FrigateEventHandler,
        on_availability: AvailabilityHandler,
    ) -> None:
        self._host = host
        self._port = port
        self._on_event = on_event
        self._on_availability = on_availability

    async def run_forever(self) -> None:
        backoff = _MIN_BACKOFF
        while True:
            try:
                async with aiomqtt.Client(self._host, self._port) as client:
                    backoff = _MIN_BACKOFF
                    await client.subscribe("frigate/events")
                    await client.subscribe("frigate/available")
                    async for message in client.messages:
                        await self._dispatch(message)
            except aiomqtt.MqttError as exc:
                logger.warning("mqtt connection error: %s, retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _dispatch(self, message: Any) -> None:
        topic = str(message.topic)
        try:
            raw = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("malformed mqtt payload on %s: not utf-8", topic)
            return

        if topic == "frigate/available":
            await self._on_availability(raw == "online")
            return

        if topic == "frigate/events":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("malformed mqtt payload on %s: invalid json", topic)
                return
            await self._on_event(payload)
