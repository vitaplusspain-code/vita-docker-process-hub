from vitahub.models import Camera
from vitahub.registry import DiscoveredCamera, reconcile


def _disc(id_, ip):
    return DiscoveredCamera(
        id=id_, ip=ip,
        rtsp_main=f"rtsp://{ip}:554/Streaming/Channels/1",
        rtsp_sub=f"rtsp://{ip}:554/Streaming/Channels/2",
    )


def test_new_camera_is_added():
    merged, changes = reconcile([], [_disc("onvif-a", "10.0.0.5")])
    assert [c.id for c in merged] == ["onvif-a"]
    assert merged[0].name == "camera-1"
    assert merged[0].last_ip == "10.0.0.5"
    assert changes == [__import__("vitahub.registry", fromlist=["RegistryChange"]).RegistryChange("onvif-a", "added")]


def test_known_camera_ip_change_is_updated():
    existing = [Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.9:554/Streaming/Channels/1",
        rtsp_sub="rtsp://10.0.0.9:554/Streaming/Channels/2",
    )]
    merged, changes = reconcile(existing, [_disc("onvif-a", "10.0.0.9")])
    assert merged[0].last_ip == "10.0.0.9"
    assert merged[0].name == "salon"  # nombre editado se preserva
    assert [ch.kind for ch in changes] == ["ip_changed"]


def test_known_camera_same_ip_no_change():
    existing = [Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/Streaming/Channels/1",
        rtsp_sub="rtsp://10.0.0.5:554/Streaming/Channels/2",
    )]
    _, changes = reconcile(existing, [_disc("onvif-a", "10.0.0.5")])
    assert changes == []


def test_unseen_camera_is_kept():
    existing = [Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")]
    merged, changes = reconcile(existing, [])
    assert [c.id for c in merged] == ["onvif-a"]
    assert changes == []


def test_new_camera_stores_its_uris():
    merged, _ = reconcile([], [_disc("onvif-a", "10.0.0.5")])
    assert merged[0].rtsp_main == "rtsp://10.0.0.5:554/Streaming/Channels/1"
    assert merged[0].rtsp_sub == "rtsp://10.0.0.5:554/Streaming/Channels/2"


def test_changed_uri_is_updated_and_reported():
    existing = [Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/vieja",
        rtsp_sub="rtsp://10.0.0.5:554/vieja-sub",
    )]
    merged, changes = reconcile(existing, [_disc("onvif-a", "10.0.0.5")])
    assert merged[0].rtsp_main == "rtsp://10.0.0.5:554/Streaming/Channels/1"
    assert "uri_changed" in [ch.kind for ch in changes]


def test_same_uris_produce_no_change():
    """Sin esto se reescribiría el YAML en cada rescan periódico."""
    existing = [Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/Streaming/Channels/1",
        rtsp_sub="rtsp://10.0.0.5:554/Streaming/Channels/2",
    )]
    _, changes = reconcile(existing, [_disc("onvif-a", "10.0.0.5")])
    assert changes == []


def test_uris_are_stored_without_credentials():
    sucia = DiscoveredCamera(
        id="onvif-a", ip="10.0.0.5",
        rtsp_main="rtsp://admin:secreto@10.0.0.5:554/main",
        rtsp_sub="rtsp://admin:secreto@10.0.0.5:554/sub",
    )
    merged, _ = reconcile([], [sucia])
    assert "secreto" not in merged[0].rtsp_main
    assert "secreto" not in merged[0].rtsp_sub
    assert merged[0].rtsp_main == "rtsp://10.0.0.5:554/main"


def test_unseen_camera_keeps_its_uris():
    """La red de seguridad: una cámara que deja de verse conserva cómo conectar."""
    existing = [Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/main", rtsp_sub="rtsp://10.0.0.5:554/sub",
    )]
    merged, changes = reconcile(existing, [])
    assert merged[0].rtsp_main == "rtsp://10.0.0.5:554/main"
    assert changes == []
