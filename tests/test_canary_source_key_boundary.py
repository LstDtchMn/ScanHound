"""The two names for one canary source, and the boundary between them.

The contract names canary sources by listing category ("4k"). The crawler
names each listing arm it traverses "hdencode:4k", and that is what reaches
hdencode_listing_membership and hdencode_canary_state. Every test in this file
deliberately puts the CONFIGURED spelling in and asserts against the CRAWLER's
spelling, because a test that uses one spelling on both sides of the boundary
passes whether or not the two agree -- which is how the mismatch survived the
whole suite.
"""
import datetime

import pytest

from backend import rss_primary_authority as authority
from backend.database import DatabaseManager


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


@pytest.fixture()
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "canary.db"))


CONFIG = {
    "hdencode_discovery_mode": "rss_primary",
    # Exactly the contract default, by category alone.
    "hdencode_listing_canary_sources": ["4k", "remux", "tv"],
    "hdencode_rss_auto_demotion_enabled": True,
}


def test_the_contract_default_is_named_by_category_not_by_source_key():
    """Pins the premise. If the default ever becomes fully qualified, the
    boundary below stops being exercised and this says so."""
    assert authority.CONTRACT_KEYS["hdencode_listing_canary_sources"] == [
        "4k", "remux", "tv"]
    assert set(authority.CONTRACT_KEYS["hdencode_listing_canary_feed_map"]) == {
        "4k", "remux", "tv"}, "the feed map is keyed the same way"


def test_a_category_resolves_to_the_key_the_crawler_writes():
    assert authority.canary_source_key("4k") == "hdencode:4k"
    assert authority.canary_source_key("tv") == "hdencode:tv"
    # Already qualified: passed through, so another listing can be named.
    assert authority.canary_source_key("ddlbase:remux") == "ddlbase:remux"


def test_the_crawler_writes_the_key_this_function_predicts():
    """Read off the producer rather than restated. A constant repeated on both
    sides of a boundary agrees with itself by construction."""
    from backend import scanner_service
    import inspect
    src = inspect.getsource(scanner_service.ScannerService._crawl_pages)
    assert '"source_key": "%s:%s" % (source_id, source_category or "default")' in src


def test_hdencode_listing_arms_are_named_by_the_contract_categories():
    """The other half of the same premise, taken from the real source builder:
    the crawler's categories for the hdencode source are the strings the
    contract configures."""
    from backend.scanner_service import ScannerService
    scanner = ScannerService.__new__(ScannerService)
    scanner.config = {"hdencode_enabled": True}
    arms = ScannerService._build_sources(
        scanner, "Incremental", "HDEncode", "https://hdencode.example",
        {"4k": True, "remux": True, "tv": True}, "")
    keys = {"%s:%s" % (s["source"], s["category"]) for s in arms}
    assert keys == {authority.canary_source_key(s)
                    for s in CONFIG["hdencode_listing_canary_sources"]}, (
        "every configured canary source must resolve to a listing arm that "
        "actually gets crawled")


def test_a_canary_recorded_by_the_crawler_is_found_by_the_status_surface(db):
    """The failure this file exists for: the canary ran and succeeded, and the
    surface reported it had never run."""
    db.record_canary_attempt("hdencode:4k", at=_now().isoformat(),
                             next_attempt_at=_now().isoformat(),
                             outcome="success")
    health = authority._canary_health(CONFIG, db, {"detail": {}})

    assert health["available"] is True
    entry = health["sources"]["4k"]
    assert entry["source_key"] == "hdencode:4k", (
        "the surface publishes the key the row is actually under")
    assert entry["has_run"] is True, (
        "a canary that succeeded must not read as one that never ran")
    assert entry["stale"] is False


def test_the_scheduler_finds_the_state_the_crawler_wrote(db):
    """Not finding it made the canary due on every cycle -- the hybrid paying
    MORE requests than the listing-only mode it replaced."""
    from backend.background_scanner import BackgroundScanner
    scanner = BackgroundScanner.__new__(BackgroundScanner)

    db.record_canary_attempt(
        "hdencode:4k", at=_now().isoformat(),
        next_attempt_at=(_now() + datetime.timedelta(hours=6)).isoformat(),
        outcome="success")
    db.record_canary_attempt(
        "hdencode:remux", at=_now().isoformat(),
        next_attempt_at=(_now() + datetime.timedelta(hours=6)).isoformat(),
        outcome="success")
    db.record_canary_attempt(
        "hdencode:tv", at=_now().isoformat(),
        next_attempt_at=(_now() + datetime.timedelta(hours=6)).isoformat(),
        outcome="success")

    assert scanner._canary_is_due(db, CONFIG) is False, (
        "every source is scheduled six hours out; nothing is due")

    db.record_canary_attempt(
        "hdencode:tv", at=_now().isoformat(),
        next_attempt_at=(_now() - datetime.timedelta(minutes=1)).isoformat(),
        outcome="success")
    assert scanner._canary_is_due(db, CONFIG) is True, (
        "one overdue source makes the canary due")


def test_the_evidence_reads_the_membership_the_crawler_wrote(db):
    """Not just the status surface. canary_evidence looked membership up by
    the configured name too, found none, and reported the evidence as
    permanently too thin to judge -- so a promoted system could never clear
    its own suspension.
    """
    promoted_at = _now() - datetime.timedelta(days=1)
    config = dict(CONFIG, hdencode_listing_canary_sources=["4k"])
    config[authority.PROMOTION_KEY] = {
        "at": promoted_at.isoformat(), "by": "test",
        "canary_version": authority.CANARY_VERSION,
        "canary_contract_hash": authority.canary_contract_hash(config),
    }
    db.record_canary_attempt("hdencode:4k", at=_now().isoformat(),
                             next_attempt_at=_now().isoformat(),
                             outcome="success")
    for i in range(6):
        at = (_now() - datetime.timedelta(hours=6 - i)).isoformat()
        db.record_listing_membership("cycle-%d" % i, "hdencode:4k", [
            {"canonical_url": "https://hdencode.example/never-in-rss",
             "page_index": 1, "rank_on_page": 0, "rss_present": False,
             "observed_at": at}])

    evidence = authority.canary_evidence(config, db)
    assert evidence["detail"]["sources"]["4k"]["source_key"] == "hdencode:4k"
    assert authority.BLOCKER_EVIDENCE_INSUFFICIENT not in evidence["blockers"], (
        "the membership is there; reading it under the wrong key is what made "
        "the evidence look thin")


def test_membership_written_by_the_crawler_is_read_as_evidence(db):
    """canary_evidence looked membership up by the configured name, found
    none, and reported the evidence as permanently too thin to judge."""
    at = _now().isoformat()
    db.record_listing_membership("cycle-1", "hdencode:4k", [
        {"canonical_url": "https://hdencode.example/a", "page_index": 1,
         "rank_on_page": 0, "rss_present": True, "observed_at": at}])
    rows = db.list_listing_membership(source_key="hdencode:4k")
    assert rows, "precondition: the producer's row exists"

    read = db.list_listing_membership(
        source_key=authority.canary_source_key("4k"))
    assert read == rows, "the consumer resolves to the producer's key"
