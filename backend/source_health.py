"""Source-health policy kept separate from operation diagnostics."""
from __future__ import annotations

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


def record_scrape_outcome(db, source: str, links) -> None:
    """Persist a successful scrape or a health-affecting structured failure."""
    if db is None:
        return
    diagnostic = getattr(links, "diagnostic", None)
    try:
        if links:
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
