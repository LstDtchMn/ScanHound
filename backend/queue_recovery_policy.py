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
VERIFICATION_HOLD = "verification_hold"    # deliberate; a timer must NEVER release it

#: Decisions that will NOT resolve on their own.
#:
#: SAFETY_HOLD BELONGS HERE and was missing, which round 14 caught. Nothing automatic
#: ever changes an unknown-outcome row, so omitting it made the tools report
#: "every deferred item has a recovery path" about a row that has none by design, and
#: made the watcher wait on it forever.
#:
#: VERIFICATION_HOLD belongs here for the same reason: the whole point of the
#: decision is that nothing automatic ever clears it.
NEEDS_HUMAN = frozenset({NO_AUTHORISATION, UNOWNED_REASON, DISABLED, BUDGET_SPENT,
                         SAFETY_HOLD, VERIFICATION_HOLD})

#: Transient: no action needed, it will clear.
WILL_CLEAR = frozenset({WAITING_OWN, WAITING_BRAKE})

# ── WHAT A HUMAN SHOULD ACTUALLY DO ─────────────────────────────────────────
#
# "Needs a human" is not one action, and round 14 found the conflation dangerous:
# watch_resume told every human-required row to "resume explicitly", which for an
# unknown-outcome row is UNSAFE ADVICE -- a blind retry can duplicate a delivery that
# already happened. That is the one case where the diagnostics could cause harm rather
# than merely mislead, so the action is part of the policy rather than left to each
# tool's phrasing.
ACTION_ADJUDICATE = "adjudicate"        # check the external state FIRST; never blind
ACTION_MANUAL_RESUME = "manual_resume"  # safe to resume explicitly
ACTION_CONFIGURATION = "configuration"  # a setting, not an item, is the problem
ACTION_WAIT = "wait"                    # will clear on its own
ACTION_NONE = "none"                    # due; the scheduler will take it
#: DISTINCT from ACTION_MANUAL_RESUME on purpose. "Safe to resume explicitly"
#: promises the resume fixes the problem; for a verification hold it does not —
#: the source is presenting a challenge our automated browser cannot complete,
#: so a resume is only a PROBE that re-checks whether the challenge has lifted.
ACTION_ATTENTION_REQUIRED = "attention_required"

ACTION_FOR = {
    SAFETY_HOLD: ACTION_ADJUDICATE,
    NO_AUTHORISATION: ACTION_MANUAL_RESUME,
    BUDGET_SPENT: ACTION_MANUAL_RESUME,
    UNOWNED_REASON: ACTION_MANUAL_RESUME,
    DISABLED: ACTION_CONFIGURATION,
    VERIFICATION_HOLD: ACTION_ATTENTION_REQUIRED,
    WAITING_OWN: ACTION_WAIT,
    WAITING_BRAKE: ACTION_WAIT,
    AUTHORISED: ACTION_NONE,
}

#: Human-readable instruction per action. The adjudicate text deliberately does NOT
#: say "retry" anywhere.
ACTION_ADVICE = {
    ACTION_ADJUDICATE: (
        "CHECK WHETHER IT ALREADY DOWNLOADED before doing anything. The previous "
        "attempt's outcome is unknown, so retrying could fetch the same release twice. "
        "Do not use a plain resume on these."
    ),
    ACTION_MANUAL_RESUME: (
        "Safe to resume explicitly (Downloads page, or the batch resume endpoint)."
    ),
    ACTION_CONFIGURATION: (
        "Turn auto-resume back on for this batch; the items themselves are fine."
    ),
    # DELIBERATELY does not promise that retrying completes the verification.
    # It cannot: the challenge runs inside ScanHound's own automated browser
    # session, which the UI offers no way to interact with. "Retry now" is a
    # probe of whether the challenge has lifted, nothing more.
    ACTION_ATTENTION_REQUIRED: (
        "Manual attention required: automated verification did not complete, and "
        "waiting will not clear it. 'Retry now' on ONE item sends a single probe "
        "to check whether the source still presents the challenge; it cannot "
        "complete the verification itself. Do not retry all items at once."
    ),
    ACTION_WAIT: "Nothing to do; this clears by itself.",
    ACTION_NONE: "Nothing to do; the scheduler will pick it up.",
}


#: Every decision decide() can return. A new one must be added HERE and to ACTION_FOR,
#: which test_every_decision_has_an_action enforces -- so a decision cannot reach an
#: operator tool without someone deciding what a human should do about it.
ALL_DECISIONS = frozenset({AUTHORISED, WAITING_OWN, WAITING_BRAKE, SAFETY_HOLD,
                           NO_AUTHORISATION, UNOWNED_REASON, DISABLED, BUDGET_SPENT,
                           VERIFICATION_HOLD})


def action_for(decision: str) -> str:
    """The action a human should take, given a decision.

    FAILS CLOSED, corrected on peer review round 15. This was:

        return ACTION_FOR.get(decision, ACTION_MANUAL_RESUME)

    under a docstring of mine that said "Never guesses 'retry'" -- while defaulting to
    exactly that. If a future decision were added to decide() and ACTION_FOR not
    extended, every operator tool would have silently called it "safe to resume
    explicitly". For an unmapped state that is the most dangerous possible default, and
    it contradicted the contract written directly above it.

    An unknown decision now raises, so the tool boundary reports UNKNOWN rather than
    inventing permission.
    """
    try:
        return ACTION_FOR[decision]
    except KeyError:
        raise KeyError(
            f"no operator action mapped for decision {decision!r}. Add it to "
            "ACTION_FOR and ALL_DECISIONS; an unmapped safety state must never "
            "default to 'safe to resume'."
        ) from None


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
    #: True when this group is under an ACTIVE human-verification hold for the
    #: item's source (the batch's `verification_hold_source` matches it). The
    #: caller resolves the source match; this type carries only the verdict, so
    #: the policy cannot mis-compare two different spellings of one source.
    verification_hold: bool = False
    #: When this batch last changed (its `updated_at`), and how long it must
    #: stay unchanged before a SPENT budget may release one further attempt.
    #: See the BUDGET_SPENT branch in decide() for why this exists and why it
    #: is not a counter reset. `None` disables it entirely, which is what a
    #: caller that cannot supply the timestamp must get: the strict old
    #: behaviour, never an accidental unlimited retry.
    quiet_since: Optional[datetime] = None
    exhausted_retry_window_seconds: Optional[int] = None


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


