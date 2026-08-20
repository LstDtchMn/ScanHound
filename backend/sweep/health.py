"""Per-source interval health.

Answers one question: *is this source's coverage still good enough to rely on?*

The rule that shapes everything here, from design rev 2.1 §6:

    Do not suppress `overdue` because a sweep or continuation is running.

An in-progress recovery is not coverage. If staleness were hidden while a sweep
ran, a source whose sweeps keep failing and restarting would report healthy
forever — the busiest possible way to go blind. So activity and staleness are
computed independently and then COMBINED, which is where the compound states
(`running_overdue`, `incomplete_overdue`) come from: they say "yes, something is
happening, and no, that does not make the data current."

The same non-suppression logic applies downward: `unknown` (never proved any
coverage) outranks everything, and `degraded` never masks `overdue`.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Optional

DUE_AFTER_HOURS = 6.0        # coverage_through + 6 h
OVERDUE_GRACE_HOURS = 1.0    # due_at + 1 h
DEGRADED_AFTER_FAILURES = 3


class IntervalState(str, Enum):
    CURRENT = "current"
    DUE = "due"
    RUNNING = "running"
    INCOMPLETE = "incomplete"
    OVERDUE = "overdue"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    # Compound — activity that does NOT clear staleness.
    RUNNING_OVERDUE = "running_overdue"
    INCOMPLETE_OVERDUE = "incomplete_overdue"


#: Only `current` clears a source for promotion (design rev 2.1 §10: "Every
#: required source interval `current`"). Everything else — including the two
#: compounds and `running` — blocks.
PROMOTION_CLEAR = {IntervalState.CURRENT}


@dataclass(frozen=True)
class SourceHealth:
    source_key: str
    state: IntervalState
    coverage_through: Optional[dt.datetime]
    due_at: Optional[dt.datetime]
    overdue_at: Optional[dt.datetime]
    coverage_age_hours: Optional[float]
    is_overdue: bool
    is_running: bool
    is_incomplete: bool
    is_degraded: bool
    consecutive_failures: int
    detail: str

    @property
    def blocks_promotion(self) -> bool:
        return self.state not in PROMOTION_CLEAR


def evaluate_source_health(
    row,
    *,
    now: dt.datetime,
    live_session=None,
    due_after_hours: float = DUE_AFTER_HOURS,
    overdue_grace_hours: float = OVERDUE_GRACE_HOURS,
    degraded_after_failures: int = DEGRADED_AFTER_FAILURES,
) -> SourceHealth:
    """Classify one source.

    `row` is a mapping from `hdencode_source_coverage` (or None if the source has
    never been recorded). `live_session` is the source's non-terminal sweep
    session, if any: a live lease means `running`, a session with continuation
    state but no live lease means `incomplete`.
    """
    row = row or {}
    source_key = row.get("source_key") or (live_session or {}).get("source_key") or ""
    coverage = _parse(row.get("coverage_through"))
    failures = int(row.get("consecutive_failures") or 0)

    is_running = False
    is_incomplete = False
    if live_session:
        lease_expires = _parse(live_session.get("lease_expires_at"))
        is_running = bool(lease_expires and lease_expires > now)
        # A session that exists, holds no live lease and has not reached a
        # terminal status is abandoned work waiting to be resumed — incomplete,
        # not running. An expired lease is not activity.
        is_incomplete = not is_running

    is_degraded = failures >= degraded_after_failures

    if coverage is None:
        # Never completed a sweep. No amount of current activity makes this
        # anything other than "we do not know what this source holds."
        return SourceHealth(
            source_key=source_key, state=IntervalState.UNKNOWN,
            coverage_through=None, due_at=None, overdue_at=None,
            coverage_age_hours=None, is_overdue=False, is_running=is_running,
            is_incomplete=is_incomplete, is_degraded=is_degraded,
            consecutive_failures=failures,
            detail="no successful sweep has ever completed for this source",
        )

    due_at = coverage + dt.timedelta(hours=due_after_hours)
    overdue_at = due_at + dt.timedelta(hours=overdue_grace_hours)
    age_hours = (now - coverage).total_seconds() / 3600.0
    is_overdue = now >= overdue_at

    if is_overdue:
        # THE NON-SUPPRESSION RULE. Activity is reported alongside staleness,
        # never instead of it.
        if is_running:
            state = IntervalState.RUNNING_OVERDUE
            detail = (f"coverage {age_hours:.1f} h old and past its "
                      f"{due_after_hours + overdue_grace_hours:.0f} h limit; a sweep is "
                      f"running but has not yet proven coverage")
        elif is_incomplete:
            state = IntervalState.INCOMPLETE_OVERDUE
            detail = (f"coverage {age_hours:.1f} h old; an unfinished sweep is "
                      f"awaiting continuation")
        else:
            state = IntervalState.OVERDUE
            detail = f"coverage {age_hours:.1f} h old with no sweep in progress"
    elif is_degraded:
        state = IntervalState.DEGRADED
        detail = f"{failures} consecutive failed attempts"
    elif is_running:
        state = IntervalState.RUNNING
        detail = f"sweep in progress; coverage {age_hours:.1f} h old"
    elif is_incomplete:
        state = IntervalState.INCOMPLETE
        detail = f"unfinished sweep awaiting continuation; coverage {age_hours:.1f} h old"
    elif now >= due_at:
        state = IntervalState.DUE
        detail = f"coverage {age_hours:.1f} h old; a sweep is due"
    else:
        state = IntervalState.CURRENT
        detail = f"coverage {age_hours:.1f} h old"

    return SourceHealth(
        source_key=source_key, state=state, coverage_through=coverage,
        due_at=due_at, overdue_at=overdue_at, coverage_age_hours=age_hours,
        is_overdue=is_overdue, is_running=is_running, is_incomplete=is_incomplete,
        is_degraded=is_degraded, consecutive_failures=failures, detail=detail,
    )


def _parse(value) -> Optional[dt.datetime]:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
