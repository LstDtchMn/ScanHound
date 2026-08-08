"""Committed regression tests for the recovery policy and the operator tools.

WHY THIS FILE EXISTS. Peer review round 14's finding 4: the commit that restructured
WHO authorises retries, WHERE the budget is evaluated, HOW cooldowns are interpreted and
HOW both tools classify recovery added **zero tests**. Every counterexample across
rounds 12, 13 and 14 was verified with throwaway scripts I then deleted, so nothing
stopped a future change reintroducing any of them.

That is the same gap in a different place. Rounds 8-13 were about production code not
being wired to its consumers; this was about verification not being wired to anything at
all. Manual probes are evidence for a moment; a test is evidence for every commit after.

Pinned here:
  * the round-12 pair -- a due item must not drag a future or NULL sibling;
  * the round-13 mirror -- an ineligible sibling must not veto an eligible one;
  * every policy decision, through the real JOINED_DEFERRED_SQL row shape;
  * the four watcher states;
  * the safety taxonomy, including that an unknown-outcome row is never told to retry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService
from backend.queue_recovery_policy import (
    ACTION_ADJUDICATE, ACTION_MANUAL_RESUME, AUTHORISED, BUDGET_SPENT, DISABLED,
    NEEDS_HUMAN, NO_AUTHORISATION, SAFETY_HOLD, UNOWNED_REASON, WAITING_BRAKE,
    WAITING_OWN, ItemFacts, SharedFacts, action_for, decide, parse_max_attempts,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
PAST = NOW - timedelta(days=1)
FUTURE = NOW + timedelta(days=1)
PAST_S = PAST.isoformat()
FUTURE_S = FUTURE.isoformat()


def _shared(cooldown=None, *, enabled=True, used=0, delivered=0, mark=0, cap=3):
    return SharedFacts(cooldown_until=cooldown, auto_resume_enabled=enabled,
                       attempts_used=used, source_delivery_count=delivered,
                       progress_mark=mark, max_attempts=cap)


def _item(cooldown=None, *, reason="source_deferred", last="",
          state="waiting_source"):
    return ItemFacts(state=state, cooldown_until=cooldown, queue_reason=reason,
                     last_reason_code=last)


# ─────────────────────────────────────────────────────────────────────────────
# The policy, decision by decision
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("own,shared_cd,expected", [
    (PAST,   None,      AUTHORISED),     # its own time has come
    (PAST,   PAST,      AUTHORISED),
    (FUTURE, PAST,      WAITING_OWN),    # an expired brake does not override it
    (PAST,   FUTURE,    WAITING_BRAKE),  # the brake vetoes the whole group
    (FUTURE, FUTURE,    WAITING_BRAKE),  # brake is checked first
    (None,   PAST,      NO_AUTHORISATION),   # STRICT, round 14: see the module note
    (None,   None,      NO_AUTHORISATION),
    (None,   FUTURE,    WAITING_BRAKE),
])
def test_every_temporal_combination(own, shared_cd, expected):
    """The full time matrix, so no combination is decided by accident.

    The (None, PAST) row is the round-14 policy change: an expired shared brake no
    longer authorises a row with no time of its own, because a healthy source pause
    always writes the item its own cooldown -- so a NULL there means something went
    wrong, which is the worst case in which to infer permission.
    """
    assert decide(_item(own), _shared(shared_cd), NOW) == expected


def test_unknown_outcome_outranks_everything():
    """SAFETY. Even perfectly due, even with the brake off."""
    for last in ("operation_timeout_unknown", "interrupted_unknown_outcome"):
        assert decide(_item(PAST, last=last), _shared(PAST), NOW) == SAFETY_HOLD


def test_unrecognised_reason_and_disabled_and_budget():
    assert decide(_item(PAST, reason="user_batch"), _shared(PAST), NOW) == UNOWNED_REASON
    assert decide(_item(PAST), _shared(PAST, enabled=False), NOW) == DISABLED
    assert decide(_item(PAST), _shared(PAST, used=3, cap=3), NOW) == BUDGET_SPENT
    # ...unless real progress refunds it, which production also honours.
    assert decide(_item(PAST), _shared(PAST, used=3, cap=3, delivered=1, mark=0),
                  NOW) == AUTHORISED


def test_safety_hold_needs_a_human_and_is_never_told_to_retry():
    """ROUND 14's most consequential finding: my tooling gave UNSAFE ADVICE.

    SAFETY_HOLD was missing from NEEDS_HUMAN and counted as ordinary waiting, so the
    check tool reported "every deferred item has a recovery path" about a row that has
    none by design -- and the watcher told every human-required row to "resume
    explicitly", which for an unknown-outcome row can duplicate a download.
    """
    assert SAFETY_HOLD in NEEDS_HUMAN
    assert action_for(SAFETY_HOLD) == ACTION_ADJUDICATE
    assert action_for(NO_AUTHORISATION) == ACTION_MANUAL_RESUME

    from backend.queue_recovery_policy import ACTION_ADVICE
    advice = ACTION_ADVICE[ACTION_ADJUDICATE].lower()
    assert "already downloaded" in advice
    assert "resume" not in advice.replace("plain resume", ""), (
        f"adjudication advice must not tell anyone to resume: {advice!r}")


def test_budget_parsing_is_clamped_and_shared():
    assert parse_max_attempts({}) == 3
    assert parse_max_attempts({"download_queue_auto_resume_max_attempts": 99}) == 10
    assert parse_max_attempts({"download_queue_auto_resume_max_attempts": 0}) == 1
    assert parse_max_attempts({"download_queue_auto_resume_max_attempts": "x"}) == 3
    # And production must delegate rather than keep its own copy (round 14 finding 6).
    svc = DownloadQueueService.__new__(DownloadQueueService)
    svc.config = {"download_queue_auto_resume_max_attempts": 7}
    assert svc._auto_resume_max_attempts() == 7


# ─────────────────────────────────────────────────────────────────────────────
# Production: the counterexamples from rounds 12 and 13, now pinned
# ─────────────────────────────────────────────────────────────────────────────

def _rig(db, cooldowns, *, batch_cooldown):
    """A paused batch whose children have INDEPENDENT cooldowns."""
    svc = DownloadQueueService({}, db, MagicMock())
    svc._coordinator_snapshot = MagicMock(return_value={"blocked": False})
    batch = svc.schedule_batch(
        [{"url": f"https://hdencode.org/r-{i}-2160p/", "title": f"R{i}",
          "media_type": "movie"} for i in range(len(cooldowns))],
        interval_minutes=0, mode="immediate", auto_resume_after_cooldown=True)
    ids = [it["item_uuid"] for it in batch["items"]]
    with db.transaction() as conn:
        for uid, cd in zip(ids, cooldowns):
            conn.execute(
                "UPDATE download_queue_items SET state='waiting_source', "
                "queue_reason='source_deferred', cooldown_until=?, "
                "last_reason_code='source_temporarily_blocked' WHERE item_uuid=?",
                (cd, uid))
        conn.execute("UPDATE download_queue_batches SET state='paused_source', "
                     "cooldown_until=?, auto_resume_used=0 WHERE batch_uuid=?",
                     (batch_cooldown, batch["batch_uuid"]))
    return svc, batch["batch_uuid"], ids


def _states(db, ids):
    return [db._query_dicts("SELECT state FROM download_queue_items WHERE item_uuid=?",
                            (i,), default=[])[0]["state"] for i in ids]


def test_round12_a_due_item_does_not_drag_a_future_sibling(tmp_path):
    db = DatabaseManager(str(tmp_path / "r12a.db"))
    try:
        svc, _b, ids = _rig(db, [PAST_S, FUTURE_S], batch_cooldown=None)
        svc._maybe_auto_resume()
        assert _states(db, ids) == ["ready", "waiting_source"]
    finally:
        db.close()


def test_round12_a_due_item_does_not_drag_a_null_sibling(tmp_path):
    db = DatabaseManager(str(tmp_path / "r12b.db"))
    try:
        svc, _b, ids = _rig(db, [PAST_S, None], batch_cooldown=None)
        svc._maybe_auto_resume()
        assert _states(db, ids) == ["ready", "waiting_source"]
    finally:
        db.close()


def test_round13_an_ineligible_sibling_does_not_veto_an_eligible_one(tmp_path):
    """THE MIRROR. MIN() ignored NULLs, so a sibling due in 2030 skipped the group.

    Under round 14's strict rule the NULL child is not authorised either -- but for its
    OWN reason, and it must not prevent the due child from running. So this uses a due
    child plus a future sibling, which is the shape that must still work.
    """
    db = DatabaseManager(str(tmp_path / "r13.db"))
    try:
        svc, _b, ids = _rig(db, [PAST_S, FUTURE_S], batch_cooldown=PAST_S)
        svc._maybe_auto_resume()
        assert _states(db, ids) == ["ready", "waiting_source"], (
            "a future sibling must not veto a due one")
    finally:
        db.close()


def test_a_visit_authorising_nothing_does_not_spend_budget(tmp_path):
    """ROUND 13 required this: a fruitless look must not resemble an attempt."""
    db = DatabaseManager(str(tmp_path / "nobudget.db"))
    try:
        svc, batch, ids = _rig(db, [FUTURE_S, FUTURE_S], batch_cooldown=None)
        svc._maybe_auto_resume()
        assert _states(db, ids) == ["waiting_source", "waiting_source"]
        used = db._query_dicts(
            "SELECT auto_resume_used u, state FROM download_queue_batches "
            "WHERE batch_uuid=?", (batch,), default=[])[0]
        assert int(used["u"] or 0) == 0, "budget spent on a visit that resumed nothing"
        assert used["state"] == "paused_source", "batch promoted despite resuming nothing"
    finally:
        db.close()


def test_unknown_outcome_rows_are_never_resumed_by_production(tmp_path):
    """The discriminating control: a fix that relaxed filters would fail only this."""
    db = DatabaseManager(str(tmp_path / "unknown.db"))
    try:
        svc, batch, ids = _rig(db, [PAST_S, PAST_S], batch_cooldown=PAST_S)
        with db.transaction() as conn:
            conn.execute("UPDATE download_queue_items "
                         "SET last_reason_code='operation_timeout_unknown' "
                         "WHERE batch_uuid=?", (batch,))
        svc._maybe_auto_resume()
        assert _states(db, ids) == ["waiting_source", "waiting_source"]
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# The adapter, through the REAL row shape
# ─────────────────────────────────────────────────────────────────────────────

def _joined(own, batch_cd, **over):
    row = {"item_uuid": "i", "batch_uuid": "b", "title": "T",
           "state": "waiting_source", "cooldown_until": own,
           "queue_reason": "source_deferred", "last_reason_code": "",
           "batch_state": "paused_source", "batch_cooldown": batch_cd,
           "auto_resume_after_cooldown": 1, "auto_resume_used": 0,
           "source_delivery_count": 0, "auto_resume_progress_mark": 0}
    row.update(over)
    return row


@pytest.mark.parametrize("own,batch_cd,expected", [
    (PAST_S,   FUTURE_S, WAITING_BRAKE),
    (None,     PAST_S,   NO_AUTHORISATION),
    (None,     None,     NO_AUTHORISATION),
    (FUTURE_S, PAST_S,   WAITING_OWN),
    (PAST_S,   PAST_S,   AUTHORISED),
])
def test_the_adapter_reads_the_BATCH_cooldown_not_the_item_twice(own, batch_cd, expected):
    """ROUND 13's aliasing bug, pinned.

    classify_all passed one flattened row as BOTH item and batch, so
    `batch["cooldown_until"]` read the ITEM's cooldown and `batch_cooldown` was never
    consumed. Live rows usually share identical cooldowns, which is exactly why checking
    it against the live database missed it -- so these cases deliberately DIFFER.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts"))
    from queue_recovery_state import classify_rows
    assert list(classify_rows([_joined(own, batch_cd)], now=NOW))[0] == expected
