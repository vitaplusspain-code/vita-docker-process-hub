from __future__ import annotations

import json
from dataclasses import dataclass, field

# Un keypoint COCO-17: (x, y, conf) en píxeles de la imagen analizada.
Keypoints = tuple[tuple[float, float, float], ...]


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    bbox: tuple[int, int, int, int]
    # Solo lo rellena un detector de pose (person_pose). None = detector sin
    # pose; la analítica de caídas degrada a geometría de caja en ese caso.
    keypoints: Keypoints | None = None


@dataclass
class Camera:
    id: str
    name: str
    last_ip: str
    enabled: bool = True
    # URIs que ONVIF resolvió en su día, sin credenciales. Permiten reconectar
    # cuando el descubrimiento no encuentra la cámara (firmware que apaga ONVIF
    # al reiniciar, multicast que no llega). Vacías = aún no descubierta.
    rtsp_main: str = ""
    rtsp_sub: str = ""


@dataclass(frozen=True)
class Event:
    hub_id: str
    camera_id: str
    camera_name: str
    type: str
    severity: str
    timestamp: str
    payload: dict[str, object] = field(default_factory=dict)
    schema_version: int = 1

    def to_json(self) -> str:
        ordered = {
            "schema_version": self.schema_version,
            "hub_id": self.hub_id,
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "type": self.type,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }
        return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
