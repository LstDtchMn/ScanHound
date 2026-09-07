"""What a canary crawl proved, and what the authority does about it.

A crawl can finish and still have protected nothing. These pin the three ways
that happens, and the rule underneath all of them: only a crawl that actually
watched the window refreshes the protection clock.
"""
import pytest

from backend import rss_primary_authority as authority
from backend.background_scanner import BackgroundScanner


class _Db:
    """Just enough database to grade one canary."""

    def __init__(self, previous=None, state=None):
        self._previous = previous or []
        self.state = state or {}
        self.overlap_calls = []
        self.attempts = []

    def list_listing_membership(self, source_key=None, **_kwargs):
        return list(self._previous)

    def record_overlap_loss(self, source_key, *, lost):
        self.overlap_calls.append((source_key, lost))

    def get_canary_state(self, source_key):
        return dict(self.state)

    def list_canary_states(self):
        return [dict(self.state, source_key="hdencode:4k")] if self.state else []

    def get_shadow_cycle_url_sets(self, **_kwargs):
        return {"cycles": [], "evidence_problems": []}


class _Reg:
    def __init__(self):
        self.config = {}
        self.db = None
        self.scanner = None
        self.lifespan_generation = 1
        self.background_scanner = None

    def owns_lifespan(self, generation):
        return True


def _scanner():
    return BackgroundScanner(_Reg())


def _rows(urls, *, cycle, page=1, rank_start=0, observed="2026-09-07T10:00:00+00:00"):
    return [{"cycle_uuid": cycle, "canonical_url": u, "page_index": page,
             "rank_on_page": rank_start + i, "observed_at": observed}
            for i, u in enumerate(urls)]


def test_an_incomplete_crawl_never_counts_as_a_success():
    db = _Db()
    outcome, reason = _scanner()._grade_canary(
        db, "hdencode:4k", _rows(["a"], cycle="now"),
        cycle_uuid="now", listing_complete=False, depth=3)
    assert outcome == "incomplete"
    assert reason == "listing_incomplete"


def test_the_first_canary_has_nothing_to_compare_against_and_succeeds():
    db = _Db(previous=[])
    outcome, reason = _scanner()._grade_canary(
        db, "hdencode:4k", _rows(["a", "b"], cycle="now"),
        cycle_uuid="now", listing_complete=True, depth=3)
    assert (outcome, reason) == ("success", None)
    assert db.overlap_calls == [], "there is no overlap to judge yet"


def test_zero_overlap_with_the_previous_canary_is_not_a_success():
    """Overlap is a NEGATIVE signal. Sharing a release with the previous
    canary shows the window did not slide past unseen; sharing none shows it
    might have, which is not protection."""
    previous = _rows(["old-1", "old-2"], cycle="before",
                     observed="2026-09-07T00:00:00+00:00")
    db = _Db(previous=previous)
    outcome, reason = _scanner()._grade_canary(
        db, "hdencode:4k", _rows(["new-1", "new-2"], cycle="now"),
        cycle_uuid="now", listing_complete=True, depth=3)
    assert outcome == "overlap_lost"
    assert reason
    assert db.overlap_calls == [("hdencode:4k", True)], (
        "the loss must be recorded durably: two in a row revokes, and a "
        "counter held in memory would be forgotten by the next restart")


def test_some_overlap_is_a_success_and_resets_the_counter():
    previous = _rows(["shared", "old"], cycle="before",
                     observed="2026-09-07T00:00:00+00:00")
    db = _Db(previous=previous)
    outcome, reason = _scanner()._grade_canary(
        db, "hdencode:4k", _rows(["shared", "new"], cycle="now"),
        cycle_uuid="now", listing_complete=True, depth=3)
    assert (outcome, reason) == ("success", None)
    assert db.overlap_calls == [("hdencode:4k", False)]


