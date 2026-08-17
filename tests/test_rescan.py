import threading
from pathlib import Path

from vitahub.config import (
    Credentials,
    DiscoveryConfig,
    HubConfig,
    InferenceConfig,
    load_config,
)
from vitahub.models import Camera
from vitahub.registry import DiscoveredCamera
from vitahub.rescan import RescanService


class _FakeSupervisor:
    def __init__(self):
        self.calls = []

    def apply(self, cameras, uris):
        self.calls.append((list(cameras), dict(uris)))
        from vitahub.supervisor import SupervisorChange

        return SupervisorChange(started=[c.id for c in cameras if uris.get(c.id)])

    def stop_all(self):
        pass


def _cfg(cameras=None):
    return HubConfig(
        hub_id="hub-test",
        discovery=DiscoveryConfig(interval_seconds=60),
        inference=InferenceConfig(detector="stub", stream="substream"),
        cameras=cameras if cameras is not None else [],
        credentials=Credentials(onvif_user="admin", onvif_password="clave"),
    )


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-test\ncameras: []\n")
    return path


def _disc(id_, ip):
    return DiscoveredCamera(
        id=id_,
        ip=ip,
        rtsp_main=f"rtsp://{ip}:554/Streaming/Channels/1",
        rtsp_sub=f"rtsp://{ip}:554/Streaming/Channels/2",
    )


def test_new_camera_is_added_and_persisted(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg()
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [_disc("onvif-a", "10.0.0.5")])

    result = service.run_once()

    assert result.status == "ok"
    assert result.found == 1
    assert result.added == [{"id": "onvif-a", "name": "camera-1", "ip": "10.0.0.5"}]
    assert result.started == ["onvif-a"]
    assert result.cameras == 1
    # persistido en el YAML
    reloaded = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert [c.id for c in reloaded.cameras] == ["onvif-a"]


def test_credentials_are_injected_in_the_uri(tmp_path):
    path = _config_file(tmp_path)
    sup = _FakeSupervisor()
    service = RescanService(_cfg(), path, sup, lambda creds: [_disc("onvif-a", "10.0.0.5")])

    service.run_once()

    _, uris = sup.calls[0]
    assert uris["onvif-a"] == "rtsp://admin:clave@10.0.0.5:554/Streaming/Channels/2"


def test_main_stream_is_used_when_configured(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg()
    cfg.inference.stream = "main"
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [_disc("onvif-a", "10.0.0.5")])

    service.run_once()

    _, uris = sup.calls[0]
    assert uris["onvif-a"].endswith("/Streaming/Channels/1")


def test_no_changes_does_not_write_to_disk(tmp_path, monkeypatch):
    """Blinda que un rescan cada 60s no escriba el YAML sin motivo."""
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")])
    saves = []
    monkeypatch.setattr(
        "vitahub.rescan.save_cameras", lambda p, c: saves.append(p)
    )
    service = RescanService(cfg, path, _FakeSupervisor(), lambda creds: [_disc("onvif-a", "10.0.0.5")])

    result = service.run_once()

    assert result.status == "ok"
    assert saves == []


def test_ip_change_is_reported_and_persisted(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")])
    service = RescanService(cfg, path, _FakeSupervisor(), lambda creds: [_disc("onvif-a", "10.0.0.9")])

    result = service.run_once()

    assert result.ip_changed == ["onvif-a"]
    assert result.added == []
    reloaded = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert reloaded.cameras[0].last_ip == "10.0.0.9"


def test_discovery_failure_returns_error_without_raising(tmp_path):
    path = _config_file(tmp_path)

    def _boom(creds):
        raise OSError("la red se cayó")

    service = RescanService(_cfg(), path, _FakeSupervisor(), _boom)

    result = service.run_once()

    assert result.status == "error"
    assert result.found == 0


def test_save_failure_does_not_block_workers(tmp_path, monkeypatch):
    """Mejor las cámaras emitiendo aunque el registro no haya persistido."""
    path = _config_file(tmp_path)

    def _boom(p, c):
        raise OSError("disco lleno")

    monkeypatch.setattr("vitahub.rescan.save_cameras", _boom)
    sup = _FakeSupervisor()
    service = RescanService(_cfg(), path, sup, lambda creds: [_disc("onvif-a", "10.0.0.5")])

    result = service.run_once()

    assert result.status == "ok"
    assert result.started == ["onvif-a"]
    assert len(sup.calls) == 1


def test_concurrent_rescan_returns_busy(tmp_path):
    path = _config_file(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    second: list[str] = []

    def _slow_discover(creds):
        entered.set()
        release.wait(timeout=5.0)
        return []

    service = RescanService(_cfg(), path, _FakeSupervisor(), _slow_discover)
    worker = threading.Thread(target=service.run_once, daemon=True)
    worker.start()
    assert entered.wait(timeout=5.0)

    second.append(service.run_once().status)

    release.set()
    worker.join(timeout=5.0)
    assert second == ["busy"]


def test_unseen_camera_is_kept_and_not_started(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")])
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [])

    result = service.run_once()

    assert result.cameras == 1  # sigue registrada
    assert result.started == []
    cameras, uris = sup.calls[0]
    assert [c.id for c in cameras] == ["onvif-a"]
    assert uris == {}  # sin URI: el supervisor no la tocará


def test_supervisor_apply_failure_returns_error(tmp_path):
    """Fallo en supervisor.apply no propaga; devuelve status=error."""
    path = _config_file(tmp_path)

    class _FailingSupervisor:
        def apply(self, cameras, uris):
            raise RuntimeError("supervisor error")

        def stop_all(self):
            pass

    service = RescanService(
        _cfg(),
        path,
        _FailingSupervisor(),
        lambda creds: [_disc("onvif-a", "10.0.0.5")],
    )

    result = service.run_once()

    assert result.status == "error"
