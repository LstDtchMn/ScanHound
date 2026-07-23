"""Process-tree cancellation regressions for metadata probe subprocesses."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import time

import pytest

from backend.rename.process_control import ProcessCancelled, run_cancellable


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
