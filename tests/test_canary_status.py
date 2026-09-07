"""What the status surface publishes about the canary.

The design record is explicit that a system must not run for long labelled
canary-protected while its canary has not actually observed the listing. That
only holds if the health is visible, and if "we could not read it" never
renders as "nothing to report".
"""
import datetime as datetime_mod

from backend import rss_primary_authority as authority


def _now_dt():
    return datetime_mod.datetime.now(datetime_mod.timezone.utc)


def _now():
    return _now_dt().isoformat()


def _old(hours):
    return (_now_dt() - datetime_mod.timedelta(hours=hours)).isoformat()


class _Db:
    def __init__(self, states=None, totals=None, raise_states=False):
        self._states = states
        self._totals = totals
        self._raise = raise_states

    def list_canary_states(self):
        if self._raise:
            raise RuntimeError("db gone")
        return self._states

    def sum_requests(self, since, until=None):
        return self._totals

    def get_shadow_cycle_url_sets(self, **_kwargs):
        return {"cycles": [], "evidence_problems": []}

    def list_listing_membership(self, **_kwargs):
        return []

    def get_hdencode_rss_readiness(self, **_kwargs):
        return {"ready": False, "reasons": ["insufficient_days"]}


CONFIG = {
    "hdencode_discovery_mode": "rss_primary",
    "hdencode_listing_canary_sources": ["hdencode:4k"],
}


def test_unreadable_state_is_published_as_unavailable_not_as_no_canaries():
    for db in (_Db(states=None), _Db(raise_states=True), None):
        health = authority._canary_health(CONFIG, db, {"detail": {}})
        assert health["available"] is False, (
            "a canary whose state cannot be read must not render as a quiet one")
        assert health["sources"] == {}


def test_a_readable_state_publishes_the_fields_an_operator_needs():
    db = _Db(states=[{"source_key": "hdencode:4k",
                      "last_attempt_at": _now(), "next_attempt_at": _now(),
                      "last_success_at": _now(), "last_outcome": "success",
                      "last_reason": None, "consecutive_failures": 0,
                      "consecutive_overlap_losses": 0}])
    health = authority._canary_health(CONFIG, db, {"detail": {}})
    assert health["available"] is True
    source = health["sources"]["hdencode:4k"]
    for key in ("last_attempt_at", "next_attempt_at", "last_success_at",
                "age_seconds", "last_outcome", "consecutive_failures",
                "consecutive_overlap_losses", "stale", "has_run"):
        assert key in source
    assert source["stale"] is False
    assert source["has_run"] is True


def test_a_canary_past_its_maximum_age_reads_stale():
    db = _Db(states=[{"source_key": "hdencode:4k",
                      "last_success_at": _old(48),
                      "consecutive_overlap_losses": 0}])
    health = authority._canary_health(CONFIG, db, {"detail": {}})
    assert health["sources"]["hdencode:4k"]["stale"] is True


def test_a_canary_that_never_ran_is_stale_and_says_so_separately():
    """Never having run and being overdue are both unprotected, but they are
    different situations and the surface distinguishes them."""
    db = _Db(states=[{"source_key": "hdencode:4k", "last_success_at": None,
                      "consecutive_overlap_losses": 0}])
    source = authority._canary_health(CONFIG, db, {"detail": {}})["sources"]["hdencode:4k"]
    assert source["stale"] is True
    assert source["has_run"] is False


def test_request_cost_reports_unavailable_rather_than_zero():
    """The ledger returns None when it cannot be read. Publishing zero there
    would read as 'the hybrid spent nothing', which is a measurement nobody
    made."""
    cost = authority._request_cost(_Db(totals=None))
    assert cost["available"] is False
    assert "totals" not in cost

    cost = authority._request_cost(_Db(totals={"rss_poll": 12, "canary": 3,
                                               "canary_retry": 0, "fallback": 1,
                                               "total": 16}))
    assert cost["available"] is True
    assert cost["totals"]["total"] == 16
    assert cost["floor"] == 0.50 and cost["target"] == 0.70


class _WindowDb(_Db):
    """Records the lower bound the cost block asks the ledger for."""

    def __init__(self):
        super().__init__(totals={"rss_poll": 1, "canary": 1, "canary_retry": 0,
                                 "fallback": 0, "total": 2})
        self.asked = []

    def sum_requests(self, since, until=None):
        self.asked.append(since)
        return self._totals


def test_the_cost_window_starts_at_the_promotion_not_seven_days_back():
    """The ledger aggregates across modes. A flat trailing window on a system
    promoted two days ago would add five days of SHADOW spending to a figure
    labelled as the hybrid's."""
    promoted = _old(48)
    config = dict(CONFIG)
    config[authority.PROMOTION_KEY] = {"at": promoted, "by": "operator"}

    db = _WindowDb()
    cost = authority._request_cost(db, config)
    assert cost["scope"] == "since_promotion"
    assert cost["since"] == promoted
    assert db.asked == [promoted], "the ledger is asked for the shorter window"


def test_an_older_promotion_falls_back_to_the_trailing_window():
    config = dict(CONFIG)
    config[authority.PROMOTION_KEY] = {"at": _old(24 * 30), "by": "operator"}
    db = _WindowDb()
    cost = authority._request_cost(db, config)
    assert cost["scope"] == "trailing_window"
    assert cost["since"] > config[authority.PROMOTION_KEY]["at"]


def test_an_unpromoted_system_reports_a_trailing_window_and_says_so():
    cost = authority._request_cost(_WindowDb(), {"hdencode_discovery_mode": "rss_shadow"})
    assert cost["scope"] == "trailing_window"
    assert cost["window_days"] == 7


