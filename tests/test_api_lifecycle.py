"""Regression tests for repeated API lifespans and registry ownership."""

import threading
import time
from unittest.mock import patch

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


# ── Background-thread ownership (lifespan shutdown joins what it started) ──


def test_lifespan_threads_are_joined_by_teardown():
    """The registry's own workers must not outlive the lifespan that made them.

    They are all daemon=True, so before teardown joined them the only thing that
    ended them was interpreter exit — under TestClient (many lifespans, one
    process) they accumulated and kept touching process globals during later
    tests.
    """
    reg = ServiceRegistry()
    reg.begin_lifespan()
    started = threading.Event()

    def _worker():
        started.set()
        # Exactly how a well-behaved worker waits: interruptible, so the join
        # below costs microseconds rather than the full nominal interval.
        reg.wait_for_shutdown(300)

    thread = reg.spawn_lifespan_thread(_worker, name="join-me")
    assert started.wait(timeout=2), "worker never started"

    _teardown_services(reg)

    assert not thread.is_alive(), "teardown returned with the worker still live"


def test_teardown_does_not_hang_on_a_wedged_worker(caplog, monkeypatch):
    """A worker stuck in an uninterruptible call must not block shutdown.

    This is the whole reason every join is bounded: a background thread blocked
    on a socket read has no way to observe the shutdown flag, so an unbounded
    join would wedge the app's shutdown behind an unreachable server.
    """
    import backend.api.main as api_main

    # Shrink the production budget so the test spends 0.3s, not 5s, proving it.
    monkeypatch.setattr(api_main, "LIFESPAN_JOIN_BUDGET_SECONDS", 0.3)

    reg = ServiceRegistry()
    reg.begin_lifespan()
    wedged = threading.Event()
    release = threading.Event()

    def _wedged_worker():
        wedged.set()
        release.wait(timeout=30)  # stands in for a socket read that never returns

    thread = reg.spawn_lifespan_thread(_wedged_worker, name="wedged-worker")
    assert wedged.wait(timeout=2), "worker never reached the blocking seam"

    try:
        with caplog.at_level("WARNING"):
            started = time.monotonic()
            _teardown_services(reg)
            elapsed = time.monotonic() - started

        # Bounded: the budget, plus slack for the service stop() calls around it.
        assert elapsed < 3.0, f"teardown blocked for {elapsed:.1f}s"
        assert thread.is_alive(), "worker was supposed to still be wedged"
        # And it is not silent — the straggler is named in the log.
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelname == "WARNING"]
        assert any("wedged-worker" in m for m in warnings), warnings
    finally:
        release.set()
        thread.join(timeout=5)


def test_join_budget_is_shared_across_threads_not_per_thread():
    """N wedged workers must cost the same as one, or shutdown scales with them."""
    reg = ServiceRegistry()
    reg.begin_lifespan()
    release = threading.Event()
    ready = []

    for i in range(5):
        started = threading.Event()
        ready.append(started)

        def _wedged(ev=started):
            ev.set()
            release.wait(timeout=30)

        reg.spawn_lifespan_thread(_wedged, name=f"wedged-{i}")
    for ev in ready:
        assert ev.wait(timeout=2)

    try:
        began = time.monotonic()
        stragglers = reg.join_lifespan_threads(timeout=0.3)
        elapsed = time.monotonic() - began

        # 5 threads x 0.3s per-thread would be 1.5s; one shared budget is ~0.3s.
        assert elapsed < 1.0, f"budget applied per-thread: {elapsed:.1f}s"
        assert len(stragglers) == 5, stragglers
    finally:
        release.set()


def test_poster_backfill_settle_delay_is_interruptible():
    """The 30s settle delay must be a shutdown-aware wait, not time.sleep.

    A plain sleep makes the thread unjoinable for its full duration, so every
    shutdown in the first 30 seconds of a lifespan either stalled on it or
    leaked it.
    """
    reg = ServiceRegistry()
    reg.begin_lifespan()
    entered = threading.Event()
    finished = threading.Event()

    def _settles():
        entered.set()
        if reg.wait_for_shutdown(30):
            finished.set()
            return
        raise AssertionError("settle delay was not interrupted by shutdown")

    thread = reg.spawn_lifespan_thread(_settles, name="poster-backfill")
    assert entered.wait(timeout=2)

    began = time.monotonic()
    reg.request_shutdown()
    thread.join(timeout=5)
    elapsed = time.monotonic() - began

    assert finished.is_set(), "worker did not observe the shutdown signal"
    assert elapsed < 2.0, f"waited {elapsed:.1f}s for an interruptible sleep"


