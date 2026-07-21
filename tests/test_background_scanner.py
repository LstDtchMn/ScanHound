"""Tests for the background pre-cache scanner service and its DB methods.

The real scraper hits external sites, so ``run_scan`` is faked here — these
tests cover the DB upsert/purge logic, that each configured source is scanned
once with the right page count, and that the full item is persisted as JSON.
"""
import json
import threading
import pytest

from backend.database import DatabaseManager
from backend.background_scanner import BackgroundScanner


@pytest.fixture(autouse=True)
def _reset_cache():
    def _clear():
        try:
            dm = DatabaseManager()
            dm.clear_background_cache()
            dm.close()
        except Exception:
            pass
    _clear()
    yield
    _clear()


@pytest.fixture
def db():
    dm = DatabaseManager()
    yield dm
    dm.close()


class _FakeMediaItem:
    def __init__(self, url, title, status="missing", year=2024):
        self.url = url
        self.title = title
        self.status = status
        self.year = year


class _FakeScanner:
    """Records run_scan calls and returns canned items per source. Mirrors the
    real ScannerService's scan-slot API so the background scanner's mutual-
    exclusion guard can be exercised."""

    def __init__(self, items_by_source=None):
        self.calls = []
        self._items = items_by_source or {}
        self._slot = threading.Lock()
        self._last_crawl_seen_urls = set()

    def run_scan(self, scan_type, source_type, pages, resolution_flags=None,
                 search_query="", track_urls=True, skip_urls=None, early_stop=False):
        self.calls.append({
            "source": source_type, "pages": pages, "scan_type": scan_type,
            "track_urls": track_urls, "skip_urls": skip_urls, "early_stop": early_stop,
        })
        return self._items.get(source_type, [])

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


class _FakeBackend:
    def save_config(self):
        pass


class _FakeRegistry:
    def __init__(self, config, scanner, db):
        self.config = config
        self._scanner_service = scanner
        self.db = db
        self.backend = _FakeBackend()
        self._lifespan_generation = 1

    @property
    def scanner(self):
        return self._scanner_service

    @property
    def lifespan_generation(self):
        return self._lifespan_generation

    def owns_lifespan(self, generation):
        return generation == self._lifespan_generation

    def advance_lifespan(self):
        self._lifespan_generation += 1


# ── DB layer ──────────────────────────────────────────────────────────

