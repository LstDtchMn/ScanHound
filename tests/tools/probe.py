"""Combined probe: blocked-egress recorder + create_scraper attribution.

No external traffic: non-loopback connects and DNS are recorded then refused.
Also records every cloudscraper.create_scraper call with the calling thread and
the test that was active at the time, so a construction by a leaked thread
during an unrelated test is visible.
"""
import socket
import threading

import pytest

_CURRENT = {"nodeid": "<session>"}
_EGRESS = []
_BUILDS = []
_LOCK = threading.Lock()

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


class BlockedEgress(OSError):
    pass


def pytest_sessionstart(session):
    import cloudscraper

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo
    real_create = cloudscraper.create_scraper

    def _note(bucket, kind, a, b):
        entry = (kind, str(a), b, threading.current_thread().name,
                 _CURRENT["nodeid"])
        with _LOCK:
            bucket.append(entry)
        return entry

    def guarded_connect(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
        if str(host) in _LOOPBACK:
            return real_connect(self, address, *a, **kw)
        e = _note(_EGRESS, "connect", host, port)
        print(f"\nEGRESS {e[0]} {e[1]}:{e[2]} thread={e[3]} test={e[4]}",
              flush=True)
        raise BlockedEgress(f"blocked connect {host}:{port}")

    def guarded_getaddrinfo(host, port, *a, **kw):
        if str(host) in _LOOPBACK:
            return real_getaddrinfo(host, port, *a, **kw)
        e = _note(_EGRESS, "dns", host, port)
        print(f"\nEGRESS {e[0]} {e[1]}:{e[2]} thread={e[3]} test={e[4]}",
              flush=True)
        raise BlockedEgress(f"blocked dns {host}")

    def watched_create(*a, **kw):
        me = threading.current_thread()
        e = _note(_BUILDS, "build", me.name, None)
        if me is not threading.main_thread():
            print(f"\nSCRAPEBUILD thread={e[3]} test={e[4]}", flush=True)
        return real_create(*a, **kw)

    socket.socket.connect = guarded_connect
    socket.getaddrinfo = guarded_getaddrinfo
    cloudscraper.create_scraper = watched_create


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _CURRENT["nodeid"] = item.nodeid
    yield
    _CURRENT["nodeid"] = f"<after {item.nodeid}>"


def pytest_sessionfinish(session, exitstatus):
    with _LOCK:
        eg, bl = list(_EGRESS), list(_BUILDS)
    print("\n\n===== PROBE SUMMARY =====", flush=True)
    print(f"egress attempts (blocked): {len(eg)}", flush=True)
    for kind, host, port, thread, node in eg:
        print(f"  {kind:8s} {host}:{port} thread={thread} test={node}",
              flush=True)
    foreign = [b for b in bl if b[3] != "MainThread"]
    print(f"create_scraper calls: {len(bl)} total, "
          f"{len(foreign)} off-main-thread", flush=True)
    for _k, tname, _p, thread, node in foreign:
        print(f"  thread={thread} test={node}", flush=True)
