"""Pytest plugin: record every outbound socket connect, block non-loopback.

Answers "does the suite attempt real egress, from which thread, during which
test" without actually sending traffic to third parties. Doubles as a
prototype of the conftest network guard.
"""
import socket
import threading

import pytest

_CURRENT = {"nodeid": "<session>"}
_SEEN = []
_LOCK = threading.Lock()

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


class BlockedEgress(OSError):
    pass


def _record(host, port):
    entry = (
        str(host),
        port,
        threading.current_thread().name,
        _CURRENT["nodeid"],
    )
    with _LOCK:
        _SEEN.append(entry)
    return entry


def pytest_sessionstart(session):
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
        if str(host) in _LOOPBACK:
            return real_connect(self, address, *a, **kw)
        entry = _record(host, port)
        print(f"\nEGRESS connect {entry[0]}:{entry[1]} "
              f"thread={entry[2]} test={entry[3]}", flush=True)
        raise BlockedEgress(f"blocked outbound connect to {host}:{port}")

    def guarded_getaddrinfo(host, port, *a, **kw):
        if str(host) in _LOOPBACK:
            return real_getaddrinfo(host, port, *a, **kw)
        entry = _record(host, port)
        print(f"\nEGRESS dns {entry[0]}:{entry[1]} "
              f"thread={entry[2]} test={entry[3]}", flush=True)
        raise BlockedEgress(f"blocked DNS lookup for {host}")

    socket.socket.connect = guarded_connect
    socket.getaddrinfo = guarded_getaddrinfo


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _CURRENT["nodeid"] = item.nodeid
    yield
    _CURRENT["nodeid"] = f"<after {item.nodeid}>"


def pytest_sessionfinish(session, exitstatus):
    with _LOCK:
        seen = list(_SEEN)
    print("\n\n===== EGRESS SUMMARY =====", flush=True)
    print(f"total blocked attempts: {len(seen)}", flush=True)
    by_host = {}
    by_thread = {}
    for host, port, thread, nodeid in seen:
        by_host[host] = by_host.get(host, 0) + 1
        # collapse Thread-N (_run_scan) -> _run_scan
        key = thread.split("(")[-1].rstrip(")") if "(" in thread else thread
        by_thread[key] = by_thread.get(key, 0) + 1
    print("by host:", flush=True)
    for h, n in sorted(by_host.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d}  {h}", flush=True)
    print("by thread:", flush=True)
    for t, n in sorted(by_thread.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d}  {t}", flush=True)
