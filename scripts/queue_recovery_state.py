"""Read-only view of download-queue recovery, for the operator tools.

THIS FILE NO LONGER CONTAINS ANY POLICY. It maps database rows onto the typed facts in
`backend/queue_recovery_policy.py` and renders the decision that module returns.

WHY IT WAS EMPTIED. I wrote its first version to end policy drift between the app and
its diagnostics, on the reasoning that a diagnostic which imports the thing it inspects
cannot notice when that thing is wrong. That reasoning produced a THIRD copy of the
rules, and round 13 found it wrong in two ways at once:

  * `classify_all()` passed the same flattened row as BOTH the item and the batch, so
    `batch["cooldown_until"]` silently read the ITEM's cooldown and `batch_cooldown`
    was never consumed. `item PAST / batch FUTURE` reported "recoverable" when
    production holds it; `item NULL / batch PAST` reported "ORPHANED" when production
    resumes it. Live rows usually have identical item and batch cooldowns, which is
    exactly why checking it against the live database missed the bug.
  * scanhound_check.py kept its own retry-budget parser and the deleted cooldown-equality
    query in the same file that imported this one, so "one shared policy" was false when
    I wrote it.

The independence I was reaching for belongs in the liveness/safety MODEL, which
deliberately does not import the policy and judges it from an independently stated
invariant. Two executable copies of "the same rules" are not independent; they are just
two things that can disagree, and they did.

Passing typed dataclasses also makes the aliasing bug impossible to write again: there
is no field name that means one thing on ItemFacts and another on SharedFacts.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from backend.queue_recovery_policy import (  # noqa: E402
    ACTION_ADVICE, AUTHORISED, BUDGET_SPENT, DISABLED, NEEDS_HUMAN, NO_AUTHORISATION,
    SAFETY_HOLD, UNOWNED_REASON, VERIFICATION_HOLD, WAITING_BRAKE, WAITING_OWN,
    WILL_CLEAR, ItemFacts, SharedFacts, action_for, decide, parse_max_attempts,
)

#: Plain-language rendering, because these are read by a person deciding whether to
#: intervene -- not by code. Only the NEEDS_HUMAN ones require action.
LABELS = {
    AUTHORISED: "due now, waiting for the next scheduler pass",
    WAITING_OWN: "waiting for its own retry time",
    WAITING_BRAKE: "held by the shared source cooldown",
    SAFETY_HOLD: "held for unknown-outcome safety (needs adjudication)",
    NO_AUTHORISATION: "NO retry time anywhere - needs an explicit resume",
    UNOWNED_REASON: "automatic recovery does not own this row - needs a resume",
    DISABLED: "auto-resume is switched off for its batch",
    BUDGET_SPENT: "retry budget spent with no progress - needs a resume",
    VERIFICATION_HOLD: (
        "held for manual verification - a timer will NOT release it"),
}

#: The SQL both tools use, so they cannot even select different populations.
JOINED_DEFERRED_SQL = """
    SELECT i.item_uuid, i.batch_uuid, i.title, i.state, i.cooldown_until,
           i.queue_reason, COALESCE(i.last_reason_code, '') AS last_reason_code,
           i.source                      AS item_source,
           b.state                       AS batch_state,
           b.cooldown_until              AS batch_cooldown,
           b.auto_resume_after_cooldown  AS auto_resume_after_cooldown,
           b.auto_resume_used            AS auto_resume_used,
           b.source_delivery_count       AS source_delivery_count,
           b.auto_resume_progress_mark   AS auto_resume_progress_mark,
           b.verification_hold_source    AS verification_hold_source
    FROM download_queue_items i
    LEFT JOIN download_queue_batches b ON b.batch_uuid = i.batch_uuid
    WHERE i.state IN ('waiting_source', 'verification_required')
    ORDER BY i.batch_uuid, i.sequence_number
