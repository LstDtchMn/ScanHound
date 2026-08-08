"""ONE implementation of "may this deferred download retry yet?".

WHY THIS EXISTS, and why the previous attempt was not enough.

Round 12 and round 13 each found a bug caused by two places that had to agree and were
kept in step by hand:

  * round 12 -- discovery took MIN(cooldown_until) to decide a GROUP was due, then the
    transaction promoted every sibling. One due item dragged along a sibling due in
    2030 and one with no retry time at all. That defeated a safety rule stated fifteen
    lines earlier in the same function.
  * round 13 -- the mirror image. MIN() ignores NULLs, so with the shared brake expired,
    an item with NO cooldown and a sibling due in 2030 gave MIN = 2030, and the whole
    group was skipped. An ineligible sibling vetoed an eligible one.

I answered round 12 by adding a per-item predicate to the transaction while LEAVING the
group gate in place, so authorisation lived in two places with different logic. Round 13
is the direct consequence.

I also built scripts/queue_recovery_state.py to end policy drift between the app and its
diagnostics -- and it shipped with an aliasing bug (the same flattened row passed as both
item and batch, so the shared cooldown was never read) plus the old copies left intact in
scanhound_check.py. Three "same rule" implementations, three disagreements.

So the rule is not duplicated any more. It lives here, as a pure function over explicit
facts, and everything else calls it:

    backend/download_queue.py   reads CURRENT facts inside its transaction, calls this,
                                executes the decision
    scripts/scanhound_check.py  reads facts, calls this, renders the decision
    scripts/watch_resume.py     same
    tests/test_queue_liveness_model.py  DOES NOT import this -- it is the independent
                                oracle, and independence belongs there rather than in a
                                second executable copy of the rules

No database, no service, no I/O. Facts in, decision out.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

#: Deliberately not runnable, and therefore requiring a recovery path.
DEFERRED_STATES = frozenset({"waiting_source", "verification_required"})

#: queue_reason values automatic recovery owns. Anything else it will not touch.
RECOGNISED_REASONS = frozenset({"interactive_challenge", "source_deferred"})

#: Outcomes never auto-retried: we do not know whether the previous attempt took
#: effect, so a retry could duplicate a delivery that already happened. Safety
#: outranks liveness, and no waiting or configuration changes that.
UNKNOWN_OUTCOMES = frozenset({"operation_timeout_unknown",
                              "interrupted_unknown_outcome"})

# ── decisions ───────────────────────────────────────────────────────────────
AUTHORISED = "authorised"                  # may be made runnable now
WAITING_OWN = "waiting_own_cooldown"       # transient; its own time has not come
WAITING_BRAKE = "waiting_shared_cooldown"  # transient; the source is quiet on purpose
SAFETY_HOLD = "unknown_outcome_hold"       # deliberate; needs adjudication
NO_AUTHORISATION = "no_authorisation_time"  # needs an explicit operator resume
UNOWNED_REASON = "reason_not_owned"        # automatic recovery does not own this row
DISABLED = "auto_resume_disabled"          # the operator turned it off
BUDGET_SPENT = "retry_budget_spent"        # deliberate policy stop

#: Decisions that will NOT resolve on their own. Everything else either runs now or
#: clears with time.
NEEDS_HUMAN = frozenset({NO_AUTHORISATION, UNOWNED_REASON, DISABLED, BUDGET_SPENT})

#: Transient: no action needed, it will clear.
WILL_CLEAR = frozenset({WAITING_OWN, WAITING_BRAKE})


@dataclass(frozen=True)
class ItemFacts:
    """One deferred row's own facts. Distinct TYPE from SharedFacts on purpose.

    Round 13's classifier passed a single flattened dict as both the item and the
    batch, so `batch["cooldown_until"]` silently read the ITEM's cooldown and the
    shared brake was never consulted. Two dataclasses cannot alias like that: there is
    no field name that means one thing on one and another on the other.
    """

    state: str
    cooldown_until: Optional[datetime]
    queue_reason: str
    last_reason_code: str = ""


@dataclass(frozen=True)
class SharedFacts:
    """The batch/breaker facts that govern a group of deferred rows."""

    cooldown_until: Optional[datetime]
    auto_resume_enabled: bool
    attempts_used: int
    source_delivery_count: int = 0
    progress_mark: int = 0
    max_attempts: int = 3


def parse_max_attempts(config, default: int = 3) -> int:
    """The retry budget, clamped exactly as production clamps it.

    A second hardcoded copy of this number is how a diagnostic came to report 45
    recoverable downloads as permanently dead.
    """
    try:
        raw = (config or {}).get("download_queue_auto_resume_max_attempts", default)
        return max(1, min(10, int(raw)))
    except (TypeError, ValueError):
        return default


def decide(item: ItemFacts, shared: SharedFacts,
           now: Optional[datetime] = None) -> str:
    """May this deferred item be made runnable? Returns one of the decisions above.

    ORDER MATTERS and is deliberate:

    1. SAFETY first. An unknown execution outcome is never automatic, whatever else
       is true.
    2. Ownership and configuration next -- automatic recovery only touches rows it
       owns, in batches where it is enabled, within budget.
    3. TIME last, and per item. The shared brake is a veto for the whole group; the
       item's own cooldown is a veto for itself. NEITHER is allowed to authorise a row
       that the other would decline, and -- the round-13 lesson -- one row's veto is
       never applied to a different row.
    """
    now = now or datetime.now(timezone.utc)

    if item.state not in DEFERRED_STATES:
        return AUTHORISED          # not deferred; nothing to authorise

    if (item.last_reason_code or "") in UNKNOWN_OUTCOMES:
        return SAFETY_HOLD

    if (item.queue_reason or "") not in RECOGNISED_REASONS:
        return UNOWNED_REASON

    if not shared.auto_resume_enabled:
        return DISABLED

    progressed = shared.source_delivery_count > shared.progress_mark
    if shared.attempts_used >= shared.max_attempts and not progressed:
        return BUDGET_SPENT

    # THE SHARED BRAKE IS A VETO, NEVER A VETO-BY-PROXY. If it is still in the future
    # the whole group waits -- but that is because the brake applies to every member,
    # not because one member is not ready.
    if shared.cooldown_until is not None and shared.cooldown_until > now:
        return WAITING_BRAKE

    # THIS ITEM'S OWN TIME, and only this item's. Round 13's bug was a sibling's future
    # cooldown being allowed to speak here via MIN().
    if item.cooldown_until is not None:
        return AUTHORISED if item.cooldown_until <= now else WAITING_OWN

    # No own cooldown. The shared brake, having passed, is the authorisation -- and if
    # there is no brake either then nothing anywhere says when this may run, so it
    # needs an explicit operator resume rather than an automatic probe at a source
    # that may have just refused us.
    #
    # NOTE the reviewer's caveat, recorded rather than silently accepted: an expired
    # batch cooldown proves only that SOME source-wide event on this batch had a time,
    # not that this particular row was deferred by that event -- a batch can be
    # `source="mixed"`, and a later pause overwrites the scalar. Properly this wants a
    # recovery-episode identity carrying source, cooldown and budget. Until that
    # exists this fallback is a documented heuristic, not a proof.
    if shared.cooldown_until is not None:
        return AUTHORISED
    return NO_AUTHORISATION
