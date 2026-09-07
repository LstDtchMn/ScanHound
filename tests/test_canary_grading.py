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


class _RecordingDb(_Db):
    """Records what _record_canary_evidence actually writes."""

    def __init__(self):
        super().__init__(previous=[])
        self.membership = []
        self.batches = []

    def record_listing_membership(self, cycle_uuid, source_key, rows):
        self.membership.append((cycle_uuid, source_key, list(rows)))

    def record_request_batch(self, mode, kind, requests, at=None):
        self.batches.append((mode, kind, requests))

    def record_canary_attempt(self, source_key, *, at, next_attempt_at,
                              outcome, reason=None):
        self.attempts.append({"source_key": source_key, "outcome": outcome,
                              "reason": reason,
                              "next_attempt_at": next_attempt_at})


class _CrawledOneSource:
    """A crawl that produced rows for 4k only -- tv and remux said nothing."""

    _last_crawl_membership = [
        {"source_key": "hdencode:4k", "canonical_url": "https://x/a",
         "page_index": 1, "rank_on_page": 0},
    ]
    _last_crawl_request_count = 3


def _canary_cfg():
    return {"hdencode_discovery_mode": "rss_primary",
            "hdencode_listing_canary_sources": ["4k", "remux", "tv"]}


def test_every_configured_source_records_an_attempt_not_only_the_ones_that_spoke():
    """ADDED 2026-09-07 after two mutants survived.

    Grading only the sources that produced rows left a silent source with no
    attempt recorded at all: its last outcome still read "success" from hours
    earlier while it was observing nothing, and its next_attempt_at never
    moved, so it stayed permanently due. A category disabled in
    background_scan_categories is exactly this case.
    """
    db = _RecordingDb()
    _scanner()._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="c1",
        canary_run=True, listing_complete=True, rss_requests=2)

    recorded = {a["source_key"]: a for a in db.attempts}
    assert set(recorded) == {"hdencode:4k", "hdencode:remux", "hdencode:tv"}
    assert recorded["hdencode:4k"]["outcome"] == "success"


def test_a_source_that_recorded_nothing_is_a_failure_with_a_reason():
    db = _RecordingDb()
    _scanner()._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="c1",
        canary_run=True, listing_complete=True, rss_requests=2)

    silent = {a["source_key"]: a for a in db.attempts}["hdencode:tv"]
    assert silent["outcome"] != "success", (
        "a canary that observed nothing protected nothing; grading it a "
        "success refreshes the protection clock on an empty crawl")
    assert silent["reason"] == "no_membership_recorded"


def test_membership_records_the_provenance_of_this_source_own_feed():
    """The producer half of RHC-9. Nothing populated rss_present, so the column
    was False for every row ever written and the systematic-gap check was
    reading a constant."""
    carried = "https://hdencode.example/carried"
    missed = "https://hdencode.example/missed"

    class _WithFeeds(_RecordingDb):
        def list_hdencode_current_feed_urls(self, feed_keys=()):
            assert tuple(feed_keys) == ("movies_2160p",), (
                "each canary source must be asked about its OWN mapped feed")
            return [carried]

    class _Crawled4k:
        _last_crawl_membership = [
            {"source_key": "hdencode:4k", "canonical_url": carried,
             "page_index": 1, "rank_on_page": 0},
            {"source_key": "hdencode:4k", "canonical_url": missed,
             "page_index": 1, "rank_on_page": 1},
        ]
        _last_crawl_request_count = 3

    db = _WithFeeds()
    _scanner()._record_canary_evidence(
        db, {"hdencode_discovery_mode": "rss_primary",
             "hdencode_listing_canary_sources": ["4k"]},
        _Crawled4k(), cycle_uuid="c1", canary_run=True,
        canary_observation=True, listing_complete=True, rss_requests=0)

    written = {r["canonical_url"]: r["rss_present"]
               for _cycle, _key, rows in db.membership for r in rows}
    assert written[carried] is True
    assert written[missed] is False


def test_unreadable_feed_provenance_is_recorded_as_unknown_not_absent():
    """None, never False. "The feed could not be read" and "the feed did not
    carry it" are different claims, and only one of them proves a gap."""
    class _NoFeeds(_RecordingDb):
        def list_hdencode_current_feed_urls(self, feed_keys=()):
            raise RuntimeError("feed table unavailable")

    db = _NoFeeds()
    _scanner()._record_canary_evidence(
        db, {"hdencode_discovery_mode": "rss_primary",
             "hdencode_listing_canary_sources": ["4k"]},
        _CrawledOneSource(), cycle_uuid="c1", canary_run=True,
        canary_observation=True, listing_complete=True, rss_requests=0)

    written = [r["rss_present"] for _c, _k, rows in db.membership for r in rows]
    assert written and all(v is None for v in written)


