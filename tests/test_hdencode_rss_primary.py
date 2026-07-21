"""Tests for RSS-primary traffic, readiness, and rollback semantics."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.background_scanner import BackgroundScanner
from backend.database import DatabaseManager
from backend.hdencode_rss_service import HDEncodeRSSService


class Scanner:
    def __init__(self):
        self.calls = []
        self._last_crawl_seen_urls = set()
        self._last_crawl_early_stopped = False
        self.scrapers = SimpleNamespace(_detail=None)

    def try_acquire_scan(self):
        return True

    def release_scan(self):
        return None

    def run_scan(self, **kwargs):
        self.calls.append(kwargs)
        return []

    def rematch_cache(self):
        return 0


class Db:
    def get_source_health(self):
        return {}

    def record_source_success(self, _source):
        return None

    def record_source_failure(self, *_args, **_kwargs):
        return None

    def recover_hdencode_hydration_queue(self):
        return 0

    def list_hdencode_candidates(self, **_kwargs):
        return []

    def get_hdencode_rss_readiness(self, **_kwargs):
        return {
            "ready": True,
            "reasons": [],
            "successful_cycles": 20,
            "observed_days": 7,
        }

    def record_hdencode_shadow_comparison(self, **_kwargs):
        # Added by the RSS completion package; the shadow crawl persists a
        # comparison row after each listing cycle.
        return None

    def get_background_cache_urls(self):
        return set()

    def touch_background_cache(self, _urls):
        return None

    def upsert_background_cache(self, _rows):
        return None

    def purge_background_cache(self, _days):
        return None

    def count_background_cache(self):
        return 0


class Backend:
    def save_config(self):
        return None


class Registry:
    def __init__(self, mode, *, fallback=False):
        self.config = {
            "background_scan_enabled": True,
            "background_scan_sources": ["HDEncode", "DDLBase"],
            "background_scan_pages": 3,
            "background_scan_retain_days": 7,
            "hdencode_enabled": True,
            "hdencode_discovery_mode": mode,
            "hdencode_rss_listing_fallback_enabled": fallback,
            "hdencode_rss_shadow_min_cycles": 20,
            "hdencode_rss_shadow_min_days": 7,
        }
        self.scanner = Scanner()
        self.db = Db()
        self.backend = Backend()
        self.lifespan_generation = 1

    def owns_lifespan(self, generation):
        return generation == self.lifespan_generation


def _patch_candidate_service(monkeypatch):
    monkeypatch.setattr(
        "backend.hdencode_candidate_service."
        "HDEncodeCandidateService.classify_pending",
        lambda self, **kwargs: {"processed": 0, "states": {}},
    )
    monkeypatch.setattr(
        "backend.hdencode_candidate_service."
        "HDEncodeCandidateService.hydrate_pending",
        lambda self, *args, **kwargs: {
            "claimed": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        },
    )


def test_primary_never_runs_ordinary_hdencode_listing(monkeypatch):
    reg = Registry("rss_primary")
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {
            "mode": "rss_primary",
            "coverage_uncertain": False,
            "fallback_qualified": False,
            "feeds": [],
        },
    )

    BackgroundScanner(reg).scan_once()

    source_types = [call["source_type"] for call in reg.scanner.calls]
    assert "HDEncode" not in source_types
    assert "DDLBase" in source_types


def test_shadow_keeps_listing_comparison(monkeypatch):
    reg = Registry("rss_shadow")
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {
            "mode": "rss_shadow",
            "coverage_uncertain": False,
            "fallback_qualified": False,
            "feeds": [],
        },
    )

    BackgroundScanner(reg).scan_once()

    source_types = [call["source_type"] for call in reg.scanner.calls]
    assert "HDEncode" in source_types
    assert "DDLBase" in source_types


def test_primary_fallback_is_one_page_and_explicit(monkeypatch):
    reg = Registry("rss_primary", fallback=True)
    _patch_candidate_service(monkeypatch)
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: {
            "mode": "rss_primary",
            "coverage_uncertain": True,
            "fallback_qualified": True,
            "feeds": [],
        },
    )

    BackgroundScanner(reg).scan_once()

    hdencode = [
        call for call in reg.scanner.calls
        if call["source_type"] == "HDEncode"
    ]
    assert len(hdencode) == 1
    assert hdencode[0]["pages"] == 1


def test_listing_mode_is_one_setting_rollback(monkeypatch):
    reg = Registry("listing")
    called = []
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        lambda self, **kwargs: called.append(True),
    )

    BackgroundScanner(reg).scan_once()

    assert called == []
    assert any(
        call["source_type"] == "HDEncode"
        for call in reg.scanner.calls
    )


def test_primary_service_refuses_before_shadow_gate():
    class NotReadyDb(Db):
        def get_hdencode_rss_readiness(self, **_kwargs):
            return {
                "ready": False,
                "reasons": ["insufficient_days"],
                "successful_cycles": 20,
                "observed_days": 2,
            }

    service = HDEncodeRSSService(
        {
            "hdencode_enabled": True,
            "hdencode_discovery_mode": "rss_primary",
            "hdencode_rss_shadow_min_cycles": 20,
            "hdencode_rss_shadow_min_days": 7,
        },
        NotReadyDb(),
        client=SimpleNamespace(fetch=lambda *_args, **_kwargs: None),
    )
    result = service.poll_cycle(include_catchup=False)
    assert result["skipped"] is True
    assert result["reason"] == "primary_not_ready"
    assert result["requests"] == 0


def test_readiness_requires_cycles_days_and_two_healthy_normal_feeds(tmp_path):
    db = DatabaseManager(str(tmp_path / "crawler.db"))
    now = datetime.now(timezone.utc)
    # The RSS completion readiness gate reads hdencode_shadow_cycles (via
    # get_hdencode_shadow_summary), not hdencode_ingest_cycles: it requires >=20
    # complete comparison cycles spanning >=7 days, zero relevant misses, proven
    # request reduction (listing_requests > rss_requests), and >=1 restart/catchup
    # recovery cycle.
    for index in range(20):
        completed = (now - timedelta(days=8) + timedelta(days=index * 0.4)).isoformat()
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO hdencode_shadow_cycles (
                    cycle_uuid, started_at, completed_at, normal_feeds_complete,
                    rss_requests, listing_requests, rss_count, listing_count,
                    duplicate_count, feed_only_count, listing_only_count,
                    relevant_miss_count, request_reduction_pct, catchup_used,
                    restart_recovery, outcome
                ) VALUES (?, ?, ?, 1, 1, 2, 5, 5, 5, 0, 0, 0, 50.0, ?, ?, 'success')
                """,
                (
                    f"cycle-{index}",
                    completed,
                    completed,
                    1 if index == 0 else 0,
                    1 if index == 1 else 0,
                ),
            )
    with db.transaction() as conn:
        for feed in ("movies_all", "tv_all"):
            conn.execute(
                """
                INSERT INTO hdencode_feed_state (
                    feed_key, feed_url, last_checked_at, last_status,
                    consecutive_failures
                ) VALUES (?, ?, ?, 304, 0)
                """,
                (
                    feed,
                    f"https://hdencode.org/{feed}/",
                    now.isoformat(),
                ),
            )

    readiness = db.get_hdencode_rss_readiness(
        min_cycles=20,
        min_days=7,
    )
    assert readiness["ready"] is True
    assert readiness["normal_feeds_healthy"] is True

    with db.transaction() as conn:
        conn.execute(
            "UPDATE hdencode_feed_state SET consecutive_failures = 1 "
            "WHERE feed_key = 'tv_all'"
        )
    readiness = db.get_hdencode_rss_readiness(
        min_cycles=20,
        min_days=7,
    )
    assert readiness["ready"] is False
    assert "normal_feeds_unhealthy_or_stale" in readiness["reasons"]
