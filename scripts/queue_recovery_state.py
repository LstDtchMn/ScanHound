"""One read-only classifier for "can this deferred download still recover?".

WHY THIS EXISTS. Three separate things have now answered that question and disagreed
with each other and with production:

  * scanhound_check.py reported ANY nonzero auto_resume_used as a spent one-shot,
    which was true of a deployed container and false of the code. It told me 45
    recoverable downloads were permanently dead, and that is a large part of why I
    spent a session refining a throttle response for a source that was serving links
    in five seconds.
  * scanhound_check.py section 3b then declared every deferred item whose batch was
    not `paused_source` an orphan. True before item-first recovery, FALSE after it --
    so the fix that rescued 34 downloads made the diagnostic start crying wolf.
  * watch_resume.py used "no paused batch" as its success predicate, printing
    `SUCCESS: no batches are paused. waiting_source=34` -- the stranded count inside
    the success line. Round 10 caught that; round 12 caught that my fix had merely
    inverted the polarity to false FAILURE.

Every one of those was a second copy of a policy drifting from the first. So the
policy lives here once, both tools import it, and neither keeps its own idea of what
recovery requires.

DELIBERATELY NOT THE PRODUCTION CODE. This is a read-only interpreter of the same
RULES, not a call into DownloadQueueService -- the tools must run against a database
without constructing a service, and a diagnostic that imports the thing it inspects
cannot notice when that thing is wrong. The liveness-model oracle stays independent of
both, for the same reason.

Keep in step with backend/download_queue.py::_maybe_auto_resume and _resume_batch.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

#: States that are deliberately not runnable and therefore need a recovery path.
DEFERRED_STATES = ("waiting_source", "verification_required")

#: queue_reason values automatic recovery owns. Anything else it will not touch.
RECOGNISED_REASONS = ("interactive_challenge", "source_deferred")

#: Outcomes never auto-retried: we do not know whether the previous attempt took
#: effect, so retrying could duplicate a delivery that already happened.
UNKNOWN_OUTCOMES = ("operation_timeout_unknown", "interrupted_unknown_outcome")

#: Verdicts. Only ORPHANED means "no automatic path exists and no future time will
#: create one" -- the single state that actually needs a human.
RECOVERABLE = "recoverable by automatic machinery"
WAITING_ITEM = "waiting for its own retry time"
WAITING_BRAKE = "held by the shared source cooldown"
SAFETY_HOLD = "held for unknown-outcome safety"
ORPHANED = "ORPHANED: no automatic path"

NEEDS_HUMAN = (ORPHANED,)


def max_auto_resume_attempts(config_path=None, default=3):
    """The retry budget, read from the SAME config key production reads.

    Clamped 1..10 exactly as _auto_resume_max_attempts does. Hardcoding a second copy
    of the number is what started this whole class of problem.
    """
    try:
        if config_path is None:
            from backend.config import CONFIG_FILE as config_path
        with open(config_path, encoding="utf-8") as fh:
            raw = json.load(fh).get(
                "download_queue_auto_resume_max_attempts", default)
        return max(1, min(10, int(raw)))
    except Exception:                                          # noqa: BLE001
        return default


def _parse(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def classify_item(item, batch, *, now=None, max_attempts=3):
    """Why this deferred item is not running, and whether that will ever change.

    ``item`` needs state, cooldown_until, queue_reason, last_reason_code.
    ``batch`` needs state, cooldown_until, auto_resume_after_cooldown,
    auto_resume_used, source_delivery_count, auto_resume_progress_mark.

    NOTE what is NOT consulted: whether the batch is `paused_source`, and whether the
    item's cooldown EQUALS the batch's. Both were liveness prerequisites before
    item-first recovery and are prerequisites for nothing now. A diagnostic that keeps
    checking them reports faults that cannot occur.
    """
    now = now or datetime.now(timezone.utc)
    state = str(item.get("state") or "")
    if state not in DEFERRED_STATES:
        return None                       # not deferred; nothing to explain

    # SAFETY FIRST, and it outranks everything below. These rows are excluded on
    # purpose and no amount of waiting or configuration changes that.
    if str(item.get("last_reason_code") or "") in UNKNOWN_OUTCOMES:
        return SAFETY_HOLD

    if str(item.get("queue_reason") or "") not in RECOGNISED_REASONS:
        return ORPHANED                   # automatic recovery does not own this row

    if not int(batch.get("auto_resume_after_cooldown") or 0):
        return ORPHANED                   # the operator turned it off for this batch

    used = int(batch.get("auto_resume_used") or 0)
    progressed = (int(batch.get("source_delivery_count") or 0)
                  > int(batch.get("auto_resume_progress_mark") or 0))
    if used >= max_attempts and not progressed:
        # Budget spent on resumes that achieved nothing. A deliberate policy stop --
        # but it still needs a human, so it is reported as such rather than as a
        # healthy wait.
        return ORPHANED

    brake = _parse(batch.get("cooldown_until"))
    own = _parse(item.get("cooldown_until"))
    if brake is not None and brake > now:
        return WAITING_BRAKE              # transient: the source is quiet on purpose
    if own is not None and own > now:
        return WAITING_ITEM               # transient: this row asked to wait
    if own is None and brake is None:
        # Nothing anywhere says when this may run. The preserved safety rule: an
        # automatic retry needs an authorisation time, so this needs an explicit
        # operator resume.
        return ORPHANED
    return RECOVERABLE


def classify_all(rows, *, now=None, max_attempts=3):
    """Classify joined item+batch rows. Returns {verdict: [row, ...]}."""
    out = {}
    for row in rows:
        verdict = classify_item(row, row, now=now, max_attempts=max_attempts)
        if verdict is not None:
            out.setdefault(verdict, []).append(row)
    return out


#: The SQL both tools use, so they cannot select different populations either.
JOINED_DEFERRED_SQL = """
    SELECT i.item_uuid, i.batch_uuid, i.title, i.state, i.cooldown_until,
           i.queue_reason, i.last_reason_code,
           b.state                       AS batch_state,
           b.cooldown_until              AS batch_cooldown,
           b.auto_resume_after_cooldown  AS auto_resume_after_cooldown,
           b.auto_resume_used            AS auto_resume_used,
           b.source_delivery_count       AS source_delivery_count,
           b.auto_resume_progress_mark   AS auto_resume_progress_mark
    FROM download_queue_items i
    LEFT JOIN download_queue_batches b ON b.batch_uuid = i.batch_uuid
    WHERE i.state IN ('waiting_source', 'verification_required')
    ORDER BY i.batch_uuid, i.sequence_number
"""
