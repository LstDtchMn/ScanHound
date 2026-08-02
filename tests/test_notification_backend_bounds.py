"""Notification backends must bound their own blocking calls.

These dispatch inside the notification loop's DEFAULT EXECUTOR, and a
permanently blocked executor callable is not merely a stranded daemon thread:
``concurrent.futures.thread`` registers every worker and ``_python_exit`` joins
them at interpreter shutdown irrespective of the daemon flag. So an unreachable
mail server can hold the whole process open, and no amount of shutdown-side
bounding fixes it — the operation itself has to have a bound.

Measured 2026-08-02 on 3.12.9:
    plain daemon thread wedged  -> process exits, code 0
    executor worker wedged      -> process does NOT exit
"""
import socket
import threading
import time

import pytest

from backend.notifications import (
    EmailChannel,
    Notification,
    NotificationType,
    SMTP_TIMEOUT_SECONDS,
)


class _BlackHoleSMTP:
    """Accepts the TCP connection, then never sends a greeting.

    A closed port would raise ConnectionRefused immediately and prove nothing;
    the failure that matters is a server that accepts and then goes silent, so
    the client blocks in recv() forever unless a socket timeout is set.
    """

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._held = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._held.append(conn)   # accepted, deliberately never answered

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        for c in self._held:
            try:
                c.close()
            except OSError:
                pass
        self._sock.close()


@pytest.fixture
def black_hole():
    server = _BlackHoleSMTP()
    try:
        yield server
    finally:
        server.close()


def test_smtp_dispatch_is_bounded_by_its_timeout(black_hole):
    """Without timeout=, smtplib waits on socket._GLOBAL_DEFAULT_TIMEOUT: forever."""
    channel = EmailChannel(
        smtp_host="127.0.0.1",
        smtp_port=black_hole.port,
        username="u",
        password="p",
        from_addr="from@example.com",
        to_addrs=["to@example.com"],
        use_tls=True,
        timeout=1.0,
    )
    note = Notification(
        type=NotificationType.INFO, title="t", message="m")

    began = time.monotonic()
    with pytest.raises(Exception):
        channel._send_sync(note)
    elapsed = time.monotonic() - began

    # Bounded by the 1s timeout, with slack for teardown. Unbounded would hang
    # until the test runner is killed.
    assert elapsed < 15.0, f"SMTP dispatch blocked for {elapsed:.1f}s"


def test_smtp_timeout_reaches_the_socket(black_hole):
    """The value must actually be applied, not merely stored on the channel."""
    channel = EmailChannel(
        smtp_host="127.0.0.1", smtp_port=black_hole.port,
        username="u", password="p", from_addr="f@example.com",
        to_addrs=["t@example.com"], use_tls=True, timeout=0.5)
    note = Notification(type=NotificationType.INFO, title="t", message="m")

    began = time.monotonic()
    with pytest.raises(Exception):
        channel._send_sync(note)
    short = time.monotonic() - began

    channel.timeout = 3.0
    began = time.monotonic()
    with pytest.raises(Exception):
        channel._send_sync(note)
    longer = time.monotonic() - began

    # A stored-but-unused timeout would make these two indistinguishable.
    assert longer > short + 1.0, (
        f"timeout not reaching the socket: {short:.1f}s vs {longer:.1f}s")


def test_default_timeout_is_set_and_finite():
    """A None/absent default would silently restore the block-forever behaviour."""
    assert SMTP_TIMEOUT_SECONDS is not None
    assert 0 < SMTP_TIMEOUT_SECONDS < 120

    channel = EmailChannel(
        smtp_host="h", smtp_port=1, username="u", password="p",
        from_addr="f@example.com", to_addrs=["t@example.com"])
    assert channel.timeout == SMTP_TIMEOUT_SECONDS
