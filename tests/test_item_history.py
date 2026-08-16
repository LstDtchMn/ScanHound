"""The per-item history the owner asked for.

Two properties this file exists to hold:

  * no reason code reaches the screen, and no wording is invented beside the
    vocabulary the notifications already use;
  * nothing is claimed that the data cannot support -- in particular, attempt
    rows written before the 2026-08-16 fix say FAILED for downloads that
    SUCCEEDED, so they must not be rendered, and their absence must be stated
    rather than passed off as "never attempted".
"""
import uuid

import pytest

from backend.database import DatabaseManager
from backend.item_history import (build_timeline, describe_reason,
                                  describe_state, item_history)


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


TRUSTED = DatabaseManager.ATTEMPT_HISTORY_TRUSTED_FROM


def _item(**kw):
    base = {"item_uuid": "i1", "title": "Some Movie", "year": 2020,
            "state": "waiting_source", "canonical_url": "https://hdencode.org/x",
            "created_at": "2026-08-16T09:00:00+00:00", "attempt_count": 2,
            "automated_retry_count": 1}
    base.update(kw)
    return base


def _att(status="FAILED", reason=None, started="2026-08-16 10:00:00", **kw):
    a = {"attempt_id": str(uuid.uuid4()), "started_at": started,
         "finished_at": started, "terminal_status": status,
         "reason_code": reason, "affected_scope": "item",
         "transport_attempted": 1, "source_progress": 0}
    a.update(kw)
    return a


class TestNoJargonReachesTheScreen:
    def test_a_known_reason_becomes_a_sentence(self):
        out = describe_reason("reveal_verification_stalled")
        assert "reveal_verification_stalled" not in out
        assert out and out[0].isupper()

    def test_an_unknown_reason_does_NOT_leak_the_code(self):
        """A screen that shows `layout_changed` has moved the problem from the
        log to the user."""
        out = describe_reason("some_new_code_nobody_mapped")
        assert "some_new_code_nobody_mapped" not in out

    def test_an_unknown_reason_does_not_invent_a_cause(self):
        """Naming a cause we have not established is the exact mistake several
        of these codes were split apart to stop making."""
        out = describe_reason("some_new_code_nobody_mapped").lower()
        for invented in ("blocked", "throttl", "layout", "captcha", "banned"):
            assert invented not in out, out

    def test_the_wording_comes_from_the_EXISTING_vocabulary(self):
        """A second wording table would drift from the one the notifications
        use, and the two would eventually disagree about the same code."""
        from backend.download_outcome import _FAILURE_TITLES
        for code, title in _FAILURE_TITLES.items():
            assert describe_reason(code) == title

    def test_every_queue_state_has_a_label(self):
        for state in ("scheduled", "ready", "claimed", "waiting_source",
                      "verification_required", "completed", "failed", "cancelled"):
            label = describe_state(state)
            assert "_" not in label, "%s rendered as %r" % (state, label)


class TestTheTimelineTellsTheTruth:
    def test_it_starts_with_the_user_adding_it(self):
        t = build_timeline(_item(), [])
        assert t[0]["text"] == "You added this to the queue"
        assert t[0]["at"] == "2026-08-16T09:00:00+00:00"

    def test_a_delivery_reads_as_getting_links(self):
        t = build_timeline(_item(), [_att(status="SUCCESS", source_progress=1)])
        assert any(e["text"] == "Got the download links" for e in t)

    def test_a_duplicate_does_NOT_claim_a_download_happened(self):
        """It completed without contacting HDEncode at all. Saying 'got the
        links' would be a lie the user could act on."""
        t = build_timeline(_item(), [_att(status="SUCCESS", source_progress=0)])
        texts = " ".join(e["text"] for e in t)
        assert "Already had this one" in texts
        assert "Got the download links" not in texts

    def test_a_source_wide_failure_says_it_was_not_this_item(self):
        t = build_timeline(_item(), [_att(reason="reveal_verification_stalled",
                                          affected_scope="source")])
        assert any("HDEncode as a whole" in e["text"] for e in t)

    def test_a_failure_that_never_reached_the_source_says_so(self):
        """'we asked and it went wrong' and 'we never got as far as asking' are
        the difference between blaming the source and blaming us."""
        t = build_timeline(_item(), [_att(reason="browser_launch_failed",
                                          transport_attempted=0)])
        assert any("no request reached the source" in e["text"] for e in t)

    def test_events_are_ordered_oldest_first(self):
        t = build_timeline(
            _item(completed_at="2026-08-16T12:00:00+00:00"),
            [_att(started="2026-08-16 11:00:00"),
             _att(started="2026-08-16 10:00:00")])
        stamps = [e["at"] for e in t]
        assert stamps == sorted(stamps)

    def test_an_in_progress_attempt_is_not_reported_as_finished(self):
        t = build_timeline(_item(), [_att(status="IN_PROGRESS")])
        assert any("still running" in e["text"] for e in t)


