"""Audit pass 2 — what a scan run reports when it ends, and what it records.

Two confirmed findings, both silent:

* #26 ``ScannerService.run_scan`` swallows every exception from the async scan
      and returns normally. background_scanner therefore records the failed
      source as error-free, ``_last_crawl_early_stopped`` keeps the PREVIOUS
      run's value, ``purge_safe`` stays True — and the background cache is
      purged against a scan that refreshed nothing. Repeat for
      ``background_scan_retain_days`` and the whole catalogue is gone.
* #20 ``save_scan_history`` had no production caller, so scan_history stayed
      empty and Analytics reported 0 scans / 0 items forever — the same reading
      it would give if scanning were genuinely broken.

Note on the shape of the #26 fix: run_scan still does not re-raise, on purpose.
background_scanner skips its own purge guard (``if not err:``) once a source
reports an error, so raising would restore the very purge being prevented.
The fix is the recorded state instead — hence the post-condition tests below.

Every fix is paired with a POSITIVE CONTROL: a fix that just marks everything
incomplete (retention disabled forever) or refuses to record anything
(Analytics still reads 0) must fail this suite.
"""
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.analytics import StatsDashboard
from backend.background_scanner import BackgroundScanner
from backend.database import DatabaseManager
from backend.scanner_service import ScannerService


# ── a real ScannerService, cut down to what run_scan touches ──────────

def _real_scanner(async_body, *, prior_early_stopped=False,
                  prior_error="stale error from a previous run"):
    """A real ScannerService with only run_scan's dependencies wired up.

    Built with __new__ rather than __init__ so the test does not need Plex,
    scrapers, a DB or a config; ``_run_scan_async`` is replaced by the body the
    test wants. The point is that the REAL run_scan executes — its
    post-conditions are what background_scanner reads.
    """
    s = ScannerService.__new__(ScannerService)
    s._stop_event = threading.Event()
    s._scanning_lock = threading.Lock()
    s._is_scanning = False
    s._items_lock = threading.Lock()
    s._scan_slot = threading.Lock()
    s.items = []
    s._item_counter = 0
    s._last_crawl_seen_urls = set()
    s._last_crawl_request_count = 0
    s._last_crawl_early_stopped = prior_early_stopped
    s.last_scan_error = prior_error
    s._log_fn = None
    s._progress_fn = None
    s.matching = SimpleNamespace(app=SimpleNamespace(download_history=set()))
    s._load_download_history = lambda: set()
    s._run_scan_async = async_body
    # Not part of run_scan; the background scanner calls it after the sources
    # loop and it is irrelevant to what is under test here.
    s.rematch_cache = lambda: 0
    s.scrapers = SimpleNamespace(_detail=None)
    return s


def _raising_body(message="Plex load failed: [Errno -3] Temporary failure "
                          "in name resolution"):
    async def _body(*_args, **_kwargs):
        raise RuntimeError(message)
    return _body


def _completed_crawl_body(seen=("https://hdencode.org/new/",)):
    """Stands in for a crawl that ran to the end of the listing.

    ``_crawl_pages`` is the only thing allowed to lower
    ``_last_crawl_early_stopped``, and it does so at its very end — this body
    mimics exactly that, so a full crawl still reports itself complete.
    ``.bind["s"]`` is the scanner it writes to; the caller sets it once the
    shell exists (the body has to be built first to construct the shell).
    """
    bound = {}

    async def _body(*_args, **_kwargs):
        scanner = bound["s"]
        scanner._last_crawl_seen_urls = set(seen)
        scanner._last_crawl_early_stopped = False
        return None

    _body.bind = bound
    return _body


def _never_crawled_body():
    """Stands in for _run_scan_async's early returns (no sources selected,
    HDEncode disabled, stop requested before the crawl) — the crawl never runs,
    so nothing lowers the flag and nothing fills the seen-set."""
    async def _body(*_args, **_kwargs):
        return None
    return _body


# ── #26, layer 1 — run_scan's post-conditions ─────────────────────────

