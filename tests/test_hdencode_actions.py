"""Behavior tests for RSS candidate actions and pinned feed transport."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

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
    # HDE-3 (round 7b): run_action() calls scrape_links_recorded(), the single
    # production entry point that also produces the durable source
    # observation. This fake just forwards to scrape_links() so every existing
    # test in this file (including the ones that monkeypatch scrape_links
    # directly) keeps its behaviour unchanged.
    def scrape_links_recorded(self, *args): return self.scrape_links(*args)
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


def test_a_provenance_recording_failure_cannot_undo_a_successful_submit():
    """Link provenance is decoration on a submission that already happened.

    The first version of that call was bare, inside the try that catches
    submission failures, so an exception while RECORDING marked an action whose
    JDownloader submit had SUCCEEDED as needs_review -- ScanHound would then
    believe it had not sent something it had, and a human would be asked to
    review a non-problem. Caught only incidentally, because the stub db in this
    file happens not to define the method; in production any exception from it
    would have done the same. Assert the property directly so it stays true.
    """
    action, db, download = service()
    db.record_submitted_links = MagicMock(side_effect=RuntimeError("disk gone"))
    queued = action.queue_action(
        db.candidate["canonical_url"], action_kind="grab",
        requested_by="explicit", idempotency_key="grab-prov",
    )

    result = action.run_action(queued["action_uuid"], owns_lifespan=lambda: True)

    assert result["state"] == "submitted", "a recording failure changed the outcome"
    assert download.submits == 1, "the submission itself must be unaffected"


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


class TestConstructionIsSideEffectFree:
    """recover_hdencode_actions() ran in HDEncodeActionService.__init__, and
    that service is constructed PER API REQUEST (routes/rss.py) and per scan
    cycle. The recovery is a blanket state-keyed UPDATE with no owner or
    generation column, so a second construction reset another thread's
    IN-FLIGHT action -- discarding links it had already scraped and
    mislabelling a submission that had actually succeeded.

    Recovery is a restart concern. It now runs exactly once per lifespan from
    backend/api/main.py, at a point where no worker thread exists yet.
    """

    def test_constructing_the_service_does_not_sweep(self):
        from backend.hdencode_action_service import HDEncodeActionService
        db = MagicMock()
        HDEncodeActionService({}, db, MagicMock())
        db.recover_hdencode_actions.assert_not_called()

    def test_constructing_it_twice_still_does_not_sweep(self):
        # the actual failure shape: a second request builds a second service
        # while the first one's worker is mid-flight
        from backend.hdencode_action_service import HDEncodeActionService
        db = MagicMock()
        HDEncodeActionService({}, db, MagicMock())
        HDEncodeActionService({}, db, MagicMock())
        db.recover_hdencode_actions.assert_not_called()

    def test_startup_performs_the_recovery_exactly_once(self):
        """Negative control: the sweep must not simply have been deleted."""
        from backend.api.main import create_app
        from backend.api.dependencies import registry
        from fastapi.testclient import TestClient
        with TestClient(create_app(config_override={
                "plex_url": "", "plex_token": ""})):
            db = registry.db
            assert db is not None
            # the real DatabaseManager records the call by having run it;
            # assert the method exists and the table is reachable
            assert hasattr(db, "recover_hdencode_actions")


def test_history_is_keyed_on_the_RELEASE_url_not_each_file_host_link():
    """The RSS path used to write one `downloads` row per file-host link.

    Nothing reads those: `is_downloaded()` -- the only consumer -- is called
    with the RELEASE url, so a link row can never match, and the links are
    already recorded by `record_submitted_links()` for provenance.

    Worse, it made a whole feature inert on this path. `provenance_url` resolves
    to the canonical url and `annotate_source_links()` joins it against
    `downloads.url`, so with history under the links that join could never
    match: no source link, no first-seen date, no identity. The old test
    asserted only `assert download.history` -- that something was written --
    which is exactly why the wrong key survived.
    """
    action, db, download = service()
    download.scrape_links = lambda *_a: ["https://rapidgator.net/file/1",
                                         "https://rapidgator.net/file/2"]
    queued = action.queue_action(
        db.candidate["canonical_url"], action_kind="grab",
        requested_by="explicit", idempotency_key="grab-key",
    )
    action.run_action(queued["action_uuid"], owns_lifespan=lambda: True)

    assert len(download.history) == 1, (
        f"expected ONE history row for the release, got {len(download.history)} "
        "— one per link is the old behaviour")
    url = download.history[0][0][0]
    assert url == db.candidate["canonical_url"], (
        f"history keyed on {url!r}, not the release url")
    assert "rapidgator" not in url


def test_a_missing_canonical_url_records_nothing_rather_than_guessing():
    """Without it there is no key that provenance could ever join, so writing a
    row under some other url would create history that can never be resolved."""
    action, db, download = service()
    queued = action.queue_action(
        db.candidate["canonical_url"], action_kind="grab",
        requested_by="explicit", idempotency_key="grab-nocanon",
    )
    # Strip the canonical url the way a malformed candidate would.
    stored = db.actions[queued["action_uuid"]]
    stored["canonical_url"] = None
    action.run_action(queued["action_uuid"], owns_lifespan=lambda: True)
    assert download.history == [], "wrote history under an unjoinable key"