class TestItNeverOverclaims:
    def test_an_item_with_no_trustworthy_history_says_so(self):
        """It must not look like an item that was never tried."""
        h = item_history(_item(attempt_count=7), [], trusted_from=TRUSTED)
        assert h["has_detailed_history"] is False
        assert h["detailed_history_from"] == TRUSTED
        assert h["tries"] == 7, "the count is still shown; only the detail is absent"

    def test_the_recorded_count_is_separate_from_the_try_count(self):
        """attempt_count includes claims made before attempt records existed, so
        a gap is expected and must be visible rather than reconciled away."""
        h = item_history(_item(attempt_count=9), [_att()], trusted_from=TRUSTED)
        assert h["tries"] == 9 and h["attempts_recorded"] == 1

    def test_the_direct_link_is_carried(self):
        h = item_history(_item(), [], trusted_from=TRUSTED)
        assert h["source_url"] == "https://hdencode.org/x"

    def test_the_current_blocker_is_one_sentence_not_a_code(self):
        h = item_history(_item(last_reason_code="reveal_verification_stalled"),
                         [], trusted_from=TRUSTED)
        assert h["current_reason"] and "_" not in h["current_reason"]

    def test_no_blocker_means_no_sentence(self):
        """Control: a healthy item must not display a reason for nothing."""
        h = item_history(_item(last_reason_code=None), [], trusted_from=TRUSTED)
        assert h["current_reason"] is None


class TestTheTrustCutoffIsEnforcedInTheQUERY:
    """Not in the UI. Every pre-fix row says FAILED/attempt_not_closed for
    downloads that succeeded, so the filter has to live where the rows are read
    or some other caller will render them."""

    def _insert(self, db, item_uuid, started, aid=None):
        aid = aid or str(uuid.uuid4())
        db.begin_queue_attempt(aid, item_uuid, "b1", "hdencode")
        db._mutate("UPDATE download_queue_attempts SET started_at = ? "
                   "WHERE attempt_id = ?", (started, aid), label="t")
        return aid

    def test_pre_fix_rows_are_excluded(self, db):
        self._insert(db, "i1", "2026-08-15T23:00:00")     # legacy shape AND date
        assert db.queue_attempts_for_item("i1") == []

    def test_rows_after_the_cutoff_are_included(self, db):
        self._insert(db, "i1", "2026-08-16 09:00:00")
        assert len(db.queue_attempts_for_item("i1")) == 1

    def test_the_legacy_T_SHAPE_cannot_sneak_past_the_cutoff(self, db):
        """'T' > ' ', so a lexical comparison would let a pre-fix row through on
        the boundary date. julianday is what makes the cutoff mean a time."""
        self._insert(db, "i1", "2026-08-15T18:00:00")
        assert db.queue_attempts_for_item("i1") == [], (
            "a pre-fix row passed the cutoff because of its separator")

    def test_ordering_is_by_TIME_not_spelling(self, db):
        self._insert(db, "i1", "2026-08-16 09:00:00", aid="second")
        self._insert(db, "i1", "2026-08-16T08:00:00", aid="first")
        got = [a["attempt_id"] for a in db.queue_attempts_for_item("i1")]
        assert got == ["first", "second"], got

    def test_another_items_attempts_are_not_shown(self, db):
        self._insert(db, "i1", "2026-08-16 09:00:00")
        self._insert(db, "i2", "2026-08-16 09:30:00")
        assert len(db.queue_attempts_for_item("i1")) == 1

    def test_trusted_only_False_is_available_for_diagnostics(self, db):
        """The rows are kept as evidence; they are just not shown as history."""
        self._insert(db, "i1", "2026-08-15T23:00:00")
        assert len(db.queue_attempts_for_item("i1", trusted_only=False)) == 1


class TestTheBackstopsDefaultIsNotEvidence:
    """Found by running the builder against PRODUCTION, not by a unit test: a
    real stuck item rendered "It did not finish, and the reason was not recorded
    (no request reached the source)" -- two halves that contradict each other.

    On an attempt_not_closed row the transport flag is the backstop's default,
    not an observation. The attempt escaped without reporting anything, so
    whether a request went out is exactly what is unknown.
    """

    def test_no_transport_claim_is_made_on_an_unclosed_attempt(self):
        t = build_timeline(_item(), [_att(reason="attempt_not_closed",
                                          transport_attempted=0)])
        text = " ".join(e["text"] for e in t)
        assert "no request reached the source" not in text, text

    def test_a_REAL_no_transport_failure_still_says_so(self):
        """Control: the qualifier must not be dropped everywhere, or the
        distinction it carries is lost."""
        t = build_timeline(_item(), [_att(reason="browser_launch_failed",
                                          transport_attempted=0)])
        assert any("no request reached the source" in e["text"] for e in t)

    def test_the_unclosed_attempt_is_still_shown_as_a_non_completion(self):
        """It is not hidden -- only the unfounded half is dropped."""
        t = build_timeline(_item(), [_att(reason="attempt_not_closed",
                                          transport_attempted=0)])
        assert any(e["kind"] == "failed" for e in t)
