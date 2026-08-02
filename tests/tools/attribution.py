"""Dump scan-operation attribution at session end.

Run with ``-p attribution`` and ``SCANHOUND_SCAN_TRACE=1``. Pair it with
``-p netwatch``: netwatch records the *thread name* of every blocked egress
attempt, and Phase 2 makes those names carry the owning scan
(``scan-<uuid8>-listing`` rather than the ambiguous ``asyncio_0``), so the two
outputs join on the thread name with no correlation step.

Read-only. This plugin never fails a run.
"""
from __future__ import annotations

import pytest


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    try:
        from backend import scan_context
    except Exception as exc:  # pragma: no cover - import guard only
        terminalreporter.write_line(f"attribution: scan_context unavailable ({exc})")
        return

    operations = scan_context.recent_operations()
    if not operations:
        terminalreporter.write_sep("=", "attribution: no scan operations recorded")
        return

    traced = [op for op in operations if len(op.trace) > 0]
    crossed = [op for op in operations if op.crossed_ownership]

    terminalreporter.write_sep(
        "=", f"attribution: {len(operations)} scan operation(s), "
             f"{len(traced)} traced, {len(crossed)} crossed ownership"
    )

    if not scan_context.tracing_enabled():
        terminalreporter.write_line(
            "SCANHOUND_SCAN_TRACE is not 1 — identities were captured but no "
            "trace events were recorded."
        )

    by_origin: dict = {}
    for op in operations:
        by_origin[op.origin] = by_origin.get(op.origin, 0) + 1
    terminalreporter.write_line("by origin:")
    for origin, count in sorted(by_origin.items(), key=lambda kv: -kv[1]):
        terminalreporter.write_line(f"  {count:5d}  {origin}")

    terminalreporter.write_line("")
    for op in operations:
        marker = "  <-- CROSSED LIFESPAN" if op.crossed_ownership else ""
        terminalreporter.write_line(
            f"  scan {op.scan_uuid[:8]} origin={op.origin} "
            f"parent={op.parent_operation} source={op.source_kind}{marker}"
        )
        terminalreporter.write_line(
            f"      accepted_owner={op.accepted_owner} "
            f"entered_owner={op.entered_owner}"
        )
        stages = op.trace.stages()
        if stages:
            terminalreporter.write_line(f"      stages: {' -> '.join(stages)}")
