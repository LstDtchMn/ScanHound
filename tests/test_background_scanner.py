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
    """Records run_scan calls and returns canned items per source."""

    def __init__(self, items_by_source=None):
        self.calls = []
        self._items = items_by_source or {}

    def run_scan(self, scan_type, source_type, pages, resolution_flags=None, search_query=""):
        self.calls.append({"source": source_type, "pages": pages, "scan_type": scan_type})
        return self._items.get(source_type, [])


class _FakeBackend:
    def save_config(self):
        pass


class _FakeRegistry:
    def __init__(self, config, scanner, db):
        self.config = config
        self._scanner_service = scanner
        self.db = db
        self.backend = _FakeBackend()

    @property
    def scanner(self):
        return self._scanner_service


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


# ── Service ───────────────────────────────────────────────────────────

class TestBackgroundScannerService:
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

            def run_scan(self, *a, **k):
                self.calls += 1
                started.set()
                release.wait(timeout=5)
                return []

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
