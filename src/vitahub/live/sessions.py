from __future__ import annotations

from dataclasses import dataclass

from vitahub.live.token import SessionClaims


@dataclass(frozen=True)
class Admitted:
    renewed: bool
    replaced: tuple[str, ...]


@dataclass(frozen=True)
class Rejected:
    reason: str  # limit | invalid


class SessionBook:
    """Conexiones autenticadas del hub y sus reglas (spec §3.4).

    Solo cuenta conexiones que ya enviaron un `auth` válido: un peer que
    negocia WebRTC pero aún no se ha autenticado no ocupa plaza, para que
    nadie pueda llenar el hogar sin un token.
    """

    def __init__(self, max_viewers: int) -> None:
        self._max = max_viewers
        self._by_conn: dict[str, SessionClaims] = {}

    def __len__(self) -> int:
        return len(self._by_conn)

    def admit(self, conn_id: str, claims: SessionClaims) -> Admitted | Rejected:
        current = self._by_conn.get(conn_id)
        if current is not None:
            if current.sub != claims.sub:
                return Rejected("invalid")
            self._by_conn[conn_id] = claims
            return Admitted(renewed=True, replaced=())
        replaced = tuple(c for c, cl in self._by_conn.items() if cl.sub == claims.sub)
        # La plaza de la conexión que se sustituye cuenta como libre: se
        # decide el tope ANTES de sacar a nadie, para no expulsar a la sesión
        # vieja y luego rechazar la nueva.
        if len(self._by_conn) - len(replaced) >= self._max:
            return Rejected("limit")
        for c in replaced:
            del self._by_conn[c]
        self._by_conn[conn_id] = claims
        return Admitted(renewed=False, replaced=replaced)

    def release(self, conn_id: str) -> SessionClaims | None:
        return self._by_conn.pop(conn_id, None)

    def claims_for(self, conn_id: str) -> SessionClaims | None:
        return self._by_conn.get(conn_id)

    def expired(self, now: float) -> list[str]:
        return [c for c, cl in self._by_conn.items() if cl.exp <= now]
