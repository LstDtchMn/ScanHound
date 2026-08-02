"""Mutation proof that the netwatch gate actually fires.

NOT part of the normal suite — it deliberately attempts egress, so running it
under ``-p netwatch`` is expected to exit non-zero. It exists so "the guard is
green" can be distinguished from "the guard is broken".

Three cases, run as three separate pytest invocations (see the Phase 1
evidence block in the peer-round doc):

  no_egress          -> guard green, exit 0   (control: not unconditionally red)
  swallowed_egress   -> guard red,   exit 1   (the real failure mode)
  direct_egress      -> guard red,   exit 1   (naive case)

``swallowed_egress`` is the case that matters. It reproduces the application's
own behaviour — attempt a connection on a background thread and catch broad
``Exception`` — and asserts nothing. Under a guard that only raised, this test
would pass and the run would be green. Under this guard the run must still
exit non-zero.
"""
from __future__ import annotations

import socket
import threading

# A host that does not resolve, so the "control" case cannot accidentally
# depend on real DNS. The guard intercepts before resolution anyway.
_TARGET = ("netwatch-selftest.invalid", 443)


def test_no_egress():
    """Control. Must leave the guard green."""
    assert 1 + 1 == 2


def test_swallowed_egress():
    """Mimics the app: egress on a worker thread, exception swallowed.

    Deliberately asserts nothing about the connection. If the guard relied on
    the raised OSError to fail the run, this test would pass silently — which
    is exactly how the real leak hid.
    """
    def worker():
        try:
            socket.create_connection(_TARGET, timeout=1)
        except Exception:  # noqa: BLE001 - mirrors the application's handler
            pass

    t = threading.Thread(target=worker, name="netwatch-selftest-worker")
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()


def test_direct_egress():
    """Naive case: egress on the main thread, exception propagates."""
    try:
        socket.getaddrinfo(*_TARGET)
    except Exception:  # noqa: BLE001
        pass
