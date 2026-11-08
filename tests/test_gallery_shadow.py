import json

import pytest

from vitahub.gallery.shadow import (
    ShadowSnapshot,
    identity_only_payload,
    parse_delta_version,
    parse_get_accepted,
    reported_payload,
    shadow_topics,
)


def test_reported_payload_nulls_removed_photos():
    body = json.loads(reported_payload(
        7, True, {"p1": ("accepted", None), "p2": ("rejected", "no_face")}, frozenset({"p1", "old"})
    ))
    assert body == {"state": {"reported": {"version": 7, "identity_enabled": True, "photos": {
        "p1": {"status": "accepted"}, "p2": {"status": "rejected", "reason": "no_face"}, "old": None}}}}


def test_identity_only_payload():
    assert json.loads(identity_only_payload(False)) == {"state": {"reported": {"identity_enabled": False}}}


def test_shadow_topics():
    t = shadow_topics("hub-1")
    assert t.update_delta == "$aws/things/hub-1/shadow/name/face-gallery/update/delta"
    assert t.get == "$aws/things/hub-1/shadow/name/face-gallery/get"
    assert t.get_accepted.endswith("/get/accepted")
    assert t.get_rejected.endswith("/get/rejected")
    assert t.update == "$aws/things/hub-1/shadow/name/face-gallery/update"
    assert t.update_rejected.endswith("/update/rejected")


def test_get_accepted_desired_only():
    raw = json.dumps({"state": {"desired": {"version": 10}}, "version": 3}).encode()
    assert parse_get_accepted(raw) == ShadowSnapshot(10, None, None, frozenset())


def test_get_accepted_both():
    raw = json.dumps({"state": {
        "desired": {"version": 10},
        "reported": {"version": 9, "identity_enabled": True, "photos": {"p1": {"status": "accepted"}}},
        "delta": {"version": 10},
    }}).encode()
    assert parse_get_accepted(raw) == ShadowSnapshot(10, 9, True, frozenset({"p1"}))


def test_get_accepted_empty_state():
    assert parse_get_accepted(b'{"state": {}}') == ShadowSnapshot(None, None, None, frozenset())


def test_get_accepted_non_int_versions():
    raw = b'{"state": {"desired": {"version": true}, "reported": {"version": "9", "identity_enabled": 1}}}'
    assert parse_get_accepted(raw) == ShadowSnapshot(None, None, None, frozenset())


def test_get_accepted_malformed_raises():
    with pytest.raises(ValueError):
        parse_get_accepted(b"{")


@pytest.mark.parametrize("raw,expected", [
    (b'{"state": {"version": 12}}', 12),
    (b'{"state": {}}', None),
    (b'{"state": {"version": true}}', None),
    (b'{"state": {"version": "1"}}', None),
    (b"{}", None),
])
def test_parse_delta_version(raw, expected):
    assert parse_delta_version(raw) == expected