def test_registry_threads_do_not_accumulate_across_a_long_lifespan():
    """Finished threads are reaped, or a weeks-long lifespan grows a dead list."""
    reg = ServiceRegistry()
    reg.begin_lifespan()

    for _ in range(20):
        thread = reg.spawn_lifespan_thread(lambda: None, name="short-lived")
        thread.join(timeout=2)

    reg.spawn_lifespan_thread(lambda: None, name="last")
    assert len(reg._lifespan_threads) <= 2, len(reg._lifespan_threads)


def test_spawn_is_atomic_so_a_join_cannot_see_a_half_spawned_thread():
    """Tracking and start() must happen under one lock hold.

    A constructed-but-not-started thread reports is_alive() == False exactly
    like a finished one, so between "track" and "start" the entry is a trap: a
    concurrent register() reaps it as dead (the worker then runs untracked),
    and a concurrent join() calls Thread.join() on it — RuntimeError, which
    aborts the rest of the shutdown.

    Driven by a stalled start() rather than by racing threads and hoping: the
    window is a few instructions wide, and a 24-way concurrent-spawn version of
    this test passed against the unlocked implementation on every run.
    """
    reg = ServiceRegistry()
    reg.begin_lifespan()

    inside_start = threading.Event()
    let_start_proceed = threading.Event()
    real_start = threading.Thread.start

    def stalled_start(self):
        # Stall only the thread under test, never pytest's own machinery.
        if self.name == "half-spawned":
            inside_start.set()
            assert let_start_proceed.wait(timeout=10)
        real_start(self)

    observed = {}

    def _joiner():
        try:
            observed["stragglers"] = reg.join_lifespan_threads(timeout=0.1)
        except BaseException as exc:      # RuntimeError on the unlocked version
            observed["exception"] = exc

    with patch.object(threading.Thread, "start", stalled_start):
        spawner = threading.Thread(
            target=reg.spawn_lifespan_thread, args=(lambda: None,),
            kwargs={"name": "half-spawned"}, name="spawner")
        spawner.start()
        assert inside_start.wait(timeout=5), "spawn never reached start()"

        joiner = threading.Thread(target=_joiner, name="joiner")
        joiner.start()
        time.sleep(0.3)  # ample time for the joiner to finish, if it can run

        # The whole point: the joiner is still blocked on the lock. Without the
        # lock held across start() it would have sailed through and tripped
        # over the unstarted thread.
        raced = dict(observed)

        # Released in a finally so a failure here reports its own message
        # instead of stalling the spawner for the full 10s stall timeout.
        let_start_proceed.set()
        spawner.join(timeout=5)
        joiner.join(timeout=5)

    assert not raced, f"join observed a half-spawned thread: {raced}"

    assert "exception" not in observed, repr(observed.get("exception"))
    assert observed.get("stragglers") == [], observed


def test_bulk_rename_loops_stop_on_shutdown():
    """RenameService's bulk loops must poll the shutdown flag, not just be joinable.

    Registering these workers without this is a net loss: teardown would wait
    out the whole join budget and log a straggler every time one is in flight.
    """
    from backend.rename.service import RenameService

    reg = ServiceRegistry()
    reg.begin_lifespan()
    svc = RenameService.__new__(RenameService)   # no DB/config needed for this
    svc._reg = reg

    assert svc._shutdown_requested() is False
    reg.request_shutdown()
    assert svc._shutdown_requested() is True


def test_dv_sync_labels_stops_at_an_item_boundary():
    """The Plex library walk takes a stop hook, and honours it mid-list."""
    from backend.rename import dv_labeler

    reconciled = []

    class _Movie:
        def __init__(self, key):
            self.ratingKey = key

    class _Lib:
        def all(self):
            return [_Movie(i) for i in range(10)]

    class _PM:
        def get_library_section(self, _name):
            return _Lib()

    class _Db:
        def get_dv_scans(self, **_kw):
            return []

        def upsert_dv_scan(self, *a, **k):
            pass

    stop_after = 3

    def _fake_reconcile(mv, *a, **k):
        reconciled.append(mv.ratingKey)
        return {"added": [], "removed": [], "matched": False}

    original = dv_labeler.reconcile_movie
    dv_labeler.reconcile_movie = _fake_reconcile
    try:
        result = dv_labeler.sync_labels(
            _Db(), _PM(), {"movie_libs": ["Movies"]},
            stop_requested=lambda: len(reconciled) >= stop_after)
    finally:
        dv_labeler.reconcile_movie = original

    # Stopped early rather than walking all 10, and still returned a summary.
    assert len(reconciled) == stop_after, reconciled
    assert isinstance(result, dict)
