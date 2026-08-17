import threading
import time

from vitahub.models import Camera
from vitahub.supervisor import CameraSupervisor


def _cam(id_, enabled=True):
    return Camera(id=id_, name=f"n-{id_}", last_ip="10.0.0.1", enabled=enabled)


def _recording_worker(starts):
    """Worker falso: apunta la URI con la que arrancó y espera su señal de parada."""
    def worker(camera, uri, stop):
        starts.append((camera.id, uri))
        stop.wait()
    return worker


def _eventually(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_new_camera_is_started():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    change = sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert change.started == ["a"]
    assert _eventually(lambda: starts == [("a", "rtsp://old")])
    sup.stop_all()


def test_apply_is_idempotent():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    change = sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert change.started == []
    assert change.restarted == []
    sup.stop_all()


def test_changed_uri_restarts_worker():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts), join_timeout=2.0)
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert _eventually(lambda: len(starts) == 1)
    change = sup.apply([_cam("a")], {"a": "rtsp://new"})
    assert change.restarted == ["a"]
    assert _eventually(lambda: starts == [("a", "rtsp://old"), ("a", "rtsp://new")])
    sup.stop_all()


def test_absent_camera_is_left_alone():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    change = sup.apply([_cam("a")], {})  # no descubierta en este ciclo
    assert change.started == []
    assert change.restarted == []
    assert len(starts) == 1  # su worker sigue vivo, no se relanzó ni se paró
    sup.stop_all()


def test_camera_without_uri_is_not_started():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    change = sup.apply([_cam("a")], {})
    assert change.started == []
    assert starts == []


def test_disabled_camera_is_not_started():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    change = sup.apply([_cam("a", enabled=False)], {"a": "rtsp://old"})
    assert change.started == []
    assert starts == []


def test_disabled_camera_stops_running_worker():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts), join_timeout=2.0)
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert _eventually(lambda: len(starts) == 1)
    sup.apply([_cam("a", enabled=False)], {"a": "rtsp://old"})
    change = sup.apply([_cam("a", enabled=False)], {"a": "rtsp://old"})
    assert change.started == []  # sigue parada


def test_stop_all_stops_every_worker():
    alive = []

    def worker(camera, uri, stop):
        alive.append(camera.id)
        stop.wait()
        alive.remove(camera.id)

    sup = CameraSupervisor(worker, join_timeout=2.0)
    sup.apply([_cam("a"), _cam("b")], {"a": "rtsp://a", "b": "rtsp://b"})
    assert _eventually(lambda: len(alive) == 2)
    sup.stop_all()
    assert alive == []


def test_replacement_waits_for_previous_worker_to_die():
    """El caso delicado: dos workers de la misma cámara a la vez duplicarían eventos."""
    starts = []
    release = threading.Event()

    def stubborn_worker(camera, uri, stop):
        starts.append(uri)
        stop.wait()
        release.wait(timeout=5.0)  # tarda en morir DESPUÉS de recibir la señal

    sup = CameraSupervisor(stubborn_worker, join_timeout=0.2)
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert _eventually(lambda: starts == ["rtsp://old"])

    change = sup.apply([_cam("a")], {"a": "rtsp://new"})
    assert change.restarted == []          # no se reporta el reinicio
    assert starts == ["rtsp://old"]        # y sobre todo: el nuevo NO arrancó

    release.set()                          # ahora el viejo muere de verdad

    # Se reintenta el apply hasta que el reemplazo arranca. No se asierta sobre
    # `started` vs `restarted`: cuál de los dos sea depende de si el hilo viejo
    # ya había muerto al entrar, y eso es una carrera. Lo que importa —y es
    # determinista— es que acabe habiendo exactamente un worker con la URI nueva.
    def _replacement_started():
        sup.apply([_cam("a")], {"a": "rtsp://new"})
        return starts == ["rtsp://old", "rtsp://new"]

    assert _eventually(_replacement_started)
    sup.stop_all()
