"""Regression tests for repeated API lifespans and registry ownership."""

import threading

import pytest
from fastapi.testclient import TestClient

from backend.api.dependencies import ServiceRegistry, registry
from backend.api.main import (
    _REGISTRY_LIFESPAN_FIELDS,
    _clear_registry_lifespan_state,
    _init_services,
    _prepare_registry_for_startup,
    _teardown_services,
    create_app,
)


def _fill_lifespan_fields(reg, value):
    for field_name in _REGISTRY_LIFESPAN_FIELDS:
        setattr(reg, field_name, value)
    reg.config = {"stale": True}


def test_prepare_registry_clears_every_lifespan_reference_and_shutdown_event():
    reg = ServiceRegistry()
    stale = object()
    _fill_lifespan_fields(reg, stale)
    reg.request_shutdown()
    prior_generation = reg.lifespan_generation

    _prepare_registry_for_startup(reg)

    assert reg.lifespan_generation == prior_generation + 1
    assert reg.shutdown_requested is False
    assert reg.config == {}
    for field_name in _REGISTRY_LIFESPAN_FIELDS:
        assert getattr(reg, field_name) is None, field_name


def test_init_clears_stale_services_before_appservice_startup(monkeypatch):
    """The synchronous maintenance pass must never see a prior lifespan service."""
    import backend.app_service as app_service_module

    stale = object()
    _fill_lifespan_fields(registry, stale)
    registry.request_shutdown()

    class StartupObserved(Exception):
        pass

    class FakeAppService:
        def __init__(self):
            self.config = {}
            self.db = None

        def startup(self):
            assert registry.shutdown_requested is False
            assert registry.backend is self
            assert registry.db is None
            for field_name in _REGISTRY_LIFESPAN_FIELDS:
                if field_name == "backend":
                    continue
                assert getattr(registry, field_name) is None, field_name
            raise StartupObserved

    monkeypatch.setattr(app_service_module, "AppService", FakeAppService)
    with pytest.raises(StartupObserved):
        _init_services(registry)
    _clear_registry_lifespan_state(registry)


class _Raises:
    def stop(self):
        raise RuntimeError("stop failed")

    def shutdown(self):
        raise RuntimeError("shutdown failed")

    def close(self):
        raise RuntimeError("close failed")


def test_teardown_clears_all_references_even_when_shutdown_hooks_fail():
    reg = ServiceRegistry()
    _fill_lifespan_fields(reg, _Raises())

    _teardown_services(reg)

    assert reg.shutdown_requested is True
    assert reg.config == {}
    for field_name in _REGISTRY_LIFESPAN_FIELDS:
        assert getattr(reg, field_name) is None, field_name


def test_repeated_real_lifespans_start_with_empty_results(monkeypatch):
    """Exercise the failure family that previously went green only on CI retry."""
    monkeypatch.setenv("SCANHOUND_ALLOW_OPEN", "1")

    for _ in range(3):
        app = create_app(config_override={"plex_url": "", "plex_token": ""})
        with TestClient(app) as client:
            response = client.get("/results")
            assert response.status_code == 200
            assert response.json()["items"] == []
            assert response.json()["total"] == 0
        for field_name in _REGISTRY_LIFESPAN_FIELDS:
            assert getattr(registry, field_name) is None, field_name
        assert registry.shutdown_requested is True

def test_late_background_worker_cannot_publish_into_next_lifespan():
    """A worker outliving stop() must not publish through a later lifespan.

    BackgroundScanner.stop() performs a bounded two-second join. This test
    blocks scan_once() beyond that join, tears down the old lifespan, starts a
    new registry lifespan (which clears the shared shutdown event), and then
    releases the old worker.

    On the accepted PR #17 head, the old worker resumes with its captured old
    database and the reused registry. The intended pre-fix result is therefore
    a failure showing late writes and/or mutation of the new lifespan config.
    """
    from backend.background_scanner import BackgroundScanner

    entered_scan = threading.Event()
    release_scan = threading.Event()
    worker_outcome = {}

    class FakeDB:
        def __init__(self):
            self.closed = False
            self.late_writes = []

        def _record_if_closed(self, operation):
            if self.closed:
                self.late_writes.append(operation)

        def get_background_cache_urls(self):
            return set()

        def touch_background_cache(self, _urls):
            self._record_if_closed("touch")

        def upsert_background_cache(self, _rows):
            self._record_if_closed("upsert")

        def purge_background_cache(self, _days):
            self._record_if_closed("purge")

        def count_background_cache(self):
            self._record_if_closed("count")
            return 0

    class FakeScanner:
        _last_crawl_seen_urls = set()
        _last_crawl_early_stopped = False

        def try_acquire_scan(self):
            return True

        def release_scan(self):
            return None

        def rematch_cache(self):
            return 0

    class FakeBackend:
        def __init__(self, db):
            self.db = db

        def shutdown(self):
            self.db.closed = True

    old_db = FakeDB()
    reg = ServiceRegistry()
    reg.config = {
        "background_scan_sources": ["HDEncode"],
        "background_scan_pages": 1,
        "background_scan_retain_days": 7,
    }
    reg.db = old_db
    reg._scanner_service = FakeScanner()
    reg.backend = FakeBackend(old_db)

    background = BackgroundScanner(reg)
    reg._background_scanner = background

    def blocked_scan_source(_source, _pages, _skip_urls=None):
        entered_scan.set()
        assert release_scan.wait(timeout=10), "test did not release old worker"
        return [object()]

    background._scan_source = blocked_scan_source
    background._to_cache_rows = lambda _items, _source: [
        {
            "url": "https://hdencode.org/example/",
            "title": "Example",
            "year": 2026,
            "status": "MISSING",
            "source_category": "HDEncode",
            "data": "{}",
        }
    ]

    def run_old_worker():
        try:
            worker_outcome["result"] = background.scan_once()
        except BaseException as exc:
            worker_outcome["exception"] = exc

    worker = threading.Thread(
        target=run_old_worker,
        name="late-background-worker-test",
        daemon=True,
    )
    # Teardown calls BackgroundScanner.stop(), which joins this exact thread.
    background._thread = worker
    worker.start()
    assert entered_scan.wait(timeout=2), "old worker never reached blocking seam"

    # Uses the production two-second BackgroundScanner.stop() join.
    _teardown_services(reg)
    assert old_db.closed is True

    # Simulate immediate reuse of the module-level registry by a new lifespan.
    _prepare_registry_for_startup(reg)
    reg.config = {"new_lifespan": True}

    release_scan.set()
    worker.join(timeout=3)
    assert not worker.is_alive(), "old worker failed to exit after release"
    assert "exception" not in worker_outcome, repr(worker_outcome.get("exception"))

    # These assertions are expected to FAIL on the current PR #17 head.
    assert old_db.late_writes == []
    assert reg.config == {"new_lifespan": True}
