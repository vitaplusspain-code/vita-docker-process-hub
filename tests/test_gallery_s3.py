from __future__ import annotations

from typing import Any

import pytest

from vitahub.gallery import s3


class FakeClient:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


def test_s3_list_paginates_with_continuation_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient([
        {"Contents": [{"Key": "hubs/h/faces/a"}], "IsTruncated": True, "NextContinuationToken": "t1"},
        {"Contents": [{"Key": "hubs/h/faces/b"}], "IsTruncated": False},
    ])
    monkeypatch.setattr(s3, "_client", lambda region, creds: client)
    keys = s3.s3_list("bucket", "eu-west-1", object())("hubs/h/faces/")  # type: ignore[arg-type]
    assert keys == frozenset({"hubs/h/faces/a", "hubs/h/faces/b"})
    assert client.calls == [
        {"Bucket": "bucket", "Prefix": "hubs/h/faces/"},
        {"Bucket": "bucket", "Prefix": "hubs/h/faces/", "ContinuationToken": "t1"},
    ]


def test_s3_list_requires_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient([])
    monkeypatch.setattr(s3, "_client", lambda region, creds: client)
    with pytest.raises(ValueError):
        s3.s3_list("bucket", "eu-west-1", object())("hubs/h/faces")  # type: ignore[arg-type]
    assert client.calls == []
