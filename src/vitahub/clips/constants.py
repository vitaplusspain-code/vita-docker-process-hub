"""Constantes globales del subsistema de clips (ver spec del clip de caída)."""
from __future__ import annotations

# Ventana del clip alrededor de la caída.
PRE_S = 15.0
POST_S = 10.0
# Tope de espera de post-roll si el episodio sigue abierto.
MAX_POST_S = 30.0
# Buffer en memoria por cámara: cubre PRE_S + MAX_POST_S con margen.
BUFFER_S = 45.0

# Caída más lenta que esto (drop_speed) se descarta como no riesgosa.
RISK_MIN_DROP = 0.5

# Cola de clips pendientes de subir a S3: límites de tamaño y antigüedad.
PENDING_MAX_MB = 200
PENDING_MAX_AGE_S = 3 * 24 * 3600
