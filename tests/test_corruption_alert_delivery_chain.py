"""The corruption alert's "confirmed delivered" must be reachable in production.

Finding #19's fix consumes the database-corruption flag only on CONFIRMED
delivery. That is only an improvement if confirmation can actually happen: if
the real bridge can never report success, the retry budget is spent on every
install and one incident becomes three duplicate alerts before the flag is
discarded unconfirmed anyway -- worse than the bug being fixed.

Every other test of this path uses a fake bridge that returns True, a value the
production object did not previously produce (`send()` was fire-and-forget and
returned None on every path). So those tests could all pass while the real
chain stayed broken. This file drives the REAL NotificationBridge and the REAL
NotificationManager, stubbing only the outermost thing -- the channel, which
is what actually talks to Discord/SMTP/Pushover.
"""

import json
import os
import tempfile

import pytest

from backend.database import (
    corruption_flag_path,
    notify_db_corruption_once,
)
from backend.notification_bridge import NotificationBridge
from backend.notifications import NotificationChannel


class RecordingChannel(NotificationChannel):
    """The outermost boundary: stands in for the wire, nothing else."""

    def __init__(self, name="recording", result=True):
        super().__init__(name)
        self._result = result
        self.sent = []

    def should_handle(self, notification):
        return True

    async def send(self, notification):
        self.sent.append(notification)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.fixture
def bridge():
    b = NotificationBridge()
    b.configure({"desktop_notifications": False})
    yield b
    try:
        b.shutdown()
    except Exception:
        pass


@pytest.fixture
def flagged_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    with open(corruption_flag_path(path), "w", encoding="utf-8") as f:
        json.dump({"detected_at": "x", "db_path": path}, f)
    yield path
    for suffix in ("", "-wal", "-shm", ".corrupt_flag.json",
                   ".corrupt_flag.notified.json"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _install(bridge, channel):
    bridge._manager.clear_channels()
    bridge._manager.add_channel(channel)


def test_a_real_bridge_can_actually_confirm_delivery(bridge):
    """The load-bearing one. If this fails the whole fix is theatre."""
    _install(bridge, RecordingChannel(result=True))

    assert bridge.notify_error_confirmed("boom") is True
    assert len(bridge._manager._channels[0].sent) == 1


def test_a_channel_that_refuses_is_not_a_confirmation(bridge):
    """Disagreeing case: a fix returning True on dispatch would pass the test
    above and fail this one."""
    _install(bridge, RecordingChannel(result=False))

    assert bridge.notify_error_confirmed("boom") is False


def test_a_channel_that_raises_is_not_a_confirmation(bridge):
    _install(bridge, RecordingChannel(result=RuntimeError("smtp down")))

    assert bridge.notify_error_confirmed("boom") is False


def test_no_channels_configured_is_not_a_confirmation(bridge):
    """"Nobody was listening" must not read as "the operator was told"."""
    bridge._manager.clear_channels()

    assert bridge.notify_error_confirmed("boom") is False


def test_an_unconfigured_bridge_is_not_a_confirmation():
    assert NotificationBridge().notify_error_confirmed("boom") is False


def test_the_flag_is_consumed_end_to_end_through_the_real_bridge(
        bridge, flagged_db):
    """The whole chain: real bridge, real manager, real database function."""
    _install(bridge, RecordingChannel(result=True))

    assert notify_db_corruption_once(flagged_db, bridge) is True

    assert not os.path.exists(corruption_flag_path(flagged_db)), (
        "a confirmed alert must consume the flag")
    assert os.path.exists(f"{flagged_db}.corrupt_flag.notified.json")
    assert len(bridge._manager._channels[0].sent) == 1


def test_the_flag_survives_a_real_undeliverable_alert(bridge, flagged_db):
    """The finding itself, end to end: nothing delivered, evidence kept."""
    _install(bridge, RecordingChannel(result=False))

    notify_db_corruption_once(flagged_db, bridge)

    assert os.path.exists(corruption_flag_path(flagged_db)), (
        "total history loss was marked notified without anything being sent")


def test_ordinary_notifications_stay_fire_and_forget(bridge):
    """notify_error must NOT have become blocking -- it is on the hot path.

    A fix that made every notification wait for an SMTP round trip would pass
    every other test in this file.
    """
    _install(bridge, RecordingChannel(result=True))

    assert bridge.notify_error("boom") is None