class TestRunScanRecordsItsOutcome:
    def test_failed_scan_marks_the_crawl_incomplete_and_records_the_error(self):
        """The stale-flag case that actually deletes the catalogue: the
        previous cycle crawled cleanly (False), this one dies inside the async
        scan, and the caller reads the leftover False as "safe to purge"."""
        scanner = _real_scanner(_raising_body(), prior_early_stopped=False)

        items = scanner.run_scan(scan_type="Deep Scan", source_type="HDEncode")

        assert items == []                       # returned, did not raise
        assert scanner._last_crawl_early_stopped is True
        assert "name resolution" in scanner.last_scan_error
        assert scanner.is_scanning is False

    def test_completed_crawl_is_still_reported_complete(self):
        """POSITIVE CONTROL. A crawl that reached the end of the listing must
        still report itself complete and clear the previous run's error — a fix
        that simply pins the flag True disables the background purge forever
        (the cache then grows without bound) and would pass every failure test
        in this class.
        """
        body = _completed_crawl_body()
        scanner = _real_scanner(body, prior_early_stopped=True)
        body.bind["s"] = scanner

        scanner.run_scan(scan_type="Deep Scan", source_type="HDEncode")

        assert scanner._last_crawl_early_stopped is False
        assert scanner.last_scan_error is None
        assert scanner._last_crawl_seen_urls == {"https://hdencode.org/new/"}

    def test_run_that_never_reached_the_crawl_is_not_reported_complete(self):
        """DISAGREEING CASE for "reset the flag to False, then set it True in
        the except handler".

        _run_scan_async returns early in several places without crawling (no
        sources selected, HDEncode disabled in Settings, stop requested). No
        exception is raised, so an except-handler-only fix leaves the flag
        False and the caller purges against a crawl that never happened. This
        run must be treated as incomplete: the seen-set is empty, so nothing
        refreshed last_seen for any cached row.
        """
        scanner = _real_scanner(_never_crawled_body(), prior_early_stopped=False)

        scanner.run_scan(scan_type="Deep Scan", source_type="HDEncode")

        assert scanner._last_crawl_early_stopped is True
        assert scanner._last_crawl_seen_urls == set()

    def test_stale_error_does_not_leak_into_the_next_run(self):
        """DISAGREEING CASE for "record the error and never clear it".

        last_scan_error gates the API route's completion path, so an error left
        over from a failed run would make every later successful scan report
        itself failed — no notification, no auto-grab, no history row.
        """
        body = _completed_crawl_body()
        scanner = _real_scanner(body, prior_error="boom from three scans ago")
        body.bind["s"] = scanner

        scanner.run_scan(scan_type="Deep Scan", source_type="HDEncode")

        assert scanner.last_scan_error is None


# ── #26, layer 2 — the consumer: does the cache survive? ──────────────

class _BgRegistry:
    def __init__(self, config, scanner, db):
        self.config = config
        self.scanner = scanner
        self.db = db
        self.backend = SimpleNamespace(save_config=lambda: None)
        self.lifespan_generation = 1

    def owns_lifespan(self, generation):
        return generation == self.lifespan_generation


def _bg_config():
    return {
        "background_scan_enabled": True,
        "background_scan_sources": ["HDEncode"],
        "background_scan_pages": 3,
        "background_scan_retain_days": 7,
        "hdencode_enabled": True,
        # "listing" (the default) keeps the RSS paths out of this test — the
        # rss_primary/rss_shadow branches have their own purge guards.
        "hdencode_discovery_mode": "listing",
    }


def _seed_aged_row(db, url="https://hdencode.org/aged/"):
    db.upsert_background_cache([{
        "url": url, "title": "Aged", "year": 2020, "status": "missing",
        "source_category": "HDEncode", "data": "{}",
    }])
    db._mutate(
        "UPDATE background_scan_cache SET last_seen_at = "
        "datetime('now','-30 days') WHERE url = ?", (url,))


