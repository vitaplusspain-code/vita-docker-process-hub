from vitahub.discovery.onvif import (
    build_probe,
    parse_probe_matches,
    parse_serial,
    parse_stream_uri,
    select_main_sub,
    wsse_header,
)

PROBE_MATCH = """<?xml version="1.0"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <e:Body><d:ProbeMatches><d:ProbeMatch>
   <d:XAddrs>http://192.168.1.190:10000/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>"""

DEVINFO = """<Envelope><Body><GetDeviceInformationResponse>
 <SerialNumber>szjsa81a81e6adf9</SerialNumber>
</GetDeviceInformationResponse></Body></Envelope>"""

STREAMURI = """<Envelope><Body><GetStreamUriResponse><MediaUri>
 <Uri>rtsp://192.168.1.190/Streaming/Channels/1</Uri>
</MediaUri></GetStreamUriResponse></Body></Envelope>"""


def test_build_probe_is_ws_discovery():
    probe = build_probe()
    assert b"Probe" in probe
    assert b"discovery" in probe


def test_parse_probe_matches_extracts_ip():
    assert parse_probe_matches(PROBE_MATCH) == ["192.168.1.190"]


def test_parse_serial():
    assert parse_serial(DEVINFO) == "szjsa81a81e6adf9"
    assert parse_serial("<empty/>") is None


def test_parse_stream_uri():
    assert parse_stream_uri(STREAMURI) == "rtsp://192.168.1.190/Streaming/Channels/1"


def test_wsse_header_contains_digest_and_nonce():
    header = wsse_header("admin", "secret", "2026-08-15T10:00:00Z", b"0123456789abcdef")
    assert "UsernameToken" in header
    assert "admin" in header
    assert "secret" not in header  # la clave nunca va en claro


def test_select_main_sub_two_uris():
    assert select_main_sub(["rtsp://a", "rtsp://b"]) == ("rtsp://a", "rtsp://b")


def test_select_main_sub_single_uri_falls_back():
    assert select_main_sub(["rtsp://a"]) == ("rtsp://a", "rtsp://a")


def test_select_main_sub_three_uris_sub_is_last():
    assert select_main_sub(["rtsp://a", "rtsp://b", "rtsp://c"]) == (
        "rtsp://a",
        "rtsp://c",
    )


def test_select_main_sub_empty():
    assert select_main_sub([]) == ("", "")


def test_select_main_sub_none_at_index_zero():
    assert select_main_sub([None, "rtsp://b"]) == ("", "rtsp://b")
