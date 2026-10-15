from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

from vitahub.live.source import IdleTrack
from vitahub.logging_setup import get_logger

_log = get_logger("live.peer")
_CONTROL_LABEL = "control"


class ViewerPeer:
    """Una RTCPeerConnection por viewer, con su DataChannel `control`.

    No decide nada: reenvía al servicio la apertura del canal, cada mensaje y
    el cierre. Las reglas viven en `LiveService` y `SessionBook`.
    """

    def __init__(
        self,
        conn_id: str,
        client_id: str,
        ice_servers: list[RTCIceServer],
        on_open: Callable[[ViewerPeer], None],
        on_message: Callable[[ViewerPeer, str | bytes], Awaitable[None]],
        on_closed: Callable[[ViewerPeer], Awaitable[None]],
    ) -> None:
        self.conn_id = conn_id
        self.client_id = client_id
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        # aiortc no publica tipos (override de mypy): Any es lo honesto aquí.
        self._channel: Any = None
        self._sender: Any = None
        self._closed = False

        @self._pc.on("datachannel")
        def _on_datachannel(channel):  # type: ignore[no-untyped-def]
            if channel.label != _CONTROL_LABEL:
                return
            self._channel = channel

            @channel.on("message")
            async def _on_msg(raw):  # type: ignore[no-untyped-def]
                await on_message(self, raw)

            # El canal del viewer llega ya abierto: el evento "datachannel" se
            # dispara al establecerse, así que el plazo del `auth` empieza aquí.
            on_open(self)

        @self._pc.on("connectionstatechange")
        async def _on_state():  # type: ignore[no-untyped-def]
            if self._pc.connectionState in ("failed", "closed") and not self._closed:
                await on_closed(self)

    async def answer(self, offer_sdp: str) -> str:
        await self._pc.setRemoteDescription(RTCSessionDescription(offer_sdp, "offer"))
        # Después de la oferta: addTrack engancha la pista al transceiver de
        # vídeo que la oferta (recvonly) ya creó, y la respuesta sale sendonly.
        self._sender = self._pc.addTrack(IdleTrack())
        await self._pc.setLocalDescription(await self._pc.createAnswer())
        # aiortc no hace trickle: setLocalDescription espera a reunir todos
        # los candidatos, así que la respuesta ya los lleva dentro.
        return str(self._pc.localDescription.sdp)

    async def add_remote_candidate(self, payload: Mapping[str, object]) -> None:
        raw = str(payload.get("candidate") or "")
        if not raw:
            return  # fin de candidatos
        candidate = candidate_from_sdp(raw.split(":", 1)[1] if raw.startswith("candidate:") else raw)
        candidate.sdpMid = payload.get("sdpMid")
        candidate.sdpMLineIndex = payload.get("sdpMLineIndex")
        await self._pc.addIceCandidate(candidate)

    def send(self, text: str) -> None:
        if self._channel is not None and self._channel.readyState == "open":
            self._channel.send(text)

    def set_track(self, track: MediaStreamTrack) -> None:
        if self._sender is not None:
            self._sender.replaceTrack(track)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._pc.close()
        except Exception:  # noqa: BLE001 — cerrar un peer roto no debe propagar
            _log.exception("live: error cerrando peer %s", self.conn_id)