class TestBackgroundCacheDB:
    def test_upsert_and_count(self, db):
        db.upsert_background_cache([
            {"url": "u1", "title": "A", "year": 2024, "status": "missing",
             "source_category": "HDEncode", "data": json.dumps({"url": "u1"})},
            {"url": "u2", "title": "B", "year": 2023, "status": "upgrade",
             "source_category": "HDEncode", "data": json.dumps({"url": "u2"})},
        ])
        assert db.count_background_cache() == 2
        assert {r["url"] for r in db.get_background_cache()} == {"u1", "u2"}

    def test_upsert_is_idempotent_by_url(self, db):
        db.upsert_background_cache([{"url": "u1", "title": "A", "year": 2024,
            "status": "missing", "source_category": "HDEncode", "data": "{}"}])
        db.upsert_background_cache([{"url": "u1", "title": "A v2", "year": 2024,
            "status": "library", "source_category": "HDEncode", "data": "{}"}])
        rows = db.get_background_cache()
        assert len(rows) == 1
        assert rows[0]["title"] == "A v2"
        assert rows[0]["status"] == "library"

    def test_upsert_preserves_first_seen_category(self, db):
        """Once a URL is tagged with a category, a second scan for a different
        category must not overwrite it — so a 4K post seen first by the 4k scan
        doesn't get relabeled 'remux' if the remux scan later also picks it up."""
        db.upsert_background_cache([{"url": "u1", "title": "A", "year": 2024,
            "status": "missing", "source_category": "4k", "data": "{}"}])
        db.upsert_background_cache([{"url": "u1", "title": "A", "year": 2024,
            "status": "missing", "source_category": "remux", "data": "{}"}])
        rows = db.get_background_cache()
        assert rows[0]["source_category"] == "4k"

    def test_upsert_sets_category_when_empty(self, db):
        """A row with no category gets categorised by the first scan that sees it."""
        db.upsert_background_cache([{"url": "u1", "title": "A", "year": 2024,
            "status": "missing", "source_category": "", "data": "{}"}])
        db.upsert_background_cache([{"url": "u1", "title": "A", "year": 2024,
            "status": "missing", "source_category": "4k", "data": "{}"}])
        rows = db.get_background_cache()
        assert rows[0]["source_category"] == "4k"

    def test_purge_removes_old_rows(self, db):
        db.upsert_background_cache([{"url": "old", "title": "Old", "year": 2020,
            "status": "missing", "source_category": "HDEncode", "data": "{}"}])
        db._mutate("UPDATE background_scan_cache SET last_seen_at = "
                   "datetime('now','-30 days') WHERE url = 'old'")
        db.purge_background_cache(7)
        assert db.count_background_cache() == 0

    def test_purge_keeps_recent_rows(self, db):
        db.upsert_background_cache([{"url": "fresh", "title": "Fresh", "year": 2024,
            "status": "missing", "source_category": "HDEncode", "data": "{}"}])
        db.purge_background_cache(7)
        assert db.count_background_cache() == 1

    def _seed_aged(self, db):
        db.upsert_background_cache([{"url": "old", "title": "Old", "year": 2020,
            "status": "missing", "source_category": "HDEncode", "data": "{}"}])
        db._mutate("UPDATE background_scan_cache SET last_seen_at = "
                   "datetime('now','-30 days') WHERE url = 'old'")

    def test_scan_once_skips_purge_when_source_early_stopped(self, db):
        """An early-stopped crawl never visited deeper pages, so the run must NOT
        purge still-listed (but un-revisited) rows (review fix #6)."""
        self._seed_aged(db)
        scanner = _FakeScanner()
        scanner._last_crawl_early_stopped = True
        BackgroundScanner(_FakeRegistry(
            {"background_scan_sources": ["HDEncode"], "background_scan_pages": 1,
             "background_scan_retain_days": 7}, scanner, db)).scan_once()
        assert db.count_background_cache() == 1  # preserved, not aged out

    def test_scan_once_purges_after_full_crawl(self, db):
        """A full (non-early-stopped) crawl still purges aged rows."""
        self._seed_aged(db)
        scanner = _FakeScanner()
        scanner._last_crawl_early_stopped = False
        BackgroundScanner(_FakeRegistry(
            {"background_scan_sources": ["HDEncode"], "background_scan_pages": 1,
             "background_scan_retain_days": 7}, scanner, db)).scan_once()
        assert db.count_background_cache() == 0  # aged row purged

    def test_get_cache_urls(self, db):
        db.upsert_background_cache([
            {"url": "u1", "title": "A", "year": 2024, "status": "missing",
             "source_category": "HDEncode", "data": "{}"},
            {"url": "u2", "title": "B", "year": 2024, "status": "missing",
             "source_category": "HDEncode", "data": "{}"},
        ])
        assert db.get_background_cache_urls() == {"u1", "u2"}

    def test_touch_keeps_still_listed_rows_from_purge(self, db):
        """An aged row that is touched (still listed) must survive a purge — this
        is what stops skipped-but-still-present items being evicted."""
        db.upsert_background_cache([{"url": "u1", "title": "A", "year": 2024,
            "status": "missing", "source_category": "HDEncode", "data": "{}"}])
        db._mutate("UPDATE background_scan_cache SET last_seen_at = "
                   "datetime('now','-30 days') WHERE url = 'u1'")
        db.touch_background_cache(["u1"])
        db.purge_background_cache(7)
        assert db.count_background_cache() == 1


# ── Service ───────────────────────────────────────────────────────────

