"""A state-machine model of download-queue LIVENESS, written from the rule.

WHY THIS FILE EXISTS, and why it is different from every other test here.

Peer review round 10 required it, and the reasoning is the important part. This
subsystem has produced the same failure four rounds running:

    producer/owner A updates one representation
    consumer/owner B relies on another
    local example tests pass
    the transition between A and B is untested

Every fix so far arrived with a test written FROM the defect, so the test inherited
the fix's blind spot. Three times a test of mine actively protected the thing it was
written to catch -- most recently in the very PR that removed an unmeasured claim,
where my own assertion required that claim to be present.

So this file does not encode any known bug. It encodes the RULE, then explores
sequences of ordinary operations looking for sequences that break it. The bug found by
peer review (retry one item -> its deferred siblings become unreachable) is not
mentioned in the invariant at all; the enumerator finds it.

THE RULE (round 10's wording, tightened into something checkable):

    Every nonterminal item is either runnable, or deliberately deferred with a
    recovery path that the AUTOMATIC machinery can take, or held because retrying an
    unknown outcome would be unsafe. There is no ordinary "nonterminal but
    unreachable" fourth state.

    Corollary the orphan violates: batch aggregate metadata must not be able to
    revoke a child's future transition.

INDEPENDENCE OF THE ORACLE. Round 10 warned specifically against defining the model's
truth in the implementation's terms. So `_liveness_violations` below never calls
`_maybe_auto_resume`, never reproduces its SQL, and never mentions
`state == 'paused_source'` or item/batch cooldown equality -- the two synchronisation
requirements that ARE the bug. It asks only: after settling (all cooldowns expired and
the automatic sweep run to quiescence), is this item runnable? An item that needs a
human to touch it is a liveness violation unless it is explicitly held for
unknown-outcome safety or has exhausted its retry budget.

NO NEW DEPENDENCY. Hypothesis' RuleBasedStateMachine would give shrinking, and the
review noted it would be useful, but the runtime image does not ship hypothesis and
adding it to the image is a bigger change than this test justifies. Round 10 said an
exhaustive deterministic enumerator over the small abstract state space is still a
major improvement, so that is what this is -- and the sequences are short enough that
the failing one it reports IS the minimal reproducer.
"""
from __future__ import annotations

import itertools
from unittest.mock import MagicMock

import pytest

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService

#: Long past, so any cooldown comparison treats it as due. "Settling" the model means
#: every cooldown in the database is already expired.
EXPIRED = "2000-01-01T00:00:00+00:00"
STAMP = "2026-08-07T04:00:00+00:00"

#: States from which the scheduler can actually make progress. `_claim_due` claims
#: only scheduled/ready, so anything else is either finished or waiting on something.
RUNNABLE = frozenset({"scheduled", "ready", "claimed"})
TERMINAL = frozenset({"completed", "cancelled", "failed"})
#: Deferred: deliberately not runnable, and therefore REQUIRING a recovery path.
DEFERRED = frozenset({"waiting_source", "verification_required"})

#: Outcomes where retrying is unsafe because we do not know whether the previous
#: attempt took effect. Liveness must NOT be asserted for these -- safety wins.
UNKNOWN_OUTCOME = ("operation_timeout_unknown", "interrupted_unknown_outcome")


def _items(n):
    return [{"url": f"https://hdencode.org/release-{i}-2160p/",
             "title": f"Release {i}", "media_type": "movie"} for i in range(n)]


def _rig(db, count=3, config=None):
    fake = MagicMock()
    service = DownloadQueueService(config or {}, db, fake)
    service._coordinator_snapshot = MagicMock(return_value={"blocked": False})
    batch = service.schedule_batch(_items(count), interval_minutes=0,
                                   mode="immediate",
                                   auto_resume_after_cooldown=True)
    return service, batch["batch_uuid"]


# ─────────────────────────────────────────────────────────────────────────────
# Operations. Each is an ordinary thing the system or an operator really does.
# ─────────────────────────────────────────────────────────────────────────────

