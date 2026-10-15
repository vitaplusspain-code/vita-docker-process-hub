import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from aiortc import RTCIceServer
from websockets.asyncio.server import ServerConnection, serve

from vitahub.live.credentials import AwsCredentials, IotCredentialsProvider
from vitahub.live.signaling import (
    ChannelEndpoints,
    KvsSignaling,
    SignalingMessage,
    channel_name,
    decode_message,
    encode_answer,
    presign_wss_url,
)

_CREDS = AwsCredentials("AKIDEXAMPLE", "SECRET", "TOKEN", 9_999_999_999.0)


def _b64(obj: object) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def test_channel_name():
    assert channel_name("vita-dev-live", "hub-1") == "vita-dev-live-hub-1"


def test_decode_offer():
    raw = json.dumps({"messageType": "SDP_OFFER", "senderClientId": "v1",
                      "messagePayload": _b64({"type": "offer", "sdp": "v=0"})})
    assert decode_message(raw) == SignalingMessage(
        "SDP_OFFER", "v1", {"type": "offer", "sdp": "v=0"}
    )


def test_decode_go_away_without_payload():
    assert decode_message('{"messageType":"GO_AWAY"}') == SignalingMessage("GO_AWAY", "", {})


def test_decode_garbage_is_none():
    # KVS manda a veces frames vacíos (keepalive): no son errores.
    assert decode_message("") is None
    assert decode_message('{"messageType":"SDP_OFFER","messagePayload":"%%%"}') is None


# JSON tan anidado que el parser de C agota su pila: RecursionError en
# cualquier versión (en 3.12, la de producción, con mucha menos profundidad).
_DEEP = "[" * 1_000_000 + "]" * 1_000_000


@pytest.mark.parametrize(
    "raw",
    [
        # messagePayload que no es texto: b64decode lanzaba TypeError.
        '{"messageType":"SDP_OFFER","messagePayload":5}',
        '{"messageType":"SDP_OFFER","messagePayload":[1]}',
        '{"messageType":"SDP_OFFER","messagePayload":{"a":1}}',
        _DEEP,
        json.dumps({"messageType": "SDP_OFFER",
                    "messagePayload": base64.b64encode(_DEEP.encode()).decode()}),
    ],
    ids=["payload-int", "payload-list", "payload-dict", "deep", "deep-payload"],
)
def test_decode_hostile_input_is_none(raw):
    # Una sola trama rara no puede tumbar la señalización (y subir el backoff).
    assert decode_message(raw) is None


def test_encode_answer():
    msg = json.loads(encode_answer("v1", "v=0"))
    assert msg["action"] == "SDP_ANSWER"
    assert msg["recipientClientId"] == "v1"
    assert json.loads(base64.b64decode(msg["messagePayload"])) == {"type": "answer", "sdp": "v=0"}


def test_presigned_url_carries_channel_and_signature():
    url = presign_wss_url("wss://v-1.kinesisvideo.eu-west-1.amazonaws.com",
                          "arn:aws:kinesisvideo:eu-west-1:1:channel/c/1", _CREDS, "eu-west-1")
    qs = parse_qs(urlsplit(url).query)
    assert qs["X-Amz-ChannelARN"] == ["arn:aws:kinesisvideo:eu-west-1:1:channel/c/1"]
    assert qs["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert qs["X-Amz-Security-Token"] == ["TOKEN"]
    assert "X-Amz-Signature" in qs


def test_kvs_signaling_against_local_server():
    async def scenario() -> None:
        received: list[str] = []

        async def handler(ws: ServerConnection) -> None:
            await ws.send(json.dumps({"messageType": "SDP_OFFER", "senderClientId": "v1",
                                      "messagePayload": _b64({"type": "offer", "sdp": "x"})}))
            received.append(str(await ws.recv()))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            body = json.dumps({"credentials": {
                "accessKeyId": "AK", "secretAccessKey": "SK", "sessionToken": "TK",
                "expiration": "2099-01-01T00:00:00Z"}}).encode()
            sig = KvsSignaling(
                resolve=lambda: ChannelEndpoints("arn:c", f"ws://127.0.0.1:{port}", []),
                creds=IotCredentialsProvider(lambda: body),
                region="eu-west-1",
            )
            await sig.open()
            msg = await sig.recv()
            assert msg.type == "SDP_OFFER" and msg.client_id == "v1"
            await sig.send_answer("v1", "y")
            await asyncio.sleep(0.05)
            await sig.close()
        assert json.loads(received[0])["recipientClientId"] == "v1"

    asyncio.run(scenario())


def test_ice_servers_refresh_when_close_to_ttl():
    # Las credenciales TURN caducan (Ttl ~300 s) y la conexión WSS dura horas:
    # una oferta tardía debe recibir servidores frescos, no los del `open`.
    async def scenario() -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.wait_closed()

        now = [1000.0]
        resolved: list[int] = []

        def resolve() -> ChannelEndpoints:
            resolved.append(1)
            ice = [RTCIceServer(urls=f"turn:t{len(resolved)}", username="u", credential="p")]
            return ChannelEndpoints("arn:c", f"ws://127.0.0.1:{port}", ice, ice_ttl_s=300.0)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            body = json.dumps({"credentials": {
                "accessKeyId": "AK", "secretAccessKey": "SK", "sessionToken": "TK",
                "expiration": "2099-01-01T00:00:00Z"}}).encode()
            sig = KvsSignaling(
                resolve=resolve, creds=IotCredentialsProvider(lambda: body),
                region="eu-west-1", clock=lambda: now[0],
            )
            await sig.open()
            now[0] += 230  # dentro de Ttl − 60 s: se reutilizan
            assert [s.urls for s in await sig.fresh_ice_servers()] == ["turn:t1"]
            assert len(resolved) == 1
            now[0] += 20  # 250 s > 300 − 60: se piden otros
            assert [s.urls for s in await sig.fresh_ice_servers()] == ["turn:t2"]
            assert len(resolved) == 2
            await sig.close()

    asyncio.run(scenario())


def test_ice_refresh_failure_keeps_previous_servers():
    async def scenario() -> None:
        async def handler(ws: ServerConnection) -> None:
            await ws.wait_closed()

        now = [0.0]
        calls: list[int] = []

        def resolve() -> ChannelEndpoints:
            calls.append(1)
            if len(calls) > 1:
                raise OSError("AWS caído")
            ice = [RTCIceServer(urls="turn:t1", username="u", credential="p")]
            return ChannelEndpoints("arn:c", f"ws://127.0.0.1:{port}", ice, ice_ttl_s=300.0)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            body = json.dumps({"credentials": {
                "accessKeyId": "AK", "secretAccessKey": "SK", "sessionToken": "TK",
                "expiration": "2099-01-01T00:00:00Z"}}).encode()
            sig = KvsSignaling(
                resolve=resolve, creds=IotCredentialsProvider(lambda: body),
                region="eu-west-1", clock=lambda: now[0],
            )
            await sig.open()
            now[0] += 1000
            # Mejor unos TURN quizá caducados que perder la oferta.
            assert [s.urls for s in await sig.fresh_ice_servers()] == ["turn:t1"]
            await sig.close()

    asyncio.run(scenario())
