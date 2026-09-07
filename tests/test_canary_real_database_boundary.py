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
