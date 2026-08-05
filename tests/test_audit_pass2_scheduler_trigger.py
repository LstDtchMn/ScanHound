"""Audit pass 2, finding 18 — the scheduler must really start a scan.

set_scan_trigger() is called from exactly one place in the tree,
ui/controllers/scanner_controller.py (the Qt desktop app). The Docker image
runs ``python -m backend.api``, so on the server build _scan_trigger stayed
None: every interval the scheduler stamped last_scan_time, logged
"Scheduled scan triggered", started nothing, and /scheduler/status kept
reporting scheduler_active True off the still-alive thread.

These tests pin both halves of the fix:
  * the server build now actually starts a scan (the healthy path — a fix
    that merely deleted the misleading log line fails TestServerBuildScan);
  * when nothing can start a scan, the scheduler says so and stops, so the
    status route the UI reads reports it inactive.

The 60s tick is driven through a stand-in for AppService._scheduler_stop
rather than by sleeping, so each test runs the real _scheduler_loop body.
"""

import logging
import threading

import pytest

from backend.app_service import AppService


# ── Tick driver ───────────────────────────────────────────────────────


class FastTick:
    """Stand-in for AppService._scheduler_stop that makes the 60s tick instant.

    wait() returns False immediately for the first ``ticks`` calls (one loop
    iteration each), then parks on a real Event exactly like production — so
    the scheduler thread stays alive unless the loop itself decides to return.
    ``ticks_done`` is set once the loop has finished those iterations (or once
    the loop stopped itself), giving tests a deterministic sync point instead
    of a sleep.
    """

    def __init__(self, ticks=1):
        self._remaining = ticks
        self._event = threading.Event()
        self.ticks_done = threading.Event()
        self.set_called = False

    def wait(self, timeout=None):
        if self._event.is_set():
            return True
        if self._remaining > 0:
            self._remaining -= 1
            return False
        self.ticks_done.set()
        return self._event.wait(timeout)

    def is_set(self):
        return self._event.is_set()

    def set(self):
        self.set_called = True
        self._event.set()
        self.ticks_done.set()

    def clear(self):
        # _start_scheduler() clears before (re)starting; re-arm the sync point
        # so a revived scheduler can be awaited the same way.
        self._event.clear()
        self.ticks_done.clear()


def make_service(ticks=1, **config):
    """AppService with a driven scheduler clock and no on-disk config writes."""
    svc = AppService()
    svc.config = {
        "scheduler_enabled": True,
        "scheduler_interval": 24,
        "scheduler_only_when_idle": False,
        "last_scan_time": 0,  # 0 => the interval has always elapsed
    }
    svc.config.update(config)
    svc.saved = []
    svc.save_config = lambda: svc.saved.append(dict(svc.config))
    svc._scheduler_stop = FastTick(ticks)
    return svc


@pytest.fixture
def services():
    """Yield a collector; every service handed to it is stopped afterwards."""
    made = []
    yield made
    for svc in made:
        svc._scheduler_stop.set()
        thread = svc._scheduler_thread
        if thread is not None:
            thread.join(timeout=5)


def run_scheduler(svc, services, trigger=None):
    """Register an optional trigger, start the real scheduler and wait a tick.

    The trigger is registered here rather than by the caller because
    set_scan_trigger() starts the scheduler itself when it is enabled (the
    revive path), so registering earlier would run the ticks outside the
    caller's caplog block.
    """
    services.append(svc)
    if trigger is not None:
        svc.set_scan_trigger(trigger)
    svc._start_scheduler()
    assert svc._scheduler_stop.ticks_done.wait(5), "scheduler loop never ticked"
    return svc._scheduler_stop


@pytest.fixture
def api_scan(monkeypatch):
    """Wire the API-server build: a scanner service plus a captured _run_scan.

    ``adopt(svc)`` publishes the service on the registry the way
    backend/api/main.py's lifespan does — the scheduler only falls back to the
    HTTP scan machinery for the AppService that process is actually serving.
    """
    from backend.api import dependencies
    from backend.api.routes import scanner as scanner_route

    class StubScanner:
        scan_in_progress = False

    calls = []
    started = threading.Event()
    stub = StubScanner()

    def fake_run_scan(reg, req):
        calls.append((reg, req))
        started.set()

    monkeypatch.setattr(scanner_route, "_run_scan", fake_run_scan)
    monkeypatch.setattr(dependencies.registry, "_scanner_service", stub)
    original_state = dict(scanner_route._scan_state)
    scanner_route._scan_state["state"] = "idle"

    def adopt(svc):
        monkeypatch.setattr(dependencies.registry, "backend", svc)
        return svc

    def unwire_scanner():
        """Reproduce the startup window: backend published, scanner not yet."""
        monkeypatch.setattr(dependencies.registry, "_scanner_service", None)

    yield {"calls": calls, "started": started, "scanner": stub,
           "module": scanner_route, "adopt": adopt,
           "unwire_scanner": unwire_scanner}
    scanner_route._scan_state.clear()
    scanner_route._scan_state.update(original_state)


