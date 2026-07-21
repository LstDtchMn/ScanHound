"""Behavior tests for RSS candidate actions and pinned feed transport."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.hdencode_action_service import (
    HDEncodeActionError,
    HDEncodeActionService,
)


class Coordinator:
    def __init__(self):
        self.priorities = []
    def prioritize(self, priority):
        owner = self
        class Context:
            def __enter__(self): owner.priorities.append(priority)
            def __exit__(self, *_args): return False
        return Context()


class Db:
    def __init__(self, candidate=None, downloaded=False):
        self.candidate = candidate or {
            "canonical_url": "https://hdencode.org/example/",
            "title": "Example",
            "clean_title": "Example",
            "media_type": "movie",
            "description_year": 2026,
            "title_year": 2026,
            "season": None,
            "resolution": "2160p",
            "size_text": "40 GB",
            "dv_evidence": "asserted",
            "hdr_evidence": "asserted",
            "hdr_formats": '["HDR10+"]',
            "identity_state": "exact",
            "hydration_state": "completed",
            "relevance_state": "relevant_missing",
            "description_complete": 1,
            "action_state": "none",
            "raw_hash": "abc",
            "discovery_source": "rss",
        }
        self.downloaded = downloaded
        self.actions = {}
        self.transitions = []
        self.cancel = False
    def recover_hdencode_actions(self): return {}
    def get_hdencode_candidate(self, _url): return dict(self.candidate)
    def get_hdencode_candidate_context(self, **_kwargs):
        return {"exact_url_downloaded": self.downloaded, "plex_matches": []}
    def create_hdencode_action(self, **kwargs):
        row = {**kwargs, "state": "queued", "created": True,
               "title": self.candidate["title"],
               "resolution": self.candidate["resolution"]}
        self.actions[kwargs["action_uuid"]] = row
        return row
    def claim_hdencode_action(self, action_uuid):
        row = self.actions[action_uuid]
        row["state"] = "retrieving_links"
        return dict(row)
    def get_hdencode_action(self, action_uuid): return dict(self.actions[action_uuid])
    def hdencode_action_cancel_requested(self, _uuid): return self.cancel
    def cancel_hdencode_action(self, uuid, *, reason):
        self.actions[uuid]["state"] = "cancelled"; self.transitions.append(reason)
    def fail_hdencode_action(self, uuid, *, error_code, correlation_id=None):
        self.actions[uuid]["state"] = "failed"; self.transitions.append(error_code)
    def mark_hdencode_action_needs_review(self, uuid, *, error_code, correlation_id=None):
        self.actions[uuid]["state"] = "needs_review"; self.transitions.append(error_code)
    def mark_hdencode_action_links_ready(self, uuid, *, links):
        self.actions[uuid]["state"] = "links_ready"; self.actions[uuid]["links"] = links; return True
    def mark_hdencode_action_submitting(self, uuid):
        self.actions[uuid]["state"] = "submitting"; return True
    def complete_hdencode_action_submitted(self, uuid):
        self.actions[uuid]["state"] = "submitted"; return True
    def record_scraped_links(self, *_args): self.transitions.append("mapped")
    def get_hdencode_rss_readiness(self, **_kwargs): return {"ready": True}
    def list_hdencode_candidates(self, **_kwargs): return [dict(self.candidate)]


class Download:
    def __init__(self): self.scrapes = 0; self.submits = 0; self.history = []
    def scrape_links(self, *_args): self.scrapes += 1; return ["https://rapidgator.net/file/1"]
    def send_to_jdownloader(self, *_args): self.submits += 1; return True
    def save_to_history(self, *args, **kwargs): self.history.append((args, kwargs)); return True


def service(config=None, *, candidate=None, downloaded=False):
    db = Db(candidate, downloaded)
    download = Download()
    instance = HDEncodeActionService(config or {"hdencode_enabled": True}, db, download)
    instance.coordinator = Coordinator()
    return instance, db, download


def test_queue_persists_before_network_and_explicit_priority_is_100():
    action, db, download = service()
    queued = action.queue_action(
        db.candidate["canonical_url"], action_kind="retrieve_links",
        requested_by="explicit", idempotency_key="key",
    )
    assert queued["state"] == "queued"
    assert download.scrapes == 0
    result = action.run_action(queued["action_uuid"], owns_lifespan=lambda: True)
    assert result["state"] == "links_ready"
    assert action.coordinator.priorities == [100]
    assert download.submits == 0


def test_explicit_grab_retrieves_maps_submits_and_records_history():
    action, db, download = service()
    queued = action.queue_action(
        db.candidate["canonical_url"], action_kind="grab",
        requested_by="explicit", idempotency_key="grab",
    )
    result = action.run_action(queued["action_uuid"], owns_lifespan=lambda: True)
    assert result["state"] == "submitted"
    assert download.scrapes == 1 and download.submits == 1
    assert download.history


def test_default_config_cannot_auto_grab():
    action, db, _download = service({"hdencode_enabled": True})
    with pytest.raises(HDEncodeActionError, match="disabled"):
        action.queue_action(
            db.candidate["canonical_url"], action_kind="grab",
            requested_by="auto",
        )


def test_unknown_video_evidence_cannot_auto_grab():
    candidate = Db().candidate
    candidate["dv_evidence"] = "unknown"
    action, db, _download = service({
        "hdencode_enabled": True,
        "hdencode_rss_auto_grab_enabled": True,
    }, candidate=candidate)
    with pytest.raises(HDEncodeActionError, match="Unknown"):
        action.queue_action(
            db.candidate["canonical_url"], action_kind="grab",
            requested_by="auto",
        )


def test_duplicate_source_history_is_refused_before_network():
    action, db, download = service(downloaded=True)
    with pytest.raises(HDEncodeActionError, match="already"):
        action.queue_action(
            db.candidate["canonical_url"], action_kind="grab",
            requested_by="explicit",
        )
    assert download.scrapes == 0


def test_stale_lifespan_never_retrieves_or_submits():
    action, db, download = service()
    queued = action.queue_action(
        db.candidate["canonical_url"], action_kind="grab",
        requested_by="explicit", idempotency_key="stale",
    )
    result = action.run_action(queued["action_uuid"], owns_lifespan=lambda: False)
    assert result["state"] == "cancelled"
    assert download.scrapes == 0 and download.submits == 0
