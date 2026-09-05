"""HDE-4: source-global HDEncode reveal ACCOUNTING.

HARD SCOPE RULE (peer-agreed): this feature is bookkeeping only. It adds one
append-only table (`hdencode_reveal_observations`), one write site
(`DownloadService.scrape_links_recorded`, the single production entry point
established by HDE-3 -- see tests/test_hde3_one_reveal_one_observation.py),
and one advisory read surface (`GET /sources`). It must NEVER add a limit,
refusal, cooldown, throttle, or warning threshold driven by a reveal count.
The negative-control test at the bottom of this file
(`test_25_success_reveals_today_are_not_limited_in_any_way`) exists
specifically to prove that.

These tests reuse `_stubbed_service`, `HDENCODE_URL`, `_candidate` and
`_run_retrieve_links_action` from test_hde3_one_reveal_one_observation.py:
a real DownloadService/HDEncodeActionService with only the browser-facing
`scrape_links()` stubbed, so every consumer under test calls real,
production `scrape_links_recorded()` / `download_item()` code.
"""
from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks

from backend.api.routes.downloads import (
    DownloadRequest,
    ScrapeBatchRequest,
    ScrapeRequest,
    _run_grab,
    copy_links_batch,
    scrape_links as scrape_links_route,
)
from backend.api.routes import sources as sources_route
from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService
from backend.download_service import DownloadService
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic, ScrapedLinks
from backend.source_health import classify_reveal_outcome

from tests.test_hde3_one_reveal_one_observation import (
    HDENCODE_URL,
    _arm_hold,
    _candidate,
    _hold,
    _run_retrieve_links_action,
    _stubbed_service,
)

DIRECT_URL = "https://rapidgator.net/some-direct-file"


def _rows(db, **where):
    sql = "SELECT * FROM hdencode_reveal_observations"
    params = ()
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
        params = tuple(where.values())
    sql += " ORDER BY id ASC"
    return db._query_dicts(sql, params, default=[])


def _seed_row(db, source, outcome, caller, context_id, url_hash, diagnostic_code,
              recorded_at):
    ok = db._mutate(
        "INSERT INTO hdencode_reveal_observations "
        "(source, outcome, caller, context_id, url_hash, diagnostic_code, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, outcome, caller, context_id, url_hash, diagnostic_code, recorded_at),
        label="test_seed_row",
    )
    assert ok, "test setup failed to seed a reveal observation row"


# ─────────────────────────────────────────────────────────────────────────────
# classify_reveal_outcome — pure function
# ─────────────────────────────────────────────────────────────────────────────

def test_classify_success_for_nonempty_links():
    links = ScrapedLinks(["https://rapidgator.net/f1"])
    assert classify_reveal_outcome(links) == ("success", None)


@pytest.mark.parametrize("code", [
    ScrapeCode.INTERACTIVE_CHALLENGE,
    ScrapeCode.REVEAL_VERIFICATION_STALLED,
])
def test_classify_challenge_codes(code):
    diagnostic = ScrapeDiagnostic(code, health_owner="coordinator")
    links = ScrapedLinks(diagnostic=diagnostic)
    assert classify_reveal_outcome(links) == ("challenge", code.value)


def test_classify_challenge_by_health_owner_alone():
    """health_owner == 'coordinator' alone used to be read as 'challenge'
    regardless of code, but SOURCE_DISABLED never sent a request at all --
    effective_transport_attempted is False for it, so the refusal check
    (checked first) must win: this is 'refused', not 'challenge'."""
    diagnostic = ScrapeDiagnostic(ScrapeCode.SOURCE_DISABLED, health_owner="coordinator")
    links = ScrapedLinks(diagnostic=diagnostic)
    assert classify_reveal_outcome(links) == ("refused", ScrapeCode.SOURCE_DISABLED.value)


@pytest.mark.parametrize("code", [
    ScrapeCode.NO_FILE_HOST_LINKS,
    ScrapeCode.LAYOUT_CHANGED,
    ScrapeCode.REVEAL_CONTROL_ABSENT,
])
def test_classify_stripped_codes(code):
    diagnostic = ScrapeDiagnostic(code)
    links = ScrapedLinks(diagnostic=diagnostic)
    assert classify_reveal_outcome(links) == ("stripped", code.value)