def test_all_four_ledger_kinds_have_a_production_writer():
    """ADDED 2026-09-07 (review MEDIUM 1). Only rss_poll and canary were ever
    written, and rss_poll only when a comparison happened -- so poll-only
    cycles under primary, the cheap ones the whole cost claim rests on, were
    never counted at all."""
    scanner = _scanner()

    # A fallback crawl books fallback, not canary.
    db = _RecordingDb()
    scanner._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="f1",
        canary_run=False, canary_observation=False, fallback_run=True,
        listing_complete=True, rss_requests=0)
    assert [k for _m, k, _n in db.batches] == ["fallback"]

    # A canary crawl while a source is failing books canary_retry.
    class _Failing(_RecordingDb):
        def get_canary_state(self, source_key):
            return {"consecutive_failures": 2}

    db = _Failing()
    scanner._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="r1",
        canary_run=True, canary_observation=True,
        listing_complete=True, rss_requests=0)
    assert "canary_retry" in [k for _m, k, _n in db.batches]

    # And a healthy one books plain canary.
    db = _RecordingDb()
    scanner._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="c1",
        canary_run=True, canary_observation=True,
        listing_complete=True, rss_requests=0)
    assert "canary" in [k for _m, k, _n in db.batches]


def test_a_poll_only_cycle_still_books_its_requests():
    """The cycle that records nothing else is exactly the one the hybrid's
    cost claim depends on."""
    db = _RecordingDb()
    _scanner()._record_poll_cost(
        db, {"hdencode_discovery_mode": "rss_primary"}, {"requests": 7})
    assert db.batches == [("rss_primary", "rss_poll", 7)]

    # Not booked when nothing was polled, and not booked in listing mode.
    db = _RecordingDb()
    _scanner()._record_poll_cost(db, {"hdencode_discovery_mode": "rss_primary"},
                                {"requests": 0})
    _scanner()._record_poll_cost(db, {"hdencode_discovery_mode": "listing"},
                                {"requests": 7})
    assert db.batches == []


def test_the_comparison_path_no_longer_double_books_the_poll():
    """Booked once per cycle now. Leaving the old call in place as well would
    count every comparison cycle's poll twice."""
    db = _RecordingDb()
    _scanner()._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="c1",
        canary_run=True, canary_observation=True,
        listing_complete=True, rss_requests=9)
    assert "rss_poll" not in [k for _m, k, _n in db.batches]


def test_the_qualification_crawl_records_a_canary_attempt():
    """REGRESSION (review HIGH 3). There was no legitimate first promotion:
    activation demands a recent canary success, but an attempt was only ever
    recorded once the EFFECTIVE mode was already rss_primary. Qualification
    could produce membership forever and never the freshness activation asked
    for, so the only ways in were hand-seeded state or residue from a previous
    promotion.

    The dense qualification crawl IS the observation -- full depth, no early
    stop, membership under the same keys -- so it is recorded as one.
    """
    db = _RecordingDb()
    _scanner()._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="qual-1",
        canary_run=False, canary_observation=True,
        listing_complete=True, rss_requests=2)

    recorded = {a["source_key"]: a for a in db.attempts}
    assert recorded["hdencode:4k"]["outcome"] == "success", (
        "a full-depth qualification crawl that observed the listing is a "
        "canary observation, and freshness must be able to start somewhere")


def test_a_qualification_crawl_is_not_booked_as_a_post_promotion_canary():
    """The other half: it updates scheduling state, and nothing else. Marking
    the comparison row rss_primary_canary or booking the requests as canary
    cost would make shadow evidence look like post-promotion evidence."""
    db = _RecordingDb()
    _scanner()._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="qual-1",
        canary_run=False, canary_observation=True,
        listing_complete=True, rss_requests=2)
    assert db.attempts, "scheduling state was updated"
    assert all(kind != "canary" for _mode, kind, _n in db.batches), (
        "qualification listing requests are qualification overhead, not "
        "hybrid canary cost"
    )


def test_a_qualification_crawl_that_observed_nothing_still_fails():
    """Recording the attempt must not become a way to manufacture freshness:
    the same grading applies."""
    db = _RecordingDb()

    class _CrawledNothing:
        _last_crawl_membership = []
        _last_crawl_request_count = 3

    _scanner()._record_canary_evidence(
        db, _canary_cfg(), _CrawledNothing(), cycle_uuid="qual-2",
        canary_run=False, canary_observation=True,
        listing_complete=True, rss_requests=2)
    assert all(a["outcome"] != "success" for a in db.attempts)


def test_a_failed_membership_write_is_never_graded_a_success():
    """REGRESSION (review HIGH 6). The write failure was logged and grading
    carried on from the in-memory rows, so last_success_at advanced while the
    durable evidence needed to detect a gap had just been lost -- protection
    asserted on evidence nobody kept."""
    class _LosesTheWrite(_RecordingDb):
        def record_listing_membership(self, cycle_uuid, source_key, rows):
            raise RuntimeError("simulated durable membership failure")

    db = _LosesTheWrite()
    _scanner()._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="c1",
        canary_run=True, listing_complete=True, rss_requests=2)

    graded = {a["source_key"]: a for a in db.attempts}["hdencode:4k"]
    assert graded["outcome"] != "success", (
        "a crawl whose evidence did not reach disk proves nothing re-readable")
    assert graded["reason"] == "membership_write_failed"


def test_an_unfinished_crawl_explains_its_own_emptiness():
    """The reason must not be 'recorded nothing' when the crawl never
    finished: those are different faults and only one is about the source."""
    db = _RecordingDb()
    _scanner()._record_canary_evidence(
        db, _canary_cfg(), _CrawledOneSource(), cycle_uuid="c1",
        canary_run=True, listing_complete=False, rss_requests=2)

    silent = {a["source_key"]: a for a in db.attempts}["hdencode:tv"]
    assert (silent["outcome"], silent["reason"]) == ("incomplete",
                                                     "listing_incomplete")


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