def scheduler_active(svc):
    """What GET /scheduler/status reports — the consumer, not the component."""
    from backend.api.dependencies import ServiceRegistry
    from backend.api.routes.scheduler import scheduler_status

    reg = ServiceRegistry(config=svc.config, backend=svc)
    return scheduler_status(reg)["scheduler_active"]


# ── Desktop build: a registered trigger still works ───────────────────


class TestRegisteredTrigger:
    """Positive control for the path that already worked (Qt desktop)."""

    def test_registered_trigger_is_invoked(self, services):
        calls = []
        svc = make_service()
        run_scheduler(svc, services, trigger=lambda: calls.append("fired"))
        assert calls == ["fired"]

    def test_success_is_logged_and_stamped(self, services, caplog):
        svc = make_service()
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services, trigger=lambda: None)
        assert "Scheduled scan triggered" in caplog.text
        assert svc.config["last_scan_time"] > 0
        assert svc.saved, "last_scan_time was not persisted"
        assert svc._scheduler_thread.is_alive()

    def test_trigger_returning_none_counts_as_started(self, services, caplog):
        # Disagreeing case: scanner_controller._on_scheduled_scan returns None.
        # An implementation that gated success on a truthy return would treat
        # the desktop trigger as a failure and stop the scheduler -- this test
        # fails for that implementation and passes for the correct one.
        svc = make_service()
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services, trigger=lambda: None)
        assert "Scheduled scan triggered" in caplog.text
        assert "Scheduler stopped" not in caplog.text
        assert svc._scheduler_thread.is_alive()

    def test_explicit_false_is_not_reported_as_triggered(self, services, caplog):
        # A trigger that returns False started nothing. The old code logged
        # "Scheduled scan triggered" before even calling the trigger, so it
        # would pass a call-count assertion while still lying in the log --
        # this case is what separates the two implementations.
        svc = make_service()
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services, trigger=lambda: False)
        assert "Scheduled scan triggered" not in caplog.text
        assert svc.config["last_scan_time"] == 0, "interval consumed by a scan that never ran"
        assert svc._scheduler_thread.is_alive(), "a retryable skip must not kill the scheduler"

    def test_raising_trigger_keeps_scheduler_alive(self, services, caplog):
        def boom():
            raise RuntimeError("scanner exploded")

        svc = make_service()
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services, trigger=boom)
        assert "scanner exploded" in caplog.text
        assert "Scheduled scan triggered" not in caplog.text
        assert svc._scheduler_thread.is_alive()

    def test_skip_warning_is_edge_triggered(self, services, caplog):
        # Three consecutive skips must not produce three warnings a minute.
        svc = make_service(ticks=3)
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services, trigger=lambda: False)
        assert caplog.text.count("Scheduled scan could not start") == 1


# ── Server build: the fix ─────────────────────────────────────────────


class TestServerBuildScan:
    """The Docker build registers no trigger; a scan must still start."""

    def test_scan_thread_is_actually_started(self, services, api_scan):
        svc = api_scan["adopt"](make_service())
        assert svc._scan_trigger is None, "the server build registers no trigger"
        run_scheduler(svc, services)
        assert api_scan["started"].wait(5), "no scan thread ran"
        assert len(api_scan["calls"]) == 1
        reg, req = api_scan["calls"][0]
        assert req.type == "incremental"
        assert reg.scanner is api_scan["scanner"]

    def test_success_is_logged_and_stamped(self, services, api_scan, caplog):
        svc = api_scan["adopt"](make_service())
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services)
        assert api_scan["started"].wait(5)
        assert "Scheduled scan triggered" in caplog.text
        assert svc.config["last_scan_time"] > 0

    def test_status_route_reports_active(self, services, api_scan):
        svc = api_scan["adopt"](make_service())
        run_scheduler(svc, services)
        assert api_scan["started"].wait(5)
        assert scheduler_active(svc) is True

    def test_scanner_not_wired_yet_retries_instead_of_stopping(self, services, api_scan, caplog):
        # The startup window: backend/api/main.py publishes reg.backend before
        # AppService.startup() (which starts this scheduler) and wires
        # reg._scanner_service only afterwards. An implementation that treated
        # "no scanner service" as fatal would kill the scheduler for good on a
        # slow boot -- this case is what distinguishes it from the fix.
        svc = api_scan["adopt"](make_service())
        api_scan["unwire_scanner"]()
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services)
        assert api_scan["calls"] == []
        assert "Scheduled scan triggered" not in caplog.text
        assert "Scheduler stopped" not in caplog.text
        assert svc._scheduler_thread.is_alive()
        assert svc.config["last_scan_time"] == 0

    def test_running_scan_defers_instead_of_double_starting(self, services, api_scan, caplog):
        # _scan_state is the same module-level dict POST /scan/start claims.
        api_scan["module"]._scan_state["state"] = "running"
        svc = api_scan["adopt"](make_service())
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services)
        assert api_scan["calls"] == []
        assert "Scheduled scan triggered" not in caplog.text
        assert svc.config["last_scan_time"] == 0, "the interval must survive a skipped tick"
        assert svc._scheduler_thread.is_alive()

    def test_background_precache_scan_defers(self, services, api_scan, caplog):
        api_scan["scanner"].scan_in_progress = True
        svc = api_scan["adopt"](make_service())
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services)
        assert api_scan["calls"] == []
        assert "Scheduled scan triggered" not in caplog.text
        assert svc._scheduler_thread.is_alive()

    def test_registered_trigger_wins_over_the_fallback(self, services, api_scan):
        # The desktop trigger must not be bypassed just because the API
        # machinery is importable in the same process.
        calls = []
        svc = api_scan["adopt"](make_service())
        run_scheduler(svc, services, trigger=lambda: calls.append("desktop"))
        assert calls == ["desktop"]
        assert api_scan["calls"] == []

    def test_interval_not_yet_elapsed_starts_nothing(self, services, api_scan, caplog):
        # Guards against "fix" by firing every tick: with last_scan_time just
        # set, the 24h interval has not elapsed and no scan may start.
        import time as _time
        svc = api_scan["adopt"](make_service(last_scan_time=_time.time()))
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services)
        assert api_scan["calls"] == []
        assert "Scheduled scan triggered" not in caplog.text
        assert svc._scheduler_thread.is_alive()


