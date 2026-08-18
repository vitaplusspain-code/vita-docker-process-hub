from datetime import UTC, datetime

from vitahub.analytics.connection_monitor import ConnectionMonitor
from vitahub.models import Camera


def _clock():
    return datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)


def _cam(id_="onvif-a"):
    return Camera(id=id_, name=f"n-{id_}", last_ip="10.0.0.5")


def _monitor():
    return ConnectionMonitor("hub-test", unreachable_after_s=300.0, clock=_clock)


def test_no_event_before_the_threshold():
    m = _monitor()
    cam = _cam()
    assert m.on_failed(cam, 0.0) == []
    assert m.on_failed(cam, 299.0) == []


def test_unreachable_is_emitted_once_the_threshold_passes():
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    events = m.on_failed(cam, 301.0)
    assert len(events) == 1
    assert events[0].type == "camera_unreachable"
    assert events[0].severity == "medium"
    assert events[0].camera_id == "onvif-a"
    assert events[0].payload["last_ip"] == "10.0.0.5"
    assert events[0].payload["minutes_down"] == 5.0


def test_unreachable_is_not_repeated_while_still_down():
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    m.on_failed(cam, 301.0)
    assert m.on_failed(cam, 900.0) == []


def test_reachable_only_after_an_unreachable():
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    assert m.on_frame(cam, 10.0) == []             # nunca se reportó caída
    m.on_failed(cam, 20.0)
    m.on_failed(cam, 400.0)                        # aquí sí se reporta
    events = m.on_frame(cam, 500.0)
    assert len(events) == 1
    assert events[0].type == "camera_reachable"
    assert events[0].severity == "info"


def test_a_new_outage_emits_again():
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    m.on_failed(cam, 301.0)
    m.on_frame(cam, 310.0)
    m.on_failed(cam, 320.0)
    events = m.on_failed(cam, 700.0)
    assert [e.type for e in events] == ["camera_unreachable"]


def test_camera_that_never_connects_still_reports():
    """El escenario del corte de luz: vuelve sin ONVIF y con otra IP, nunca conecta."""
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    events = m.on_failed(cam, 400.0)
    assert [e.type for e in events] == ["camera_unreachable"]


def test_cameras_do_not_contaminate_each_other():
    m = _monitor()
    a, b = _cam("onvif-a"), _cam("onvif-b")
    m.on_failed(a, 0.0)
    m.on_failed(b, 0.0)
    m.on_frame(b, 100.0)
    events = m.on_failed(a, 400.0)
    assert [e.camera_id for e in events] == ["onvif-a"]
    # El brief original decía `assert m.on_failed(b, 401.0) == []`, pero era
    # contradictorio: con 401 - 100 = 301 s, que supera el umbral de 300 s,
    # la implementación *debe* reportar. El test verifica que `b` reporta con
    # su propia identidad, independiente del episodio de `a`.
    events_b = m.on_failed(b, 401.0)
    assert [e.camera_id for e in events_b] == ["onvif-b"]


def test_long_running_camera_is_not_falsely_reported_right_after_unplugging():
    """Bug crítico: la evidencia de "conectada" es el fotograma, no el `open`.

    Antes de la corrección, `last_ok` se fijaba en el `open` y nunca se
    refrescaba mientras fluían fotogramas: una cámara con horas de vídeo que
    se desenchufaba reportaba `camera_unreachable` a los pocos segundos
    (con `down_for` calculado desde el `open` inicial, horas atrás), anulando
    el umbral de 5 minutos.
    """
    m = _monitor()
    cam = _cam()
    m.on_frame(cam, 0.0)
    m.on_frame(cam, 10_800.0)  # tres horas de fotogramas sostenidos
    # se desenchufa; el primer reintento de `open_capture` falla 10s después
    # del último fotograma real (el watchdog de `is_stalled`/backoff inicial)
    events = m.on_failed(cam, 10_810.0)
    assert events == []  # 10s desde el último fotograma, muy por debajo del umbral
    events = m.on_failed(cam, 10_800.0 + 301.0)
    assert len(events) == 1
    assert events[0].type == "camera_unreachable"
    assert events[0].payload["minutes_down"] == 5.0  # no las ~180 horas conectada


def test_camera_that_opens_repeatedly_without_frames_still_reports():
    """Bug crítico (reverso): abrir el socket no es evidencia de vídeo.

    Una cámara con el slot de stream agotado (o RTP filtrado) puede abrir
    la conexión una y otra vez sin entregar jamás un fotograma. El episodio
    de fallos debe acumularse igualmente y acabar reportando, sin que ningún
    `open` sin fotograma lo resetee.
    """
    m = _monitor()
    cam = _cam()
    # varios ciclos de "abre pero no entrega vídeo": nada llama a on_frame
    m.on_failed(cam, 0.0)
    m.on_failed(cam, 60.0)
    m.on_failed(cam, 120.0)
    assert m.on_failed(cam, 200.0) == []  # aún por debajo del umbral
    events = m.on_failed(cam, 301.0)
    assert [e.type for e in events] == ["camera_unreachable"]