class TestFailedScanDoesNotPurgeTheCache:
    def test_scan_that_died_inside_run_scan_leaves_the_cache_alone(
            self, tmp_path):
        """End to end through the real BackgroundScanner and real DB. The scan
        raised internally, so run_scan swallowed it and ``err`` is None here —
        the crawl-completeness flag is the only thing standing between a failed
        scan and the deletion of every row older than retain_days."""
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        _seed_aged_row(db)
        # prior False on purpose: this is the leftover from the last healthy
        # cycle that used to be read as "this crawl was complete".
        scanner = _real_scanner(_raising_body(), prior_early_stopped=False)

        BackgroundScanner(
            _BgRegistry(_bg_config(), scanner, db)).scan_once()

        assert db.count_background_cache() == 1
        db.close()

    def test_completed_crawl_still_purges_rows_the_site_no_longer_lists(
            self, tmp_path):
        """POSITIVE CONTROL. Retention must still work after a real crawl — a
        fix that blocks the purge unconditionally passes the test above and
        silently lets the cache grow forever.

        The seen-set deliberately excludes the aged URL: if it were touched the
        row would survive for the wrong reason and prove nothing.
        """
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        _seed_aged_row(db)
        body = _completed_crawl_body()
        scanner = _real_scanner(body, prior_early_stopped=True)
        body.bind["s"] = scanner

        BackgroundScanner(
            _BgRegistry(_bg_config(), scanner, db)).scan_once()

        assert db.count_background_cache() == 0
        db.close()


# ── route doubles (#20 and the route half of #26) ─────────────────────

class _Item:
    """Minimal MediaItem stand-in — only __dict__ attrs are serialized."""

    def __init__(self, url, title="A Movie", status="missing", year=2024):
        self.url = url
        self.title = title
        self.status = status
        self.year = year


class _WS:
    def __init__(self):
        self.sent = []

    def broadcast_sync(self, payload):
        self.sent.append(payload)

    def types(self):
        return [p.get("type") for p in self.sent]

    def data_for(self, kind):
        for payload in self.sent:
            if payload.get("type") == kind:
                return payload.get("data")
        return None


class _Notifications:
    def __init__(self):
        self.calls = []

    def notify_scan_complete(self, total, missing, upgrades):
        self.calls.append((total, missing, upgrades))


class _AutoGrab:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = []

    def process_items(self, items):
        self.calls.append(list(items))
        return SimpleNamespace(grabbed=0, failed=0, evaluated=len(items))


class _RouteScanner:
    """ScannerService stand-in for the route's _run_scan."""

    def __init__(self, items, *, stop_scan_flag=False, last_scan_error=None):
        self._items = items
        self.stop_scan_flag = stop_scan_flag
        self.last_scan_error = last_scan_error
        self._slot = threading.Lock()

    def try_acquire_scan(self):
        return self._slot.acquire(blocking=False)

    def release_scan(self):
        try:
            self._slot.release()
        except RuntimeError:
            pass

    @property
    def scan_in_progress(self):
        return self._slot.locked()

    def set_progress_callback(self, fn):
        pass

    def set_log_callback(self, fn):
        pass

    def run_scan(self, **kwargs):
        return list(self._items)


class _RouteRegistry:
    def __init__(self, scanner, db, *, auto_grab=None, notifications=None):
        self.scanner = scanner
        self.db = db
        self.auto_grab = auto_grab
        self.notifications = notifications
        self.config = {}
        self.backend = SimpleNamespace(save_config=lambda: None)


@pytest.fixture
def route_env(monkeypatch):
    """Patch the route's ws_manager and reset its module-global scan state."""
    from backend.api.routes import scanner as scanner_route

    ws = _WS()
    monkeypatch.setattr(scanner_route, "ws_manager", ws)
    with scanner_route._scan_lock:
        scanner_route._scan_state.update({
            "state": "idle", "progress": 0.0, "phase": "",
            "scanned": 0, "total": 0, "holds_slot": False,
        })
    yield ws, scanner_route
    with scanner_route._scan_lock:
        scanner_route._scan_state.update({
            "state": "idle", "progress": 0.0, "phase": "",
            "holds_slot": False,
        })