def test_classify_served_other_host_for_requested_host_missing():
    """REQUESTED_HOST_MISSING means the page DID serve links, just not for
    the requested file host -- it is not the 'nothing came back' shape that
    'stripped' names, so it gets its own bucket."""
    diagnostic = ScrapeDiagnostic(ScrapeCode.REQUESTED_HOST_MISSING)
    links = ScrapedLinks(diagnostic=diagnostic)
    assert classify_reveal_outcome(links) == (
        "served_other_host", ScrapeCode.REQUESTED_HOST_MISSING.value)


@pytest.mark.parametrize("code", [
    ScrapeCode.SOURCE_DISABLED,
    ScrapeCode.SOURCE_TEMPORARILY_BLOCKED,
])
def test_classify_refused_local_denials(code):
    diagnostic = ScrapeDiagnostic(code)
    links = ScrapedLinks(diagnostic=diagnostic)
    assert classify_reveal_outcome(links) == ("refused", code.value)


def test_classify_refused_browser_launch_failed_if_declared():
    """BROWSER_LAUNCH_FAILED never leaves the machine either -- confirm it
    exists on ScrapeCode and classifies the same way, only if present."""
    assert hasattr(ScrapeCode, "BROWSER_LAUNCH_FAILED"), (
        "test setup: BROWSER_LAUNCH_FAILED must exist on ScrapeCode for this "
        "test to mean anything")
    diagnostic = ScrapeDiagnostic(ScrapeCode.BROWSER_LAUNCH_FAILED)
    links = ScrapedLinks(diagnostic=diagnostic)
    assert classify_reveal_outcome(links) == (
        "refused", ScrapeCode.BROWSER_LAUNCH_FAILED.value)


def test_classify_error_scrape_exception():
    diagnostic = ScrapeDiagnostic(ScrapeCode.SCRAPE_EXCEPTION)
    links = ScrapedLinks(diagnostic=diagnostic)
    assert classify_reveal_outcome(links) == ("error", ScrapeCode.SCRAPE_EXCEPTION.value)


def test_classify_error_when_no_diagnostic_at_all():
    links = ScrapedLinks()
    assert classify_reveal_outcome(links) == ("error", None)


# ─────────────────────────────────────────────────────────────────────────────
# One row per reveal, at the boundary, for every outcome
# ─────────────────────────────────────────────────────────────────────────────

