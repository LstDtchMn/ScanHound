"""N-1: one hung channel must not hide another channel's confirmed delivery.

The confirming path (NotificationBridge.notify_error_confirmed, used by the
database-corruption alert) waited on asyncio.gather over EVERY selected channel.
Webhook channels cap themselves at 10s inside _post_webhook, but EmailChannel
ran blocking smtplib with NO timeout on an executor thread, so a wedged SMTP
server could hold the gather open past the bridge's 15s budget while a Discord
webhook had already delivered in milliseconds. The bridge then reported "not
delivered" for an alert the operator had actually received -- which for the
corruption flag (a one-shot, consumed only on confirmed delivery) means a burnt
retry and a duplicate alert on the next boot, then eventual budget exhaustion.

So an unobserved success is exactly as damaging as a failed send, and these
tests are about OBSERVING, not just about sending.

Timing: the "never returning" channel awaits a long sleep, and assertions use a
wide margin (seconds) against it, so they are not sensitive to machine load.
"""

import asyncio
import json
import os
import tempfile
import threading
import time
from unittest.mock import patch

import pytest

from backend.database import corruption_flag_path, notify_db_corruption_once
from backend.notification_bridge import NotificationBridge
from backend.notifications import (
    DEFAULT_CHANNEL_SEND_TIMEOUT,
    DEFAULT_SMTP_TIMEOUT,
    EmailChannel,
    Notification,
    NotificationChannel,
    NotificationManager,
    NotificationType,
)

# Long enough to be "never" for a test that must finish in seconds, short enough
# that a leaked task cannot outlive the session.
NEVER = 30.0


# ===================================================================
# Channel doubles — the wire is the ONLY thing stubbed
# ===================================================================

class FastChannel(NotificationChannel):
    """Confirms immediately. Stands in for a healthy Discord webhook."""

    def __init__(self, name="fast", result=True):
        super().__init__(name)
        self._result = result
        self.sent = []

    async def send(self, notification):
        self.sent.append(notification)
        return self._result


class HungChannel(NotificationChannel):
    """Never returns. Stands in for EmailChannel against a wedged SMTP host."""

    def __init__(self, name="hung", delay=NEVER):
        super().__init__(name)
        self.delay = delay
        self.started = threading.Event()
        self.sent = []

    async def send(self, notification):
        self.sent.append(notification)
        self.started.set()
        await asyncio.sleep(self.delay)
        return True


class RaisingChannel(NotificationChannel):
    def __init__(self, name="raising"):
        super().__init__(name)

    async def send(self, notification):
        raise RuntimeError("smtp refused the connection")


class SlowButHealthyChannel(NotificationChannel):
    """Succeeds, just later than the fast channel. Used to prove the losers of
    the race are left running rather than cancelled."""

    def __init__(self, delay=0.4):
        super().__init__("slow")
        self.delay = delay
        self.completed = threading.Event()

    async def send(self, notification):
        await asyncio.sleep(self.delay)
        self.completed.set()
        return True


class _KwargRecordingSMTP:
    """Records how smtplib was constructed.

    Deliberately accepts host/port positionally and everything else by keyword
    ONLY: smtplib's third positional parameter is local_hostname, so an
    implementation that passed the timeout positionally would be silently
    setting the wrong field. That mistake raises TypeError here instead of
    passing.
    """

    calls = []

    def __init__(self, host, port, **kwargs):
        _KwargRecordingSMTP.calls.append(
            {"host": host, "port": port, "kwargs": kwargs})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, *a):
        pass


def _notification(type=NotificationType.ERROR, title="ScanHound Error",
                  message="boom"):
    return Notification(type=type, title=title, message=message)


@pytest.fixture
def bridge():
    b = NotificationBridge()
    b.configure({"desktop_notifications": False})
    b._manager.clear_channels()
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


# ===================================================================
# POSITIVE CONTROLS — the healthy paths must still work
# ===================================================================

def test_positive_control_a_single_healthy_channel_confirms(bridge):
    """If the feature were disabled (no confirmation ever returned) this fails
    first."""
    fast = FastChannel()
    bridge._manager.add_channel(fast)

    assert bridge.notify_error_confirmed("boom") is True
    assert len(fast.sent) == 1


def test_positive_control_default_mode_still_waits_for_and_counts_all(bridge):
    """Ordinary (non-confirming) sends must still reach EVERY channel and report
    the full count. A fix that made first-success the global behaviour would
    return 1 here and silently stop aggregating."""
    a, b = FastChannel("a"), FastChannel("b")
    bridge._manager.add_channel(a)
    bridge._manager.add_channel(b)

    delivered = asyncio.run(
        bridge._manager._send_notification(_notification()))

    assert delivered == 2
    assert len(a.sent) == 1 and len(b.sent) == 1


def test_positive_control_email_channel_still_sends_successfully():
    """The SMTP timeout must not have broken the send itself."""
    _KwargRecordingSMTP.calls = []
    channel = EmailChannel(
        smtp_host="smtp.example.com", smtp_port=587,
        username="u", password="p",
        from_addr="scanhound@example.com", to_addrs="jesse@example.com")

    with patch("backend.notifications.smtplib.SMTP", _KwargRecordingSMTP):
        assert asyncio.run(channel.send(_notification())) is True
    assert len(_KwargRecordingSMTP.calls) == 1