def _three_items():
    # 2 missing + 1 in_library — distinct counts so a fix that reports
    # len(items) for every field, or swaps two of them, disagrees.
    return [
        _Item("https://x/a", status="missing"),
        _Item("https://x/b", status="missing"),
        _Item("https://x/c", status="in_library"),
    ]


# ── #20 — the scan must reach the Analytics dashboard ─────────────────

class TestScanHistoryIsRecorded:
    def test_completed_scan_is_visible_to_the_analytics_dashboard(
            self, tmp_path, route_env):
        """Verified at the CONSUMER, not at save_scan_history: the numbers the
        Analytics page actually renders come from StatsDashboard.get_scan_stats,
        so that is what has to stop reading 0."""
        _ws, route = route_env
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        reg = _RouteRegistry(_RouteScanner(_three_items()), db)

        route._run_scan(reg, route.ScanRequest(type="deep", source="HDEncode"))

        stats = StatsDashboard(db_manager=db).get_scan_stats(days=30)
        assert stats.total_scans == 1
        assert stats.total_items_scanned == 3
        assert stats.total_missing_found == 2
        assert stats.last_scan_time is not None
        db.close()

    def test_analytics_reads_zero_when_no_scan_ran(self, tmp_path):
        """Control for the assertion above: a fresh DB really does read 0, so
        total_scans == 1 there proves the row came from the scan."""
        db = DatabaseManager(str(tmp_path / "crawler.db"))

        stats = StatsDashboard(db_manager=db).get_scan_stats(days=30)

        assert stats.total_scans == 0
        assert stats.total_items_scanned == 0
        db.close()

    def test_recorded_row_carries_this_run_s_type_and_source(
            self, tmp_path, route_env):
        """DISAGREEING CASE for a hardcoded payload. save_scan_history defaults
        scan_type to 'Full Scan' when the caller omits it, and the deep/HDEncode
        test above cannot tell a real value from a constant."""
        _ws, route = route_env
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        reg = _RouteRegistry(_RouteScanner(_three_items()), db)

        route._run_scan(
            reg, route.ScanRequest(type="incremental", source="ddlbase"))

        row = db.get_scan_history()[0]
        assert row["scan_type"] == "Incremental"
        assert row["sources_scanned"] == "DDLBase"   # normalized, not "ddlbase"
        assert row["items_scanned"] == 3
        assert row["missing_count"] == 2
        assert row["in_library_count"] == 1
        assert row["duration_seconds"] >= 0
        db.close()

    def test_timestamp_is_in_the_format_analytics_queries_with(
            self, tmp_path, route_env):
        """The timestamp format is load-bearing, and a wrong one fails silently
        — the row is written, and the dashboard still reads 0. analytics.py
        compares it as a STRING against datetime.now().isoformat(), parses it
        with fromisoformat(), and groups on SQLite date(timestamp). An epoch
        float or a 'YYYY-MM-DD HH:MM:SS' string passes the write and fails all
        three reads.
        """
        _ws, route = route_env
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        reg = _RouteRegistry(_RouteScanner(_three_items()), db)

        route._run_scan(reg, route.ScanRequest(type="deep", source="HDEncode"))

        stamp = db.get_scan_history()[0]["timestamp"]
        assert isinstance(stamp, str)
        parsed = datetime.fromisoformat(stamp)          # analytics.py line 327
        assert abs((datetime.now() - parsed).total_seconds()) < 300
        assert stamp > (datetime.now() - timedelta(days=1)).isoformat()

        trends = StatsDashboard(db_manager=db).get_trend_data(days=30)
        assert trends["dates"] == [datetime.now().strftime("%Y-%m-%d")]
        assert trends["items_scanned"] == [3]
        db.close()

    def test_a_history_write_failure_does_not_break_the_scan(
            self, tmp_path, route_env):
        """Analytics is a reporting nicety; the scan is the product. A DB error
        while recording must not cost the operator the completion, the
        notification or the auto-grab."""
        ws, route = route_env

        class _ExplodingDB:
            def save_scan_history(self, _data):
                raise RuntimeError("database is locked")

        notifications = _Notifications()
        reg = _RouteRegistry(_RouteScanner(_three_items()), _ExplodingDB(),
                             notifications=notifications)

        route._run_scan(reg, route.ScanRequest(type="deep", source="HDEncode"))

        assert "scan:complete" in ws.types()
        assert notifications.calls == [(3, 2, 0)]
        assert "last_scan_time" in reg.config


