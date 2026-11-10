"""Cajas cortadas por el borde del encuadre.

Una persona que sale parcialmente del plano deja una caja truncada: sus
proporciones y su base no dicen nada fiable de la postura ni de dónde pisa.
`frame_size` es `(ancho, alto)` en píxeles del frame original, el mismo espacio
que las cajas del detector. `None` = tamaño desconocido: nunca toca el borde.
"""
from __future__ import annotations

# Holgura en píxeles: el detector rara vez clava la caja exactamente en el borde.
EDGE_MARGIN_PX = 2


def touches_edge(bbox: tuple[int, int, int, int], frame_size: tuple[int, int] | None) -> bool:
    """True si algún lado de la caja está a EDGE_MARGIN_PX o menos del borde."""
    if frame_size is None:
        return False
    x1, y1, x2, y2 = bbox
    w, h = frame_size
    return (
        x1 <= EDGE_MARGIN_PX
        or y1 <= EDGE_MARGIN_PX
        or x2 >= w - EDGE_MARGIN_PX
        or y2 >= h - EDGE_MARGIN_PX
    )


def touches_bottom(bbox: tuple[int, int, int, int], frame_size: tuple[int, int] | None) -> bool:
    """Igual que touches_edge pero solo el borde inferior."""
    if frame_size is None:
        return False
    return bbox[3] >= frame_size[1] - EDGE_MARGIN_PX