def test_one_row_written_for_success(tmp_path):
    db = DatabaseManager(str(tmp_path / "row-success.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "success"
        assert rows[0]["diagnostic_code"] is None
    finally:
        db.close()


def test_one_row_written_for_challenge(tmp_path):
    diagnostic = ScrapeDiagnostic(ScrapeCode.INTERACTIVE_CHALLENGE, health_owner="coordinator")
    db = DatabaseManager(str(tmp_path / "row-challenge.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(diagnostic=diagnostic))
        svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "challenge"
        assert rows[0]["diagnostic_code"] == "interactive_challenge"
    finally:
        db.close()


def test_one_row_written_for_stripped(tmp_path):
    diagnostic = ScrapeDiagnostic(ScrapeCode.NO_FILE_HOST_LINKS)
    db = DatabaseManager(str(tmp_path / "row-stripped.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(diagnostic=diagnostic))
        svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "stripped"
        assert rows[0]["diagnostic_code"] == "no_file_host_links"
    finally:
        db.close()


def test_one_row_written_for_error_by_diagnostic(tmp_path):
    diagnostic = ScrapeDiagnostic(ScrapeCode.SCRAPE_EXCEPTION)
    db = DatabaseManager(str(tmp_path / "row-error.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(diagnostic=diagnostic))
        svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "error"
        assert rows[0]["diagnostic_code"] == "scrape_exception"
    finally:
        db.close()


@pytest.mark.parametrize("code", [
    ScrapeCode.SOURCE_DISABLED,
    ScrapeCode.SOURCE_TEMPORARILY_BLOCKED,
])
def test_one_row_written_refused_end_to_end_for_local_denials(tmp_path, code):
    """A local denial -- source disabled, or the coordinator's own temporary
    block -- never sends a request. Driven end to end through
    scrape_links_recorded (not just the pure classifier), this must record
    as 'refused', never 'challenge': lumping it with a real interactive
    challenge would count a refusal that never touched the site as if the
    site itself had resisted."""
    diagnostic = ScrapeDiagnostic(code, health_owner="coordinator")
    db = DatabaseManager(str(tmp_path / f"refused-{code.value}.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(diagnostic=diagnostic))
        svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "refused"
        assert rows[0]["diagnostic_code"] == code.value
    finally:
        db.close()


def test_one_row_written_and_exception_still_propagates(tmp_path):
    """When scrape_links() itself raises, one 'error'/'exception' row is
    written AND the exception still propagates out of scrape_links_recorded
    -- accounting must never swallow a real failure."""
    db = DatabaseManager(str(tmp_path / "row-raise.db"))
    try:
        svc = DownloadService(config={"hdencode_enabled": True}, db=db, server_mode=True)
        svc.scrape_links = MagicMock(side_effect=RuntimeError("browser exploded"))
        with pytest.raises(RuntimeError):
            svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "error"
        assert rows[0]["diagnostic_code"] == "exception"
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# The caller is recorded, per call site
# ─────────────────────────────────────────────────────────────────────────────

def test_caller_recorded_route_scrape(tmp_path):
    db = DatabaseManager(str(tmp_path / "caller-route-scrape.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        reg = SimpleNamespace(download=svc, db=db)
        req = ScrapeRequest(url=HDENCODE_URL, service_type="Rapidgator")
        scrape_links_route(req, reg)
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["caller"] == "route_scrape"
    finally:
        db.close()


def test_caller_recorded_route_copy_links(tmp_path):
    db = DatabaseManager(str(tmp_path / "caller-copy-links.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        svc.copy_to_clipboard = MagicMock(return_value=True)
        reg = SimpleNamespace(download=svc, db=db)
        req = ScrapeBatchRequest(items=[ScrapeRequest(url=HDENCODE_URL, service_type="Rapidgator")])
        background = BackgroundTasks()
        copy_links_batch(req, background, reg)
        for task in background.tasks:
            task.func(*task.args, **task.kwargs)
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["caller"] == "route_copy_links"
    finally:
        db.close()


def test_caller_recorded_rss_action_carries_action_uuid_as_context(tmp_path):
    db = DatabaseManager(str(tmp_path / "caller-rss.db"))
    try:
        _candidate(db)
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        result = _run_retrieve_links_action(db, svc, "k-hde4-rss")
        assert result["state"] == "links_ready"
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["caller"] == "rss_action"
        assert rows[0]["context_id"] == result["action_uuid"]
    finally:
        db.close()


def test_caller_recorded_qt_batch(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from PySide6.QtCore import QCoreApplication
    from ui.controllers.download_controller import ScrapeAndCopyWorker

    QCoreApplication.instance() or QCoreApplication([])

    db = DatabaseManager(str(tmp_path / "caller-qt-batch.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        svc.copy_to_clipboard = MagicMock(return_value=True)
        item = SimpleNamespace(url=HDENCODE_URL, host_pref="RG", title="Example")
        worker = ScrapeAndCopyWorker(svc, [item], save_history_fn=lambda *a, **k: None)
        worker.run()
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["caller"] == "qt_batch"
    finally:
        db.close()


def test_caller_recorded_route_download(tmp_path, monkeypatch):
    from backend.api.routes import downloads as downloads_route
    monkeypatch.setattr(downloads_route.ws_manager, "broadcast_sync", lambda *a, **k: None)

    db = DatabaseManager(str(tmp_path / "caller-route-download.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        reg = SimpleNamespace(download_queue=None)
        req = DownloadRequest(url=HDENCODE_URL, title="Example Title",
                              resolution="2160p", size="40 GB")
        _run_grab(svc, reg, req, False)
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["caller"] == "route_download"
    finally:
        db.close()


def test_caller_recorded_auto_grab(tmp_path):
    from backend.auto_grab_service import AutoGrabService
    from backend.scanner_service import MediaItem, ScanStatus

    db = DatabaseManager(str(tmp_path / "caller-auto-grab.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        auto_grab = AutoGrabService({"auto_grab_enabled": True}, svc)
        item = MediaItem(
            id="i1", title="Example", year=2026, status=ScanStatus.MISSING,
            rating=7.5, votes=50000, genres=["Action"], language="en",
            url=HDENCODE_URL, resolution="2160p", size="40 GB",
        )
        auto_grab.process_items([item])
        # Whether the downstream JDownloader/clipboard hand-off succeeds in
        # this headless test environment is irrelevant to accounting: the
        # reveal itself ran, and that is what must be recorded.
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["caller"] == "auto_grab"
    finally:
        db.close()


def _queue_item(index: int) -> dict:
    return {
        "url": f"https://hdencode.org/release/{index}",
        "title": f"Title {index}",
        "year": 2026,
        "season": None,
        "resolution": "2160p",
        "size": "40 GB",
        "hdr": "",
        "dovi": False,
        "service_type": "Rapidgator",
    }


def test_caller_recorded_queue_item_carries_a_context_id(tmp_path):
    """Drives the worker exactly like tests/test_queue_records_category.py:
    the row the real producer (schedule_batch) wrote, then _execute_inner
    directly, asserting what the queue actually PASSED to download_item --
    the queue's own state (item_uuid/attempt tracking) is covered elsewhere."""
    db = DatabaseManager(str(tmp_path / "caller-queue.db"))
    try:
        svc = DownloadQueueService({}, db, MagicMock(), poll_seconds=0.01)
        batch = svc.schedule_batch([_queue_item(1)], interval_minutes=0)
        item = svc.get_item(batch["items"][0]["item_uuid"])

        svc.download = MagicMock()
        svc.download.download_item = MagicMock(
            return_value={"success": True, "method": "jdownloader"}
        )
        try:
            svc._execute_inner(item, "attempt-hde4-1")
        except Exception:
            # Only the outgoing call is under test; downstream bookkeeping
            # against a mocked download service is covered elsewhere.
            pass

        assert svc.download.download_item.called
        kwargs = svc.download.download_item.call_args.kwargs
        assert kwargs.get("caller") == "queue_item"
        assert kwargs.get("context_id") == "attempt-hde4-1"
    finally:
        db.close()


def test_caller_recorded_qt_manual(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from PySide6.QtCore import QCoreApplication
    from ui.controllers.download_controller import DownloadItemWorker

    QCoreApplication.instance() or QCoreApplication([])

    db = DatabaseManager(str(tmp_path / "caller-qt-manual.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        item = SimpleNamespace(url=HDENCODE_URL, title="Example", season=None,
                               resolution="2160p", size="40 GB")
        worker = DownloadItemWorker(svc, item, "Rapidgator")
        worker.run()
        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["caller"] == "qt_manual"
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Non-HDEncode traffic writes nothing
# ─────────────────────────────────────────────────────────────────────────────

def test_non_hdencode_url_writes_no_row(tmp_path):
    db = DatabaseManager(str(tmp_path / "non-hdencode.db"))
    try:
        svc = DownloadService(config={"hdencode_enabled": True}, db=db, server_mode=True)
        svc.scrape_links = MagicMock(return_value=ScrapedLinks(["https://rapidgator.net/f1"]))
        assert svc.owns_source_health(DIRECT_URL, "hdencode") is False, (
            "test setup: this URL must NOT classify as hdencode, or the "
            "assertion below is vacuous")
        svc.scrape_links_recorded(DIRECT_URL, "Rapidgator", caller="t")
        assert _rows(db) == []
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# get_reveal_accounting / list_reveal_days — UTC day bucketing
# ─────────────────────────────────────────────────────────────────────────────

def test_get_reveal_accounting_buckets_by_utc_day_and_list_reveal_days_orders_newest_first(tmp_path):
    db = DatabaseManager(str(tmp_path / "days.db"))
    try:
        _seed_row(db, "hdencode", "success", "route_scrape", None, "hash1", None,
                  "2026-09-01T10:00:00+00:00")
        _seed_row(db, "hdencode", "success", "route_scrape", None, "hash2", None,
                  "2026-09-01T11:00:00+00:00")
        _seed_row(db, "hdencode", "challenge", "rss_action", "a1", "hash3",
                  "interactive_challenge", "2026-09-01T12:00:00+00:00")
        _seed_row(db, "hdencode", "success", "qt_batch", None, "hash4", None,
                  "2026-09-02T09:00:00+00:00")

        day1 = db.get_reveal_accounting("hdencode", day="2026-09-01")
        assert day1["source"] == "hdencode"
        assert day1["day"] == "2026-09-01"
        assert day1["total"] == 3
        assert day1["by_outcome"] == {"success": 2, "challenge": 1}
        assert day1["by_caller"] == {"route_scrape": 2, "rss_action": 1}
        assert day1["first_at"] == "2026-09-01T10:00:00+00:00"
        assert day1["last_at"] == "2026-09-01T12:00:00+00:00"
        assert len(day1["recent"]) == 3
        assert day1["recent"][0]["recorded_at"] == "2026-09-01T12:00:00+00:00", (
            "recent must be newest first")

        day2 = db.get_reveal_accounting("hdencode", day="2026-09-02")
        assert day2["total"] == 1
        assert day2["by_outcome"] == {"success": 1}

        empty_day = db.get_reveal_accounting("hdencode", day="2026-08-30")
        assert empty_day["total"] == 0
        assert empty_day["by_outcome"] == {}
        assert empty_day["first_at"] is None
        assert empty_day["last_at"] is None

        trend = db.list_reveal_days("hdencode", limit=14)
        days_in_order = [d["day"] for d in trend]
        assert days_in_order[:2] == ["2026-09-02", "2026-09-01"], (
            "list_reveal_days must order newest day first")
        by_day = {d["day"]: d for d in trend}
        assert by_day["2026-09-01"]["total"] == 3
        assert by_day["2026-09-01"]["by_outcome"] == {"success": 2, "challenge": 1}
        assert by_day["2026-09-02"]["total"] == 1
    finally:
        db.close()


def test_get_reveal_accounting_and_list_reveal_days_break_out_by_diagnostic_code(tmp_path):
    """Two 'stripped' codes (NO_FILE_HOST_LINKS and LAYOUT_CHANGED) must be
    reported separately under by_diagnostic_code, not merged just because
    they share an outcome bucket."""
    db = DatabaseManager(str(tmp_path / "by-code.db"))
    try:
        _seed_row(db, "hdencode", "stripped", "route_scrape", None, "hash1",
                  "no_file_host_links", "2026-09-03T10:00:00+00:00")
        _seed_row(db, "hdencode", "stripped", "route_scrape", None, "hash2",
                  "no_file_host_links", "2026-09-03T11:00:00+00:00")
        _seed_row(db, "hdencode", "stripped", "route_scrape", None, "hash3",
                  "layout_changed", "2026-09-03T12:00:00+00:00")
        _seed_row(db, "hdencode", "success", "route_scrape", None, "hash4",
                  None, "2026-09-03T13:00:00+00:00")

        day = db.get_reveal_accounting("hdencode", day="2026-09-03")
        assert day["by_diagnostic_code"] == {
            "no_file_host_links": 2, "layout_changed": 1, "none": 1,
        }

        trend = db.list_reveal_days("hdencode", limit=14)
        by_day = {d["day"]: d for d in trend}
        assert by_day["2026-09-03"]["by_diagnostic_code"] == {
            "no_file_host_links": 2, "layout_changed": 1, "none": 1,
        }
    finally:
        db.close()


def test_list_reveal_days_survives_a_null_day(tmp_path):
    """A NULL day (a malformed recorded_at that date() cannot parse) must not
    raise out of the sort -- key=lambda e: e['day'] or ''."""
    db = DatabaseManager(str(tmp_path / "null-day.db"))
    try:
        _seed_row(db, "hdencode", "success", "route_scrape", None, "hash1",
                  None, "not-a-real-timestamp")
        _seed_row(db, "hdencode", "success", "route_scrape", None, "hash2",
                  None, "2026-09-03T13:00:00+00:00")
        trend = db.list_reveal_days("hdencode", limit=14)  # must not raise
        assert any(d["day"] == "2026-09-03" for d in trend)
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# The raw URL is never stored
# ─────────────────────────────────────────────────────────────────────────────

def test_raw_url_never_stored_only_its_sha256_hash(tmp_path):
    db = DatabaseManager(str(tmp_path / "urlhash.db"))
    try:
        url = "https://hdencode.org/some-very-specific-release-title-2160p/"
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        svc.scrape_links_recorded(url, "Rapidgator", caller="t")

        rows = _rows(db, source="hdencode")
        assert len(rows) == 1
        assert rows[0]["url_hash"] == hashlib.sha256(url.encode("utf-8")).hexdigest()

        dump = db._query_dicts("SELECT * FROM hdencode_reveal_observations", default=[])
        assert url not in str(dump), "the raw URL must never appear in a stored row"
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Fail-soft: a DB write failure never raises into the scrape path
# ─────────────────────────────────────────────────────────────────────────────

def test_db_write_failure_is_fail_soft_and_scrape_still_returns_links(tmp_path):
    db = DatabaseManager(str(tmp_path / "failsoft.db"))
    try:
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        db.close()
        # Simulate a connection that cannot be (re)opened -- the same failure
        # mode _mutate/_query already treat as fail-soft everywhere else.
        db.get_connection = lambda: None

        result = svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")
        assert list(result) == ["https://rapidgator.net/f1"]

        # And the DB method itself, called directly, must not raise either.
        db.record_reveal_observation("hdencode", "success", "t", HDENCODE_URL)

        # Second channel, and the one that pins the method's OWN handler: an
        # unexpected exception below the method (not the "no connection" case
        # that _mutate already swallows). A first version of this test only
        # used get_connection -> None and did NOT fail when the method's
        # except clause was made to re-raise. This half does.
        def _boom(*_a, **_k):
            raise RuntimeError("simulated unexpected DB failure")

        db._mutate = _boom
        result = svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")
        assert list(result) == ["https://rapidgator.net/f1"]
        db.record_reveal_observation("hdencode", "success", "t", HDENCODE_URL)
    finally:
        pass


def test_reveal_accounting_write_failure_never_skips_health_or_hold_release(tmp_path, monkeypatch):
    """The classify_reveal_outcome + record_reveal_observation pair is wrapped
    in its own try/except in scrape_links_recorded. Even if
    record_reveal_observation itself raises (bypassing its own internal
    fail-soft handling), that must never skip record_scrape_outcome (source
    health) or the verification-hold release that follow it."""
    import backend.download_service as download_service_module

    db = DatabaseManager(str(tmp_path / "guard.db"))
    try:
        batch = "batch-guard-1"
        _arm_hold(db, batch, "hdencode")
        assert _hold(db, batch) == "hdencode"

        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))

        def _boom(*_a, **_k):
            raise RuntimeError("simulated accounting failure")

        monkeypatch.setattr(db, "record_reveal_observation", _boom)

        outcome_calls = []
        orig_record_scrape_outcome = download_service_module.record_scrape_outcome

        def _spy_record_scrape_outcome(*a, **k):
            outcome_calls.append((a, k))
            return orig_record_scrape_outcome(*a, **k)

        monkeypatch.setattr(
            download_service_module, "record_scrape_outcome", _spy_record_scrape_outcome
        )

        result = svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")

        assert list(result) == ["https://rapidgator.net/f1"], (
            "links must still be returned even though accounting raised")
        assert len(outcome_calls) == 1, (
            "record_scrape_outcome must still run when accounting write raises")
        assert _hold(db, batch) is None, (
            "the verification hold must still be released when accounting write raises")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# GET /sources surfaces reveal_accounting / reveal_days for hdencode only
# ─────────────────────────────────────────────────────────────────────────────

def test_sources_route_carries_reveal_accounting_for_hdencode_only(tmp_path):
    db = DatabaseManager(str(tmp_path / "sources-ok.db"))
    try:
        db.record_reveal_observation("hdencode", "success", "route_scrape", HDENCODE_URL)
        reg = SimpleNamespace(config={}, db=db)

        rows = sources_route.list_sources(reg)

        hd = next(r for r in rows if r["name"] == "hdencode")
        assert hd["reveal_accounting"]["total"] == 1
        assert isinstance(hd["reveal_days"], list)

        others = [r for r in rows if r["name"] != "hdencode"]
        assert others, "test setup: there must be a non-hdencode source to contrast with"
        for r in others:
            assert "reveal_accounting" not in r
            assert "reveal_days" not in r
    finally:
        db.close()


def test_sources_route_survives_a_reveal_accounting_db_error(tmp_path):
    db = DatabaseManager(str(tmp_path / "sources-error.db"))
    try:
        db.get_reveal_accounting = MagicMock(side_effect=RuntimeError("db exploded"))
        db.list_reveal_days = MagicMock(side_effect=RuntimeError("db exploded"))
        reg = SimpleNamespace(config={}, db=db)

        rows = sources_route.list_sources(reg)  # must not raise -> 200 in the real route

        hd = next(r for r in rows if r["name"] == "hdencode")
        assert hd["reveal_accounting"] is None
        assert hd["reveal_days"] is None
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# HDE4-R1: "0" and "unavailable" must never collapse into the same result
# ─────────────────────────────────────────────────────────────────────────────

def test_accounting_reads_report_unavailable_not_zero_when_the_database_is_down(tmp_path):
    """A dead database must surface as None ("unavailable"), never as a
    legitimate-looking zeroed result. Same idiom as the fail-soft write test
    above: close the db, then make get_connection() return None so every
    fresh connection attempt fails, the same failure mode _query/_mutate
    already treat as fail-soft everywhere else."""
    db = DatabaseManager(str(tmp_path / "down.db"))
    db.close()
    db.get_connection = lambda: None

    assert db.get_reveal_accounting("hdencode") is None
    assert db.list_reveal_days("hdencode") is None

    reg = SimpleNamespace(config={}, db=db)
    rows = sources_route.list_sources(reg)  # must not raise -> 200 in the real route
    hd = next(r for r in rows if r["name"] == "hdencode")
    assert hd["reveal_accounting"] is None
    assert hd["reveal_days"] is None


def test_healthy_database_with_no_observations_returns_a_real_zero(tmp_path):
    """The flip side of the test above: a database that works fine but has
    no rows yet is a real, legitimate zero -- not unavailable."""
    db = DatabaseManager(str(tmp_path / "empty.db"))
    try:
        accounting = db.get_reveal_accounting("hdencode")
        assert accounting is not None
        assert accounting["total"] == 0
        assert accounting["by_outcome"] == {}
        assert accounting["by_caller"] == {}
        assert accounting["by_diagnostic_code"] == {}
        assert accounting["first_at"] is None
        assert accounting["last_at"] is None
        assert accounting["recent"] == []

        days = db.list_reveal_days("hdencode")
        assert days == []

        reg = SimpleNamespace(config={}, db=db)
        rows = sources_route.list_sources(reg)
        hd = next(r for r in rows if r["name"] == "hdencode")
        assert hd["reveal_accounting"] is not None
        assert hd["reveal_accounting"]["total"] == 0
        assert hd["reveal_days"] == []
    finally:
        db.close()


def test_accounting_reports_unavailable_on_a_query_error_other_than_no_connection(tmp_path):
    """A query failure that is NOT "no connection" -- a real sqlite error
    while a connection exists -- must also report None, not an empty/zeroed
    result. Drives this through a fake connection/cursor rather than
    monkeypatching _query_dicts_strict itself (that helper is the thing
    under test)."""
    db = DatabaseManager(str(tmp_path / "query-error.db"))
    try:
        real_conn = db.get_connection()
        assert real_conn is not None, "test setup: must have a real connection"

        class _BoomCursor:
            def execute(self, *_a, **_k):
                raise sqlite3.OperationalError("simulated query failure")

        class _BoomConn:
            def cursor(self):
                return _BoomCursor()

        db.get_connection = lambda: _BoomConn()

        assert db.get_reveal_accounting("hdencode") is None
        assert db.list_reveal_days("hdencode") is None
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# NEGATIVE POLICY TEST (load-bearing): no limit exists, at any count
# ─────────────────────────────────────────────────────────────────────────────

def test_25_success_reveals_today_are_not_limited_in_any_way(tmp_path):
    """HARD SCOPE RULE proof. 25 recorded successes today must not stop,
    slow, deny, or flag the 26th reveal -- scrape_links() still runs, its
    links still come back, and nothing in the coordinator's own snapshot is
    touched by the count. A prior version of this test asserted against two
    invented key names ("reveal_accounting_total", "reveal_count") that
    would never have existed in this dict regardless of whether a limit was
    added -- a limit could show up as a changed `blocked`, `state`,
    `reason_code`, `cooldown_until`, `block_streak`, or a new metrics entry,
    none of which those two names would catch. Comparing the WHOLE snapshot
    before and after covers all of those in one assertion. A verification
    hold armed for hdencode before the 26th reveal must also still behave
    normally: a successful reveal releases it, exactly as it would after
    reveal #1, proving the 25 prior rows changed nothing about that either.
    If a limit is ever added at >= 20 (or any threshold), this test must
    fail."""
    from backend.hdencode_coordinator import get_hdencode_coordinator

    db = DatabaseManager(str(tmp_path / "no-limit.db"))
    try:
        for i in range(25):
            db.record_reveal_observation(
                "hdencode", "success", "route_scrape",
                f"https://hdencode.org/release-{i}/",
            )
        accounting = db.get_reveal_accounting("hdencode")
        assert accounting["total"] == 25, "test setup: 25 rows must actually exist"

        batch = "batch-no-limit-1"
        _arm_hold(db, batch, "hdencode")
        assert _hold(db, batch) == "hdencode"

        # Construct the service BEFORE either snapshot: DownloadService.__init__
        # calls configure_hdencode_coordinator(config, db), which repoints the
        # coordinator singleton's own db reference (see
        # scanhound-coordinator-configure-reset-hazard). Taking snap_before
        # ahead of that call would compare against a stale db from whichever
        # earlier test last configured the coordinator -- a false difference
        # that has nothing to do with this test's 26 reveals.
        svc = _stubbed_service(db, ScrapedLinks(["https://rapidgator.net/f1"]))
        coordinator = get_hdencode_coordinator()
        snap_before = coordinator.snapshot()

        result = svc.scrape_links_recorded(HDENCODE_URL, "Rapidgator", caller="t")

        assert svc.scrape_links.called, (
            "the 26th reveal must still actually run -- accounting volume "
            "must never short-circuit the real scrape")
        assert list(result) == ["https://rapidgator.net/f1"]

        after = db.get_reveal_accounting("hdencode")
        assert after["total"] == 26

        health = db.get_source_health("hdencode")
        assert health is not None and health.get("state") == "healthy", (
            "a run of successes must leave the source healthy, not blocked "
            "or cooled down by volume")

        assert _hold(db, batch) is None, (
            "a successful reveal must release an armed verification hold "
            "the normal way -- volume must not have disabled that behavior")

        snap_after = coordinator.snapshot()
        assert snap_after == snap_before, (
            "26 reveals recorded, all through source-health/accounting "
            "bookkeeping, must leave the coordinator's own snapshot "
            "byte-for-byte unchanged -- any difference is a limit, "
            "throttle, or warning threshold this feature must never add")
    finally:
        db.close()
