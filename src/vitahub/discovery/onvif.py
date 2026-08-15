from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
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


def select_main_sub(uris: list[str | None]) -> tuple[str, str]:
    main = sub = ""
    for i, uri in enumerate(uris):
        if uri and i == 0:
            main = uri
        elif uri:
            sub = uri
    return main, sub or main


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
    return cameras


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
    tokens = re.findall(r'token="([^"]+)"', profiles)
    uris: list[str | None] = [
        parse_stream_uri(
            _soap(
                media_url,
                "<trt:GetStreamUri><trt:StreamSetup>"
                "<tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>"
                "<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{tok}</trt:ProfileToken></trt:GetStreamUri>",
                creds,
            )
        )
        for tok in dict.fromkeys(tokens)
    ]
    main, sub = select_main_sub(uris)
    return DiscoveredCamera(
        id=f"onvif-{serial}", ip=ip, rtsp_main=main, rtsp_sub=sub
    )
