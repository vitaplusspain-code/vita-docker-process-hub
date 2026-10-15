from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from aiortc import RTCIceServer
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from vitahub.live.credentials import AwsCredentials, IotCredentialsProvider
from vitahub.logging_setup import get_logger

_log = get_logger("live.signaling")

# La URL prefirmada solo tiene que valer para el handshake: la conexión WSS
# ya abierta no caduca con ella.
_PRESIGN_EXPIRES_S = 300
# Los servidores TURN se renuevan si les queda menos de esto: una oferta debe
# llevarse credenciales que aún valgan cuando el viewer haga ICE.
_ICE_REFRESH_MARGIN_S = 60.0


@dataclass(frozen=True)
class SignalingMessage:
    type: str
    client_id: str
    payload: dict[str, object]


class SignalingClosed(Exception):
    pass


@dataclass(frozen=True)
class ChannelEndpoints:
    arn: str
    wss: str
    ice_servers: list[RTCIceServer]
    # El menor `Ttl` de los TURN (s); None si no hay TURN (no hace falta renovar).
    ice_ttl_s: float | None = None


def channel_name(prefix: str, hub_id: str) -> str:
    # Contrato con la policy IAM del rol hub-live (spec §3.1): no es configurable.
    return f"{prefix}-{hub_id}"


def decode_message(raw: str | bytes) -> SignalingMessage | None:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("messageType"), str):
            return None
        payload: dict[str, object] = {}
        raw_payload = data.get("messagePayload")
        if raw_payload:
            if not isinstance(raw_payload, str):
                return None  # el contrato es base64 en texto; cualquier otra cosa se ignora
            decoded = json.loads(base64.b64decode(raw_payload, validate=True))
            if not isinstance(decoded, dict):
                return None
            payload = decoded
        return SignalingMessage(data["messageType"], str(data.get("senderClientId", "")), payload)
    except (ValueError, binascii.Error, UnicodeDecodeError, TypeError, RecursionError):
        # Lo que llega por el WSS no es de fiar: una trama rara (o JSON anidado
        # hasta agotar la pila del parser) se descarta en vez de cortar la
        # señalización, que reconectaría con backoff creciente.
        return None


def encode_answer(client_id: str, sdp: str) -> str:
    body = json.dumps({"type": "answer", "sdp": sdp}).encode()
    return json.dumps(
        {
            "action": "SDP_ANSWER",
            "messagePayload": base64.b64encode(body).decode(),
            "recipientClientId": client_id,
        }
    )


def presign_wss_url(
    wss_endpoint: str, channel_arn: str, creds: AwsCredentials, region: str
) -> str:
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    url = f"{wss_endpoint.rstrip('/')}/?X-Amz-ChannelARN={quote(channel_arn, safe='')}"
    request = AWSRequest(method="GET", url=url)
    SigV4QueryAuth(
        Credentials(creds.access_key, creds.secret_key, creds.token),
        "kinesisvideo",
        region,
        expires=_PRESIGN_EXPIRES_S,
    ).add_auth(request)
    signed: str = request.url
    return signed


def resolve_endpoints(channel: str, region: str, creds: AwsCredentials) -> ChannelEndpoints:
    """ARN, endpoint WSS y servidores ICE (TURN) del canal. Runtime only: llama a AWS."""
    import botocore.session

    session = botocore.session.get_session()
    keys = {
        "aws_access_key_id": creds.access_key,
        "aws_secret_access_key": creds.secret_key,
        "aws_session_token": creds.token,
    }
    kv = session.create_client("kinesisvideo", region_name=region, **keys)
    arn = kv.describe_signaling_channel(ChannelName=channel)["ChannelInfo"]["ChannelARN"]
    endpoints = {
        e["Protocol"]: e["ResourceEndpoint"]
        for e in kv.get_signaling_channel_endpoint(
            ChannelARN=arn,
            SingleMasterChannelEndpointConfiguration={"Protocols": ["WSS", "HTTPS"], "Role": "MASTER"},
        )["ResourceEndpointList"]
    }
    sig = session.create_client(
        "kinesis-video-signaling", region_name=region, endpoint_url=endpoints["HTTPS"], **keys
    )
    ice = [RTCIceServer(urls=f"stun:stun.kinesisvideo.{region}.amazonaws.com:443")]
    ttls: list[float] = []
    for server in sig.get_ice_server_config(ChannelARN=arn, Service="TURN")["IceServerList"]:
        ice.append(
            RTCIceServer(urls=server["Uris"], username=server["Username"], credential=server["Password"])
        )
        if "Ttl" in server:
            ttls.append(float(server["Ttl"]))
    return ChannelEndpoints(
        arn=arn, wss=endpoints["WSS"], ice_servers=ice, ice_ttl_s=min(ttls) if ttls else None
    )


