"""Cross-track gates for lifecycle ownership, RSS, and traffic policy."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import backend.hdencode_transport as transport
from backend.api.dependencies import ServiceRegistry
from backend.background_scanner import BackgroundScanner
from backend.hdencode_coordinator import (
    HDEncodeRequestCancelled,
    HDEncodeTrafficCoordinator,
)
from backend.hdencode_rss_service import HDEncodeRSSService


class _HealthDb:
    def get_source_health(self):
        return {}

    def record_source_success(self, _source):
        return None

    def record_source_failure(self, *_args, **_kwargs):
        return None


def test_new_lifespan_cancels_waiting_rss_before_transport_construction(
        monkeypatch):
    """PR #17 ownership and PR C authorization must fail closed together."""
    registry = ServiceRegistry()
    owner_generation = registry.begin_lifespan()

    coordinator = HDEncodeTrafficCoordinator()
    coordinator._MIN_START_INTERVAL = 0
    coordinator.configure({"hdencode_enabled": True}, _HealthDb())
    coordinator._semaphores["rss"].acquire()

    constructors = []
    monkeypatch.setattr(
        transport.cloudscraper,
        "create_scraper",
        lambda: constructors.append("constructed") or object(),
    )
    outcome = []

    def worker():
        try:
            with coordinator.request(
                "rss",
                stop_requested=lambda: not registry.owns_lifespan(
                    owner_generation
                ),
            ):
                transport.create_source_http_client(hdencode=True)
                outcome.append("started")
        except HDEncodeRequestCancelled:
            outcome.append("cancelled")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(0.03)

    # Beginning a later lifespan changes ownership even though its own shutdown
    # event is clear.
    registry.begin_lifespan()
    coordinator._semaphores["rss"].release()
    thread.join(timeout=2)

    assert outcome == ["cancelled"]
    assert constructors == []


class _Scanner:
    def __init__(self):
        self.held = False
        self.releases = 0
        self.calls = []
        self._last_crawl_seen_urls = set()
        self._last_crawl_early_stopped = False
        self.scrapers = SimpleNamespace(_detail=None)

    def try_acquire_scan(self):
        if self.held:
            return False
        self.held = True
        return True

    def release_scan(self):
        self.held = False
        self.releases += 1

    def run_scan(self, **kwargs):
        self.calls.append(kwargs)
        return []

    def rematch_cache(self):
        return 0


class _BackgroundDb(_HealthDb):
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


class _Registry:
    def __init__(self):
        self.config = {
            "background_scan_sources": ["HDEncode"],
            "background_scan_pages": 3,
            "background_scan_retain_days": 7,
            "hdencode_enabled": True,
            "hdencode_discovery_mode": "rss_shadow",
        }
        self.scanner = _Scanner()
        self.db = _BackgroundDb()
        self.backend = None
        self.lifespan_generation = 1
        self.active = True

    def owns_lifespan(self, generation):
        return self.active and generation == self.lifespan_generation


def test_stale_rss_return_releases_global_scan_slot(monkeypatch):
    registry = _Registry()

    def expire_lifespan(_service, **_kwargs):
        registry.active = False
        return {
            "mode": "rss_shadow",
            "feeds": [],
            "listing_fallback_started": False,
            "downloads_started": 0,
        }

    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
        expire_lifespan,
    )

    result = BackgroundScanner(registry).scan_once()

    assert result["reason"] == "stale_lifespan"
    assert registry.scanner.held is False
    assert registry.scanner.releases == 1
    assert registry.scanner.calls == []


class _NotReadyDb(_HealthDb):
    def get_hdencode_rss_readiness(self, **_kwargs):
        return {
            "ready": False,
            "reasons": ["insufficient_cycles", "insufficient_days"],
            "successful_cycles": 0,
            "observed_days": 0,
            "normal_feeds_healthy": False,
        }


def test_primary_readiness_gate_issues_no_request():
    calls = []
    service = HDEncodeRSSService(
        {
            "hdencode_enabled": True,
            "hdencode_discovery_mode": "rss_primary",
            "hdencode_rss_shadow_min_cycles": 20,
            "hdencode_rss_shadow_min_days": 7,
        },
        _NotReadyDb(),
        client=SimpleNamespace(
            fetch=lambda *_args, **_kwargs: calls.append("request")
        ),
    )

    result = service.poll_cycle(include_catchup=False)

    assert result["skipped"] is True
    assert result["reason"] == "primary_not_ready"
    assert result["requests"] == 0
    assert calls == []
