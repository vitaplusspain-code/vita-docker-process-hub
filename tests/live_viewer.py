"""Viewer WebRTC local para tests: lo que hace la app web, en aiortc."""
from __future__ import annotations

import asyncio
import json

from aiortc import RTCPeerConnection, RTCSessionDescription


class LocalViewer:
    def __init__(self) -> None:
        self.pc = RTCPeerConnection()
        self.pc.addTransceiver("video", direction="recvonly")
        self.channel = self.pc.createDataChannel("control")
        self.inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.opened = asyncio.Event()
        self.frames = 0
        self.channel.on("open", self.opened.set)
        self.channel.on("message", lambda m: self.inbox.put_nowait(json.loads(m)))

        @self.pc.on("track")
        def _on_track(track):  # type: ignore[no-untyped-def]
            async def _drain() -> None:
                while True:
                    await track.recv()
                    self.frames += 1

            asyncio.ensure_future(_drain())

    async def offer(self) -> str:
        await self.pc.setLocalDescription(await self.pc.createOffer())
        return str(self.pc.localDescription.sdp)

    async def accept(self, answer_sdp: str) -> None:
        await self.pc.setRemoteDescription(RTCSessionDescription(answer_sdp, "answer"))

    def send(self, obj: dict[str, object]) -> None:
        self.channel.send(json.dumps(obj))

    async def next(self, timeout: float = 5.0) -> dict[str, object]:
        return await asyncio.wait_for(self.inbox.get(), timeout)

    async def close(self) -> None:
        await self.pc.close()