# ===================================================================
# The finding: a fast success is observable despite a hung channel
# ===================================================================

def test_fast_success_is_observed_despite_a_never_returning_channel(bridge):
    """THE test the review asked for: one fast-success channel plus one
    never-returning channel."""
    hung = HungChannel()
    fast = FastChannel()
    bridge._manager.add_channel(hung)
    bridge._manager.add_channel(fast)

    started = time.monotonic()
    confirmed = bridge.notify_error_confirmed("boom", timeout=10.0)
    elapsed = time.monotonic() - started

    assert confirmed is True, (
        "a delivery that already succeeded was reported as undelivered")
    # The hung channel sleeps NEVER seconds; anything near that means we are
    # still waiting for it.
    assert elapsed < 3.0, f"waited {elapsed:.1f}s on the hung channel"
    assert len(fast.sent) == 1


def test_the_hung_channel_was_actually_started(bridge):
    """Guards the test above from passing for the wrong reason: if the hung
    channel were never dispatched at all there would be nothing to race."""
    hung = HungChannel()
    bridge._manager.add_channel(hung)
    bridge._manager.add_channel(FastChannel())

    assert bridge.notify_error_confirmed("boom", timeout=10.0) is True
    assert hung.started.wait(timeout=5.0), "the hung channel never ran"


def test_channel_order_does_not_matter(bridge):
    """Fast channel registered FIRST rather than second. Both orders must work
    -- an implementation that only polled the last task would pass one."""
    fast = FastChannel()
    bridge._manager.add_channel(fast)
    bridge._manager.add_channel(HungChannel())

    started = time.monotonic()
    assert bridge.notify_error_confirmed("boom", timeout=10.0) is True
    assert time.monotonic() - started < 3.0


def test_the_slower_channel_still_delivers_after_the_early_return(bridge):
    """The losers of the race are abandoned, NOT cancelled.

    Disagreeing case: cancelling the pending sends on first success would pass
    every other test here while silently dropping the email copy of an alert
    the operator asked to receive.
    """
    fast = FastChannel()
    slow = SlowButHealthyChannel(delay=0.4)
    bridge._manager.add_channel(fast)
    bridge._manager.add_channel(slow)

    assert bridge.notify_error_confirmed("boom", timeout=10.0) is True
    assert not slow.completed.is_set(), (
        "the slow channel finished first; the race did not happen")
    assert slow.completed.wait(timeout=5.0), (
        "the slower channel was cancelled instead of being left to deliver")


def test_an_exception_from_one_channel_does_not_hide_another_success(bridge):
    bridge._manager.add_channel(RaisingChannel())
    bridge._manager.add_channel(FastChannel())

    assert bridge.notify_error_confirmed("boom", timeout=10.0) is True


# ===================================================================
# Disagreeing cases — a wrong "first to finish" fix passes the above
# ===================================================================

def test_first_completion_is_not_a_confirmation(bridge):
    """A channel that finishes FAST with a refusal must not be read as success.

    An implementation that returned on the first task to COMPLETE (rather than
    the first to confirm) passes every test above and fails here: the refusing
    channel completes in microseconds.
    """
    bridge._manager.add_channel(FastChannel("refuser", result=False))
    bridge._manager.add_channel(HungChannel())

    assert bridge.notify_error_confirmed("boom", timeout=1.5) is False


def test_a_refusal_plus_a_success_still_confirms(bridge):
    """The mirror of the case above, so it cannot be satisfied by always
    returning False."""
    bridge._manager.add_channel(FastChannel("refuser", result=False))
    bridge._manager.add_channel(FastChannel("real", result=True))

    assert bridge.notify_error_confirmed("boom", timeout=10.0) is True


def test_only_hung_channels_means_undelivered_not_hung_forever(bridge):
    """No confirmation exists, so the bridge must fall back to its own budget
    and answer False -- promptly, and without raising."""
    bridge._manager.add_channel(HungChannel())

    started = time.monotonic()
    assert bridge.notify_error_confirmed("boom", timeout=1.0) is False
    elapsed = time.monotonic() - started
    assert 0.5 < elapsed < 5.0, f"returned after {elapsed:.1f}s"


# ===================================================================
# Every channel send is individually bounded
# ===================================================================

def test_a_hung_channel_is_bounded_and_a_timeout_is_not_a_success():
    """Without a per-channel ceiling this call never returns."""
    mgr = NotificationManager()
    mgr.add_channel(HungChannel())

    started = time.monotonic()
    delivered = asyncio.run(mgr._send_notification(
        _notification(), channel_timeout=0.2))
    elapsed = time.monotonic() - started

    assert delivered == 0, "a timed-out channel was counted as delivered"
    assert elapsed < 5.0, f"the gather was not bounded ({elapsed:.1f}s)"


