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
        return SourceHealthState.DEGRADED.value
    return SourceHealthState.COOLDOWN.value


_CHALLENGE_CODES = frozenset({
    ScrapeCode.INTERACTIVE_CHALLENGE,
    ScrapeCode.REVEAL_VERIFICATION_STALLED,
})

_STRIPPED_CODES = frozenset({
    ScrapeCode.NO_FILE_HOST_LINKS,
    ScrapeCode.LAYOUT_CHANGED,
    ScrapeCode.REVEAL_CONTROL_ABSENT,
})


def classify_reveal_outcome(links) -> tuple[str, Optional[str]]:
    """Classify one reveal's outcome for reveal ACCOUNTING. Pure function.

    Returns (outcome, diagnostic_code):
      - ("success", None) for non-empty links.
      - ("refused", code) when diagnostic.effective_transport_attempted is
        False: no request was sent at all (source disabled, a coordinator
        denial, a browser launch failure). This is NOT a reveal attempt
        against the site -- it never left the machine -- so it must never be
        bucketed with "challenge", which means the source was reached and
        actively resisted.
      - ("challenge", code) when the diagnostic's health_owner is "coordinator"
        (the coordinator, not the outcome recorder, owns this diagnostic) or
        its code is one of the two coordinator-owned challenge codes.
      - ("stripped", code) when the page came back without usable links for a
        reason that is not a challenge and not an error.
      - ("served_other_host", code) when the page loaded and served links,
        just not for the requested file host -- it is not the "the source
        gave nothing back" shape that "stripped" names.
      - ("error", code_or_None) otherwise (includes SCRAPE_EXCEPTION and the
        no-diagnostic case).

    This function makes NO decision and has NO side effect -- it exists only
    to name what a reveal's diagnostic means for accounting purposes. See the
    HARD SCOPE RULE above the reveal-accounting table in backend/database.py:
    no limit, refusal, cooldown, throttle, or warning threshold may be
    derived from this.

    This accounting outcome is a separate axis from source-HEALTH outcome:
    see record_scrape_outcome() below, which decides health independently
    from the same ScrapeDiagnostic and does not consult this function's
    result (or vice versa).
    """
    if links:
        return ("success", None)
    diagnostic = getattr(links, "diagnostic", None)
    if diagnostic is None:
        return ("error", None)
    code = diagnostic.code
    if not diagnostic.effective_transport_attempted:
        return ("refused", code.value)
    if diagnostic.health_owner == "coordinator" or code in _CHALLENGE_CODES:
        return ("challenge", code.value)
    if code is ScrapeCode.REQUESTED_HOST_MISSING:
        return ("served_other_host", code.value)
    if code in _STRIPPED_CODES:
        return ("stripped", code.value)
    return ("error", code.value)


def record_scrape_outcome(db, source: str, links) -> None:
    """Persist a successful scrape or a health-affecting structured failure.

    HDEncode traffic-policy events are persisted by the coordinator. Recording
    those diagnostics here used to increment the failure streak twice and
    overwrite the one-hour challenge cooldown with NULL.

    Health outcome is a separate semantic axis from reveal-accounting outcome
    (classify_reveal_outcome, above): both are computed independently from the
    same ScrapeDiagnostic, and neither reads the other's result. Concretely: a
    diagnostic coded REQUESTED_HOST_MISSING or NO_FILE_HOST_LINKS records a
    health SUCCESS here (db.record_source_success), for any source, though
    today the only production caller (DownloadService.scrape_links_recorded)
    always passes source='hdencode', even though classify_reveal_outcome
    names that same diagnostic
    "served_other_host" or "stripped" for accounting -- "stripped" (or
    "served_other_host") in accounting does not imply a health failure was
    recorded, or vice versa. This is current, intentional behaviour, not a
    bug: whether "the page loaded but had nothing for us" should count as the
    source being healthy is an open policy decision, owned by whoever next
    reviews source-health policy (round-7 peer review), not settled by this
    module. Coupling the two systems to close that gap is exactly what the
    HARD SCOPE RULE above the reveal-accounting table in backend/database.py
    forbids.
    """
    if db is None:
        return
    diagnostic = getattr(links, "diagnostic", None)
    try:
        if links:
            db.record_source_success(source)
            return
        if (
            source == "hdencode"
            and diagnostic is not None
            and diagnostic.health_owner == "coordinator"
        ):
            return
        if diagnostic is not None and diagnostic.code in {
            ScrapeCode.REQUESTED_HOST_MISSING,
            ScrapeCode.NO_FILE_HOST_LINKS,
        }:
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
        return