# ── #26 (route half) + #20 — a failed run is not a completed one ──────

class TestFailedOrCancelledScanIsNotRecorded:
    def test_failed_scan_reports_an_error_and_records_no_history(
            self, tmp_path, route_env):
        """run_scan swallowed the exception, so this thread sees a normal
        return with partial items. Without the recorded error it would
        broadcast scan:complete, stamp last_scan_time, notify, auto-grab
        unmatched items and write a scan_history row claiming a clean run."""
        ws, route = route_env
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        auto_grab = _AutoGrab()
        notifications = _Notifications()
        reg = _RouteRegistry(
            _RouteScanner(_three_items(), last_scan_error="Plex load failed"),
            db, auto_grab=auto_grab, notifications=notifications)

        route._run_scan(reg, route.ScanRequest(type="deep", source="HDEncode"))

        assert "scan:complete" not in ws.types()
        assert "scan:error" in ws.types()
        assert "Plex load failed" in ws.data_for("scan:error")["message"]
        assert db.get_scan_history() == []
        assert notifications.calls == []
        assert auto_grab.calls == []
        assert "last_scan_time" not in reg.config
        # The partial results are still published — the operator should see
        # what was found, they just must not be treated as a finished scan.
        assert "scan:result" in ws.types()
        db.close()

    def test_cancelled_scan_records_no_history(self, tmp_path, route_env):
        """A stopped scan never reached _match_against_plex, so its counts are
        wrong in a specific direction: everything reads MISSING. Recording it
        would put a fabricated missing-count spike in the trend chart."""
        ws, route = route_env
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        reg = _RouteRegistry(
            _RouteScanner(_three_items(), stop_scan_flag=True), db)

        route._run_scan(reg, route.ScanRequest(type="deep", source="HDEncode"))

        assert "scan:cancelled" in ws.types()
        assert db.get_scan_history() == []
        assert StatsDashboard(db_manager=db).get_scan_stats(
            days=30).total_scans == 0
        db.close()

    def test_mock_style_error_attribute_is_not_read_as_a_failure(
            self, tmp_path, route_env):
        """DISAGREEING CASE for a bare truth test on the attribute.

        A Mock scanner auto-creates a truthy attribute for every name, so
        `if scanner.last_scan_error:` would turn every stub-driven scan into a
        failure — no completion, no history, no auto-grab. Only a genuine
        non-empty string counts (same rule _scan_was_cancelled uses for bools).
        """
        ws, route = route_env
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        scanner = _RouteScanner(_three_items())
        scanner.last_scan_error = object()          # truthy, but not a str
        reg = _RouteRegistry(scanner, db)

        route._run_scan(reg, route.ScanRequest(type="deep", source="HDEncode"))

        assert "scan:complete" in ws.types()
        assert len(db.get_scan_history()) == 1
        db.close()

    def test_empty_error_string_is_not_a_failure(self, tmp_path, route_env):
        """DISAGREEING CASE for `isinstance(err, str)` without a truth check.
        A cleared error is the empty-ish state, not a failed scan."""
        ws, route = route_env
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        reg = _RouteRegistry(
            _RouteScanner(_three_items(), last_scan_error=""), db)

        route._run_scan(reg, route.ScanRequest(type="deep", source="HDEncode"))

        assert "scan:complete" in ws.types()
        assert len(db.get_scan_history()) == 1
        db.close()
