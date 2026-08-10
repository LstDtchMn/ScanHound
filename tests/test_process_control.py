"""Process-tree cancellation regressions for metadata probe subprocesses."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import time

import pytest

import subprocess
from unittest.mock import patch

from backend.rename.process_control import (
    ProcessCancelled,
    ProcessStalled,
    process_read_bytes,
    run_cancellable,
)


def _pid_alive(pid: int) -> bool:
    if os.name != "posix":
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        stat = stat_path.read_text(encoding="utf-8")
        # A zombie has terminated and cannot hold the inherited pipe open.
        if ") Z " in stat:
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_cancel_kills_descendant_with_inherited_pipes_promptly(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    parent_script = tmp_path / "spawn_descendant.py"
    parent_script.write_text(
        "\n".join([
            "import pathlib",
            "import subprocess",
            "import sys",
            "import time",
            "pid_file = pathlib.Path(sys.argv[1])",
            "child = subprocess.Popen(",
            "    [sys.executable, '-c', 'import time; time.sleep(30)'],",
            "    stdout=sys.stdout,",
            "    stderr=sys.stderr,",
            ")",
            "pid_file.write_text(str(child.pid), encoding='utf-8')",
            "print('descendant-started', flush=True)",
            "time.sleep(30)",
        ]) + "\n",
        encoding="utf-8",
    )

    cancellation_observed_at = None

    def cancel_requested():
        nonlocal cancellation_observed_at
        if not pid_file.exists():
            return False
        if cancellation_observed_at is None:
            cancellation_observed_at = time.monotonic()
        return True

    descendant_pid = None
    try:
        with pytest.raises(ProcessCancelled):
            run_cancellable(
                [sys.executable, str(parent_script), str(pid_file)],
                timeout=20,
                cancel_requested=cancel_requested,
                text=True,
            )
        assert cancellation_observed_at is not None
        cancellation_latency = time.monotonic() - cancellation_observed_at
        assert cancellation_latency < 2.0

        descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2.0
        while _pid_alive(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _pid_alive(descendant_pid)
    finally:
        if descendant_pid and _pid_alive(descendant_pid):
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class TestStallWatchdog:
    """A wedged child must die on the stall window; a busy one must not.

    These spawn REAL processes on purpose. The whole point of the watchdog is
    that it distinguishes "doing nothing" from "doing work slowly", and a
    mocked child cannot demonstrate that distinction — it would pass whether
    the byte counter were consulted or ignored.
    """

    # Spins on the CPU and issues no read syscalls: the exact shape measured
    # from dovi_tool on 2026-08-09 (95% of a core, zero bytes read).
    SPIN = "import time\nend = time.time() + 60\nwhile time.time() < end: pass\n"

    def test_spinning_process_with_no_reads_is_stalled(self):
        started = time.monotonic()
        with pytest.raises(ProcessStalled):
            run_cancellable(
                [sys.executable, "-c", self.SPIN],
                timeout=60,
                stall_timeout=3,
            )
        elapsed = time.monotonic() - started
        # Killed on the stall window, nowhere near the 60 s wall-clock cap —
        # which is the entire behavioural claim.
        assert elapsed < 25, f"took {elapsed:.1f}s; stall window was 3s"

    def test_process_that_keeps_reading_is_NOT_killed(self, tmp_path):
        """The control that makes the test above mean something.

        Without this, a watchdog that killed every child after 3 seconds would
        pass the stall test and look correct.
        """
        target = tmp_path / "payload.bin"
        target.write_bytes(b"\0" * (2 * 1024 * 1024))
        script = (
            "import sys, time\n"
            "p = sys.argv[1]\n"
            "end = time.time() + 8\n"
            "while time.time() < end:\n"
            "    with open(p, 'rb') as fh:\n"
            "        fh.read()\n"
            "    time.sleep(0.05)\n"
            "print('done')\n"
        )
        result = run_cancellable(
            [sys.executable, "-c", script, str(target)],
            timeout=60,
            stall_timeout=3,
        )
        assert result.returncode == 0
        assert b"done" in (result.stdout or b"")

    def test_watchdog_disables_itself_when_progress_is_unmeasurable(self):
        """No measurement must mean no stall-kill, not an instant one."""
        with patch("backend.rename.process_control.process_read_bytes",
                   return_value=None):
            with pytest.raises(subprocess.TimeoutExpired):
                run_cancellable(
                    [sys.executable, "-c", self.SPIN],
                    timeout=4,
                    stall_timeout=1,
                )

    def test_no_stall_timeout_preserves_subprocess_run(self):
        """The established contract: no watchdog, no cancel callback, no Popen."""
        with patch("backend.rename.process_control.subprocess.run") as m:
            m.return_value = "sentinel"
            out = run_cancellable([sys.executable, "-c", "pass"], timeout=5)
        assert out == "sentinel"
        assert m.call_count == 1

    def test_read_bytes_is_measurable_on_this_platform(self):
        """Positive control for the measurement itself.

        If this fails, the watchdog silently degrades to "disabled" everywhere
        and the two tests above would still pass for the wrong reason.
        """
        script = (
            "import sys, time\n"
            "end = time.time() + 10\n"
            "while time.time() < end:\n"
            "    with open(sys.argv[0] if False else __file__ if False else "
            "sys.executable, 'rb') as fh:\n"
            "        fh.read(1 << 20)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            # Poll rather than sample once: a child sampled the instant it is
            # spawned has legitimately read nothing yet, and asserting on that
            # first sample would make this control flaky rather than meaningful.
            deadline = time.monotonic() + 8
            value = process_read_bytes(proc.pid)
            while (value is None or value == 0) and time.monotonic() < deadline:
                time.sleep(0.1)
                value = process_read_bytes(proc.pid)
        finally:
            proc.kill()
            proc.wait(timeout=5)
        assert value is not None, "read progress is unmeasurable on this platform"
        assert value > 0
