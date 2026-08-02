"""Pytest plugin: report which tests leave live threads behind.

The flake under investigation fails intermittently, but the LEAK that causes it
is deterministic — a thread either survives a test or it does not. So find the
leak, not the flake.

Loaded with `-p threadleak`; the module itself is the plugin, so the hooks are
module-level functions rather than a separately registered class. It needs to be
importable, so put tests/tools on the path:

    PYTHONPATH=tests/tools python -m pytest tests/ -q -p threadleak

THE TEARDOWN HOOK MUST BE A WRAPPER. A plain `pytest_runtest_teardown` impl
runs BEFORE `_pytest.runner`'s — pluggy calls hookimpls last-registered-first,
and `-p threadleak` registers after the core plugins — so it samples while the
test's fixtures are still open. That measures "threads alive during the test",
not "threads that outlived it": every `with TestClient(app)` fixture then looks
like a leak, including TestClient's own portal and AnyIO worker threads, which
its `__exit__` reliably cleans up. Measured 2026-08-01: sampling early reported
223 leaking tests in test_api_routes.py both before AND after the lifespan was
fixed to join its background threads — an instrument that could not see the
change it existed to measure. Sampling after finalization reported 223 -> 1.
"""
import threading

import pytest

_BASELINE = {}
_LEAKS = []


def _snapshot():
    return {t.ident: t for t in threading.enumerate()
            if t is not threading.main_thread()}


def pytest_runtest_setup(item):
    _BASELINE.clear()
    _BASELINE.update(_snapshot())


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    yield  # let the runner finalize this test's fixtures first
    new = {i: t for i, t in _snapshot().items()
           if i not in _BASELINE and t.is_alive()}
    if new:
        _LEAKS.append((item.nodeid,
                       [f"{t.name}(daemon={t.daemon})" for t in new.values()]))


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line("")
    if not _LEAKS:
        terminalreporter.write_line("THREADLEAK: none")
        return
    terminalreporter.write_line(f"THREADLEAK: {len(_LEAKS)} test(s) leaked threads")
    for nodeid, names in _LEAKS:
        terminalreporter.write_line(f"  {nodeid}")
        for n in names:
            terminalreporter.write_line(f"      -> {n}")
