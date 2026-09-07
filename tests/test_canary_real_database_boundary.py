"""The authority against a REAL database, end to end.

ADDED 2026-09-07 after an independent review found two defects that the whole
5,633-test suite could not see. Both had the same cause: every existing test
handed the authority values built in the test, and the real reader produces
different ones. A double that returns a datetime cannot show that the database
returns a string, and a double keyed the way the consumer asks cannot show that
the producer writes a different key.

So these tests use `DatabaseManager` itself, write through the real writers,
and read through the real readers. Nothing here constructs the values under
test.
"""
import datetime

import pytest

from backend import rss_primary_authority as authority
from backend.database import DatabaseManager

URL = "https://hdencode.example/a-release"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(hours_ago=0):
    return (_now() - datetime.timedelta(hours=hours_ago)).isoformat()


@pytest.fixture()
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "authority.db"))


def _promoted_config(**overrides):
    config = {
        "hdencode_discovery_mode": "rss_primary",
        "hdencode_rss_auto_demotion_enabled": True,
        # The CONTRACT DEFAULT spelling: bare categories, as an operator
        # configures them. The crawler writes "hdencode:4k".
        "hdencode_listing_canary_sources": ["4k"],
    }
    config.update(overrides)
    config[authority.PROMOTION_KEY] = {
        "at": (_now() - datetime.timedelta(days=1)).isoformat(), "by": "test",
        "canary_version": authority.CANARY_VERSION,
        "canary_contract_hash": authority.canary_contract_hash(config),
    }
    return config


def _record_canary_cycle(db, uuid, hours_ago, *, listing_only=(), feed_only=(),
                         duplicate=()):
    """Write one cycle the way production writes it."""
    db.record_listing_membership(uuid, "hdencode:4k", [
        {"canonical_url": url, "page_index": 1, "rank_on_page": i,
         "rss_present": False, "observed_at": _iso(hours_ago)}
        for i, url in enumerate(listing_only)])
    db.record_hdencode_shadow_comparison(
        cycle_uuid=uuid, started_at=_iso(hours_ago), completed_at=_iso(hours_ago),
        metrics={"normal_feeds_complete": True, "listing_complete": True,
                 "feed_only": list(feed_only), "listing_only": list(listing_only),
                 "duplicate_urls": list(duplicate), "outcome": "complete"},
        mode="rss_primary_canary")


def test_the_stored_cycle_timestamp_is_a_string_and_the_authority_survives_it(db):
    """REGRESSION (review HIGH 1). The reader returns `completed_at` as SQLite
    stored it -- a str. It was handed straight to the policy, which compares it
    with `<=` against parsed datetimes, so the authority raised TypeError the
    first time it had genuine evidence to judge. `evaluate_runtime` documents
    that it never raises; it did.
    """
    _record_canary_cycle(db, "cycle-old", 10, listing_only=[URL])
    _record_canary_cycle(db, "cycle-new", 2, listing_only=[URL])
    db.record_canary_attempt("hdencode:4k", at=_iso(0),
                             next_attempt_at=_iso(-6), outcome="success")

    stored = db.get_shadow_cycle_url_sets()["cycles"][0]["at"]
    assert isinstance(stored, str), (
        "precondition: the real reader returns a string, which is exactly what "
        "the doubles never did")

    evidence = authority.canary_evidence(_promoted_config(), db)
    assert isinstance(evidence["blockers"], list)

    runtime = authority.evaluate_runtime(_promoted_config(), db)
    assert runtime["state"] in (authority.STATE_AUTHORIZED,
                                authority.STATE_SUSPENDED,
                                authority.STATE_REVOKED)

    fields = authority.status_fields(_promoted_config(), db)
    assert fields["effective_mode"] in ("rss_primary", "rss_shadow")


