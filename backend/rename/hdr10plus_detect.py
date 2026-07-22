"""Conservative HDR10+ evidence detection.

A first-frame ffprobe observation can prove HDR10+ is present, but it cannot
prove that a full feature lacks dynamic metadata.  This module therefore uses
the fast observation only as a positive shortcut and otherwise delegates to a
full-file ``hdr10plus_tool`` extraction.  Tool absence, timeout, malformed
output, and parser failures are all represented as ``unknown``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from backend.rename.process_control import ProcessCancelled, run_cancellable


def _quick_frame_evidence(path: str, timeout: int = 30, cancel_requested=None) -> bool:
    """Return True only when ffprobe observes HDR10+ on its first decoded frame."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        result = run_cancellable(
            [
                ffprobe, "-v", "quiet", "-print_format", "json", "-show_frames",
                "-read_intervals", "%+#1", "-select_streams", "v:0", path,
            ],
            text=True,
            timeout=timeout,
            cancel_requested=cancel_requested,
        )
        if result.returncode != 0:
            return False
        for frame in (json.loads(result.stdout).get("frames") or []):
            for side_data in (frame.get("side_data_list") or []):
                kind = str(side_data.get("side_data_type", ""))
                if "HDR10+" in kind or "SMPTE2094-40" in kind:
                    return True
    except ProcessCancelled:
        raise
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return False
    return False


def _tool_version(tool: str, timeout: int, cancel_requested=None) -> str | None:
    try:
        result = run_cancellable(
            [tool, "--version"], text=True, timeout=min(timeout, 10),
            cancel_requested=cancel_requested,
        )
        if result.returncode == 0:
            return (result.stdout or result.stderr).strip() or None
    except ProcessCancelled:
        raise
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _full_extract(path: str, timeout: int = 300, cancel_requested=None) -> dict:
    """Run a full-file extraction without writing beside the source media file."""
    tool = shutil.which("hdr10plus_tool")
    if not tool:
        return {"state": "unknown", "method": "full_extract", "tool_version": None,
                "error": "tool_unavailable"}

    version = _tool_version(tool, timeout, cancel_requested)
    try:
        with tempfile.TemporaryDirectory(prefix="scanhound-hdr10plus-") as temp_dir:
            output_path = os.path.join(temp_dir, "metadata.json")
            result = run_cancellable(
                [tool, "extract", path, "-o", output_path],
                text=True,
                timeout=timeout,
                cancel_requested=cancel_requested,
            )
            if result.returncode != 0:
                return {"state": "unknown", "method": "full_extract", "tool_version": version,
                        "error": "extract_failed"}
            # A successful full extraction with an emitted JSON payload is positive.
            # The tool also completes successfully when it parses a stream with no
            # dynamic metadata, in which case no output payload is produced.
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                try:
                    with open(output_path, encoding="utf-8") as output_file:
                        json.load(output_file)
                except (OSError, ValueError, json.JSONDecodeError):
                    return {"state": "unknown", "method": "full_extract", "tool_version": version,
                            "error": "invalid_extract_output"}
                return {"state": "present", "method": "full_extract", "tool_version": version,
                        "error": None}
            return {"state": "absent", "method": "full_extract", "tool_version": version,
                    "error": None}
    except ProcessCancelled:
        return {"state": "unknown", "method": "full_extract", "tool_version": version,
                "error": "cancelled"}
    except subprocess.TimeoutExpired:
        return {"state": "unknown", "method": "full_extract", "tool_version": version,
                "error": "timeout"}
    except OSError:
        return {"state": "unknown", "method": "full_extract", "tool_version": version,
                "error": "extract_failed"}


def detect_hdr10plus(path: str, *, quick_timeout: int = 30, full_timeout: int = 300,
                     cancel_requested=None) -> dict:
    """Return ``present``, ``absent``, or ``unknown`` HDR10+ evidence.

    Only the full extractor may return ``absent``.  This preserves the crucial
    distinction between an authoritative negative and a quick probe that did
    not encounter dynamic metadata in its first frame.
    """
    try:
        quick_positive = _quick_frame_evidence(path, quick_timeout, cancel_requested)
    except ProcessCancelled:
        return {"state": "unknown", "method": "ffprobe_first_frame",
                "tool_version": None, "error": "cancelled"}
    if quick_positive:
        return {
            "state": "present",
            "method": "ffprobe_first_frame",
            "tool_version": None,
            "error": None,
        }
    result = _full_extract(path, full_timeout, cancel_requested)
    return {
        "state": result.get("state", "unknown"),
        "method": result.get("method", "full_extract"),
        "tool_version": result.get("tool_version"),
        "error": result.get("error"),
    }
