"""Pytest plugin: report which tests leave live threads behind.

The flake under investigation fails intermittently, but the LEAK that causes it
is deterministic — a thread either survives a test or it does not. So find the
leak, not the flake.

Loaded with `-p threadleak`; the module itself is the plugin, so the hooks are
module-level functions rather than a separately registered class.
"""
import threading

_BASELINE = {}
_LEAKS = []


def _snapshot():
    return {t.ident: t for t in threading.enumerate()
            if t is not threading.main_thread()}


def pytest_runtest_setup(item):
    _BASELINE.clear()
    _BASELINE.update(_snapshot())


def pytest_runtest_teardown(item):
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