def test_a_gap_is_still_proven_once_the_timestamps_parse(db):
    """The fix must not buy "never raises" by never deciding. Two protected
    sightings of a URL RSS never carried, with a complete canary after the
    first, is a proven gap -- and that verdict is only reachable because the
    canary's own timestamp now parses."""
    _record_canary_cycle(db, "cycle-old", 10, listing_only=[URL])
    _record_canary_cycle(db, "cycle-new", 2, listing_only=[URL])
    db.record_canary_attempt("hdencode:4k", at=_iso(0),
                             next_attempt_at=_iso(-6), outcome="success")

    evidence = authority.canary_evidence(_promoted_config(), db)
    assert authority.BLOCKER_GAP_PROVEN in evidence["blockers"], (
        "the authority must still reach a verdict on real evidence")
    assert evidence["detail"]["gap_proven_count"] >= 1


def test_a_cycle_whose_timestamp_cannot_be_read_is_unassessable_not_ignored(db):
    """The other half: a canary that cannot say when it ran cannot establish
    that it ran after anything. That is a durable "we can no longer tell",
    never a silent skip."""
    _record_canary_cycle(db, "cycle-ok", 5, listing_only=[URL])
    with db.transaction() as conn:
        conn.execute("UPDATE hdencode_shadow_cycles SET completed_at = ? "
                     "WHERE cycle_uuid = ?", ("not-a-timestamp", "cycle-ok"))
    db.record_canary_attempt("hdencode:4k", at=_iso(0),
                             next_attempt_at=_iso(-6), outcome="success")

    evidence = authority.canary_evidence(_promoted_config(), db)
    assert authority.BLOCKER_COVERAGE_UNASSESSABLE in evidence["blockers"]
    assert "cycle-ok" in evidence["detail"]["unreadable_cycle_timestamps"]
    assert authority.BLOCKER_COVERAGE_UNASSESSABLE in authority.REVOCATION_BLOCKERS


def test_the_newest_canary_is_chosen_by_instant_not_by_string_order(db):
    """A "+09:00" row sorts after a later "+00:00" one. Picking the newest
    canary by string comparison would name the wrong cycle, and the coverage
    verdict is derived from exactly that choice."""
    _record_canary_cycle(db, "cycle-newest", 1, listing_only=[URL])
    _record_canary_cycle(db, "cycle-older", 8, listing_only=[URL])
    plus_nine = datetime.timezone(datetime.timedelta(hours=9))
    older_in_plus_nine = (_now() - datetime.timedelta(hours=8)).astimezone(
        plus_nine).isoformat()
    newest = (_now() - datetime.timedelta(hours=1)).isoformat()
    assert older_in_plus_nine > newest, "precondition: the OLDER string sorts last"
    with db.transaction() as conn:
        conn.execute("UPDATE hdencode_shadow_cycles SET completed_at = ? "
                     "WHERE cycle_uuid = ?", (older_in_plus_nine, "cycle-older"))
        conn.execute("UPDATE hdencode_shadow_cycles SET completed_at = ? "
                     "WHERE cycle_uuid = ?", (newest, "cycle-newest"))
    db.record_canary_attempt("hdencode:4k", at=_iso(0),
                             next_attempt_at=_iso(-6), outcome="success")

    evidence = authority.canary_evidence(_promoted_config(), db)
    assert authority.BLOCKER_COVERAGE_UNASSESSABLE not in evidence["blockers"], (
        "both timestamps are valid; neither is unreadable")
    assert authority.BLOCKER_GAP_PROVEN in evidence["blockers"], (
        "the newest canary is an hour old and ran after the first sighting, so "
        "the gap is proven -- a string sort would have named the 8-hour-old "
        "cycle as newest and reported 'pending' instead")


