from vitahub.live.sessions import Admitted, Rejected, SessionBook
from vitahub.live.token import SessionClaims


def _c(sub: str, sid: str = "s", exp: float = 100.0) -> SessionClaims:
    return SessionClaims(sid=sid, sub=sub, hub="hub-1", exp=exp)


def test_admits_new_viewer():
    book = SessionBook(max_viewers=3)
    assert book.admit("c1", _c("a")) == Admitted(renewed=False, replaced=())
    assert len(book) == 1


def test_same_account_on_new_connection_replaces_old():
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a", "s1"))
    assert book.admit("c2", _c("a", "s2")) == Admitted(renewed=False, replaced=("c1",))
    assert book.claims_for("c1") is None
    assert len(book) == 1


def test_renewal_on_same_connection_is_not_replacement():
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a", "s1", exp=100))
    assert book.admit("c1", _c("a", "s2", exp=700)) == Admitted(renewed=True, replaced=())
    assert book.claims_for("c1") == _c("a", "s2", exp=700)


def test_renewal_does_not_hit_limit():
    book = SessionBook(max_viewers=1)
    book.admit("c1", _c("a"))
    assert isinstance(book.admit("c1", _c("a", "s2")), Admitted)


def test_other_account_on_same_connection_is_invalid():
    # Un token de otra cuenta por una conexión ya autenticada: alguien pasando
    # tokens ajenos. No se mezcla la sesión de dos personas.
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a"))
    assert book.admit("c1", _c("b")) == Rejected("invalid")
    assert book.claims_for("c1") == _c("a")


def test_limit_rejects_new_account():
    book = SessionBook(max_viewers=2)
    book.admit("c1", _c("a"))
    book.admit("c2", _c("b"))
    assert book.admit("c3", _c("c")) == Rejected("limit")
    assert len(book) == 2


def test_replacement_frees_its_own_slot_at_limit():
    # Con el hogar lleno, la misma cuenta cambiando de móvil debe entrar: su
    # conexión vieja sale y deja hueco.
    book = SessionBook(max_viewers=2)
    book.admit("c1", _c("a"))
    book.admit("c2", _c("b"))
    assert book.admit("c3", _c("a")) == Admitted(renewed=False, replaced=("c1",))


def test_rejected_admission_keeps_existing_sessions():
    book = SessionBook(max_viewers=1)
    book.admit("c1", _c("a"))
    book.admit("c2", _c("b"))
    assert book.claims_for("c1") == _c("a")


def test_expired_lists_without_removing():
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a", exp=100))
    book.admit("c2", _c("b", exp=200))
    assert book.expired(now=150) == ["c1"]
    assert len(book) == 2


def test_release_returns_claims_once():
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a"))
    assert book.release("c1") == _c("a")
    assert book.release("c1") is None
