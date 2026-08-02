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
    crossed_entry = [op for op in operations if op.crossed_ownership_at_entry]
    crossed_post = [op for op in operations if op.observed_post_entry_crossing]

    terminalreporter.write_sep(
        "=", f"attribution: {len(operations)} operation(s), {len(traced)} traced, "
             f"{len(crossed_entry)} crossed at entry, "
             f"{len(crossed_post)} crossed post-entry"
    )
    terminalreporter.write_line(
        "'crossed at entry' compares acceptance to entry only; "
        "'crossed post-entry' is sampled live at completion/publication."
    )

    # Q4: did any executor worker outlive its outer scan thread? Decided on the
    # monotonic clock, not inferred.
    terminalreporter.write_line("")
    terminalreporter.write_line("outer-vs-worker completion:")
    for op in operations:
        events = op.trace.events()
        outer = [e for e in events if e.stage == scan_context.THREAD_FINISHED]
        worker = [e for e in events if e.stage == scan_context.WORKER_FINISHED]
        if not outer and not worker:
            continue
        outer_ns = max((e.monotonic_ns for e in outer), default=None)
        worker_ns = max((e.monotonic_ns for e in worker), default=None)
        if outer_ns is None or worker_ns is None:
            verdict = "incomplete (missing one marker)"
        elif worker_ns > outer_ns:
            verdict = f"WORKER OUTLIVED OUTER by {worker_ns - outer_ns} ns"
        else:
            verdict = f"outer finished last (+{outer_ns - worker_ns} ns)"
        terminalreporter.write_line(
            f"  scan {op.scan_uuid[:8]} {op.origin}: {verdict}")

    # Q5: was any publication attempted after ownership was lost?
    terminalreporter.write_line("")
    terminalreporter.write_line("publication ownership:")
    any_stale = False
    for op in operations:
        for e in op.trace.events():
            if not e.stage.startswith("publish_"):
                continue
            if e.still_owns_lifespan is False:
                any_stale = True
                # owns_lifespan() is (generation matches) AND (no shutdown
                # requested), so equal generations with owns=False means the
                # lifespan was TEARING DOWN, not that it rolled over. The two
                # need different fences in Phase 3, so name which one it was.
                if e.active_lifespan_generation != op.accepted_lifespan_generation:
                    reason = (f"generation rolled over "
                              f"{op.accepted_lifespan_generation}"
                              f"->{e.active_lifespan_generation}")
                else:
                    reason = (f"shutdown requested during teardown of "
                              f"generation {e.active_lifespan_generation}")
                terminalreporter.write_line(
                    f"  scan {op.scan_uuid[:8]} PUBLISHED WITHOUT OWNERSHIP "
                    f"at {e.stage} -- {reason}")
    if not any_stale:
        terminalreporter.write_line(
            "  no publication observed while ownership was lost "
            "(still_owns_lifespan was never False)")

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
