from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from vitahub.config import ConfigError
from vitahub.gallery.s3 import gallery_key
from vitahub.gallery.service import CloudGalleryService
from vitahub.gallery.shadow import identity_only_payload, shadow_topics
from vitahub.identity.base import FaceEngine, FaceObservation
from vitahub.identity.stub import StubFaceEngine

HUB = "hub-1"
TOPICS = shadow_topics(HUB)
K1 = "a" * 32
P1 = "11111111-1111-1111-1111-111111111111"
P2 = "22222222-2222-2222-2222-222222222222"


def _jpeg_bytes() -> bytes:
    image = np.full((32, 32, 3), 128, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", image)
    assert ok
    return bytes(buf)


def _face() -> FaceObservation:
    emb = np.zeros(512, dtype=np.float32)
    emb[0] = 1.0
    return FaceObservation(bbox=(0, 0, 10, 10), embedding=emb)


class FakeLink:
    """Doble de `MqttLink`: guarda publicaciones y entrega mensajes por topic."""

    def __init__(self, calls: list[tuple[str, str]] | None = None) -> None:
        self.handlers: dict[str, Callable[[bytes], None]] = {}
        self.listeners: list[Callable[[], None]] = []
        self.published: list[tuple[str, str]] = []
        self.calls = calls if calls is not None else []
        self.connected = False

    def subscribe(self, topic_filter: str, handler: Callable[[bytes], None]) -> None:
        self.handlers[topic_filter] = handler

    def add_connect_listener(self, fn: Callable[[], None]) -> None:
        self.listeners.append(fn)
        if self.connected:
            fn()

    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))
        self.calls.append(("publish", topic))

    def connect(self) -> None:
        self.connected = True
        for fn in self.listeners:
            fn()

    def deliver(self, topic: str, body: Any) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.handlers[topic](raw)

    def on(self, topic: str) -> list[str]:
        return [payload for t, payload in self.published if t == topic]

    def updates(self) -> list[dict[str, Any]]:
        return [json.loads(p) for p in self.on(TOPICS.update)]


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Store:
    """S3 en memoria, con contadores para comprobar qué se pidió."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.unlisted: set[str] = set()
        self.list_errors = 0
        self.list_calls = 0
        self.gets: list[str] = []
        self.on_get: Callable[[str], None] | None = None

    def put_manifest(self, version: int, persons: dict[str, list[str]]) -> None:
        self.objects[gallery_key(HUB, "manifest.json")] = json.dumps(
            {"version": version, "persons": persons}
        ).encode()

    def put_photo(self, person_key: str, photo_id: str) -> None:
        self.objects[gallery_key(HUB, person_key, f"{photo_id}.jpg")] = _jpeg_bytes()

    def list_keys(self, prefix: str) -> frozenset[str]:
        self.list_calls += 1
        if self.list_errors > 0:
            self.list_errors -= 1
            raise RuntimeError("AccessDenied (403)")
        return frozenset(k for k in self.objects if k.startswith(prefix) and k not in self.unlisted)

    def get_object(self, key: str) -> bytes:
        self.gets.append(key)
        if self.on_get is not None:
            self.on_get(key)
        return self.objects[key]


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        identity_enabled: bool = True,
        engine: Callable[[], FaceEngine] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.link = FakeLink(self.calls)
        self.store = Store()
        self.clock = FakeClock()
        self.faces = tmp_path / "faces"
        self.faces.mkdir()
        self.engine = StubFaceEngine([[_face()] for _ in range(20)])
        self.service = CloudGalleryService(
            link=self.link,  # type: ignore[arg-type]
            hub_id=HUB,
            faces_dir=self.faces,
            list_keys=self.store.list_keys,
            get_object=self.store.get_object,
            engine=engine or (lambda: self.engine),
            identity_enabled=identity_enabled,
            request_restart=lambda: self.calls.append(("restart", "")),
            clock=self.clock,
        )
        # Con stop ya activo el hilo sale sin hacer nada: los tests llaman a step a mano.
        stopped = threading.Event()
        stopped.set()
        self.service.start(stopped)

    @property
    def restarts(self) -> int:
        return sum(1 for kind, _ in self.calls if kind == "restart")

    def sync(self, state: dict[str, Any], at: float = 0.0) -> None:
        """Conecta y entrega `get/accepted` con ese `state`."""
        self.clock.t = at
        self.link.connect()
        self.service.step(at)
        self.link.deliver(TOPICS.get_accepted, {"state": state})

    def delta(self, version: int, at: float) -> None:
        self.clock.t = at
        self.link.deliver(TOPICS.update_delta, {"state": {"version": version}})


def test_get_on_connect_and_apply_after_debounce(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(10, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.link.connect()
    h.service.step(0)
    assert h.link.on(TOPICS.get) == ["{}"]

    h.link.deliver(TOPICS.get_accepted, {"state": {"desired": {"version": 10}}})
    h.service.step(0)
    assert h.link.updates() == []
    h.service.step(30)
    assert [u["state"]["reported"]["version"] for u in h.link.updates()] == [10]
    assert (h.faces / K1 / f"{P1}.jpg").is_file()


def test_get_repeats_until_answered(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.link.connect()
    assert h.service.step(0) == 30
    assert h.service.step(30) == 30
    assert h.link.on(TOPICS.get) == ["{}", "{}"]
    h.link.deliver(TOPICS.get_accepted, {"state": {}})
    assert h.service.step(60) is None
    assert len(h.link.on(TOPICS.get)) == 2


def test_get_rejected_stops_get_retries(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.link.connect()
    h.service.step(0)
    h.service.step(30)
    assert len(h.link.on(TOPICS.get)) == 2
    h.link.deliver(TOPICS.get_rejected, {"code": 404, "message": "No shadow exists"})
    assert h.service.step(60) is None
    assert h.service.step(1000) is None
    assert len(h.link.on(TOPICS.get)) == 2


def test_each_reconnect_asks_again(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.sync({})
    h.clock.t = 100
    h.link.connect()
    h.service.step(100)
    assert len(h.link.on(TOPICS.get)) == 2


def test_delta_before_get_response_waits_for_get_not_busy(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(10, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.link.connect()
    assert h.service.step(0) == 30
    h.delta(10, at=5)
    # Debounce vencido sin respuesta al get: step(35) repite el get (vencido en
    # 30) y desde ahí se espera al siguiente, en 65; nunca 0.
    returned = [h.service.step(t) for t in (35, 35.001, 36, 59.9)]
    assert returned == [30, pytest.approx(29.999), 29, pytest.approx(5.1)]
    assert 0.0 not in returned
    assert h.link.updates() == []

    h.clock.t = 60
    h.link.deliver(TOPICS.get_accepted, {"state": {"desired": {"version": 10}}})
    returned.append(h.service.step(60))
    assert returned[-1] == 30
    assert 0.0 not in returned
    assert h.link.updates() == []
    assert h.service.step(90) is None
    assert [u["state"]["reported"]["version"] for u in h.link.updates()] == [10]


def test_delta_resets_debounce(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(11, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.sync({})
    h.delta(10, at=0)
    h.delta(11, at=20)
    assert h.service.step(40) == 10
    assert h.link.updates() == []
    h.service.step(50)
    assert [u["state"]["reported"]["version"] for u in h.link.updates()] == [11]


def test_restart_only_when_identity_enabled_and_changed(tmp_path: Path) -> None:
    on_dir = tmp_path / "on"
    on_dir.mkdir()
    h = Harness(on_dir)
    h.store.put_manifest(10, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.sync({"desired": {"version": 10}})
    h.service.step(30)
    assert h.restarts == 1

    # Mismo contenido con otra versión: el disco no cambia, no se reinicia.
    h.store.put_manifest(11, {K1: [P1]})
    h.delta(11, at=40)
    h.service.step(70)
    assert [u["state"]["reported"]["version"] for u in h.link.updates()] == [10, 11]
    assert h.restarts == 1

    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = Harness(off_dir, identity_enabled=False)
    off.store.put_manifest(10, {K1: [P1]})
    off.store.put_photo(K1, P1)
    off.sync({"desired": {"version": 10}})
    off.service.step(30)
    assert (off.faces / K1 / f"{P1}.jpg").is_file()
    assert off.restarts == 0
    assert off.link.updates()[0]["state"]["reported"]["identity_enabled"] is False


def test_restart_happens_after_report(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(10, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.sync({"desired": {"version": 10}})
    h.calls.clear()
    h.service.step(30)
    assert h.calls == [("publish", TOPICS.update), ("restart", "")]


def test_manifest_not_listed_is_empty_gallery(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    (h.faces / K1).mkdir()
    (h.faces / K1 / f"{P1}.jpg").write_bytes(_jpeg_bytes())
    h.sync(
        {
            "desired": {"version": 10},
            "reported": {"version": 5, "identity_enabled": True, "photos": {P1: {"status": "accepted"}}},
        }
    )
    h.service.step(30)
    assert not (h.faces / K1).exists()
    assert h.store.gets == []
    [update] = h.link.updates()
    assert update["state"]["reported"]["version"] == 10
    assert update["state"]["reported"]["photos"] == {P1: None}


def test_previous_ids_follow_each_report(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(10, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.sync({"desired": {"version": 10}})
    h.service.step(30)
    # P1 desaparece del manifiesto: el siguiente reported lo borra con null.
    h.store.put_manifest(11, {})
    h.delta(11, at=40)
    h.service.step(70)
    assert h.link.updates()[-1]["state"]["reported"]["photos"] == {P1: None}


def test_stale_manifest_retries(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(9, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.sync({"desired": {"version": 10}})
    assert h.service.step(30) == 60
    assert h.link.updates() == []
    assert not (h.faces / K1).exists()
    h.store.put_manifest(10, {K1: [P1]})
    assert h.service.step(90) is None
    assert [u["state"]["reported"]["version"] for u in h.link.updates()] == [10]


def test_photo_not_listed_is_pending_without_get(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(10, {K1: [P1, P2]})
    h.store.put_photo(K1, P1)
    h.sync({"desired": {"version": 10}})
    h.service.step(30)
    assert gallery_key(HUB, K1, f"{P2}.jpg") not in h.store.gets
    [update] = h.link.updates()
    assert update["state"]["reported"]["photos"] == {P1: {"status": "accepted"}}


def test_fetch_error_backs_off_and_retries(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(10, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.store.list_errors = 3
    h.sync({"desired": {"version": 10}})
    assert h.service.step(30) == 60
    assert h.service.step(90) == 120
    assert h.service.step(210) == 240
    assert h.link.updates() == []
    assert h.service.step(450) is None
    assert [u["state"]["reported"]["version"] for u in h.link.updates()] == [10]
    assert h.store.list_calls == 4


def test_backoff_caps_at_900_and_resets_after_success(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(10, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.store.list_errors = 7
    h.sync({"desired": {"version": 10}})
    now = 30.0
    delays = []
    for _ in range(7):
        delay = h.service.step(now)
        assert delay is not None
        delays.append(delay)
        now += delay
    assert delays == [60, 120, 240, 480, 900, 900, 900]
    assert h.service.step(now) is None

    # Tras el éxito, un fallo nuevo empieza otra vez en 60 s.
    h.store.list_errors = 1
    h.delta(11, at=now)
    assert h.service.step(now + 30) == 60


def test_engine_that_does_not_load_backs_off(tmp_path: Path) -> None:
    def broken() -> FaceEngine:
        raise ConfigError("no hay modelo de caras")

    h = Harness(tmp_path, engine=broken)
    h.store.put_manifest(10, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.sync({"desired": {"version": 10}})
    assert h.service.step(30) == 60
    assert h.link.updates() == []
    assert h.restarts == 0


def test_startup_republishes_identity_enabled_when_stale(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.sync(
        {
            "desired": {"version": 10},
            "reported": {"version": 10, "identity_enabled": False, "photos": {}},
        }
    )
    assert h.service.step(1000) is None
    assert h.link.on(TOPICS.update) == [identity_only_payload(True)]
    assert h.store.list_calls == 0
    assert h.store.gets == []


def test_no_identity_republish_when_current(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.sync(
        {
            "desired": {"version": 10},
            "reported": {"version": 10, "identity_enabled": True, "photos": {}},
        }
    )
    assert h.service.step(1000) is None
    assert h.link.on(TOPICS.update) == []


def test_delta_during_apply_schedules_again(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.store.put_manifest(10, {K1: [P1]})
    h.store.put_photo(K1, P1)
    h.sync({"desired": {"version": 10}})

    def delta_meanwhile(key: str) -> None:
        h.store.on_get = None
        h.delta(11, at=30)

    h.store.on_get = delta_meanwhile
    assert h.service.step(30) == 30
    assert [u["state"]["reported"]["version"] for u in h.link.updates()] == [10]
    h.store.put_manifest(11, {K1: [P1]})
    h.service.step(60)
    assert [u["state"]["reported"]["version"] for u in h.link.updates()] == [10, 11]


def test_malformed_messages_are_ignored(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.link.connect()
    h.service.step(0)
    h.link.deliver(TOPICS.get_accepted, b"{roto")
    h.link.deliver(TOPICS.update_delta, b"\xff\xfe")
    # Sigue esperando respuesta al get: el mensaje roto no cuenta.
    assert h.service.step(30) == 30
    assert len(h.link.on(TOPICS.get)) == 2


def test_thread_applies_and_stops(tmp_path: Path) -> None:
    link = FakeLink()
    store = Store()
    store.put_manifest(10, {K1: [P1]})
    store.put_photo(K1, P1)
    faces = tmp_path / "faces"
    faces.mkdir()
    service = CloudGalleryService(
        link=link,  # type: ignore[arg-type]
        hub_id=HUB,
        faces_dir=faces,
        list_keys=store.list_keys,
        get_object=store.get_object,
        engine=lambda: StubFaceEngine([[_face()]]),
        identity_enabled=True,
        request_restart=lambda: None,
        debounce_s=0.0,
    )
    stop = threading.Event()
    service.start(stop)
    [thread] = [t for t in threading.enumerate() if t.name == "face-gallery"]
    assert thread.daemon
    link.connect()
    link.deliver(TOPICS.get_accepted, {"state": {"desired": {"version": 10}}})
    deadline = time.monotonic() + 5
    while not link.updates() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [u["state"]["reported"]["version"] for u in link.updates()] == [10]
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
