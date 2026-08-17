"""Source-level verification holds, reported as a condition not as N stuck rows.

On 2026-08-16 one interactive challenge armed a source-scoped hold on HDEncode
and 39 queue items each rendered as an independently stuck download, every one
of them showing a "Retry after 8:57 PM" that had no bearing on the outcome --
decide() returns VERIFICATION_HOLD *before* it looks at any cooldown.

The open PR #84 infers the hold from `state = 'verification_required'` rows.
These tests exist because that is a different fact, and the difference is not
theoretical: at the time of writing, ONE such row was holding all 39.
"""
from unittest.mock import MagicMock

import pytest

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService


@pytest.fixture
def db():
    dm = DatabaseManager()

    def clear():
        for t in ("download_queue_attempts", "download_queue_items",
                  "download_queue_batches"):
            dm._mutate("DELETE FROM %s" % t, (), label="test_clear")
    clear()
    yield dm
    clear()
    dm.close()


@pytest.fixture
def svc(db):
    return DownloadQueueService({}, db, MagicMock(), broadcast=lambda *a: None)


def _batch(db, uuid_, hold=None):
    # Every NOT NULL column is supplied. Omitting one makes the INSERT fail
    # silently and the assertions then pass against an EMPTY table -- four of
    # these tests did exactly that on the first run, reporting green while
    # nothing had been inserted at all.
    ok = db._mutate(
        "INSERT INTO download_queue_batches "
        "(batch_uuid, mode, interval_seconds, state, total_items, completed_items, "
        " failed_items, deferred_items, auto_resume_after_cooldown, auto_resume_used, "
        " auto_resume_progress_mark, source_delivery_count, created_at, updated_at, "
        " verification_hold_source) "
        "VALUES (?, 'immediate', 0, 'paused_source', 0,0,0,0, 0,0,0,0, "
        "        datetime('now'), datetime('now'), ?)",
        (uuid_, hold), label="t")
    assert ok, "batch fixture insert FAILED -- the test would pass vacuously"


def _item(db, batch, source="hdencode", state="waiting_source",
          reason="source_deferred", cooldown="2026-08-17T00:57:09+00:00", n=0):
    ok = db._mutate(
        "INSERT INTO download_queue_items "
        "(item_uuid, batch_uuid, sequence_number, source, canonical_url, title, "
        " dovi, service_type, queue_reason, state, attempt_count, "
        " automated_retry_count, cooldown_until, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?, 0,'Rapidgator',?,?, 0,0, ?, "
        "        datetime('now'), datetime('now'))",
        ("i-%s-%d" % (batch, n), batch, n, source,
         "https://hdencode.org/%s-%d" % (batch, n), "T%d" % n, reason, state,
         cooldown), label="t")
    assert ok, "item fixture insert FAILED -- the test would pass vacuously"


class TestTheHoldIsReadFromTheMARKER:
    def test_no_hold_means_no_entry(self, svc, db):
        _batch(db, "b1")
        _item(db, "b1")
        assert svc.active_verification_holds() == []

    def test_an_armed_hold_is_reported_with_its_source(self, svc, db):
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1")
        holds = svc.active_verification_holds()
        assert len(holds) == 1
        assert holds[0]["source"] == "hdencode"

    def test_it_counts_EVERY_affected_row_not_just_the_trigger(self, svc, db):
        """The number the user needs. One batch records the hold; the hold is
        source-scoped, so rows in OTHER batches are held too."""
        _batch(db, "b1", hold="hdencode")
        _batch(db, "b2")                       # no marker of its own
        _item(db, "b1", state="verification_required",
              reason="interactive_challenge", n=0)
        for i in range(1, 6):
            _item(db, "b2", n=i)
        h = svc.active_verification_holds()[0]
        assert h["affected"] == 6, h
        assert h["triggers"] == 1, h

    def test_the_hold_SURVIVES_losing_its_trigger_row(self, svc, db):
        """THE BUG IN PR #84, made concrete. Infer the hold from
        verification_required rows and removing the single trigger makes the
        escape hatch vanish while the hold stays armed -- stranding every
        sibling with no way out. Today one row holds 39."""
        _batch(db, "b1", hold="hdencode")
        for i in range(5):
            _item(db, "b1", n=i)               # NOT verification_required
        triggers = db._query_dicts(
            "SELECT COUNT(*) n FROM download_queue_items "
            "WHERE state='verification_required'", (), default=[])[0]["n"]
        assert triggers == 0, "fixture must have no trigger row left"

        holds = svc.active_verification_holds()
        assert len(holds) == 1, (
            "the hold disappeared when its trigger did -- the 5 held rows now "
            "have no visible cause and no way to be released")
        assert holds[0]["affected"] == 5

    def test_a_trigger_row_WITHOUT_a_hold_reports_nothing(self, svc, db):
        """The mirror error: a row can still read verification_required after
        the hold was cleared, which would show a button that does nothing."""
        _batch(db, "b1")                       # hold already cleared
        _item(db, "b1", state="verification_required",
              reason="interactive_challenge")
        assert svc.active_verification_holds() == [], (
            "reported a hold that is not armed")

    def test_two_sources_are_reported_separately(self, svc, db):
        """PR #84 hardcodes 'hdencode', so it cannot clear anything else."""
        _batch(db, "b1", hold="hdencode")
        _batch(db, "b2", hold="ddlbase")
        _item(db, "b1", source="hdencode", n=0)
        _item(db, "b2", source="ddlbase", n=1)
        got = sorted(h["source"] for h in svc.active_verification_holds())
        assert got == ["ddlbase", "hdencode"]


