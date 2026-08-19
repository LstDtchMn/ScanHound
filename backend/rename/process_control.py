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

import ctypes
import os
import signal
import subprocess
import time
from typing import Callable, Optional, Sequence


_POLL_SECONDS = 0.10
_STALL_SAMPLE_SECONDS = 2.0
_TERM_GRACE_SECONDS = 0.50
_KILL_GRACE_SECONDS = 0.75
_WINDOWS_SIGNAL_TIMEOUT_SECONDS = 1.0


class ProcessCancelled(Exception):
    """Raised after a caller-requested subprocess-tree termination completes."""


class ProcessStalled(Exception):
    """Raised when a subprocess stopped reading input but kept running.

    A WALL-CLOCK timeout cannot tell "slow" from "wedged", and on a media
    library those two want opposite responses: a genuinely slow 90 GB read
    deserves more time, while a wedged one deserves less. Measured on
    2026-08-09, ``dovi_tool extract-rpu`` on two specific titles read ~25 GB
    and then issued ZERO further read operations while holding 95% of a core
    for the remaining 30 minutes -- three times in one day, deterministically,
    on files whose bytes a plain sequential read streams at 221 MB/s. Raising
    the wall-clock cap (the obvious fix) would have made those runs waste
    MORE time, not less.

    So progress is measured as bytes-read, and the absence of progress is what
    ends the process. The caller still resolves this to a non-authoritative
    result -- a stall is a failed detection, never a finding.
    """


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_kernel32 = None


def _win_kernel32():
    """kernel32 with EXPLICIT signatures, built once.

    The signatures are not optional tidiness. ctypes defaults a foreign
    function's restype to C int, so on 64-bit Windows an un-annotated
    OpenProcess TRUNCATES the returned HANDLE to 32 bits — which happens to
    work while handle values stay small and starts failing later, under load,
    for no visible reason. Declaring the types removes that entirely.
    """
    global _kernel32
    if _kernel32 is None:
        from ctypes import wintypes
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        lib.OpenProcess.restype = wintypes.HANDLE
        lib.GetProcessIoCounters.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(_IoCounters)]
        lib.GetProcessIoCounters.restype = wintypes.BOOL
        lib.CloseHandle.argtypes = [wintypes.HANDLE]
        lib.CloseHandle.restype = wintypes.BOOL
        _kernel32 = lib
    return _kernel32


def process_read_bytes(pid: int) -> Optional[int]:
    """Bytes this process has read so far, or None when unmeasurable.

    None is the load-bearing return: a caller that cannot measure progress
    must NOT stall-kill, because "no measurement" and "no progress" would
    otherwise look identical and a healthy probe would be killed on a platform
    we simply cannot instrument. Every failure path here returns None.

    Counts bytes requested through read syscalls (Windows ReadTransferCount,
    Linux ``rchar``) rather than bytes fetched from the physical device, so a
    process being served entirely from page cache still registers as making
    progress.
    """
    try:
        if os.name == "nt":
            lib = _win_kernel32()
            handle = lib.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return None
            try:
                counters = _IoCounters()
                if not lib.GetProcessIoCounters(handle, ctypes.byref(counters)):
                    return None
                return int(counters.ReadTransferCount)
            finally:
                lib.CloseHandle(handle)
        with open(f"/proc/{int(pid)}/io", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("rchar:"):
                    return int(line.split(":", 1)[1].strip())
        return None
    except Exception:  # noqa: BLE001 - measurement must never break the probe
        return None


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
    stall_timeout: Optional[int | float] = None,
):
    """Run a bounded subprocess and terminate its tree when cancellation is requested.

    When neither a cancellation callback nor *stall_timeout* is supplied, this
    preserves the repository's established subprocess.run behavior. Polled calls
    use Popen solely so the caller can watch the durable scan's stop flag and/or
    the child's read progress.

    *stall_timeout* ends the process after that many seconds with no increase in
    bytes read, raising ProcessStalled. It is deliberately independent of
    *timeout*: the wall-clock cap bounds a slow-but-working run, while the stall
    window catches a wedged one that would otherwise hold the cap open doing
    nothing. When read progress cannot be measured the watchdog disables itself,
    so the wall-clock cap remains the only bound.
    """
    if cancel_requested is None and stall_timeout is None:
        return subprocess.run(
            args,
            capture_output=True,
            text=text,
            timeout=timeout,
        )

    if cancel_requested is not None and cancel_requested():
        raise ProcessCancelled

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        **_group_launch_options(),
    )
    deadline = time.monotonic() + float(timeout)

    # Watch read progress only if the platform actually reports it. Probing
    # once up front means an unmeasurable platform never enters the stall
    # branch at all, rather than entering it and being saved by a None check
    # on every poll.
    watch_stall = stall_timeout is not None
    last_read_bytes = process_read_bytes(process.pid) if watch_stall else None
    if watch_stall and last_read_bytes is None:
        watch_stall = False
    last_progress_at = time.monotonic()
    last_sample_at = last_progress_at

    while True:
        if cancel_requested is not None and cancel_requested():
            _stop(process)
            raise ProcessCancelled

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop(process)
            raise subprocess.TimeoutExpired(args, timeout)

        # Sampled on its own cadence, not the 100 ms process poll: at that rate
        # a 30-minute extract would open and close 18,000 process handles to
        # answer a question whose answer changes on the scale of seconds.
        if watch_stall and time.monotonic() - last_sample_at >= _STALL_SAMPLE_SECONDS:
            last_sample_at = time.monotonic()
            current = process_read_bytes(process.pid)
            now = time.monotonic()
            if current is None:
                # The process is exiting (or the handle closed); let the normal
                # communicate() path below reap it rather than calling it stalled.
                last_progress_at = now
            elif current > last_read_bytes:
                last_read_bytes = current
                last_progress_at = now
            elif now - last_progress_at >= float(stall_timeout):
                _stop(process)
                raise ProcessStalled(
                    f"no read progress for {stall_timeout}s after "
                    f"{last_read_bytes} bytes")

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