def test_a_hung_channel_does_not_starve_a_healthy_one_in_default_mode():
    """Even the non-confirming path must run channels concurrently: the healthy
    channel's success has to be counted despite the hung channel's ceiling."""
    mgr = NotificationManager()
    hung = HungChannel()
    fast = FastChannel()
    mgr.add_channel(hung)
    mgr.add_channel(fast)

    delivered = asyncio.run(mgr._send_notification(
        _notification(), channel_timeout=0.2))

    assert delivered == 1
    assert len(fast.sent) == 1


def test_the_default_channel_ceiling_is_finite():
    """A None/0 ceiling would restore the unbounded behaviour."""
    assert DEFAULT_CHANNEL_SEND_TIMEOUT and DEFAULT_CHANNEL_SEND_TIMEOUT > 0


# ===================================================================
# The root cause: EmailChannel had no SMTP timeout
# ===================================================================

def test_smtp_is_constructed_with_an_explicit_positive_timeout():
    _KwargRecordingSMTP.calls = []
    channel = EmailChannel(
        smtp_host="smtp.example.com", smtp_port=587,
        username="u", password="p",
        from_addr="a@x", to_addrs="b@x", use_tls=True)

    with patch("backend.notifications.smtplib.SMTP", _KwargRecordingSMTP):
        asyncio.run(channel.send(_notification()))

    kwargs = _KwargRecordingSMTP.calls[0]["kwargs"]
    assert "timeout" in kwargs, (
        "smtplib inherits socket.getdefaulttimeout() (None) without this, so a "
        "wedged server blocks an executor thread for the OS TCP timeout")
    assert isinstance(kwargs["timeout"], (int, float))
    assert kwargs["timeout"] > 0


def test_the_ssl_branch_also_gets_the_timeout():
    """Disagreeing case: fixing only the starttls branch passes the test above
    and leaves smtp_tls=False installs unbounded."""
    _KwargRecordingSMTP.calls = []
    channel = EmailChannel(
        smtp_host="smtp.example.com", smtp_port=465,
        username="u", password="p",
        from_addr="a@x", to_addrs="b@x", use_tls=False)

    with patch("backend.notifications.smtplib.SMTP_SSL", _KwargRecordingSMTP):
        asyncio.run(channel.send(_notification()))

    assert _KwargRecordingSMTP.calls[0]["kwargs"].get("timeout", 0) > 0


def test_an_email_channel_built_from_config_is_bounded():
    mgr = NotificationManager()
    mgr.configure_from_dict({
        "email_enabled": True,
        "smtp_host": "smtp.example.com",
        "email_from": "a@x",
        "email_to": "b@x",
    })

    email = [c for c in mgr._channels if c.name == "email"][0]
    assert email.timeout and email.timeout > 0


@pytest.mark.parametrize("bad", [0, None, -5, ""])
def test_an_unusable_configured_timeout_falls_back_to_the_default(bad):
    """smtplib REJECTS timeout=0 (non-blocking sockets unsupported) and None
    restores the unbounded default, so neither may reach it. A fix that passed
    the config value straight through would fail here."""
    channel = EmailChannel(
        smtp_host="h", smtp_port=587, username="u", password="p",
        from_addr="a@x", to_addrs="b@x", timeout=bad)

    assert channel.timeout == DEFAULT_SMTP_TIMEOUT


# ===================================================================
# notify_error() must stay fire-and-forget (it is on the hot path)
# ===================================================================

def test_notify_error_stays_fire_and_forget_even_with_a_hung_channel(bridge):
    bridge._manager.add_channel(HungChannel())

    started = time.monotonic()
    assert bridge.notify_error("boom") is None
    assert time.monotonic() - started < 1.0, (
        "the non-confirming path became blocking")


# ===================================================================
# The consumer: the corruption flag, end to end
# ===================================================================

def test_the_flag_is_consumed_when_one_channel_confirms_despite_a_hung_one(
        bridge, flagged_db):
    """The whole chain -- real bridge, real manager, real database function.

    Before the fix the hung channel ate the bridge's budget, the corruption
    alert came back "unconfirmed", and the flag was kept for a duplicate alert
    on the next boot even though the operator had already been told.
    """
    bridge._manager.add_channel(HungChannel())
    fast = FastChannel()
    bridge._manager.add_channel(fast)

    started = time.monotonic()
    assert notify_db_corruption_once(flagged_db, bridge) is True
    assert time.monotonic() - started < 5.0

    assert not os.path.exists(corruption_flag_path(flagged_db)), (
        "a confirmed alert must consume the one-shot flag")
    notified = f"{flagged_db}.corrupt_flag.notified.json"
    assert os.path.exists(notified)
    with open(notified, encoding="utf-8") as f:
        assert json.load(f)["delivery_confirmed"] is True
    assert len(fast.sent) == 1


def test_the_flag_survives_when_nothing_confirms(bridge, flagged_db):
    """The other direction: a genuinely undeliverable alert must still keep the
    evidence. Proves the fix did not buy speed by inventing confirmations."""
    bridge._manager.add_channel(FastChannel(result=False))

    notify_db_corruption_once(flagged_db, bridge)

    assert os.path.exists(corruption_flag_path(flagged_db)), (
        "total history loss was marked notified without anything being sent")
