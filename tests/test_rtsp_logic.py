from vitahub.ingest.rtsp import (
    MAX_BACKOFF_ATTEMPT,
    backoff_delay,
    is_stalled,
    next_attempt,
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


def test_backoff_huge_attempt_returns_cap_without_overflow():
    # Cámara caída durante horas: attempt crece sin límite (visto en el Jetson
    # del piloto, 2026-08-22). 2**attempt no cabe en float y reventaba el worker.
    assert backoff_delay(5000) == 30.0
    assert backoff_delay(10**6, base=0.5, cap=7.5) == 7.5


def test_next_attempt_saturates_at_max():
    assert next_attempt(0) == 1
    assert next_attempt(MAX_BACKOFF_ATTEMPT - 1) == MAX_BACKOFF_ATTEMPT
    assert next_attempt(MAX_BACKOFF_ATTEMPT) == MAX_BACKOFF_ATTEMPT
    assert next_attempt(10**9) == MAX_BACKOFF_ATTEMPT
    # El tope de attempt siempre produce la espera máxima.
    assert backoff_delay(MAX_BACKOFF_ATTEMPT) == 30.0