def op_pause_source(db, service, batch_uuid):
    """Park the batch the way a source-wide throttle parks one."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE download_queue_items "
            "SET state='waiting_source', queue_reason='source_deferred', "
            "    cooldown_until=?, last_reason_code='source_temporarily_blocked', "
            "    updated_at=? WHERE batch_uuid=? AND state NOT IN "
            "    ('completed','cancelled')",
            (EXPIRED, STAMP, batch_uuid))
        conn.execute(
            "UPDATE download_queue_batches SET state='paused_source', paused_at=?, "
            "  cooldown_until=?, last_reason_code='reveal_verification_stalled' "
            "WHERE batch_uuid=?", (STAMP, EXPIRED, batch_uuid))
        service._refresh_batch_locked(conn, batch_uuid, STAMP)


def op_retry_one(db, service, batch_uuid):
    """An operator retries ONE item. Completely ordinary; it is also the trigger."""
    rows = db._query_dicts(
        "SELECT item_uuid FROM download_queue_items WHERE batch_uuid=? "
        "AND state IN ('waiting_source','verification_required','failed') "
        "ORDER BY sequence_number LIMIT 1", (batch_uuid,), default=[])
    if rows:
        try:
            service.retry_item(rows[0]["item_uuid"])
        except Exception:
            pass


def op_resume_batch(db, service, batch_uuid):
    try:
        service.resume_batch(batch_uuid, 0)
    except Exception:
        pass


def op_complete_one(db, service, batch_uuid):
    rows = db._query_dicts(
        "SELECT item_uuid FROM download_queue_items WHERE batch_uuid=? "
        "AND state IN ('ready','scheduled') ORDER BY sequence_number LIMIT 1",
        (batch_uuid,), default=[])
    if rows:
        with db.transaction() as conn:
            conn.execute("UPDATE download_queue_items SET state='completed', "
                         "updated_at=? WHERE item_uuid=?",
                         (STAMP, rows[0]["item_uuid"]))
            service._refresh_batch_locked(conn, batch_uuid, STAMP)


def op_cancel_one(db, service, batch_uuid):
    rows = db._query_dicts(
        "SELECT item_uuid FROM download_queue_items WHERE batch_uuid=? "
        "AND state IN ('ready','scheduled','waiting_source') "
        "ORDER BY sequence_number DESC LIMIT 1", (batch_uuid,), default=[])
    if rows:
        try:
            service.cancel_item(rows[0]["item_uuid"])
        except Exception:
            pass


OPERATIONS = {
    "pause": op_pause_source,
    "retry1": op_retry_one,
    "resume": op_resume_batch,
    "done1": op_complete_one,
    "cancel1": op_cancel_one,
}


def settle(db, service, batch_uuid, rounds=4):
    """Run the AUTOMATIC recovery machinery to quiescence, all cooldowns expired.

    This is the model's notion of "give the system every chance to recover by
    itself". No operator action. If an item is still deferred after this, the system
    has no automatic future transition for it.
    """
    with db.transaction() as conn:
        # Every cooldown in the world is already in the past.
        conn.execute("UPDATE download_queue_items SET cooldown_until=? "
                     "WHERE cooldown_until IS NOT NULL", (EXPIRED,))
        conn.execute("UPDATE download_queue_batches SET cooldown_until=? "
                     "WHERE cooldown_until IS NOT NULL", (EXPIRED,))
    # NO blanket except. My first version called `_maybe_auto_resume("hdencode")` --
    # the method takes NO arguments -- so every call raised TypeError, `except
    # Exception: pass` swallowed it, and auto-resume NEVER RAN in any sequence. The
    # model then reported 49 "stranded" sequences that were pure harness artifact,
    # and I almost published that as a finding. A broken driver makes every arm fail,
    # which is indistinguishable from every arm being broken.
    #
    # test_settle_actually_recovers_a_paused_batch below is the positive control that
    # makes this impossible to repeat: if settling cannot recover a batch that is
    # perfectly eligible, the harness is broken and that test says so.
    for _ in range(rounds):
        service._maybe_auto_resume()


def _liveness_violations(db, batch_uuid):
    """Items with no future automatic transition. The ORACLE.

    Deliberately says nothing about batch state or cooldown equality -- those are the
    implementation's synchronisation requirements, and encoding them here would be
    copying the defect into the oracle.
    """
    rows = db._query_dicts(
        "SELECT item_uuid, state, last_reason_code, queue_reason "
        "FROM download_queue_items WHERE batch_uuid=?", (batch_uuid,), default=[])
    bad = []
    for r in rows:
        state = str(r["state"] or "")
        if state in TERMINAL or state in RUNNABLE:
            continue
        if state not in DEFERRED:
            bad.append((r["item_uuid"], state, "unrecognised state"))
            continue
        # Unknown-outcome safety is a LEGITIMATE reason to stay put; retrying could
        # duplicate a download that actually happened. Safety outranks liveness.
        if str(r["last_reason_code"] or "") in UNKNOWN_OUTCOME:
            continue
        bad.append((r["item_uuid"], state, str(r["last_reason_code"] or "")))
    return bad


def _budget_exhausted(db, batch_uuid, limit=3):
    row = db._query_dicts(
        "SELECT auto_resume_used u FROM download_queue_batches WHERE batch_uuid=?",
        (batch_uuid,), default=[])
    return bool(row) and int(row[0]["u"] or 0) >= limit


# ─────────────────────────────────────────────────────────────────────────────
# The enumerator
# ─────────────────────────────────────────────────────────────────────────────

def test_settle_actually_recovers_a_paused_batch(tmp_path):
    """POSITIVE CONTROL. Run this before trusting any result in this file.

    A batch that is eligible in every respect -- paused_source, cooldown expired,
    auto-resume enabled, budget unspent, children deferred with a recognised reason
    and matching cooldowns -- MUST come back after settling. If it does not, the
    harness is not exercising automatic recovery and every "violation" this module
    reports is meaningless.

    This test exists because that is exactly what happened: settle() called
    `_maybe_auto_resume("hdencode")`, the method takes no arguments, and a blanket
    except hid the TypeError.
    """
    db = DatabaseManager(str(tmp_path / "control.db"))
    try:
        service, batch = _rig(db, count=3)
        op_pause_source(db, service, batch)
        deferred_before = db._query_dicts(
            "SELECT COUNT(*) n FROM download_queue_items "
            "WHERE batch_uuid=? AND state='waiting_source'", (batch,), default=[])
        assert int(deferred_before[0]["n"]) == 3, "the pause did not defer anything"

        settle(db, service, batch)

        after = db._query_dicts(
            "SELECT state, COUNT(*) n FROM download_queue_items "
            "WHERE batch_uuid=? GROUP BY 1", (batch,), default=[])
        states = {r["state"]: r["n"] for r in after}
        assert not states.get("waiting_source"), (
            f"settling did NOT recover a fully eligible paused batch: {states}. "
            "The harness is broken; ignore every other result in this file until "
            "this passes.")
    finally:
        db.close()


@pytest.mark.parametrize("depth", [2, 3])
def test_no_operation_sequence_strands_an_item(tmp_path, depth):
    """Explore every operation sequence of the given depth and check the RULE.

    Reports the shortest offending sequence with the item states, so the failure
    message is a reproducer rather than a hint.
    """
    failures = []
    for i, seq in enumerate(itertools.product(sorted(OPERATIONS), repeat=depth)):
        db = DatabaseManager(str(tmp_path / f"m{depth}_{i}.db"))
        try:
            service, batch = _rig(db)
            for name in seq:
                OPERATIONS[name](db, service, batch)
            settle(db, service, batch)
            if _budget_exhausted(db, batch):
                continue          # a deliberate policy stop, not a liveness loss
            bad = _liveness_violations(db, batch)
            if bad:
                failures.append((seq, bad))
        finally:
            db.close()

    if failures:
        seq, bad = min(failures, key=lambda f: len(f[0]))
        lines = [f"  {u[:8]}  state={s:22s} last_reason={c}" for u, s, c in bad]
        raise AssertionError(
            f"{len(failures)} of the explored sequences leave an item with no "
            f"automatic recovery path.\n"
            f"shortest offender: {' -> '.join(seq)} -> settle\n"
            f"{len(bad)} stranded item(s):\n" + "\n".join(lines) + "\n\n"
            "The rule: a nonterminal item is runnable, or deferred with a recovery "
            "path the automatic machinery can take, or held for unknown-outcome "
            "safety. These are none of those.")


# ─────────────────────────────────────────────────────────────────────────────
# The five concrete cases round 10 required, kept readable alongside the model.
# The model explores; these document.
# ─────────────────────────────────────────────────────────────────────────────

def test_pause_then_retry_one_then_settle_recovers_the_siblings(tmp_path):
    """THE INCIDENT. 34 of Jesse's downloads reached exactly this state.

    retry_item() sets the batch to `scheduled` with `cooldown_until = NULL`
    regardless of deferred siblings, and _refresh_batch_locked only ever writes
    `completed` -- it never restores `paused_source`. Automatic recovery starts by
    selecting paused_source batches, so the siblings become unreachable.
    """
    db = DatabaseManager(str(tmp_path / "incident.db"))
    try:
        service, batch = _rig(db, count=3)
        op_pause_source(db, service, batch)
        op_retry_one(db, service, batch)
        settle(db, service, batch)
        bad = _liveness_violations(db, batch)
        assert not bad, (
            "retrying ONE item stranded its deferred siblings: "
            + ", ".join(f"{u[:8]}={s}" for u, s, _ in bad))
    finally:
        db.close()


def test_a_deferred_child_is_reachable_even_when_the_batch_is_not_paused(tmp_path):
    """Batch aggregate state must not be the sole liveness authority."""
    db = DatabaseManager(str(tmp_path / "notpaused.db"))
    try:
        service, batch = _rig(db, count=2)
        op_pause_source(db, service, batch)
        with db.transaction() as conn:
            # The batch moves on -- exactly what retry_item does -- while a child
            # stays deferred.
            conn.execute("UPDATE download_queue_batches "
                         "SET state='scheduled', cooldown_until=NULL "
                         "WHERE batch_uuid=?", (batch,))
        settle(db, service, batch)
        assert not _liveness_violations(db, batch), (
            "a deferred child became unreachable because its BATCH changed state")
    finally:
        db.close()


def test_a_ready_sibling_does_not_revoke_a_deferred_one(tmp_path):
    db = DatabaseManager(str(tmp_path / "mixed.db"))
    try:
        service, batch = _rig(db, count=3)
        op_pause_source(db, service, batch)
        op_retry_one(db, service, batch)
        op_complete_one(db, service, batch)      # the retried one finishes
        settle(db, service, batch)
        assert not _liveness_violations(db, batch), (
            "one child completing left the others with no way back")
    finally:
        db.close()


def test_benign_cooldown_divergence_is_not_permanent_liveness_loss(tmp_path):
    """Two copies of one recovery fact must not have to match exactly.

    The resume path joins items to their batch on `cooldown_until` EQUALITY, so a
    one-second difference between two timestamps that mean the same thing is enough
    to strand the work.
    """
    db = DatabaseManager(str(tmp_path / "divergent.db"))
    try:
        service, batch = _rig(db, count=2)
        op_pause_source(db, service, batch)
        with db.transaction() as conn:
            conn.execute("UPDATE download_queue_items "
                         "SET cooldown_until='2000-01-01T00:00:01+00:00' "
                         "WHERE batch_uuid=?", (batch,))
        settle(db, service, batch)
        assert not _liveness_violations(db, batch), (
            "a one-second timestamp difference cost these items their recovery path")
    finally:
        db.close()


def test_unknown_outcome_items_are_never_auto_retried(tmp_path):
    """Safety outranks liveness, and must stay that way after any recovery fix.

    The DISCRIMINATING half of this file: a fix that made everything reachable by
    relaxing the filters would pass every test above and fail this one.
    """
    db = DatabaseManager(str(tmp_path / "unknown.db"))
    try:
        service, batch = _rig(db, count=2)
        op_pause_source(db, service, batch)
        with db.transaction() as conn:
            conn.execute("UPDATE download_queue_items "
                         "SET last_reason_code='operation_timeout_unknown' "
                         "WHERE batch_uuid=?", (batch,))
        settle(db, service, batch)
        rows = db._query_dicts(
            "SELECT state FROM download_queue_items WHERE batch_uuid=?",
            (batch,), default=[])
        assert all(r["state"] in DEFERRED for r in rows), (
            "an unknown-outcome item was made runnable again; retrying it could "
            "duplicate a download that already happened")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# TEMPORAL SAFETY — the second invariant, added on peer review round 12
# ─────────────────────────────────────────────────────────────────────────────
#
# WHY THE MODEL ABOVE COULD NOT SEE THIS CLASS OF BUG.
#
# settle() rewrites EVERY non-NULL cooldown to an expired timestamp before running
# recovery. That is exactly right for the liveness question -- "given every chance,
# does this item come back?" -- and it destroys the only thing needed to ask the
# safety question, because after settling no item is ever "not yet due". The model
# was structurally incapable of expressing "A is due while B is not", which is
# precisely the state the round-12 defect needed.
#
# So liveness-only was not a depth problem or an alphabet problem. It was the wrong
# question. The invariant here is the missing half:
#
#     Automatic recovery must never make an item runnable before the retry
#     authorisation applying to THAT item is due.
#
# THE DEFECT IT CATCHES WAS MINE, introduced in the commit that fixed the liveness
# hole. Discovery grouped by (batch, source) and used MIN(cooldown_until) to decide
# the group was due; _resume_batch then promoted every deferred child regardless of
# its own time. One due item dragged its siblings along -- including one due in 2030,
# and one with no retry time at all, which defeated the NULL-on-both-sides safety rule
# preserved fifteen lines earlier in the same function.

PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2030-01-01T00:00:00+00:00"


def _two_deferred(db, *, batch_cooldown, first, second):
    """Two deferred siblings with independently chosen cooldowns."""
    service, batch = _rig(db, count=2)
    ids = [r["item_uuid"] for r in db._query_dicts(
        "SELECT item_uuid FROM download_queue_items WHERE batch_uuid=? "
        "ORDER BY sequence_number", (batch,), default=[])]
    with db.transaction() as conn:
        for uid, cooldown in zip(ids, (first, second)):
            conn.execute(
                "UPDATE download_queue_items SET state='waiting_source', "
                "queue_reason='source_deferred', cooldown_until=?, "
                "last_reason_code='source_temporarily_blocked' WHERE item_uuid=?",
                (cooldown, uid))
        conn.execute("UPDATE download_queue_batches SET state='paused_source', "
                     "cooldown_until=?, auto_resume_used=0 WHERE batch_uuid=?",
                     (batch_cooldown, batch))
    return service, batch, ids


def _states(db, ids):
    return [db._query_dicts(
        "SELECT state FROM download_queue_items WHERE item_uuid=?",
        (i,), default=[])[0]["state"] for i in ids]


def test_a_due_item_does_not_drag_a_future_sibling(tmp_path):
    """ROUND 12 CASE 1. Verified against the pre-fix code: both went ready."""
    db = DatabaseManager(str(tmp_path / "drag_future.db"))
    try:
        service, _batch, ids = _two_deferred(
            db, batch_cooldown=None, first=PAST, second=FUTURE)
        service._maybe_auto_resume()
        got = _states(db, ids)
        assert got[0] == "ready", f"the DUE item must resume; got {got}"
        assert got[1] == "waiting_source", (
            f"the sibling is due in 2030 and must not be resumed now; got {got}")
    finally:
        db.close()


def test_a_due_item_does_not_drag_a_sibling_with_no_retry_time(tmp_path):
    """ROUND 12 CASE 2, and the sharper one.

    MIN() ignores NULLs, so a due sibling made the group look authorised and the
    NULL-cooldown item came along -- defeating the safety rule that a row with no
    retry time anywhere must not be retried automatically. That rule is stated in
    _maybe_auto_resume and was being enforced at one gate only.
    """
    db = DatabaseManager(str(tmp_path / "drag_null.db"))
    try:
        service, _batch, ids = _two_deferred(
            db, batch_cooldown=None, first=PAST, second=None)
        service._maybe_auto_resume()
        got = _states(db, ids)
        assert got[0] == "ready", f"the DUE item must resume; got {got}"
        assert got[1] == "waiting_source", (
            "no item cooldown and no shared brake means nothing authorises this "
            f"retry; got {got}")
    finally:
        db.close()


def test_an_expired_shared_brake_does_not_authorise_a_future_item(tmp_path):
    """ROUND 12 CASE 3. The brake being off is permission for the SOURCE, not for
    an item that has asked to wait longer."""
    db = DatabaseManager(str(tmp_path / "brake_off.db"))
    try:
        service, _batch, ids = _two_deferred(
            db, batch_cooldown=PAST, first=PAST, second=FUTURE)
        service._maybe_auto_resume()
        got = _states(db, ids)
        assert got[0] == "ready", got
        assert got[1] == "waiting_source", (
            f"an expired batch brake must not override the item's own time; {got}")
    finally:
        db.close()


def test_two_due_items_both_resume(tmp_path):
    """ROUND 12 CASE 4 — the POSITIVE control for this whole section.

    Without it, every test above passes on an implementation that resumes nothing.
    """
    db = DatabaseManager(str(tmp_path / "both_due.db"))
    try:
        service, _batch, ids = _two_deferred(
            db, batch_cooldown=None, first=PAST, second=PAST)
        service._maybe_auto_resume()
        got = _states(db, ids)
        assert got == ["ready", "ready"], (
            f"both items are due and must both resume; got {got}")
    finally:
        db.close()


def test_a_future_shared_brake_holds_everything(tmp_path):
    """ROUND 12 CASE 5. The shared brake outranks individual readiness."""
    db = DatabaseManager(str(tmp_path / "brake_on.db"))
    try:
        service, _batch, ids = _two_deferred(
            db, batch_cooldown=FUTURE, first=PAST, second=PAST)
        service._maybe_auto_resume()
        got = _states(db, ids)
        assert got == ["waiting_source", "waiting_source"], (
            f"the source is deliberately quiet until 2030; got {got}")
    finally:
        db.close()
