"""Bounded subprocess execution with cooperative process-tree cancellation.

Metadata probes can launch helpers that launch their own descendants. On POSIX,
every cancellable probe gets a new session/process group so cancellation and
timeout can signal the complete tree. Windows receives a new process group and
uses CTRL_BREAK_EVENT/taskkill fallbacks before direct-child termination.

After signaling, this module closes the parent pipe readers and waits with
bounded timeouts. It never calls communicate() after cancellation, so a
descendant that inherited stdout/stderr cannot keep cancellation blocked on EOF.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Callable, Optional, Sequence


_POLL_SECONDS = 0.10
_TERM_GRACE_SECONDS = 0.50
_KILL_GRACE_SECONDS = 0.75
_WINDOWS_SIGNAL_TIMEOUT_SECONDS = 1.0


class ProcessCancelled(Exception):
    """Raised after a caller-requested subprocess-tree termination completes."""


def _group_launch_options() -> dict:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flag} if flag else {}
    return {}


def _close_pipe(stream) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _close_pipes(process: subprocess.Popen) -> None:
    _close_pipe(process.stdout)
    _close_pipe(process.stderr)


def _wait_direct_child(process: subprocess.Popen, timeout: float) -> bool:
    try:
        process.wait(timeout=max(0.0, timeout))
        return True
    except (subprocess.TimeoutExpired, OSError):
        return process.poll() is not None


def _posix_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _signal_posix_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except OSError:
        # The process may have exited before the group signal. A direct-child
        # fallback below still bounds cleanup on unusual POSIX implementations.
        pass


def _taskkill_tree(pid: int, *, force: bool) -> bool:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_WINDOWS_SIGNAL_TIMEOUT_SECONDS,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _signal_windows_tree(process: subprocess.Popen, *, force: bool) -> None:
    # Attempt tree signaling even if the direct child has just exited: a
    # descendant may still be alive and holding inherited stdout/stderr handles.
    if not force:
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break is not None:
            try:
                process.send_signal(ctrl_break)
            except (OSError, ValueError):
                pass
        if _taskkill_tree(process.pid, force=False):
            return
        try:
            process.terminate()
        except OSError:
            pass
        return

    if _taskkill_tree(process.pid, force=True):
        return
    try:
        process.kill()
    except OSError:
        pass


def _stop(process: subprocess.Popen) -> None:
    """Stop a cancellable process tree without waiting on inherited pipe EOF."""
    if os.name == "posix":
        # start_new_session=True makes the child PID the new process-group ID.
        pgid = process.pid
        _signal_posix_group(pgid, signal.SIGTERM)
        _close_pipes(process)

        deadline = time.monotonic() + _TERM_GRACE_SECONDS
        while _posix_group_exists(pgid) and time.monotonic() < deadline:
            time.sleep(0.02)

        if _posix_group_exists(pgid):
            _signal_posix_group(pgid, signal.SIGKILL)

        if not _wait_direct_child(process, _KILL_GRACE_SECONDS):
            try:
                process.kill()
            except OSError:
                pass
            _wait_direct_child(process, _KILL_GRACE_SECONDS)
        return

    if os.name == "nt":
        _signal_windows_tree(process, force=False)
        _close_pipes(process)
        if not _wait_direct_child(process, _TERM_GRACE_SECONDS):
            _signal_windows_tree(process, force=True)
            _wait_direct_child(process, _KILL_GRACE_SECONDS)
        return

    # Conservative fallback for other Python platforms.
    try:
        process.terminate()
    except OSError:
        pass
    _close_pipes(process)
    if not _wait_direct_child(process, _TERM_GRACE_SECONDS):
        try:
            process.kill()
        except OSError:
            pass
        _wait_direct_child(process, _KILL_GRACE_SECONDS)


def run_cancellable(
    args: Sequence[str],
    *,
    timeout: int | float,
    cancel_requested: Optional[Callable[[], bool]] = None,
    text: bool = False,
):
    """Run a bounded subprocess and terminate its tree when cancellation is requested.

    When no cancellation callback is supplied, this preserves the repository's
    established subprocess.run behavior. Cancellable calls use Popen solely so
    the caller can poll the durable scan's stop flag.
    """
    if cancel_requested is None:
        return subprocess.run(
            args,
            capture_output=True,
            text=text,
            timeout=timeout,
        )

    if cancel_requested():
        raise ProcessCancelled

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        **_group_launch_options(),
    )
    deadline = time.monotonic() + float(timeout)

    while True:
        if cancel_requested():
            _stop(process)
            raise ProcessCancelled

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop(process)
            raise subprocess.TimeoutExpired(args, timeout)

        try:
            stdout, stderr = process.communicate(
                timeout=min(_POLL_SECONDS, remaining)
            )
            return subprocess.CompletedProcess(
                args=args,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            continue
