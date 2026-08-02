"""Enforcement gate: unauthorized outbound network access fails the test run.

Run with ``-p netwatch``.

This is the *enforcement* half of the pair. ``tests/tools/probe.py`` is the
*diagnostic* half: it blocks and records but lets the run stay green. This
module blocks, records, and forces a non-zero pytest exit status.

Why the exit status is forced rather than surfaced as a test failure: the
application catches broad ``Exception`` around its network calls, so an
``OSError`` raised at the socket boundary is swallowed and the run still
reports "296 passed". Measured 2026-08-02 — two blocked egress attempts to
hdencode.org produced zero test failures. An exception alone is therefore not
a usable signal; the ledger has to be checked out-of-band at session end.

Attribution caveat: ``observed_during_test`` is the pytest node that happened
to be running when a *leaked* worker made the attempt. It is NOT necessarily
the test that created that worker. The ``originating_operation`` field is
populated from the scan context bound to the calling thread, so it names the
owning scan directly rather than by parsing the thread name. It is None when no
scan owns that thread. Do not read the observed node as the culprit.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
import traceback
from dataclasses import dataclass, field

import pytest

# Loopback and non-inet targets are always permitted: in-process ASGI clients,
# unix sockets, and local fixture servers are legitimate.
_ALWAYS_ALLOWED = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "", "::"}

# Additional hosts a local fixture server may declare, comma-separated.
# Deliberately NOT a place to put hdencode.org — allowlisting the real target
# to make the suite green would encode the defect being hunted.
_ENV_ALLOW = "SCANHOUND_NETWATCH_ALLOW"

_STACK_FRAMES = 12


@dataclass
class _Attempt:
    kind: str
    host: str
    port: object
    thread_name: str
    thread_ident: object
    observed_during_test: str
    monotonic_ns: int = 0
    # Read from the scan context bound to THIS thread, not parsed out of the
    # thread name. None when no scan owns the calling thread.
    originating_operation: object = None
    originating_origin: object = None
    stack: list = field(default_factory=list)


class UnauthorizedEgress(OSError):
    """Raised at the socket boundary to stop the call.

    Subclasses OSError so application code that catches OSError/Exception
    behaves as it would against a real network failure. The run is failed via
    the ledger, not via this exception.
    """


_LEDGER: list = []
_LOCK = threading.Lock()
_CURRENT = {"nodeid": "<session start, before any test>"}
_INSTALLED = {"done": False}


def _allowed_hosts() -> set:
    extra = os.environ.get(_ENV_ALLOW, "")
    return _ALWAYS_ALLOWED | {h.strip() for h in extra.split(",") if h.strip()}


def _is_ip_literal(host) -> bool:
    """True when *host* is a numeric address rather than a name to resolve."""
    try:
        ipaddress.ip_address(str(host).strip("[]"))
    except ValueError:
        return False
    return True


def _current_operation():
    """Ask scan_context which operation owns this thread, if any."""
    try:
        from backend import scan_context
    except Exception:
        return None, None
    op = scan_context.current_operation()
    if op is None:
        return None, None
    return op.scan_uuid, op.origin


def _record(kind: str, host, port) -> _Attempt:
    uuid_, origin = _current_operation()
    attempt = _Attempt(
        kind=kind,
        host=str(host),
        port=port,
        thread_name=threading.current_thread().name,
        thread_ident=threading.current_thread().ident,
        observed_during_test=_CURRENT["nodeid"],
        monotonic_ns=time.monotonic_ns(),
        originating_operation=uuid_,
        originating_origin=origin,
        stack=traceback.format_stack(limit=_STACK_FRAMES),
    )
    with _LOCK:
        _LEDGER.append(attempt)
    return attempt


def pytest_configure(config):
    """Install the socket guard as early as possible."""
    if _INSTALLED["done"]:
        return
    _INSTALLED["done"] = True

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _split(address):
        if isinstance(address, tuple) and address:
            return address[0], (address[1] if len(address) > 1 else None)
        # AF_UNIX and anything else non-inet.
        return address, None

    def guarded_connect(self, address, *a, **kw):
        host, port = _split(address)
        if not isinstance(address, tuple) or str(host) in _allowed_hosts():
            return real_connect(self, address, *a, **kw)
        _record("connect", host, port)
        raise UnauthorizedEgress(f"blocked outbound connect to {host}:{port}")

    def guarded_connect_ex(self, address, *a, **kw):
        host, port = _split(address)
        if not isinstance(address, tuple) or str(host) in _allowed_hosts():
            return real_connect_ex(self, address, *a, **kw)
        _record("connect_ex", host, port)
        raise UnauthorizedEgress(f"blocked outbound connect_ex to {host}:{port}")

    def guarded_create_connection(address, *a, **kw):
        host, port = _split(address)
        if str(host) in _allowed_hosts():
            return real_create_connection(address, *a, **kw)
        _record("create_connection", host, port)
        raise UnauthorizedEgress(f"blocked create_connection to {host}:{port}")

    def guarded_getaddrinfo(host, port, *a, **kw):
        if str(host) in _allowed_hosts():
            return real_getaddrinfo(host, port, *a, **kw)
        if _is_ip_literal(host):
            # A numeric literal resolves locally — getaddrinfo parses it and
            # issues no DNS query, so nothing leaves the host and there is
            # nothing to block. Connecting to it is still blocked below.
            #
            # This is not a nicety: the app's own SSRF protection resolves a
            # configured webhook to decide whether it points at a private
            # range. Blocking that lookup breaks the security check the test
            # suite asserts on (test_ssrf_rejects_private_discord_webhook),
            # which is how this was found.
            return real_getaddrinfo(host, port, *a, **kw)
        _record("dns", host, port)
        raise UnauthorizedEgress(f"blocked DNS lookup for {host}")

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection
    socket.getaddrinfo = guarded_getaddrinfo


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Track which node is running, including the gaps between tests."""
    _CURRENT["nodeid"] = item.nodeid
    yield
    # Attempts landing here came from a worker that outlived the test body.
    _CURRENT["nodeid"] = f"<between tests, after {item.nodeid}>"