def _quiet_long_enough(shared: SharedFacts, now: datetime) -> bool:
    """Has this batch been untouched long enough to earn one more attempt?

    Fails CLOSED on every uncertainty -- absent timestamp, absent window,
    non-positive window, unparsable value. A spent budget that stays spent is
    a visible stall someone can act on; a spent budget that silently retries
    forever is the failure mode this whole clause was warned about.
    """
    if shared.quiet_since is None or shared.exhausted_retry_window_seconds is None:
        return False
    try:
        window = int(shared.exhausted_retry_window_seconds)
    except (TypeError, ValueError):
        return False
    if window <= 0:
        return False
    quiet = shared.quiet_since
    if quiet.tzinfo is None:
        quiet = quiet.replace(tzinfo=timezone.utc)
    return (now - quiet).total_seconds() >= window


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

    # A HUMAN-VERIFICATION HOLD IS NOT A COOLDOWN. Added 2026-08-09, after the
    # Turnstile root cause: this function returned AUTHORISED for
    # (state="verification_required", queue_reason="interactive_challenge",
    # cooldown_until=<expired>) — so a timer alone released a hold whose entire
    # meaning is "an interactive challenge our automated browser cannot complete
    # is in the way". Reclassifying Turnstile correctly would then have fed every
    # item straight back into the same failing challenge on a schedule.
    #
    # Two facts can put a row under the hold, and each covers the other's gap:
    #   * its OWN queue_reason is interactive_challenge (the triggering item);
    #   * its group carries an active hold (shared.verification_hold) — the
    #     siblings parked as source_deferred by the same challenge, which would
    #     otherwise auto-resume one by one into the challenge.
    #
    # Placed BEFORE the configuration/budget/time checks because every later
    # decision's advice is wrong for this row: DISABLED says "turn auto-resume
    # on; the items are fine", BUDGET_SPENT and NO_AUTHORISATION say "safe to
    # resume explicitly" — and none of those actions completes a verification.
    # SAFETY and OWNERSHIP still outrank it: an unknown outcome must be
    # adjudicated first, and an unowned reason must keep saying it is unowned.
    #
    # Release is NOT time-based by design. The hold clears only when a probe the
    # operator explicitly promoted actually succeeds against the source
    # (download_queue._complete clears the batch's verification_hold_source on a
    # real source delivery).
    if item.queue_reason == "interactive_challenge" or shared.verification_hold:
        return VERIFICATION_HOLD

    if not shared.auto_resume_enabled:
        return DISABLED

    progressed = shared.source_delivery_count > shared.progress_mark
    if shared.attempts_used >= shared.max_attempts and not progressed:
        # THE BUDGET DEADLOCKS ITSELF (design review F5). Refunding requires a
        # source DELIVERY; getting a delivery requires a RETRY. Once the counter
        # is spent with nothing delivered, the only exit is a human -- and the
        # items that most need recovery are exactly the ones that never
        # delivered.
        #
        # So a batch that has been completely QUIET for a long window gets one
        # further attempt. This is deliberately NOT a counter reset, which the
        # review warned turns a finite budget into an unbounded retry loop in
        # slow motion: the counter stays spent, so the batch does not regain an
        # allowance, and _resume_batch stamps `updated_at` on every attempt --
        # which restarts the quiet period. The result is at most one attempt per
        # window, forever, instead of none, forever.
        #
        # It cannot reach a held row: VERIFICATION_HOLD returns above this, and
        # it is checked before the budget precisely so no timer can release one.
        if not _quiet_long_enough(shared, now):
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

    # NO OWN COOLDOWN -> NOT AUTHORISED. Strict, changed on round 14.
    #
    # I previously treated an expired shared brake as the authorisation for a row with
    # no time of its own, and recorded the reviewer's provenance objection as a caveat
    # instead of acting on it. The objection is decisive once you check the producer:
    # _pause_for_source writes `cooldown_until` to the triggering item, to every
    # same-source sibling it defers, AND to the batch. So a healthy source pause always
    # gives a row its own time.
    #
    # A deferred row with cooldown_until = NULL is therefore NOT a healthy row whose
    # authorisation merely lives elsewhere. It means the outcome carried no time, or the
    # row is legacy/manual, or an invariant was broken -- exactly the cases where
    # inferring permission from an unprovenanced batch scalar is least defensible. The
    # batch has no `cooldown_source` and no `recovery_episode_id`; a batch can be
    # source="mixed", and a later pause overwrites the scalar. An expired batch cooldown
    # proves only that SOME event on that batch had a time, not that THIS row was
    # deferred by it.
    #
    # So it fails closed and asks for a human. When recovery-episode identity exists,
    # the episode can authorise its own rows without copying the time into each one.
    return NO_AUTHORISATION