class TestItSaysTheCooldownWillNotSaveYou:
    def test_it_states_plainly_that_no_timer_clears_this(self, svc, db):
        """The single most misleading thing in the old UI: 39 cards each
        promising 'Retry after 8:57 PM' for a condition that ignores the clock
        entirely."""
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1")
        h = svc.active_verification_holds()[0]
        assert h["clears_on_timer"] is False
        assert "succeeds" in h["clears_when"]

    def test_the_cooldown_is_still_reported_so_the_UI_can_contradict_it(self, svc, db):
        """Omitting it would leave the item cards' own 'Retry after' as the only
        timestamp on screen, unchallenged."""
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1", cooldown="2026-08-17T00:57:09+00:00")
        assert svc.active_verification_holds()[0]["cooldown_until"] is not None


class TestItFailsQuietlyRatherThanBreakingTheScreen:
    def test_no_database_returns_empty(self):
        s = DownloadQueueService.__new__(DownloadQueueService)
        s.db = None
        assert s.active_verification_holds() == []

    def test_a_query_failure_does_not_take_down_the_retries_page(self):
        """This rides along on /download/retries. A hold-reporting failure must
        not remove the user's ability to see and retry their downloads."""
        s = DownloadQueueService.__new__(DownloadQueueService)
        s.db = MagicMock()
        s.db._query_dicts.side_effect = RuntimeError("db gone")
        assert s.active_verification_holds() == []


class TestAffectedIsThePOLICYCount:
    """Review MEDIUM 1. `affected` used to count every row in a deferred state
    for a held source. decide() ranks SAFETY_HOLD (unknown outcome) and
    UNOWNED_REASON ABOVE the verification hold, so that set is not the set the
    hold is stopping -- and the card says "N requests paused", which was
    therefore a false statement for those rows.

    Counting the wrong set and naming it the right one is the same error PR #84
    made from item state. Doing it in SQL would only have moved it.
    """

    def test_an_ordinary_deferred_row_IS_counted(self, svc, db):
        """Positive control: without this the negatives below are vacuous."""
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1")
        assert svc.active_verification_holds()[0]["affected"] == 1

    def test_an_UNKNOWN_OUTCOME_row_is_not_counted(self, svc, db):
        """SAFETY_HOLD outranks the verification hold: we do not know whether
        the previous attempt took effect, so it is parked for a different and
        stronger reason. The hold is not what is stopping it."""
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1", n=0)
        _item(db, "b1", n=1)
        db._mutate("UPDATE download_queue_items SET last_reason_code = "
                   "'operation_timeout_unknown' WHERE item_uuid = 'i-b1-1'",
                   (), label="t")
        h = svc.active_verification_holds()[0]
        assert h["affected"] == 1, "counted a row held by SAFETY_HOLD, not by us"
        assert "i-b1-1" not in h["item_uuids"]

    def test_an_UNOWNED_REASON_row_is_not_counted(self, svc, db):
        """Automatic recovery does not own this row at all."""
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1", n=0)
        _item(db, "b1", n=1, reason="manual_retry")
        h = svc.active_verification_holds()[0]
        assert h["affected"] == 1, "counted a row the recovery policy does not own"

    def test_a_NON_DEFERRED_row_is_not_counted(self, svc, db):
        """A ready row is not held by anything."""
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1", n=0)
        _item(db, "b1", n=1, state="ready", reason="user_batch")
        assert svc.active_verification_holds()[0]["affected"] == 1

    def test_a_marker_with_NOTHING_behind_it_is_still_reported(self, svc, db):
        """Deliberate. cancel_item()/cancel_batch() do not clear the marker, so
        it can outlive its rows. Hiding a zero-affected hold would make a sticky
        marker invisible while it silently blocks future work -- the card says
        so instead, and offers the release."""
        _batch(db, "b1", hold="hdencode")
        holds = svc.active_verification_holds()
        assert len(holds) == 1
        assert holds[0]["affected"] == 0
        assert holds[0]["item_uuids"] == []

    def test_the_held_uuids_are_returned_so_the_UI_need_not_guess(self, svc, db):
        """MEDIUM 2's fix depends on this: the UI must partition by the
        BACKEND's verdict, not re-derive it from state and source."""
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1", n=0)
        _item(db, "b1", n=1)
        h = svc.active_verification_holds()[0]
        assert sorted(h["item_uuids"]) == ["i-b1-0", "i-b1-1"]

    def test_a_trigger_needs_BOTH_facts(self, svc, db):
        """verification_required alone is not proof of being the trigger; the
        queue_reason has to say interactive_challenge too."""
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1", n=0, state="verification_required",
              reason="interactive_challenge")
        _item(db, "b1", n=1, state="verification_required",
              reason="source_deferred")
        assert svc.active_verification_holds()[0]["triggers"] == 1