class Signaling(Protocol):
    ice_servers: list[RTCIceServer]

    async def open(self) -> None: ...
    async def recv(self) -> SignalingMessage: ...
    async def fresh_ice_servers(self) -> list[RTCIceServer]: ...
    async def send_answer(self, client_id: str, sdp: str) -> None: ...
    async def close(self) -> None: ...


class KvsSignaling:
    """Cliente WSS del canal KVS como master. Una instancia = una conexión.

    No reintenta: si la conexión cae, `recv` lanza `SignalingClosed` y quien
    reconecta (con backoff) es `LiveService`, creando una instancia nueva con
    credenciales y servidores TURN frescos. Como una conexión puede durar
    horas y los TURN caducan en minutos, `fresh_ice_servers` los renueva.
    """

    def __init__(
        self,
        resolve: Callable[[], ChannelEndpoints],
        creds: IotCredentialsProvider,
        region: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolve = resolve
        self._creds = creds
        self._region = region
        self._clock = clock
        self._ws: ClientConnection | None = None
        self.ice_servers: list[RTCIceServer] = []
        self._ice_fetched_at = 0.0
        self._ice_ttl_s: float | None = None

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        # botocore y el fetch HTTPS son bloqueantes: al executor, para no
        # congelar a los viewers ya conectados mientras se resuelve.
        endpoints = await loop.run_in_executor(None, self._resolve)
        creds = await loop.run_in_executor(None, self._creds.get)
        self._set_ice(endpoints)
        url = presign_wss_url(endpoints.wss, endpoints.arn, creds, self._region)
        self._ws = await connect(url, open_timeout=10)

    async def recv(self) -> SignalingMessage:
        if self._ws is None:
            raise SignalingClosed("no abierto")
        while True:
            try:
                raw = await self._ws.recv()
            except ConnectionClosed as exc:
                raise SignalingClosed(str(exc)) from exc
            msg = decode_message(raw)
            if msg is not None:
                return msg

    def _set_ice(self, endpoints: ChannelEndpoints) -> None:
        self.ice_servers = endpoints.ice_servers
        self._ice_ttl_s = endpoints.ice_ttl_s
        self._ice_fetched_at = self._clock()

    async def fresh_ice_servers(self) -> list[RTCIceServer]:
        """Servidores ICE para una oferta nueva, renovados si están por caducar."""
        ttl = self._ice_ttl_s
        if ttl is not None and self._clock() - self._ice_fetched_at >= ttl - _ICE_REFRESH_MARGIN_S:
            try:
                # Bloqueante (botocore): al executor, como en `open`.
                endpoints = await asyncio.get_running_loop().run_in_executor(None, self._resolve)
            except Exception as exc:  # noqa: BLE001 — sin renovar se usan los anteriores: mejor que perder la oferta
                _log.warning("live: no se pudieron renovar los servidores ICE: %s", exc)
            else:
                self._set_ice(endpoints)
        return self.ice_servers

    async def send_answer(self, client_id: str, sdp: str) -> None:
        if self._ws is None:
            raise SignalingClosed("no abierto")
        try:
            await self._ws.send(encode_answer(client_id, sdp))
        except ConnectionClosed as exc:
            raise SignalingClosed(str(exc)) from exc

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
