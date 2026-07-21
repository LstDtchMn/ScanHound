"""Enforcement tests for the process-wide HDEncode coordinator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

import backend.hdencode_transport as transport
from backend.hdencode_coordinator import (
    HDEncodeRequestCancelled,
    HDEncodeTrafficCoordinator,
    HDEncodeTrafficDenied,
    require_transport_authorization,
)


class _Db:
    def __init__(self):
        self.health = {}
        self.successes = 0
        self.failures = []

    def get_source_health(self):
        return {"hdencode": dict(self.health)}

    def record_source_success(self, source):
        assert source == "hdencode"
        self.successes += 1
        self.health = {
            "state": "healthy",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def record_source_failure(
        self,
        source,
        state,
        reason_code,
        cooldown_seconds=None,
    ):
        assert source == "hdencode"
        self.failures.append((state, reason_code, cooldown_seconds))
        now = datetime.now(timezone.utc)
        self.health = {
            "state": state,
            "reason_code": reason_code,
            "updated_at": now.isoformat(),
            "cooldown_until": (
                (now + timedelta(seconds=cooldown_seconds)).isoformat()
                if cooldown_seconds
                else None
            ),
        }


def _coordinator(enabled=True):
    db = _Db()
    coordinator = HDEncodeTrafficCoordinator()
    coordinator._MIN_START_INTERVAL = 0.01
    coordinator._HEALTH_CACHE_SECONDS = 0
    coordinator.configure({"hdencode_enabled": enabled}, db)
    return coordinator, db



def test_unconfigured_coordinator_preserves_legacy_default_access():
    coordinator = HDEncodeTrafficCoordinator()
    coordinator._MIN_START_INTERVAL = 0

    with coordinator.request("detail"):
        assert require_transport_authorization() == "detail"

    assert coordinator.snapshot()["enabled"] is True


def test_partial_config_without_switch_preserves_legacy_default_access():
    coordinator = HDEncodeTrafficCoordinator()
    coordinator._MIN_START_INTERVAL = 0
    coordinator.configure({"debug_mode": False}, None)

    with coordinator.request("detail"):
        assert require_transport_authorization() == "detail"


def test_new_application_context_clears_stale_local_cooldown():
    coordinator, _ = _coordinator()
    coordinator.observe_http_status(403)
    coordinator.observe_http_status(403)
    assert coordinator.observe_http_status(403).blocked is True

    # A new partial-config parser must not inherit another application's
    # process-local cooldown or DB reference.
    coordinator.configure({"debug_mode": False}, None)

    with coordinator.request("detail"):
        assert require_transport_authorization() == "detail"
    assert coordinator.snapshot()["block_streak"] == 0


def test_same_application_context_retains_shared_block_streak():
    config = {"hdencode_enabled": True}
    db = _Db()
    coordinator = HDEncodeTrafficCoordinator()
    coordinator._MIN_START_INTERVAL = 0
    coordinator._HEALTH_CACHE_SECONDS = 0
    coordinator.configure(config, db)
    coordinator.observe_http_status(403)
    coordinator.configure(config, db)
    coordinator.observe_http_status(403)

    assert coordinator.observe_http_status(403).blocked is True

def test_off_switch_denies_before_authorization():
    coordinator, _ = _coordinator(False)
    with pytest.raises(HDEncodeTrafficDenied) as caught:
        with coordinator.request("listing"):
            raise AssertionError("request body must not execute")
    assert caught.value.code == "source_disabled"
    assert coordinator.snapshot()["metrics"]["started"]["listing"] == 0


def test_transport_factory_requires_coordinator_authorization(monkeypatch):
    coordinator, _ = _coordinator()
    constructed = []
    monkeypatch.setattr(
        transport.cloudscraper,
        "create_scraper",
        lambda: constructed.append("created") or object(),
    )

    with pytest.raises(HDEncodeTrafficDenied) as caught:
        transport.create_source_http_client(hdencode=True)
    assert caught.value.code == "unauthorized_transport"
    assert constructed == []

    with coordinator.request("detail"):
        transport.create_source_http_client(hdencode=True)
        assert require_transport_authorization() == "detail"
    assert constructed == ["created"]


def test_non_hdencode_transport_remains_independent(monkeypatch):
    constructed = []
    monkeypatch.setattr(
        transport.cloudscraper,
        "create_scraper",
        lambda: constructed.append("created") or object(),
    )
    transport.create_source_http_client(hdencode=False)
    assert constructed == ["created"]


def test_three_block_responses_create_global_cooldown():
    coordinator, db = _coordinator()
    assert coordinator.observe_http_status(403).blocked is False
    assert coordinator.observe_http_status(503).blocked is False
    decision = coordinator.observe_http_status(403)
    assert decision.blocked is True
    assert db.failures[-1][:2] == ("cooldown", "http_403")
    with pytest.raises(HDEncodeTrafficDenied):
        with coordinator.request("selenium"):
            pass


def test_success_resets_block_streak():
    coordinator, db = _coordinator()
    coordinator.observe_http_status(403)
    coordinator.observe_http_status(200)
    assert coordinator.snapshot()["block_streak"] == 0
    assert db.successes == 1


def test_cancelled_waiter_never_receives_authorization():
    coordinator, _ = _coordinator()
    coordinator._semaphores["listing"].acquire()
    stop = threading.Event()
    outcomes = []

    def worker():
        try:
            with coordinator.request("listing", stop_requested=stop.is_set):
                outcomes.append("started")
        except HDEncodeRequestCancelled:
            outcomes.append("cancelled")

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.03)
    stop.set()
    thread.join(timeout=2)
    coordinator._semaphores["listing"].release()

    assert outcomes == ["cancelled"]
    assert coordinator.snapshot()["metrics"]["started"]["listing"] == 0


def test_request_starts_are_spaced_across_classes():
    coordinator, _ = _coordinator()
    coordinator._MIN_START_INTERVAL = 0.04
    starts = []
    lock = threading.Lock()

    def run(kind):
        with coordinator.request(kind):
            with lock:
                starts.append(time.monotonic())

    first = threading.Thread(target=run, args=("listing",))
    second = threading.Thread(target=run, args=("detail",))
    first.start()
    second.start()
    first.join()
    second.join()

    starts.sort()
    assert starts[1] - starts[0] >= 0.035


def test_expired_legacy_block_allows_one_probe():
    coordinator, db = _coordinator()
    db.health = {
        "state": "blocked",
        "reason_code": "legacy_block",
        "updated_at": (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat(),
    }
    with coordinator.request("listing"):
        pass