# ── Nothing can start a scan: report it honestly ──────────────────────


class TestNoTriggerAvailable:
    """No registered trigger, and this AppService is not the one served over
    HTTP — the desktop build, where the Qt controller never got as far as
    registering its trigger. Nothing can ever start a scan."""

    @pytest.fixture(autouse=True)
    def _no_scanner(self, monkeypatch):
        from backend.api import dependencies
        from backend.api.routes import scanner as scanner_route

        monkeypatch.setattr(dependencies.registry, "backend", None)
        monkeypatch.setattr(dependencies.registry, "_scanner_service", None)
        original_state = dict(scanner_route._scan_state)
        scanner_route._scan_state["state"] = "idle"
        yield
        scanner_route._scan_state.clear()
        scanner_route._scan_state.update(original_state)

    def test_does_not_claim_a_scan_was_triggered(self, services, caplog):
        svc = make_service()
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services)
        assert "Scheduled scan triggered" not in caplog.text
        assert svc.config["last_scan_time"] == 0

    def test_logs_the_reason(self, services, caplog):
        svc = make_service()
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services)
        assert "no scan trigger is registered" in caplog.text
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_thread_stops_so_status_route_reports_inactive(self, services, caplog):
        svc = make_service()
        with caplog.at_level(logging.INFO, logger="backend.app_service"):
            run_scheduler(svc, services)
        svc._scheduler_thread.join(timeout=5)
        assert not svc._scheduler_thread.is_alive()
        # The lie the finding is about: a green "Scheduler active" dot over a
        # scheduler that cannot scan.
        assert scheduler_active(svc) is False

    def test_forwards_to_the_ui_log_callback(self, services):
        # The old code's warning branch was `elif self._log_callback:`, which
        # was dead on the server. self.log() forwards to whatever UI callback
        # is registered as well as the file log.
        seen = []
        svc = make_service()
        svc.set_log_callback(lambda msg, level: seen.append((msg, level)))
        run_scheduler(svc, services)
        assert seen and seen[0][1] == "error"


# ── A trigger registered after startup revives the scheduler ──────────


class TestLateTriggerRevivesScheduler:

    @pytest.fixture(autouse=True)
    def _no_scanner(self, monkeypatch):
        from backend.api import dependencies
        monkeypatch.setattr(dependencies.registry, "backend", None)
        monkeypatch.setattr(dependencies.registry, "_scanner_service", None)

    def test_registering_a_trigger_restarts_and_fires(self, services):
        svc = make_service(ticks=2)
        run_scheduler(svc, services)
        svc._scheduler_thread.join(timeout=5)
        assert not svc._scheduler_thread.is_alive()

        fired = threading.Event()
        svc.set_scan_trigger(fired.set)
        assert svc._scheduler_thread.is_alive(), "scheduler was not revived"
        assert fired.wait(5), "revived scheduler never fired the new trigger"

    def test_disabled_scheduler_is_not_started(self, services):
        # Disagreeing case: an implementation that unconditionally started the
        # thread from set_scan_trigger would turn the scheduler on for a user
        # who has it switched off.
        svc = make_service(scheduler_enabled=False)
        services.append(svc)
        svc.set_scan_trigger(lambda: None)
        assert svc._scheduler_thread is None

    def test_clearing_the_trigger_does_not_start_it(self, services):
        svc = make_service()
        services.append(svc)
        svc.set_scan_trigger(None)
        assert svc._scheduler_thread is None