class TestBackgroundScannerService:
    def test_stale_lifespan_skips_without_touching_scanner(self, db):
        scanner = _FakeScanner({"HDEncode": [_FakeMediaItem("h1", "H1")]})
        reg = _FakeRegistry(
            {"background_scan_sources": ["HDEncode"], "background_scan_pages": 1},
            scanner,
            db,
        )
        background = BackgroundScanner(reg)
        reg.advance_lifespan()

        result = background.scan_once()

        assert result["skipped"] is True
        assert result["reason"] == "stale_lifespan"
        assert scanner.calls == []

    def test_scan_once_scans_each_source_with_page_count(self, db):
        scanner = _FakeScanner({
            "HDEncode": [_FakeMediaItem("h1", "H1")],
            "DDLBase": [_FakeMediaItem("d1", "D1"), _FakeMediaItem("d2", "D2")],
        })
        config = {
            "background_scan_sources": ["HDEncode", "DDLBase"],
            "background_scan_pages": 2,
            "background_scan_retain_days": 7,
        }
        result = BackgroundScanner(_FakeRegistry(config, scanner, db)).scan_once()
        assert len(scanner.calls) == 2  # one run_scan per source
        assert {c["source"] for c in scanner.calls} == {"HDEncode", "DDLBase"}
        assert all(c["pages"] == 2 for c in scanner.calls)
        assert result["scanned"] == 3
        assert db.count_background_cache() == 3

    def test_scan_once_skips_disabled_hdencode_and_preserves_cache(self, db):
        db.upsert_background_cache([{
            "url": "old-hdencode", "title": "Old", "year": 2020,
            "status": "missing", "source_category": "HDEncode", "data": "{}",
        }])
        db._mutate(
            "UPDATE background_scan_cache SET last_seen_at = "
            "datetime('now','-30 days') WHERE url = 'old-hdencode'"
        )
        scanner = _FakeScanner({
            "HDEncode": [_FakeMediaItem("must-not-be-read", "Must Not Be Read")]
        })
        bs = BackgroundScanner(_FakeRegistry({
            "background_scan_sources": ["HDEncode"],
            "background_scan_pages": 3,
            "background_scan_retain_days": 7,
            "hdencode_enabled": False,
        }, scanner, db))

        result = bs.scan_once()

        assert scanner.calls == []
        assert result["scanned"] == 0
        assert db.count_background_cache() == 1
        assert bs.last_run["sources"] == [{
            "source": "HDEncode", "new": 0, "error": None,
            "skipped": "disabled",
        }]

    def test_scan_once_persists_full_item_as_json(self, db):
        scanner = _FakeScanner({"HDEncode": [_FakeMediaItem("h1", "Heat", year=1995)]})
        config = {"background_scan_sources": ["HDEncode"], "background_scan_pages": 1}
        BackgroundScanner(_FakeRegistry(config, scanner, db)).scan_once()
        row = db.get_background_cache()[0]
        data = json.loads(row["data"])
        assert data["title"] == "Heat"
        assert data["url"] == "h1"
        assert row["source_category"] == "HDEncode"

    def test_scan_once_skips_without_scanner_or_db(self):
        out = BackgroundScanner(
            _FakeRegistry({"background_scan_sources": ["HDEncode"]}, None, None)).scan_once()
        assert out["skipped"] is True

    def test_scan_once_uses_precache_flags(self, db):
        """Background scans must not disturb the incremental URL baseline
        (track_urls=False) and must early-stop at the previous endpoint."""
        scanner = _FakeScanner({"HDEncode": [_FakeMediaItem("h1", "H1")]})
        config = {"background_scan_sources": ["HDEncode"], "background_scan_pages": 1}
        BackgroundScanner(_FakeRegistry(config, scanner, db)).scan_once()
        call = scanner.calls[0]
        assert call["track_urls"] is False
        assert call["early_stop"] is True
        assert call["skip_urls"] == set()  # nothing cached on the first run

    def test_scan_once_skips_already_cached_on_next_run(self, db):
        scanner = _FakeScanner({"HDEncode": [_FakeMediaItem("h1", "H1")]})
        bs = BackgroundScanner(_FakeRegistry(
            {"background_scan_sources": ["HDEncode"], "background_scan_pages": 1}, scanner, db))
        bs.scan_once()  # caches h1
        bs.scan_once()  # second pass should pass h1 as a skip URL
        assert scanner.calls[-1]["skip_urls"] == {"h1"}

    def test_scan_once_yields_to_foreground_scan(self, db):
        """If a foreground scan holds the shared slot, the background scan must
        skip rather than run concurrently on the same ScannerService."""
        scanner = _FakeScanner({"HDEncode": [_FakeMediaItem("h1", "H1")]})
        assert scanner.try_acquire_scan() is True  # simulate foreground scan
        try:
            out = BackgroundScanner(_FakeRegistry(
                {"background_scan_sources": ["HDEncode"], "background_scan_pages": 1},
                scanner, db)).scan_once()
            assert out.get("skipped") is True
            assert out.get("reason") == "busy"
            assert scanner.calls == []  # never started
        finally:
            scanner.release_scan()

    def test_scan_once_records_per_source_summary(self, db):
        scanner = _FakeScanner({"HDEncode": [_FakeMediaItem("h1", "H1")]})
        bs = BackgroundScanner(_FakeRegistry(
            {"background_scan_sources": ["HDEncode"], "background_scan_pages": 1}, scanner, db))
        bs.scan_once()
        assert bs.last_run is not None
        src = bs.last_run["sources"][0]
        assert src["source"] == "HDEncode"
        assert src["new"] == 1
        assert src["error"] is None

    def test_interval_and_next_run(self, db):
        config = {"background_scan_enabled": True, "background_scan_interval_hours": 3,
                  "background_scan_last_run": 1000.0}
        bs = BackgroundScanner(_FakeRegistry(config, _FakeScanner(), db))
        assert bs._interval_seconds() == 3 * 3600
        assert bs.next_run_at() == 1000.0 + 3 * 3600

    def test_next_run_none_when_disabled(self, db):
        bs = BackgroundScanner(
            _FakeRegistry({"background_scan_enabled": False}, _FakeScanner(), db))
        assert bs.next_run_at() is None

    def test_scan_once_is_not_reentrant(self, db):
        """A second scan_once() while one is mid-flight must skip, not run a
        concurrent pass that interleaves writes (the /scan-now-vs-scheduled
        race). The guard is a lock-protected test-and-set, not just the
        endpoint's pre-check.
        """
        started = threading.Event()
        release = threading.Event()

        class _BlockingScanner:
            def __init__(self):
                self.calls = 0
                self._slot = threading.Lock()

            def run_scan(self, *a, **k):
                self.calls += 1
                started.set()
                release.wait(timeout=5)
                return []

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

        scanner = _BlockingScanner()
        config = {"background_scan_sources": ["HDEncode"], "background_scan_pages": 1}
        bs = BackgroundScanner(_FakeRegistry(config, scanner, db))

        t = threading.Thread(target=bs.scan_once)
        t.start()
        try:
            assert started.wait(timeout=5)        # first scan is in progress
            result = bs.scan_once()               # re-entrant call
            assert result.get("skipped") is True
        finally:
            release.set()
            t.join(timeout=5)
        assert scanner.calls == 1                  # only the first pass ran


# ── Feature: configurable pre-cache category set ─────────────────────

class TestConfigurableCategories:
    def test_defaults_to_all_categories(self, db):
        bs = BackgroundScanner(_FakeRegistry({}, _FakeScanner(), db))
        flags = bs._category_flags()
        assert all(flags.values()) and set(flags) == {
            '4k', 'remux', 'tv', '4k_webdl', '4k_remux', '1080p_remux'}

    def test_subset_limits_categories(self, db):
        bs = BackgroundScanner(_FakeRegistry(
            {"background_scan_categories": ["4k"]}, _FakeScanner(), db))
        flags = bs._category_flags()
        assert flags['4k'] is True
        assert flags['tv'] is False and flags['remux'] is False

    def test_empty_subset_falls_back_to_all(self, db):
        bs = BackgroundScanner(_FakeRegistry(
            {"background_scan_categories": ["nonsense"]}, _FakeScanner(), db))
        assert all(bs._category_flags().values())  # never scan nothing