def test_rss_carriage_from_before_the_promotion_cannot_excuse_a_gap_after_it(db):
    """REGRESSION (review HIGH 5). Every stored cycle was read, and every
    historical feed_only/duplicate URL was unioned into one set applied to all
    coverage decisions. So a URL RSS carried BEFORE the promotion made a
    listing-only sighting AFTER it look acquired, and the authority answered
    "authorized" on evidence that said the opposite."""
    promoted_at = _now() - datetime.timedelta(days=1)
    config = _promoted_config()
    config[authority.PROMOTION_KEY]["at"] = promoted_at.isoformat()

    # BEFORE the promotion: a shadow cycle in which RSS carried the URL.
    db.record_hdencode_shadow_comparison(
        cycle_uuid="pre-promotion", started_at=_iso(72), completed_at=_iso(72),
        metrics={"normal_feeds_complete": True, "listing_complete": True,
                 "feed_only": [URL], "listing_only": [], "duplicate_urls": [],
                 "outcome": "complete"},
        mode="rss_shadow")

    # AFTER it: four canaries that saw the URL on the listing and never in RSS.
    for i, hours in enumerate((20, 14, 8, 2)):
        _record_canary_cycle(db, "post-%d" % i, hours, listing_only=[URL])
    db.record_canary_attempt("hdencode:4k", at=_iso(0),
                             next_attempt_at=_iso(-6), outcome="success")

    evidence = authority.canary_evidence(config, db)
    assert authority.BLOCKER_GAP_PROVEN in evidence["blockers"], (
        "the pre-promotion carriage is outside the protected window and must "
        "not excuse a gap inside it")

    runtime = authority.evaluate_runtime(config, db)
    assert runtime["authorized"] is False
    assert authority.BLOCKER_GAP_PROVEN in runtime["revocations"]


def _qualification_cycles(db, *, days, every_hours=2, gap_after=None,
                          gap_hours=0):
    """Write eligible comparison cycles across `days`, the way shadow does."""
    start = _now() - datetime.timedelta(days=days)
    at = start
    i = 0
    inserted = 0
    while at <= _now():
        db.record_hdencode_shadow_comparison(
            cycle_uuid="qual-%d" % i, started_at=at.isoformat(),
            completed_at=at.isoformat(),
            metrics={"normal_feeds_complete": True, "listing_complete": True,
                     "feed_only": [], "listing_only": [], "duplicate_urls": [],
                     "outcome": "complete"},
            mode="rss_shadow")
        inserted += 1
        i += 1
        step = every_hours
        if gap_after is not None and inserted == gap_after:
            step = gap_hours
        at = at + datetime.timedelta(hours=step)
    return start


def test_a_full_clean_epoch_completes_the_qualification_window(db):
    """REGRESSION (review HIGH 4). The gate is 14 consecutive clean days with
    no gap over six hours. It was `if not cfg.get(EPOCH_KEY)` -- a truthiness
    test that four cycles spanning three minutes satisfied."""
    start = _qualification_cycles(db, days=16, every_hours=2)
    config = {authority.EPOCH_KEY: start.isoformat()}

    result = authority.qualification_continuity(config, db)
    assert result["complete"] is True, result["reasons"]
    assert result["consecutive_clean_days"] >= authority.QUALIFICATION_DAYS
    assert result["max_gap_hours"] <= authority.QUALIFICATION_MAX_GAP_HOURS


def test_three_minutes_of_cycles_is_not_fourteen_days(db):
    at = _now() - datetime.timedelta(minutes=3)
    for i in range(4):
        db.record_hdencode_shadow_comparison(
            cycle_uuid="quick-%d" % i,
            started_at=(at + datetime.timedelta(seconds=i * 60)).isoformat(),
            completed_at=(at + datetime.timedelta(seconds=i * 60)).isoformat(),
            metrics={"normal_feeds_complete": True, "listing_complete": True,
                     "feed_only": [], "listing_only": [], "duplicate_urls": [],
                     "outcome": "complete"},
            mode="rss_shadow")
    result = authority.qualification_continuity(
        {authority.EPOCH_KEY: at.isoformat()}, db)
    assert result["complete"] is False
    assert "fewer_than_14_clean_days" in result["reasons"]
    assert result["consecutive_clean_days"] < 1


def test_an_unreadable_epoch_timestamp_is_not_a_qualified_epoch(db):
    _qualification_cycles(db, days=16)
    for bad in ("not-a-timestamp", True, 1, ""):
        result = authority.qualification_continuity({authority.EPOCH_KEY: bad}, db)
        assert result["complete"] is False, "%r qualified" % (bad,)
    assert authority.qualification_continuity({}, db)["reasons"] == [
        "epoch_not_started"]


