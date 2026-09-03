"""HDE-3 (round 7b, two-reviewer spec): one HDEncode reveal, one durable
source observation, regardless of which consumer requested it.

THE DEFECT. `backend/download_service.py`'s `scrape_links()` is a real,
quota-spending HDEncode operation (a Turnstile-gated reveal). Before this
fix, each of its five production consumers decided FOR ITSELF whether to
turn that operation's outcome into a durable source-health observation:

    download_item()                       DID  (backend/download_service.py)
    POST /download/scrape                 DID  (backend/api/routes/downloads.py)
    POST /download/copy-links             DID  (backend/api/routes/downloads.py)
    hdencode_action_service.run_action()  DID NOT  (the RSS action path)
    ui/controllers/download_controller.py DID NOT  (the Qt batch scrape)

So a real reveal spent by an RSS action (POST /rss/actions, operator-facing,
no auto-grab gate — this was LIVE, not latent) vanished: its failure never
reached source health or the scraper-drift instrument, and its SUCCESS never
released a `verification_hold_source` armed for "hdencode" — an armed hold
and a source that had just proven it could reveal, at the same time.

THE FIX. `DownloadService.scrape_links_recorded()` is now the single
production entry point: it calls `scrape_links()`, then produces exactly one
`record_scrape_outcome` observation and, on success, releases the
source-wide `hdencode` verification hold via the new
`DatabaseManager.release_verification_hold_for_source()`. Every consumer
above now calls this wrapper instead of `scrape_links()` + its own
recording, so one scrape attempt can only ever produce one observation.

These tests exercise EVERY consumer, plus the negative control the reviewers
named explicitly: an HDEncode success must never clear a DIFFERENT source's
hold (closes HDE-2 — the WHERE-source-match on the release SQL was
otherwise untested).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

from backend.api.routes.downloads import (
    ScrapeBatchRequest,
    ScrapeRequest,
    copy_links_batch,
    scrape_links as scrape_links_route,
)
from backend.database import DatabaseManager
from backend.download_service import DownloadService
from backend.hdencode_action_service import HDEncodeActionService
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic, ScrapedLinks

HDENCODE_URL = "https://hdencode.org/example-2160p/"


class _NoOpCoordinator:
    """A coordinator that never throttles, denies, or cancels."""

    def prioritize(self, priority):
        class _Ctx:
            def __enter__(self_inner):
                return None

            def __exit__(self_inner, *exc):
                return False

        return _Ctx()


def _candidate(db, url=HDENCODE_URL):
    now = "2026-09-01T00:00:00+00:00"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO hdencode_candidates (
                canonical_url, guid, title, pub_date, media_type, raw_hash,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url, "guid-hde3", "Example 2026 2160p", now, "movie",
             "raw-hde3", now, now, now),
        )


def _arm_hold(db, batch_uuid, source):
    now = "2026-09-01T00:00:00+00:00"
    # Every NOT NULL column supplied deliberately (test_source_hold_surface.py's
    # lesson): an incomplete INSERT fails silently and every assertion below
    # would then pass against an empty table.
    ok = db._mutate(
        "INSERT INTO download_queue_batches "
        "(batch_uuid, mode, interval_seconds, state, total_items, completed_items, "
        " failed_items, deferred_items, auto_resume_after_cooldown, auto_resume_used, "
        " auto_resume_progress_mark, source_delivery_count, created_at, updated_at, "
        " verification_hold_source) "
        "VALUES (?, 'immediate', 0, 'paused_source', 0,0,0,0, 0,0,0,0, ?, ?, ?)",
        (batch_uuid, now, now, source),
        label="test_arm_hold",
    )
    assert ok, "test setup failed to arm the hold"


def _hold(db, batch_uuid):
    rows = db._query_dicts(
        "SELECT verification_hold_source FROM download_queue_batches "
        "WHERE batch_uuid=?", (batch_uuid,), default=[])
    return rows[0]["verification_hold_source"] if rows else None


def _spy_observations(db):
    """Count calls that write a durable source-health observation.

    `record_source_success`/`record_source_failure` are the only two writers
    of the `source_health` table (see backend/source_health.py). Wrapping
    them, rather than counting rows, catches a scrape that calls the recorder
    twice for the SAME outcome just as well as one that writes two rows.
    """
    calls = []
    orig_success = db.record_source_success
    orig_failure = db.record_source_failure

    def success(source):
        calls.append(("success", source))
        return orig_success(source)

    def failure(source, state, reason_code, **kw):
        calls.append(("failure", source))
        return orig_failure(source, state, reason_code, **kw)

    db.record_source_success = success
    db.record_source_failure = failure
    return calls


def _stubbed_service(db, outcome):
    """A DownloadService whose scrape is stubbed at the browser boundary.

    `outcome` is whatever `scrape_links()` would have returned — a
    `ScrapedLinks` of real links, or one carrying a failure diagnostic. Every
    consumer under test calls `scrape_links_recorded()`, which is real,
    production code: only the browser-facing `scrape_links()` itself is a
    stub.
    """
    svc = DownloadService(config={"hdencode_enabled": True}, db=db, server_mode=True)
    svc.scrape_links = MagicMock(return_value=outcome)
    return svc


def _run_retrieve_links_action(db, svc, key):
    action = HDEncodeActionService({"hdencode_enabled": True}, db, svc)
    action.coordinator = _NoOpCoordinator()
    queued = action.queue_action(
        HDENCODE_URL, action_kind="retrieve_links",
        requested_by="explicit", idempotency_key=key,
    )
    return action.run_action(queued["action_uuid"], owns_lifespan=lambda: True)


# ─────────────────────────────────────────────────────────────────────────────
# (1) RSS retrieve_links + an armed hold + a successful stubbed scrape
# ─────────────────────────────────────────────────────────────────────────────

def test_rss_success_makes_one_observation_and_releases_the_hold(tmp_path):
    db = DatabaseManager(str(tmp_path / "t1.db"))
    try:
        _candidate(db)
        batch = "batch-hdencode-1"
        _arm_hold(db, batch, "hdencode")
        assert _hold(db, batch) == "hdencode"

        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        calls = _spy_observations(db)

        result = _run_retrieve_links_action(db, svc, "k1")

        assert result["state"] == "links_ready"
        assert calls == [("success", "hdencode")], (
            f"expected exactly one source-health success observation, got {calls}")
        assert _hold(db, batch) is None, (
            "a successful HDEncode reveal from the RSS path must release the "
            "armed verification hold, exactly like the /download path does")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# (2) RSS classified failure -> same source-health semantics as the download path
# ─────────────────────────────────────────────────────────────────────────────

def test_rss_classified_failure_matches_the_download_paths_semantics(tmp_path):
    diagnostic = ScrapeDiagnostic(ScrapeCode.LAYOUT_CHANGED, affects_source_health=True)

    db = DatabaseManager(str(tmp_path / "t2-rss.db"))
    try:
        _candidate(db)
        svc = _stubbed_service(db, ScrapedLinks(diagnostic=diagnostic))
        calls = _spy_observations(db)

        result = _run_retrieve_links_action(db, svc, "k2")

        assert result["state"] == "failed", (
            "an empty, health-affecting scrape must fail the RSS action itself")
        assert calls == [("failure", "hdencode")]
        health = db.get_source_health("hdencode")
        assert health["state"] == "degraded"
        assert health["consecutive_failures"] == 1
    finally:
        db.close()

    # THE SAME diagnostic through the normal /download path (download_item)
    # must produce the identical source-health outcome — proving the RSS path
    # is not a special case, just another caller of the same centralized
    # observation.
    db2 = DatabaseManager(str(tmp_path / "t2-download.db"))
    try:
        svc2 = _stubbed_service(db2, ScrapedLinks(diagnostic=diagnostic))
        svc2.download_item(HDENCODE_URL, "Example", None, "2160p", "40 GB")
        health2 = db2.get_source_health("hdencode")
        assert health2["state"] == "degraded"
        assert health2["consecutive_failures"] == 1
    finally:
        db2.close()


# ─────────────────────────────────────────────────────────────────────────────
# (3) An HDEncode success must never clear a DIFFERENT source's hold (HDE-2)
# ─────────────────────────────────────────────────────────────────────────────

def test_hdencode_success_does_not_clear_a_different_sources_hold(tmp_path):
    db = DatabaseManager(str(tmp_path / "t3.db"))
    try:
        _candidate(db)
        held_batch = "batch-ddlbase-1"
        _arm_hold(db, held_batch, "ddlbase")

        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        result = _run_retrieve_links_action(db, svc, "k3")

        assert result["state"] == "links_ready"
        assert _hold(db, held_batch) == "ddlbase", (
            "an HDEncode reveal cleared a hold armed for a different source")
    finally:
        db.close()


def test_release_helper_is_source_matched_directly(tmp_path):
    """Narrower, direct pin on the SQL itself (HDE-2), independent of the
    RSS action plumbing above."""
    db = DatabaseManager(str(tmp_path / "t3-direct.db"))
    try:
        _arm_hold(db, "batch-a", "hdencode")
        _arm_hold(db, "batch-b", "ddlbase")

        released = db.release_verification_hold_for_source("hdencode")

        assert released == 1
        assert _hold(db, "batch-a") is None
        assert _hold(db, "batch-b") == "ddlbase", (
            "release_verification_hold_for_source must be source-matched, "
            "not a blanket clear")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# (4) One operation, one observation -- on EVERY consumer
# ─────────────────────────────────────────────────────────────────────────────

def test_download_item_makes_exactly_one_observation(tmp_path):
    db = DatabaseManager(str(tmp_path / "t4-item.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        calls = _spy_observations(db)

        svc.download_item(HDENCODE_URL, "Example", None, "2160p", "40 GB")

        assert len(calls) == 1, calls
    finally:
        db.close()


def test_rss_action_makes_exactly_one_observation(tmp_path):
    db = DatabaseManager(str(tmp_path / "t4-rss.db"))
    try:
        _candidate(db)
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        calls = _spy_observations(db)

        result = _run_retrieve_links_action(db, svc, "k4")

        assert result["state"] == "links_ready"
        assert len(calls) == 1, calls
    finally:
        db.close()


def test_scrape_route_makes_exactly_one_observation(tmp_path):
    db = DatabaseManager(str(tmp_path / "t4-route.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        calls = _spy_observations(db)
        reg = SimpleNamespace(download=svc, db=db)
        req = ScrapeRequest(url=HDENCODE_URL, service_type="Rapidgator")

        response = scrape_links_route(req, reg)

        assert response["count"] == 1
        assert len(calls) == 1, calls
    finally:
        db.close()


def test_copy_links_batch_route_makes_exactly_one_observation(tmp_path):
    db = DatabaseManager(str(tmp_path / "t4-batch.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        # The real OS clipboard is not available (and unsafe) in this test
        # environment; only the observation-counting behavior is under test.
        svc.copy_to_clipboard = MagicMock(return_value=True)
        calls = _spy_observations(db)
        reg = SimpleNamespace(download=svc, db=db)
        req = ScrapeBatchRequest(items=[
            ScrapeRequest(url=HDENCODE_URL, service_type="Rapidgator"),
        ])
        background = BackgroundTasks()

        copy_links_batch(req, background, reg)
        # /copy-links defers the real work to a background task; run it
        # synchronously so the test observes what it actually does.
        for task in background.tasks:
            task.func(*task.args, **task.kwargs)

        assert len(calls) == 1, calls
    finally:
        db.close()


def test_qt_batch_scrape_makes_exactly_one_observation(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from PySide6.QtCore import QCoreApplication
    from ui.controllers.download_controller import ScrapeAndCopyWorker

    QCoreApplication.instance() or QCoreApplication([])

    db = DatabaseManager(str(tmp_path / "t4-qt.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        # The real OS clipboard is not available (and unsafe) in this test
        # environment; only the observation-counting behavior is under test.
        svc.copy_to_clipboard = MagicMock(return_value=True)
        calls = _spy_observations(db)
        item = SimpleNamespace(url=HDENCODE_URL, host_pref="RG", title="Example")

        worker = ScrapeAndCopyWorker(svc, [item], save_history_fn=lambda *a, **k: None)
        worker.run()

        assert len(calls) == 1, calls
    finally:
        db.close()
