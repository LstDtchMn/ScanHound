"""Small subprocess runner with cooperative cancellation for media probes."""

from __future__ import annotations

import subprocess
import time


class ProcessCancelled(Exception):
    """Raised after a caller-requested subprocess termination completes."""


def run_cancellable(args, *, timeout: int, cancel_requested=None, text: bool = False):
    """Run a bounded subprocess and terminate it when cancellation is requested."""
    if cancel_requested is None:
        return subprocess.run(
            args, capture_output=True, text=text, timeout=timeout
        )

    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop(process)
            raise subprocess.TimeoutExpired(args, timeout)
        try:
            stdout, stderr = process.communicate(timeout=min(0.25, remaining))
            return subprocess.CompletedProcess(
                args=args,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            if cancel_requested():
                _stop(process)
                raise ProcessCancelled


def _stop(process) -> None:
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