"""


def _dt(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def load_max_attempts():
    """Read the budget from the same config production reads."""
    try:
        from backend.config import CONFIG_FILE
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            return parse_max_attempts(json.load(fh))
    except Exception:                                          # noqa: BLE001
        return parse_max_attempts({})


def facts_from_row(row):
    """Map one JOINED_DEFERRED_SQL row onto the two DISTINCT fact types.

    THE MAPPING IS THE WHOLE POINT. `i.cooldown_until` and `b.cooldown_until` arrive
    under different column names precisely so this function must choose deliberately;
    the previous version handed the same dict to both parameters and the shared brake
    was never read.
    """
    item = ItemFacts(
        state=str(row["state"] or ""),
        cooldown_until=_dt(row["cooldown_until"]),
        queue_reason=str(row["queue_reason"] or ""),
        last_reason_code=str(row["last_reason_code"] or ""),
    )
    # The hold applies to this row only when the batch's held source IS this
    # row's source — resolved here, exactly as _resume_batch resolves it, so
    # the tools and production judge the same fact the same way.
    hold_source = str(row["verification_hold_source"] or "")
    shared = SharedFacts(
        cooldown_until=_dt(row["batch_cooldown"]),          # <- the batch's, not the item's
        auto_resume_enabled=bool(row["auto_resume_after_cooldown"]),
        attempts_used=int(row["auto_resume_used"] or 0),
        source_delivery_count=int(row["source_delivery_count"] or 0),
        progress_mark=int(row["auto_resume_progress_mark"] or 0),
        max_attempts=load_max_attempts(),
        verification_hold=bool(
            hold_source and hold_source == str(row["item_source"] or "")
        ),
    )
    return item, shared


def classify_rows(rows, now=None):
    """Return {decision: [row, ...]} for JOINED_DEFERRED_SQL rows."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for row in rows:
        item, shared = facts_from_row(row)
        out.setdefault(decide(item, shared, now), []).append(row)
    return out


def needs_human(verdicts):
    return sum(len(verdicts.get(v, [])) for v in NEEDS_HUMAN)


def still_deferred(verdicts):
    """Rows that are deferred but WILL clear on their own, plus those merely due.

    A watcher must not call these "resolved": the recovery event it is watching for has
    not happened yet. Round 13 caught watch_resume.py exiting success on exactly this.
    """
    # SAFETY_HOLD IS NOT WAITING. Round 14: including it here made the tools treat a
    # row that nothing automatic will ever move as ordinary patience. It is counted by
    # needs_human() instead.
    return sum(len(verdicts.get(v, [])) for v in tuple(WILL_CLEAR) + (AUTHORISED,))


def advice_for(decision):
    """What a human should DO about this decision, from the shared policy.

    Never composed locally. Round 14 found watch_resume telling every human-required
    row to "resume explicitly" -- unsafe for an unknown-outcome row, where a blind
    retry can duplicate a delivery.
    """
    return ACTION_ADVICE[action_for(decision)]


def watcher_status(verdicts):
    """(code, message) for a watcher, from classified verdicts. PURE.

    EXTRACTED on peer review round 15. It lived inside watch_resume.py, which opens a
    database connection and cannot be imported in a test -- so the 22-test file's own
    docstring claimed to pin "the four watcher states" while pinning none of them. That
    is the second time I have written a false claim into test documentation (round 8's
    "drives the ROUTE, not the service" did the same), and the mechanism is identical:
    the prose described the intent, not the code.

    The four branches exist because this decision was got wrong four separate times, so
    they are now testable without a database.
    """
    human = needs_human(verdicts)
    waiting = still_deferred(verdicts)
    if human:
        # PER DECISION, never one blanket action. An unknown-outcome row must be told to
        # adjudicate, not to resume.
        lines = [f"{len(verdicts[d])} item(s) {d}: {advice_for(d)}"
                 for d in sorted(verdicts) if d in NEEDS_HUMAN]
        return "ACTION REQUIRED", " | ".join(lines)
    if waiting:
        return "WAITING", (f"{waiting} item(s) are deferred but all have a recovery "
                           "path; continuing to watch.")
    return "RESOLVED", "nothing is paused and nothing is deferred."
