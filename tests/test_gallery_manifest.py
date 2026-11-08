import pytest

from vitahub.gallery.manifest import Manifest, ManifestError, parse_manifest

PID = "0b9f3c2e-4d1a-4f6b-8c7d-123456789abc"


def test_parse_manifest_ok():
    m = parse_manifest(b'{"version": 5, "persons": {"' + b"a" * 32 + b'": ["' + PID.encode() + b'"]}}')
    assert m == Manifest(version=5, persons={"a" * 32: (PID,)})


def test_parse_manifest_empty_persons():
    assert parse_manifest(b'{"version": 1, "persons": {}}').persons == {}


def test_parse_manifest_dedups_keeping_order():
    other = "1b9f3c2e-4d1a-4f6b-8c7d-123456789abc"
    raw = ('{"version": 1, "persons": {"' + "a" * 32 + f'": ["{PID}", "{other}", "{PID}"]}}}}').encode()
    assert parse_manifest(raw).persons == {"a" * 32: (PID, other)}


@pytest.mark.parametrize("raw", [
    b'{"version": 1, "persons": {"../etc": []}}',
    b'{"version": 1, "persons": {"' + b"a" * 32 + b'": ["../x"]}}',
    b'{"version": 1, "persons": {"' + b"A" * 32 + b'": []}}',
])
def test_parse_manifest_rejects_path_traversal(raw):
    with pytest.raises(ManifestError):
        parse_manifest(raw)


@pytest.mark.parametrize("raw", [
    b"{",
    b"\xff",
    b"[]",
    b'{"version": true, "persons": {}}',
    b'{"version": -1, "persons": {}}',
    b'{"version": 1, "persons": []}',
    b'{"version": 1, "persons": {"' + b"a" * 32 + b'": "x"}}',
])
def test_parse_manifest_rejects_bad_shape(raw):
    with pytest.raises(ManifestError):
        parse_manifest(raw)
