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

        self._on_open = on_open
        self._on_message = on_message
        self._on_closed = on_closed
        # Separado de `_closed`: `on_closed` se avisa una sola vez (aiortc pasa
        # por "failed" y luego "closed"), pero `close()` debe seguir cerrando el pc.
        self._reported = False

        @self._pc.on("datachannel")
        def _on_datachannel(channel):  # type: ignore[no-untyped-def]
            if channel.label != _CONTROL_LABEL:
                return
            if self._channel is not None:
                # Un segundo `control` reiniciaría el plazo del `auth` sin fin
                # (viewer hostil): solo vale el primero.
                _log.warning("live: segundo canal control rechazado en %s", self.conn_id)
                channel.close()
                return
            self._channel = channel

            @channel.on("message")
            async def _on_msg(raw):  # type: ignore[no-untyped-def]
                try:
                    await self._on_message(self, raw)
                except Exception:  # noqa: BLE001 — un fallo del servicio no debe romper el canal
                    _log.exception("live: error en on_message de %s", self.conn_id)

            # El canal del viewer llega ya abierto: el evento "datachannel" se
            # dispara al establecerse, así que el plazo del `auth` empieza aquí.
            try:
                self._on_open(self)
            except Exception:  # noqa: BLE001 — la excepción subiría por la ruta SCTP de aiortc
                _log.exception("live: error en on_open de %s", self.conn_id)

        @self._pc.on("connectionstatechange")
        async def _on_state():  # type: ignore[no-untyped-def]
            if self._pc.connectionState in ("failed", "closed"):
                await self._report_closed()

    async def _report_closed(self) -> None:
        if self._closed or self._reported:
            return
        self._reported = True
        try:
            await self._on_closed(self)
        except Exception:  # noqa: BLE001 — un fallo del servicio no debe propagar a aiortc
            _log.exception("live: error en on_closed de %s", self.conn_id)

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
        # Entrada del viewer: cualquier forma rara se descarta, nunca se propaga.
        raw = payload.get("candidate")
        if not raw:
            return  # fin de candidatos
        mid = payload.get("sdpMid")
        index = payload.get("sdpMLineIndex")
        if (
            not isinstance(raw, str)
            or not (mid is None or isinstance(mid, str))
            or not (index is None or (isinstance(index, int) and not isinstance(index, bool)))
            or (mid is None and index is None)
        ):
            _log.warning("live: candidato ICE descartado en %s", self.conn_id)
            return
        try:
            candidate = candidate_from_sdp(raw.split(":", 1)[1] if raw.startswith("candidate:") else raw)
            candidate.sdpMid = mid
            candidate.sdpMLineIndex = index
            await self._pc.addIceCandidate(candidate)
        except Exception:  # noqa: BLE001 — candidato malformado del viewer: se ignora
            _log.warning("live: candidato ICE inválido en %s", self.conn_id, exc_info=True)

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
