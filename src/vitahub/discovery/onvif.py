from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime

from vitahub.config import Credentials
from vitahub.logging_setup import get_logger
from vitahub.registry import DiscoveredCamera

_log = get_logger("discovery")

_MCAST = ("239.255.255.250", 3702)


def build_probe() -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>"
        '<w:To e:mustUnderstand="true">'
        "urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        '<w:Action e:mustUnderstand="true">'
        "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header><e:Body><d:Probe>"
        "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe></e:Body></e:Envelope>"
    ).encode()


def parse_probe_matches(xml: str) -> list[str]:
    ips: list[str] = []
    for xaddr in re.findall(r"<[^>]*XAddrs>(.*?)</[^>]*XAddrs>", xml, re.DOTALL):
        for url in xaddr.split():
            m = re.search(r"https?://([\d.]+)", url)
            if m and m.group(1) not in ips:
                ips.append(m.group(1))
    return ips


def wsse_header(user: str, password: str, created: str, nonce: bytes) -> str:
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()
    b64nonce = base64.b64encode(nonce).decode()
    return (
        '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org'
        '/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f"<UsernameToken><Username>{user}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{b64nonce}</Nonce>"
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        "</UsernameToken></Security></s:Header>"
    )


def _tag(xml: str, name: str) -> str | None:
    m = re.search(rf"<(?:\w+:)?{name}[^>]*>(.*?)</(?:\w+:)?{name}>", xml, re.DOTALL)
    return m.group(1).strip() if m else None


def parse_serial(xml: str) -> str | None:
    return _tag(xml, "SerialNumber")


def parse_stream_uri(xml: str) -> str | None:
    return _tag(xml, "Uri")


def parse_profile_tokens(xml: str) -> list[str]:
    """Extrae SOLO los tokens de los elementos <Profiles> de un GetProfilesResponse.

    Un GetProfilesResponse trae un token por perfil (p. ej. PROFILE_000) pero
    también tokens anidados de cada configuración (fuente de vídeo, encoder,
    audio, PTZ...). Solo los tokens de perfil son válidos como ProfileToken en
    GetStreamUri; pedir GetStreamUri con un token anidado hace que la cámara
    responda HTTP 400. Por eso aquí se acotan al atributo token de <Profiles>.
    """
    tokens = re.findall(r'<(?:\w+:)?Profiles\b[^>]*\btoken="([^"]+)"', xml)
    return list(dict.fromkeys(tokens))


def select_main_sub(uris: list[str | None]) -> tuple[str, str]:
    main = sub = ""
    for i, uri in enumerate(uris):
        if uri and i == 0:
            main = uri
        elif uri:
            sub = uri
    return main, sub or main


def dedupe_by_id(cameras: list[DiscoveredCamera]) -> list[DiscoveredCamera]:
    """Deduplica por `id`, quedándose con la primera aparición.

    Función pura (sin red) para poder testearla sin LAN/hardware, siguiendo
    el patrón de este módulo: parsers puros testeados, orquestación de red
    no. Dos cámaras del mismo modelo barato a veces comparten `SerialNumber`
    en el firmware, o una misma cámara responde por dos IPs (multihoming).
    Sin deduplicar, cada rescan periódico ve una `ip_changed` distinta para
    el mismo `id`, el registro nunca converge y el supervisor para/relanza
    ese worker en cada ciclo.
    """
    seen: dict[str, DiscoveredCamera] = {}
    result: list[DiscoveredCamera] = []
    for cam in cameras:
        primera = seen.get(cam.id)
        if primera is not None:
            _log.warning(
                "descubrimiento: id duplicado %s — se descarta la IP %s "
                "(ya asignada a %s)",
                cam.id,
                cam.ip,
                primera.ip,
            )
            continue
        seen[cam.id] = cam
        result.append(cam)
    return result


def discover(creds: Credentials, timeout: float = 3.0) -> list[DiscoveredCamera]:
    """Orquestación de red. No cubierto por unit tests (requiere LAN/hardware)."""
    ips = _probe_network(timeout)
    cameras: list[DiscoveredCamera] = []
    for ip in ips:
        try:
            cam = _interrogate(ip, creds)
            if cam is not None:
                cameras.append(cam)
        except OSError as exc:  # red/timeout de una cámara concreta
            _log.warning("no se pudo interrogar %s: %s", ip, exc)
    return dedupe_by_id(cameras)


def _probe_network(timeout: float) -> list[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    sock.bind(("0.0.0.0", 0))
    found: list[str] = []
    try:
        sock.sendto(build_probe(), _MCAST)
        while True:
            try:
                data, _ = sock.recvfrom(65535)
            except TimeoutError:
                break
            for ip in parse_probe_matches(data.decode("utf-8", "replace")):
                if ip not in found:
                    found.append(ip)
    finally:
        sock.close()
    return found


def _soap(url: str, body: str, creds: Credentials) -> str:
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = wsse_header(creds.onvif_user, creds.onvif_password, created, os.urandom(16))
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{header}<s:Body>{body}</s:Body></s:Envelope>"
    )
    req = urllib.request.Request(
        url, data=envelope.encode(),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw: bytes = resp.read()
        return raw.decode("utf-8", "replace")


def _interrogate(ip: str, creds: Credentials) -> DiscoveredCamera | None:
    dev_url = f"http://{ip}:10000/onvif/device_service"
    media_url = f"http://{ip}:10000/onvif/media_service"
    serial = parse_serial(_soap(dev_url, "<tds:GetDeviceInformation/>", creds))
    if not serial:
        return None
    profiles = _soap(media_url, "<trt:GetProfiles/>", creds)
    uris: list[str | None] = []
    for tok in parse_profile_tokens(profiles):
        try:
            resp = _soap(
                media_url,
                "<trt:GetStreamUri><trt:StreamSetup>"
                "<tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>"
                "<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{tok}</trt:ProfileToken></trt:GetStreamUri>",
                creds,
            )
        except OSError as exc:  # un perfil sin stream no debe abortar el resto
            _log.warning("cam %s: GetStreamUri(%s) falló: %s", ip, tok, exc)
            uris.append(None)
            continue
        uris.append(parse_stream_uri(resp))
    main, sub = select_main_sub(uris)
    if not main:
        _log.warning("cam %s (%s): sin URI RTSP resoluble", ip, serial)
        return None
    return DiscoveredCamera(id=f"onvif-{serial}", ip=ip, rtsp_main=main, rtsp_sub=sub)