def test_an_unparseable_promotion_time_does_not_mislabel_the_window():
    config = dict(CONFIG)
    config[authority.PROMOTION_KEY] = {"at": "whenever", "by": "operator"}
    cost = authority._request_cost(_WindowDb(), config)
    assert cost["scope"] == "trailing_window"


def test_the_scalar_health_fields_describe_the_worst_source():
    """These three were placeholders while no canary existed. Now that one
    does, a constant None would assert it has never succeeded."""
    db = _Db(states=[{"source_key": "hdencode:4k", "last_success_at": _old(1),
                      "consecutive_overlap_losses": 0},
                     {"source_key": "hdencode:1080p", "last_success_at": _old(5),
                      "consecutive_overlap_losses": 0}])
    config = dict(CONFIG,
                  hdencode_listing_canary_sources=["hdencode:4k", "hdencode:1080p"])
    fields = authority.status_fields(config, db)

    assert fields["canary_last_success"] is not None, (
        "a canary that succeeded an hour ago must not read as never having run")
    assert fields["canary_age_seconds"] > 4 * 3600, (
        "the scalar describes the weakest link, not the freshest source")
    assert fields["canary_interval_seconds"]


def test_the_timestamp_and_the_age_always_describe_the_same_source():
    """Reduced separately, they could name different sources.

    The older stamp here is stored in a +09:00 offset and the newer one in
    UTC, which is the mixed-shape case a stored timestamp can genuinely be in.
    Its local clock reads LATER, so picking the oldest by sorting the strings
    picks the wrong source -- while the parsed ages are unambiguous.
    """
    plus_nine = datetime_mod.timezone(datetime_mod.timedelta(hours=9))
    old = (_now_dt() - datetime_mod.timedelta(hours=9)).astimezone(
        plus_nine).isoformat()
    new = (_now_dt() - datetime_mod.timedelta(hours=1)).isoformat()
    assert old > new, "precondition: lexically the OLD stamp sorts last"

    db = _Db(states=[{"source_key": "hdencode:4k", "last_success_at": new,
                      "consecutive_overlap_losses": 0},
                     {"source_key": "hdencode:1080p", "last_success_at": old,
                      "consecutive_overlap_losses": 0}])
    config = dict(CONFIG,
                  hdencode_listing_canary_sources=["hdencode:4k", "hdencode:1080p"])
    fields = authority.status_fields(config, db)

    assert fields["canary_last_success"] == old, (
        "the scalar names the weakest source, not the lexically smallest string")
    assert fields["canary_age_seconds"] > 8 * 3600


def test_one_healthy_source_does_not_speak_for_a_source_that_never_ran():
    db = _Db(states=[{"source_key": "hdencode:4k", "last_success_at": _now(),
                      "consecutive_overlap_losses": 0},
                     {"source_key": "hdencode:1080p", "last_success_at": None,
                      "consecutive_overlap_losses": 0}])
    config = dict(CONFIG,
                  hdencode_listing_canary_sources=["hdencode:4k", "hdencode:1080p"])
    fields = authority.status_fields(config, db)
    assert fields["canary_last_success"] is None
    assert fields["canary_age_seconds"] is None


def test_the_scalars_stay_empty_when_the_state_cannot_be_read():
    fields = authority.status_fields(CONFIG, _Db(raise_states=True))
    assert fields["canary_last_success"] is None
    assert fields["canary_age_seconds"] is None


def test_the_promotion_route_sees_the_same_canary_the_status_page_does():
    """POST /rss/mode answers with this block. A permanent 'never succeeded'
    there would have the owner deciding against a fact nobody measured."""
    db = _Db(states=[{"source_key": "hdencode:4k", "last_success_at": _old(2),
                      "consecutive_overlap_losses": 0}])
    out = authority.evaluate_rss_primary_authority(CONFIG, db)
    assert out["canary"]["last_success"] is not None
    assert out["canary"]["age_seconds"] >= 2 * 3600 - 60
    assert out["canary"]["contract_hash"], "the existing field survives"

    blind = authority.evaluate_rss_primary_authority(CONFIG, _Db(raise_states=True))
    assert blind["canary"]["available"] is False
    assert blind["canary"]["last_success"] is None


def test_every_coverage_finding_is_counted_and_a_few_are_listed():
    """The detail could only ever hold ONE example, because the url was
    appended inside the 'blocker not already present' guard -- so one missed
    release and fifty read identically."""
    # Literal numbers, not EVIDENCE_EXAMPLES arithmetic: a test whose input is
    # derived from the constant it pins passes for ANY value of that constant,
    # including one so large the cap does nothing.
    assert authority.EVIDENCE_EXAMPLES == 20, (
        "the cap is a published policy; changing it should break a test")

    detail = {}
    for i in range(35):
        authority._note_example(detail, "gap_proven", "https://x/%d" % i)

    assert detail["gap_proven_count"] == 35, (
        "the count is exact however many there are")
    assert len(detail["gap_proven"]) == 20, (
        "the examples are capped; this is published on a polled endpoint")
    assert detail["gap_proven"][0] == "https://x/0"
    assert "https://x/34" not in detail["gap_proven"]


def test_the_status_block_carries_the_canary_and_the_cost():
    db = _Db(states=[{"source_key": "hdencode:4k", "last_success_at": _now(),
                      "consecutive_overlap_losses": 0}],
             totals={"rss_poll": 5, "canary": 1, "canary_retry": 0,
                     "fallback": 0, "total": 6})
    fields = authority.status_fields(CONFIG, db)
    assert fields["canary"]["available"] is True
    assert "hdencode:4k" in fields["canary"]["sources"]
    assert fields["request_cost"]["available"] is True
    assert "canary_evidence" in fields
    # And the surface still says what it always did.
    assert fields["requested_mode"] == "rss_primary"
    assert fields["effective_mode"] == "rss_shadow"
    assert fields["promotion_blockers"]
