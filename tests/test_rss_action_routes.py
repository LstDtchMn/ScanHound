"""Closed API tests for RSS candidate actions."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.routes import rss
from backend.hdencode_action_service import HDEncodeActionError


class Service:
    def __init__(self, *_args): pass
    def queue_action(self, *_args, **_kwargs):
        return {"action_uuid": "a1", "state": "queued", "created": True}
    def run_action(self, *_args, **_kwargs): return {"state": "links_ready"}


class Db:
    def list_hdencode_actions(self, **_kwargs): return []
    def request_cancel_hdencode_action(self, action_uuid):
        return {"action_uuid": action_uuid, "state": "cancelled"}
    def retry_hdencode_action(self, action_uuid):
        return {"action_uuid": action_uuid, "state": "queued"}


def registry():
    return SimpleNamespace(
        db=Db(), download=object(), config={"hdencode_enabled": True},
        lifespan_generation=3, owns_lifespan=lambda generation: generation == 3,
        backend=None,
    )


def test_action_queue_returns_persisted_identifier(monkeypatch):
    monkeypatch.setattr(rss, "HDEncodeActionService", Service)
    monkeypatch.setattr(rss, "_start_tracked_action_thread", lambda _reg, target: None)
    result = rss.start_rss_action(
        rss.ActionRequest(
            canonical_url="https://hdencode.org/example/",
            action_kind="retrieve_links",
        ),
        registry(),
    )
    assert result == {
        "status": "queued", "action_uuid": "a1",
        "created": True, "idempotent": False,
    }


def test_expected_action_error_is_closed(monkeypatch):
    class Failing(Service):
        def queue_action(self, *_args, **_kwargs):
            raise HDEncodeActionError("already_downloaded", "Already submitted.")
    monkeypatch.setattr(rss, "HDEncodeActionService", Failing)
    with pytest.raises(HTTPException) as caught:
        rss.start_rss_action(
            rss.ActionRequest(
                canonical_url="https://hdencode.org/example/",
                action_kind="grab",
            ),
            registry(),
        )
    assert caught.value.detail == {
        "code": "already_downloaded", "message": "Already submitted."
    }
