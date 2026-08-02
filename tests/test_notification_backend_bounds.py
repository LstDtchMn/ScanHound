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
import pathlib
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


# ── Process-level contract ────────────────────────────────────────────────
# The bounded-socket tests above prove smtplib returns. They do NOT prove the
# interpreter can exit, which is the property that actually matters: a blocked
# executor callable is joined by concurrent.futures' _python_exit regardless of
# any daemon flag. Only a child process can demonstrate that, so this asserts a
# POSITIVE contract — SMTP cannot hold exit open — rather than pinning a defect.

_CHILD = r'''
import sys, time
from backend.notification_bridge import NotificationBridge

host, port = sys.argv[1], int(sys.argv[2])
bridge = NotificationBridge()
bridge.configure({
    "desktop_notifications": False,     # keep the unbounded plyer path out of it
    "email_enabled": True,
    "smtp_host": host, "smtp_port": port,
    "smtp_username": "u", "smtp_password": "p",
    "email_from": "f@example.com", "email_to": ["t@example.com"],
    "smtp_tls": True,
    "smtp_timeout": 1.0,
})
bridge.send("info", "t", "m")
time.sleep(1.0)                          # let the executor callable reach SMTP
print("DISPATCHED", flush=True)
bridge.shutdown()
print("SHUTDOWN-RETURNED", flush=True)
'''


def test_a_blocked_smtp_send_cannot_hold_interpreter_exit(black_hole, tmp_path):
    """A live SMTP executor operation must not stop the process terminating.

    The child connects to a server that accepts and never replies, so the
    executor worker is genuinely blocked in SMTP when shutdown runs — not
    cancelled before it started. If smtp_timeout stops reaching the deployed
    channel (it is forwarded through NotificationBridge.configure), the worker
    blocks forever and _python_exit refuses to let the child exit.
    """
    import subprocess
    import sys as _sys

    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")

    import os
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    # Running a script BY PATH puts the script's dir on sys.path, not cwd,
    # so the repo root has to be passed explicitly for `backend` to import.
    env = dict(os.environ, PYTHONPATH=str(repo_root))
    proc = subprocess.Popen(
        [_sys.executable, str(script), "127.0.0.1", str(black_hole.port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(repo_root), env=env,
    )
    began = time.monotonic()
    try:
        # Deliberately tight. The child's smtp_timeout is 1s, so a wired build
        # finishes in a few seconds. If smtp_timeout stops reaching the deployed
        # channel, the socket falls back to the 30s default and blows this
        # budget — which is how this test enforces the BRIDGE PROPAGATION, not
        # merely the existence of a timeout somewhere. Verified by mutation: at
        # a 45s budget the unwired build still passed, in 31s.
        out, err = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()                      # never let this hang the suite
        proc.communicate()
        raise AssertionError(
            "child did not exit: a blocked SMTP send held interpreter exit open")

    assert "DISPATCHED" in out, f"child never dispatched.\nout={out}\nerr={err}"
    assert "SHUTDOWN-RETURNED" in out, f"shutdown() did not return.\nerr={err}"
    assert proc.returncode == 0, f"child exited {proc.returncode}\nerr={err}"
    elapsed = time.monotonic() - began
    assert elapsed < 15.0, f"child took {elapsed:.1f}s"