def test_more_new_urls_than_half_the_window_is_not_a_success():
    """Churn: the source turned over faster than this cadence can watch it,
    so the crawl finished but the window it claims to cover slid past."""
    previous = _rows(["shared"], cycle="before",
                     observed="2026-09-07T00:00:00+00:00")
    # depth 1 page, four posts per page -> capacity 4, half is 2.
    current = _rows(["shared", "n1", "n2", "n3"], cycle="now")
    db = _Db(previous=previous)
    outcome, reason = _scanner()._grade_canary(
        db, "hdencode:4k", current,
        cycle_uuid="now", listing_complete=True, depth=1)
    assert outcome == "incomplete"
    assert reason == "visibility_margin_lost"


def test_unreadable_previous_membership_is_not_graded_a_success():
    """REWRITTEN 2026-09-07 after a mutant survived it.

    The first version asserted this case returned "success", which is what the
    code did -- and the code was wrong. An unreadable read and a genuinely
    absent predecessor both came back as None, so an outage refreshed the
    protection clock on evidence nobody had seen. They are separate answers
    now, and the mutant that folds them back together fails here.
    """
    class _Blind(_Db):
        def list_listing_membership(self, source_key=None, **_kwargs):
            return None

    db = _Blind()
    outcome, reason = _scanner()._grade_canary(
        db, "hdencode:4k", _rows(["a"], cycle="now"),
        cycle_uuid="now", listing_complete=True, depth=3)
    assert outcome == "error", (
        "a read that failed says nothing about overlap, and must not refresh "
        "the protection clock")
    assert reason == "previous_membership_unreadable"
    assert db.overlap_calls == [], "no overlap verdict is invented from it"


def test_a_genuinely_absent_predecessor_is_still_a_success():
    """The other side of the same distinction: an empty read is not a failure.
    The first canary after promotion has no predecessor and is fine."""
    db = _Db(previous=[])
    outcome, reason = _scanner()._grade_canary(
        db, "hdencode:4k", _rows(["a"], cycle="now"),
        cycle_uuid="now", listing_complete=True, depth=3)
    assert (outcome, reason) == ("success", None)


def test_a_recorded_margin_loss_reaches_the_authority():
    """The counter and the reason are only worth writing if something reads
    them. This is the consumer end of the churn guard."""
    db = _Db(state={"last_success_at": "2026-09-07T10:00:00+00:00",
                    "consecutive_overlap_losses": 0,
                    "last_reason": "visibility_margin_lost"})
    config = {
        "hdencode_discovery_mode": "rss_primary",
        "hdencode_listing_canary_sources": ["hdencode:4k"],
        authority.PROMOTION_KEY: {"at": "2026-09-07T09:00:00+00:00"},
    }
    evidence = authority.canary_evidence(config, db)
    assert authority.BLOCKER_MARGIN_LOST in evidence["blockers"]
    assert authority.BLOCKER_MARGIN_LOST in authority.REVOCATION_BLOCKERS


def test_two_overlap_losses_reach_the_authority():
    db = _Db(state={"last_success_at": "2026-09-07T10:00:00+00:00",
                    "consecutive_overlap_losses": 2})
    config = {
        "hdencode_discovery_mode": "rss_primary",
        "hdencode_listing_canary_sources": ["hdencode:4k"],
        authority.PROMOTION_KEY: {"at": "2026-09-07T09:00:00+00:00"},
    }
    evidence = authority.canary_evidence(config, db)
    assert authority.BLOCKER_OVERLAP_LOST in evidence["blockers"]


@pytest.mark.parametrize("age_hours,expected", [(0, False), (48, True)])
def test_a_canary_that_has_not_succeeded_recently_is_stale(age_hours, expected):
    import datetime
    when = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=age_hours)).isoformat()
    db = _Db(state={"last_success_at": when, "consecutive_overlap_losses": 0})
    config = {
        "hdencode_discovery_mode": "rss_primary",
        "hdencode_listing_canary_sources": ["hdencode:4k"],
        authority.PROMOTION_KEY: {"at": "2026-09-01T00:00:00+00:00"},
    }
    evidence = authority.canary_evidence(config, db)
    assert (authority.BLOCKER_CANARY_STALE in evidence["blockers"]) is expected