def pytest_sessionfinish(session, exitstatus):
    """Force a non-zero exit when the ledger is non-empty.

    The application swallows the OSError, so per-test failures cannot be
    relied on. This is the signal.
    """
    with _LOCK:
        empty = not _LEDGER
    if empty:
        return
    if session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    with _LOCK:
        ledger = list(_LEDGER)
    if not ledger:
        terminalreporter.write_sep(
            "=", "netwatch: no unauthorized egress", green=True)
        return

    terminalreporter.write_sep(
        "=", f"netwatch: {len(ledger)} UNAUTHORIZED EGRESS ATTEMPT(S)", red=True
    )
    terminalreporter.write_line(
        "Run failed by netwatch, not by an assertion: the application catches "
        "the socket error, so no test reports it."
    )
    terminalreporter.write_line(
        "'observed during' is where the attempt LANDED, not necessarily the "
        "test that leaked the worker."
    )
    by_host: dict = {}
    for a in ledger:
        by_host[a.host] = by_host.get(a.host, 0) + 1
    for host, count in sorted(by_host.items(), key=lambda kv: -kv[1]):
        terminalreporter.write_line(f"  {count:5d}  {host}")
    terminalreporter.write_line("")
    for a in ledger:
        terminalreporter.write_line(f"  {a.kind:18s} {a.host}:{a.port}")
        terminalreporter.write_line(f"      thread:          {a.thread_name}")
        terminalreporter.write_line(
            f"      observed during: {a.observed_during_test}")
        terminalreporter.write_line(
            f"      monotonic_ns:    {a.monotonic_ns}")
        terminalreporter.write_line(
            f"      originating op:  {a.originating_operation} "
            f"origin={a.originating_origin}")
