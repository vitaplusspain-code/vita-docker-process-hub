from vitahub.ingest.rtsp import (
    backoff_delay,
    is_stalled,
    should_sample,
    strip_credentials,
    with_credentials,
)


def test_backoff_is_capped_exponential():
    assert backoff_delay(0) == 1.0
    assert backoff_delay(1) == 2.0
    assert backoff_delay(2) == 4.0
    assert backoff_delay(10) == 30.0  # cap


def test_should_sample_respects_fps():
    # 2 fps => 1 muestra cada 0.5s
    assert should_sample(last_sample_t=0.0, now=0.4, sample_fps=2.0) is False
    assert should_sample(last_sample_t=0.0, now=0.5, sample_fps=2.0) is True


def test_is_stalled():
    assert is_stalled(last_frame_t=0.0, now=9.0, max_stale_s=10.0) is False
    assert is_stalled(last_frame_t=0.0, now=11.0, max_stale_s=10.0) is True


def test_with_credentials_injects_userinfo_urlencoded():
    assert (
        with_credentials("rtsp://10.0.0.5:554/Streaming/Channels/1", "admin", "p@ss")
        == "rtsp://admin:p%40ss@10.0.0.5:554/Streaming/Channels/1"
    )


def test_with_credentials_leaves_existing_userinfo_unchanged():
    url = "rtsp://foo:bar@10.0.0.5:554/Streaming/Channels/1"
    assert with_credentials(url, "admin", "p@ss") == url


def test_with_credentials_empty_user_unchanged():
    url = "rtsp://10.0.0.5:554/Streaming/Channels/1"
    assert with_credentials(url, "", "p@ss") == url


def test_strip_credentials_removes_userinfo():
    url = "rtsp://admin:cl%40ve@10.0.0.5:554/V_ENC_000"
    assert strip_credentials(url) == "rtsp://10.0.0.5:554/V_ENC_000"


def test_strip_credentials_leaves_clean_url_untouched():
    url = "rtsp://10.0.0.5:554/V_ENC_000"
    assert strip_credentials(url) == url


def test_strip_credentials_keeps_query_and_port():
    url = "rtsp://user:pass@10.0.0.5:8554/cam?channel=1"
    assert strip_credentials(url) == "rtsp://10.0.0.5:8554/cam?channel=1"


def test_strip_then_with_credentials_roundtrip():
    original = "rtsp://admin:secreto@10.0.0.5:554/V_ENC_000"
    limpia = strip_credentials(original)
    assert "secreto" not in limpia
    assert with_credentials(limpia, "admin", "secreto") == original