def test_an_outage_longer_than_the_maximum_gap_resets_the_clock(db):
    """The design says a gap over six hours resets the clock to the first
    eligible cycle after it, surfaced with its cause. The 2026-08-31 outage is
    exactly this case."""
    start = _qualification_cycles(db, days=20, every_hours=2, gap_after=100,
                                  gap_hours=30)
    result = authority.qualification_continuity(
        {authority.EPOCH_KEY: start.isoformat()}, db)
    assert result["max_gap_hours"] > authority.QUALIFICATION_MAX_GAP_HOURS
    assert "clock_reset_by_gap" in result["reasons"]
    assert result["complete"] is False
    assert result["consecutive_clean_days"] < 20, (
        "the run is measured from AFTER the outage, not from the epoch start")


def test_cycles_that_did_not_observe_cleanly_do_not_extend_the_window(db):
    start = _now() - datetime.timedelta(days=16)
    at = start
    i = 0
    while at <= _now():
        db.record_hdencode_shadow_comparison(
            cycle_uuid="dirty-%d" % i, started_at=at.isoformat(),
            completed_at=at.isoformat(),
            metrics={"normal_feeds_complete": False, "listing_complete": True,
                     "feed_only": [], "listing_only": [], "duplicate_urls": [],
                     "outcome": "incomplete"},
            mode="rss_shadow")
        i += 1
        at = at + datetime.timedelta(hours=2)
    result = authority.qualification_continuity(
        {authority.EPOCH_KEY: start.isoformat()}, db)
    assert result["eligible_cycles"] == 0
    assert "no_eligible_cycles_in_epoch" in result["reasons"]
    assert result["complete"] is False


def test_the_replay_ignores_membership_from_before_the_epoch(db):
    """REGRESSION (review HIGH 4/5). The replay is a claim about what this
    cadence would have missed DURING qualification. Reading all retained
    membership let an abandoned earlier qualification -- or evidence from
    before a reset -- decide whether the current one is safe."""
    epoch = _now() - datetime.timedelta(days=2)

    # Before the epoch: a dense run that would satisfy the replay on its own.
    for i in range(4):
        db.record_listing_membership("old-%d" % i, "hdencode:4k", [
            {"canonical_url": URL, "page_index": 1, "rank_on_page": 0,
             "rss_present": True,
             "observed_at": (epoch - datetime.timedelta(days=5 - i)).isoformat()}])

    config = {authority.EPOCH_KEY: epoch.isoformat(),
              "hdencode_listing_canary_sources": ["4k"]}
    replay = authority.canary_activation_evidence(config, db)
    assert replay["detail"]["per_source"]["4k"]["cycles"] == 0, (
        "pre-epoch cycles are not evidence about this qualification")
    assert authority.BLOCKER_WINDOW_UNKNOWN in replay["blockers"]

    # Two cycles INSIDE the epoch, and it can judge again.
    for i in range(2):
        db.record_listing_membership("new-%d" % i, "hdencode:4k", [
            {"canonical_url": URL, "page_index": 1, "rank_on_page": 0,
             "rss_present": True,
             "observed_at": (epoch + datetime.timedelta(hours=6 + i * 6)).isoformat()}])
    replay = authority.canary_activation_evidence(config, db)
    assert replay["detail"]["per_source"]["4k"]["cycles"] == 2
    assert replay["detail"]["epoch_started_at"] == epoch.isoformat()


def test_the_activation_replay_reads_the_rows_the_crawler_wrote(db):
    """REGRESSION (review HIGH 2). Membership is stored under "hdencode:4k";
    the replay asked for "4k". It found nothing and answered
    visibility_window_unknown -- a promotion that could never be granted, for a
    reason that was not true."""
    _record_canary_cycle(db, "cycle-1", 10, listing_only=[URL])
    _record_canary_cycle(db, "cycle-2", 2, listing_only=[URL])

    assert len(db.list_listing_membership(source_key="hdencode:4k")) == 2
    assert db.list_listing_membership(source_key="4k") == [], (
        "precondition: nothing is stored under the configured spelling")

    replay = authority.canary_activation_evidence(_promoted_config(), db)
    assert replay["detail"]["per_source"]["4k"]["cycles"] == 2, (
        "the replay must see the cycles the crawler actually recorded")
    assert authority.BLOCKER_WINDOW_UNKNOWN not in replay["blockers"]
