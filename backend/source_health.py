"""Source-health policy kept separate from operation diagnostics."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic


class SourceHealthState(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    COOLDOWN = "cooldown"


def health_state_for_diagnostic(
    diagnostic: Optional[ScrapeDiagnostic],
) -> Optional[SourceHealthState]:
    """Map one operation outcome to a persistent state when appropriate."""
    if diagnostic is None or not diagnostic.affects_source_health:
        return None
    if diagnostic.code is ScrapeCode.INTERACTIVE_CHALLENGE:
        return SourceHealthState.BLOCKED
    if diagnostic.code is ScrapeCode.LAYOUT_CHANGED:
        return SourceHealthState.DEGRADED
    return SourceHealthState.DEGRADED


def effective_health_state(health, *, now=None) -> str:
    """Return the user-facing state, expiring cooldowns without a DB write."""
    if not health:
        return SourceHealthState.UNKNOWN.value

    state = health.get("state") or SourceHealthState.UNKNOWN.value
    if state != SourceHealthState.COOLDOWN.value:
        return state

    until = health.get("cooldown_until")
    if not until:
        return SourceHealthState.DEGRADED.value
    try:
        expires = datetime.fromisoformat(until)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if expires <= current:
            return SourceHealthState.DEGRADED.value
    except (TypeError, ValueError):
        # Invalid metadata cannot justify claiming an active cooldown.
        return SourceHealthState.DEGRADED.value
    return SourceHealthState.COOLDOWN.value


def record_scrape_outcome(db, source: str, links) -> None:
    """Persist a successful scrape or a health-affecting structured failure."""
    if db is None:
        return
    diagnostic = getattr(links, "diagnostic", None)
    try:
        if links:
            db.record_source_success(source)
            return
        if diagnostic is not None and diagnostic.code in {
            ScrapeCode.REQUESTED_HOST_MISSING,
            ScrapeCode.NO_FILE_HOST_LINKS,
        }:
            # The source page loaded successfully; only this host/release lacked
            # usable links. Clear a stale block/cooldown snapshot.
            db.record_source_success(source)
            return
        state = health_state_for_diagnostic(diagnostic)
        if state is not None:
            db.record_source_failure(
                source,
                state.value,
                diagnostic.code.value,
            )
    except Exception:
        # Health persistence must never turn a scrape result into an exception.
        return
