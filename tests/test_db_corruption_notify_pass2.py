"""Audit pass 2, finding #19 — notify_db_corruption_once must consume the
on-disk corruption flag on evidence of DELIVERY, not on the mere act of
dispatching, while staying fire-once-per-event across restarts.

A quarantine means the whole download history was thrown away, and the
notification is the only push signal that it happened. The previous code
renamed the flag to .notified.json immediately after a fire-and-forget
dispatch — even when the bridge was None and nothing at all was attempted —
so a container that restarted a second later lost the alert permanently.
"""
import json
import os
from concurrent.futures import Future

import pytest

from backend.database import (
    CORRUPTION_NOTIFY_MAX_ATTEMPTS,
    corruption_flag_path,
    db_corruption_flag_present,
    notify_db_corruption_once,
)

QUARANTINE_RECORD = {
    "detected_at": "2026-08-04T09:00:00",
    "backup_path": "/dbvol/crawler.db.corrupt.1785886652",
    "error": "database disk image is malformed",
}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "crawler.db")


@pytest.fixture
def flag(db_path):
    """A fresh, un-notified corruption flag, shaped like the real one written
    by DatabaseManager._write_corruption_flag."""
    path = corruption_flag_path(db_path)
    payload = dict(QUARANTINE_RECORD, db_path=db_path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def notified_path(db_path):
    return f"{db_path}.corrupt_flag.notified.json"


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class _Bridge:
    """Minimal stand-in for NotificationBridge.

    Deliberately NOT a MagicMock: an auto-speccing mock returns a truthy
    object from notify_error, which is exactly the ambiguity under test here.
    """

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.calls = []

    def notify_error(self, message):
        self.calls.append(message)
        if self._raises is not None:
            raise self._raises
        return self._result


class TestConfirmedDelivery:
    def test_confirmed_alert_consumes_the_flag_on_the_first_attempt(self, db_path, flag):
        """POSITIVE CONTROL: the healthy path still fires exactly once and
        then goes quiet. A "fix" that just stops consuming the flag — or that
        stops notifying at all — fails here."""
        bridge = _Bridge(result=True)

        assert db_corruption_flag_present(db_path) is True
        assert notify_db_corruption_once(db_path, bridge) is True

        assert len(bridge.calls) == 1
        assert "corruption" in bridge.calls[0].lower()

        assert not os.path.exists(flag)
        assert db_corruption_flag_present(db_path) is False
        record = read_json(notified_path(db_path))
        assert record["delivery_confirmed"] is True
        assert record["notify_attempts"] == 1

        # Fire-once: a later startup finds nothing to do and stays silent.
        bridge2 = _Bridge(result=True)
        assert notify_db_corruption_once(db_path, bridge2) is False
        assert bridge2.calls == []

    def test_consumed_flag_keeps_the_original_quarantine_record(self, db_path, flag):
        """The .notified.json is the permanent on-disk record of the incident;
        the attempt bookkeeping must be added to it, not written over it."""
        assert notify_db_corruption_once(db_path, _Bridge(result=True)) is True

        record = read_json(notified_path(db_path))
        for key, value in QUARANTINE_RECORD.items():
            assert record[key] == value

    def test_no_flag_present_is_a_noop(self, db_path):
        bridge = _Bridge(result=True)
        assert db_corruption_flag_present(db_path) is False
        assert notify_db_corruption_once(db_path, bridge) is False
        assert bridge.calls == []
        assert not os.path.exists(notified_path(db_path))


class TestUnconfirmedDispatchIsNotDelivery:
    def test_fire_and_forget_bridge_keeps_the_flag(self, db_path, flag):
        """The stock NotificationBridge.notify_error returns None — it has
        only scheduled the send on a daemon loop. That is dispatch, not
        delivery, so the retry token must survive."""
        bridge = _Bridge(result=None)

        assert notify_db_corruption_once(db_path, bridge) is True

        assert len(bridge.calls) == 1
        assert os.path.exists(flag)
        assert not os.path.exists(notified_path(db_path))
        # Health signal: /rename/health can finally observe the quarantine,
        # which the old rename-before-first-request ordering made impossible.
        assert db_corruption_flag_present(db_path) is True
        assert read_json(flag)["notify_attempts"] == 1
        assert read_json(flag)["delivery_confirmed"] is False

    def test_none_bridge_keeps_the_flag(self, db_path, flag):
        """Nothing was even attempted, so nothing may be marked notified."""
        assert notify_db_corruption_once(db_path, None) is True
        assert os.path.exists(flag)
        assert not os.path.exists(notified_path(db_path))
        assert read_json(flag)["notify_attempts"] == 1

    def test_raising_bridge_keeps_the_flag_and_does_not_propagate(self, db_path, flag):
        bridge = _Bridge(raises=RuntimeError("bridge down"))

        assert notify_db_corruption_once(db_path, bridge) is True  # must not raise

        assert os.path.exists(flag)
        assert not os.path.exists(notified_path(db_path))
        assert read_json(flag)["notify_attempts"] == 1

    def test_truthy_non_true_return_is_not_a_confirmation(self, db_path, flag):
        """DISAGREEING CASE. A plausible wrong fix writes `if bridge.notify_
        error(msg):` — truthiness rather than an explicit True. It passes
        every other test in this file, and then reintroduces the original bug
        the moment the bridge returns its pending Future (truthy, undelivered),
        which is precisely what a fire-and-forget send hands back."""
        pending = Future()
        assert bool(pending) is True  # pins the premise of this test

        assert notify_db_corruption_once(db_path, _Bridge(result=pending)) is True

        assert os.path.exists(flag)
        assert not os.path.exists(notified_path(db_path))


class TestBoundedRetries:
    def test_retries_stop_after_the_budget_so_a_broken_bridge_cannot_spam(
            self, db_path, flag):
        """Fire-once-per-event across restarts: each startup retries, but a
        permanently broken bridge is silenced after the budget rather than
        alerting on every boot forever."""
        bridge = _Bridge(result=None)

        for attempt in range(1, CORRUPTION_NOTIFY_MAX_ATTEMPTS):
            assert notify_db_corruption_once(db_path, bridge) is True
            assert os.path.exists(flag), f"gave up early on attempt {attempt}"

        # Final permitted attempt consumes the flag.
        assert notify_db_corruption_once(db_path, bridge) is True
        assert len(bridge.calls) == CORRUPTION_NOTIFY_MAX_ATTEMPTS
        assert not os.path.exists(flag)

        record = read_json(notified_path(db_path))
        assert record["delivery_confirmed"] is False
        assert record["notify_attempts"] == CORRUPTION_NOTIFY_MAX_ATTEMPTS

        # And every boot after that is silent.
        assert notify_db_corruption_once(db_path, bridge) is False
        assert len(bridge.calls) == CORRUPTION_NOTIFY_MAX_ATTEMPTS

    def test_attempt_count_is_read_from_disk_not_memory(self, db_path):
        """DISAGREEING CASE. An implementation that counted attempts in a
        module global or a per-call variable would restart the budget on every
        boot — the exact failure mode being guarded against, since the process
        is expected to die between attempts. Seeding the count on disk and
        asserting a SINGLE call exhausts it separates the two."""
        path = corruption_flag_path(db_path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(QUARANTINE_RECORD,
                           notify_attempts=CORRUPTION_NOTIFY_MAX_ATTEMPTS - 1), f)

        assert notify_db_corruption_once(db_path, _Bridge(result=None)) is True

        assert not os.path.exists(path)
        assert read_json(notified_path(db_path))["notify_attempts"] == (
            CORRUPTION_NOTIFY_MAX_ATTEMPTS)

    def test_confirmed_delivery_short_circuits_a_partly_spent_budget(self, db_path):
        """A confirmed alert consumes the flag immediately, even mid-budget —
        the budget is a give-up cap, not a required number of attempts."""
        path = corruption_flag_path(db_path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(QUARANTINE_RECORD, notify_attempts=1), f)

        assert notify_db_corruption_once(db_path, _Bridge(result=True)) is True

        assert not os.path.exists(path)
        assert read_json(notified_path(db_path))["delivery_confirmed"] is True

    def test_malformed_flag_still_terminates(self, db_path):
        """An unreadable flag must not become an infinite retry loop: the
        parse failure resets the count to zero once, then the rewritten,
        well-formed JSON carries the budget as normal."""
        path = corruption_flag_path(db_path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json at all")

        bridge = _Bridge(result=None)
        for _ in range(CORRUPTION_NOTIFY_MAX_ATTEMPTS):
            assert notify_db_corruption_once(db_path, bridge) is True

        assert not os.path.exists(path)
        assert notify_db_corruption_once(db_path, bridge) is False
        assert len(bridge.calls) == CORRUPTION_NOTIFY_MAX_ATTEMPTS

    def test_non_integer_attempt_count_does_not_crash(self, db_path):
        """Hand-edited or truncated flags happen; a junk counter reads as 0."""
        path = corruption_flag_path(db_path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(QUARANTINE_RECORD, notify_attempts="lots"), f)

        assert notify_db_corruption_once(db_path, _Bridge(result=None)) is True
        assert read_json(path)["notify_attempts"] == 1
