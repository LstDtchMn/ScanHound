"""Adversarial verification of run_cancellable against REAL OS child processes.

PR #21's own new tests all mock subprocess.Popen with a hand-written fake
Process class whose terminate() unconditionally "succeeds" on the first call.
That never exercises: a real process actually dying, the kill()-escalation
path when a child ignores SIGTERM, real stdout/stderr draining, or an
empirically measured cancellation latency. This file closes that gap using
real `python -c ...` child processes -- exactly what the review handoff asked
for and explicitly warned the implementation not to skip.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from backend.rename.process_control import ProcessCancelled, run_cancellable

_IGNORES_SIGTERM = (
    "import signal, time, sys;"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    "print('started', flush=True);"
    "time.sleep(30)"
)

_COOPERATES = (
    "import time, sys;"
    "print('started', flush=True);"
    "print('stderr-line', file=sys.stderr, flush=True);"
    "time.sleep(30)"
)

_HAS_SIGTERM_IGNORE = hasattr(signal, "SIGTERM") and os.name != "nt"


@pytest.mark.skipif(not _HAS_SIGTERM_IGNORE, reason="SIGTERM ignoring is POSIX-only")
def test_kill_escalation_against_a_real_stubborn_child():
    """A child that ignores SIGTERM must still be gone via kill(), bounded."""
    calls = iter([False, False, True])  # let it start, then cancel
    start = time.monotonic()
    with pytest.raises(ProcessCancelled):
        run_cancellable(
            [sys.executable, "-c", _IGNORES_SIGTERM],
            timeout=60,
            cancel_requested=lambda: next(calls, True),
        )
    elapsed = time.monotonic() - start
    # Bounded: poll interval (<=0.25s * few) + terminate-wait(5s) + kill.
    assert elapsed < 8, f"cancellation took {elapsed:.2f}s, expected well under 8s"


def test_real_child_actually_terminates_not_just_marked_cancelled():
    """Prove the OS process is gone, not merely that our code raised."""
    calls = iter([False, False, True])
    process_holder = {}
    real_popen = subprocess.Popen

    def _spy(*args, **kwargs):
        p = real_popen(*args, **kwargs)
        process_holder["pid"] = p.pid
        return p

    import backend.rename.process_control as pc
    orig = pc.subprocess.Popen
    pc.subprocess.Popen = _spy
    try:
        with pytest.raises(ProcessCancelled):
            run_cancellable(
                [sys.executable, "-c", _COOPERATES],
                timeout=60,
                cancel_requested=lambda: next(calls, True),
            )
    finally:
        pc.subprocess.Popen = orig

    pid = process_holder["pid"]
    time.sleep(0.3)  # let the OS reap
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # signal 0 = existence probe, no actual signal sent
    else:
        # Windows: confirm via tasklist that the pid is gone.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
        ).stdout
        assert str(pid) not in out


def test_cancellation_latency_is_bounded_for_a_cooperative_child():
    calls = iter([False, False, True])
    start = time.monotonic()
    with pytest.raises(ProcessCancelled):
        run_cancellable(
            [sys.executable, "-c", _COOPERATES],
            timeout=60,
            cancel_requested=lambda: next(calls, True),
        )
    elapsed = time.monotonic() - start
    assert elapsed < 6, f"cancellation took {elapsed:.2f}s for a cooperative child"


def test_uncancelled_real_child_returns_real_stdout_stderr():
    result = run_cancellable(
        [sys.executable, "-c",
         "import sys; print('hello-stdout'); print('hello-stderr', file=sys.stderr)"],
        timeout=10,
        cancel_requested=lambda: False,
        text=True,
    )
    assert result.returncode == 0
    assert "hello-stdout" in result.stdout
    assert "hello-stderr" in result.stderr


@pytest.mark.skipif(not _HAS_SIGTERM_IGNORE, reason="POSIX-only descendant probe")
def test_descendant_process_survives_direct_child_kill_general_principle():
    """Does killing ONLY the direct child (current design) leave a
    grandchild it spawned still running?

    This does not use the real dovi_tool/hdr10plus_tool binaries (not
    installed in this environment) -- it demonstrates the GENERAL
    Popen.terminate()/kill() behavior the design relies on: those calls
    signal only the immediate child, not its descendants. If dovi_tool or
    hdr10plus_tool ever internally shell out to a helper process (e.g.
    ffmpeg), that helper would be orphaned exactly like this grandchild is.
    """
    parent_script = (
        "import subprocess, sys, time;"
        "gc = subprocess.Popen([sys.executable, '-c', "
        "'import time,sys; open(sys.argv[1], \"w\").close(); time.sleep(30)', "
        "sys.argv[1]]);"
        "print(gc.pid, flush=True);"
        "time.sleep(30)"
    )
    import tempfile
    marker = tempfile.mktemp(prefix="scanhound-grandchild-", suffix=".marker")
    calls = iter([False, False, False, True])

    with pytest.raises(ProcessCancelled):
        run_cancellable(
            [sys.executable, "-c", parent_script, marker],
            timeout=60,
            cancel_requested=lambda: next(calls, True),
        )

    # Give the OS a moment, then scan /proc for any python process whose
    # parent is no longer alive but which is still running (an orphan).
    time.sleep(0.5)
    orphan_found = False
    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmdline = f.read().decode(errors="replace")
            except OSError:
                continue
            if marker in cmdline or (
                "grandchild" in cmdline and str(os.getpid()) not in cmdline
            ):
                orphan_found = True
                try:
                    os.kill(int(entry), signal.SIGKILL)  # test cleanup
                except OSError:
                    pass

    try:
        os.remove(marker)
        leaked = False
    except FileNotFoundError:
        leaked = True  # marker never got created OR was cleaned by the child itself

    # This assertion is intentionally a REPORT, not a hard pass/fail gate --
    # its purpose is to tell the reviewer whether descendant orphaning is
    # real in this environment, which the handoff explicitly asked to prove
    # empirically before deciding on process-group complexity.
    print(f"\n[descendant-probe] orphan grandchild process found: {orphan_found}")
    print(f"[descendant-probe] marker file leaked (not cleaned by anyone): {leaked}")
