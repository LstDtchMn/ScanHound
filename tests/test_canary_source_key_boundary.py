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


def test_the_categories_here_are_the_ones_the_scanner_can_actually_build():
    """The authority keeps its own copy of the listing categories so it does
    not depend on the scanner. This is what stops the copy drifting: it comes
    from the real source builder, not from a list written twice."""
    from backend.scanner_service import ScannerService
    scanner = ScannerService.__new__(ScannerService)
    scanner.config = {"hdencode_enabled": True}
    every_flag = {c: True for c in authority.ALL_SCAN_CATEGORIES}
    arms = ScannerService._build_sources(
        scanner, "Incremental", "HDEncode", "https://hdencode.example",
        every_flag, "")
    assert {a["category"] for a in arms} == set(authority.HDENCODE_LISTING_CATEGORIES)


def test_the_flag_set_here_is_the_one_the_background_scan_understands():
    from backend.background_scanner import _ALL_CATEGORY_FLAGS
    assert set(_ALL_CATEGORY_FLAGS) == set(authority.ALL_SCAN_CATEGORIES)


@pytest.mark.parametrize("categories,expected", [
    (None, []),                                   # the default: everything
    ([], []),                                     # empty means ALL, not none
    (["4k", "remux", "tv"], []),
    (["4k"], ["remux", "tv"]),
    (["4k", "tv"], ["remux"]),
    # DDLBase-only categories are REAL flags, so the scanner does not fall
    # back to everything -- the HDEncode arms simply never get built.
    (["4k_webdl"], ["4k", "remux", "tv"]),
    # Nothing the scanner recognises: it falls back to all categories, so
    # refusing here would block a promotion the scanner would have served.
    (["nonsense"], []),
])
def test_which_canary_sources_the_scanner_would_never_crawl(categories, expected):
    config = {"hdencode_listing_canary_sources": ["4k", "remux", "tv"]}
    if categories is not None:
        config["background_scan_categories"] = categories
    assert sorted(authority.uncrawled_canary_sources(config)) == sorted(expected)


def test_the_authority_agrees_with_the_scanner_it_is_predicting():
    """Read off both producers for every selection above, rather than trusting
    the mirrored rule to have stayed a mirror."""
    from backend.background_scanner import BackgroundScanner
    from backend.scanner_service import ScannerService

    scanner = ScannerService.__new__(ScannerService)
    bg = BackgroundScanner.__new__(BackgroundScanner)

    for categories in (None, [], ["4k"], ["4k", "tv"], ["4k_webdl"],
                       ["nonsense"], ["4k", "remux", "tv"]):
        config = {"hdencode_enabled": True,
                  "hdencode_listing_canary_sources": ["4k", "remux", "tv"]}
        if categories is not None:
            config["background_scan_categories"] = categories

        bg._reg = type("Reg", (), {"config": config})()
        scanner.config = config
        arms = ScannerService._build_sources(
            scanner, "Incremental", "HDEncode", "https://hdencode.example",
            bg._category_flags(), "")
        really_crawled = {"%s:%s" % (a["source"], a["category"]) for a in arms}

        predicted_uncrawled = {
            authority.canary_source_key(s)
            for s in authority.uncrawled_canary_sources(config)}
        actually_uncrawled = {
            authority.canary_source_key(s)
            for s in config["hdencode_listing_canary_sources"]
            if authority.canary_source_key(s) not in really_crawled}
        assert predicted_uncrawled == actually_uncrawled, (
            "categories=%r: the authority predicted %r, the scanner crawls %r"
            % (categories, predicted_uncrawled, really_crawled))


@pytest.mark.parametrize("scan_sources,scan_enabled,expected_uncrawled", [
    (None, True, []),                                  # default list has HDEncode
    (["HDEncode", "DDLBase"], True, []),
    (["hdencode"], True, []),                          # case-insensitive
    (["DDLBase"], True, ["4k", "remux", "tv"]),        # the loop never enters it
    (["Adit-HD", "DDLBase"], True, ["4k", "remux", "tv"]),
    # RSS active with the background scan disabled: the scanner FORCES the
    # source list to ["HDEncode"], so the canaries are reachable after all.
    (["DDLBase"], False, []),
])
def test_the_guard_sees_the_source_switch_too(scan_sources, scan_enabled,
                                              expected_uncrawled):
    """ADDED 2026-09-07 (review MEDIUM 2). The guard mirrored hdencode_enabled
    and the category flags but ignored background_scan_sources, so with
    ["DDLBase"] it reported every HDEncode canary crawlable while the scan loop
    would never enter that arm."""
    config = {"hdencode_enabled": True,
              "hdencode_discovery_mode": "rss_shadow",
              "background_scan_enabled": scan_enabled,
              "hdencode_listing_canary_sources": ["4k", "remux", "tv"]}
    if scan_sources is not None:
        config["background_scan_sources"] = scan_sources
    assert sorted(authority.uncrawled_canary_sources(config)) == sorted(
        expected_uncrawled)


def test_the_guard_agrees_with_the_source_list_the_scan_loop_actually_uses():
    """Read off the scanner, not restated: the default list and the forced-list
    rule both come from background_scanner itself."""
    from backend import background_scanner
    import inspect
    assert "hdencode" in {s.lower() for s in background_scanner._DEFAULT_SOURCES}
    src = inspect.getsource(background_scanner.BackgroundScanner.scan_once)
    assert 'sources = cfg.get("background_scan_sources") or _DEFAULT_SOURCES' in src
    assert 'sources = ["HDEncode"]' in src, (
        "the forced-list rule the guard mirrors must still exist")


def test_the_authority_and_the_scanner_agree_on_cadence_and_depth():
    """ADDED 2026-09-07 (review MEDIUM 4). With canary_minutes = 1 the replay
    judged a 60-second cadence while the scheduler clamps the real crawl to
    900 seconds, and the authority accepted any depth while the crawler caps at
    99. A promotion granted on a cadence nobody runs is granted on nothing.

    Both numbers are read from the real scheduler and the real crawler here,
    not restated."""
    from backend.background_scanner import BackgroundScanner
    from backend.scanner_service import ScannerService
    import inspect

    scanner = BackgroundScanner.__new__(BackgroundScanner)
    for configured in (1, 5, 14, 15, 360, 1440):
        cfg = {"hdencode_listing_canary_minutes": configured}
        contract_seconds = int(
            authority.contract_inputs(cfg)["hdencode_listing_canary_minutes"]) * 60
        assert scanner._canary_interval_seconds(cfg) == contract_seconds, (
            "the scheduler and the contract disagree at %r minutes" % configured)

    for configured in (0, 1, 3, 99, 500):
        cfg = {"hdencode_listing_canary_pages": configured}
        contract_pages = authority.contract_inputs(cfg)["hdencode_listing_canary_pages"]
        assert contract_pages == min(max(1, configured), 99)
        assert scanner._canary_pages(cfg) == contract_pages

    # And the caps themselves come from the real code, so a change there
    # breaks this rather than drifting silently.
    assert "return max(900, minutes * 60)" in inspect.getsource(
        BackgroundScanner._canary_interval_seconds)
    assert "pages = min(max(1, pages), 99)" in inspect.getsource(
        ScannerService.run_scan)


def test_normalising_the_contract_does_not_move_the_default_hash():
    """The defaults sit inside both ranges, so no ordinary configuration's
    promotion is invalidated by the normalisation."""
    plain = authority.canary_contract_hash({})
    explicit = authority.canary_contract_hash({
        "hdencode_listing_canary_minutes":
            authority.CONTRACT_KEYS["hdencode_listing_canary_minutes"],
        "hdencode_listing_canary_pages":
            authority.CONTRACT_KEYS["hdencode_listing_canary_pages"],
    })
    assert plain == explicit
    # A value the machinery would have overridden hashes as what will run.
    assert authority.canary_contract_hash({"hdencode_listing_canary_minutes": 1}) == \
        authority.canary_contract_hash({"hdencode_listing_canary_minutes": 15})


def test_a_disabled_listing_source_makes_every_canary_source_uncrawled():
    config = {"hdencode_enabled": False,
              "hdencode_listing_canary_sources": ["4k", "remux"]}
    assert sorted(authority.uncrawled_canary_sources(config)) == ["4k", "remux"]


def test_promotion_is_refused_while_a_canary_source_is_never_crawled(db):
    """It was already fail-safe -- the source never succeeds, ages out, and
    the runtime revokes. What it was not, was knowable at the moment of
    promotion rather than half a day later."""
    config = {"hdencode_discovery_mode": "rss_shadow",
              "hdencode_enabled": True,
              "hdencode_listing_canary_sources": ["4k", "remux", "tv"],
              "background_scan_categories": ["4k"],
              "hdencode_rss_auto_demotion_enabled": True}
    activation = authority.evaluate_activation(config, db, None)
    assert authority.BLOCKER_CANARY_SOURCE_NOT_CRAWLED in activation["blockers"]
    assert sorted(activation["uncrawled_canary_sources"]) == ["remux", "tv"], (
        "the owner is told WHICH sources, not just that something is wrong")
    assert activation["eligible"] is False

    config["background_scan_categories"] = ["4k", "remux", "tv"]
    reopened = authority.evaluate_activation(config, db, None)
    assert authority.BLOCKER_CANARY_SOURCE_NOT_CRAWLED not in reopened["blockers"]


def test_the_status_surface_names_the_uncrawled_sources(db):
    """The blocker alone is not actionable: an owner has to know WHICH source
    to switch back on."""
    config = {"hdencode_discovery_mode": "rss_shadow",
              "hdencode_enabled": True,
              "hdencode_listing_canary_sources": ["4k", "remux", "tv"],
              "background_scan_categories": ["4k"],
              "hdencode_rss_auto_demotion_enabled": True}
    activation = authority.status_fields(config, db)["activation"]
    assert authority.BLOCKER_CANARY_SOURCE_NOT_CRAWLED in activation["blockers"]
    assert sorted(activation["uncrawled_canary_sources"]) == ["remux", "tv"]


def test_the_runtime_gains_no_new_way_to_demote_a_running_system(db):
    """Deliberately activation-only. A category switched off under a live
    promotion is already covered by the canary going stale, and adding a
    second, faster demotion path would demote on a config read rather than on
    observed evidence."""
    config = {"hdencode_discovery_mode": "rss_primary",
              "hdencode_enabled": True,
              "hdencode_listing_canary_sources": ["4k", "remux"],
              "background_scan_categories": ["4k"],
              "hdencode_rss_auto_demotion_enabled": True}
    config[authority.PROMOTION_KEY] = {
        "at": _now().isoformat(), "by": "operator",
        "canary_version": authority.CANARY_VERSION,
        "canary_contract_hash": authority.canary_contract_hash(config),
    }
    db.record_canary_attempt("hdencode:4k", at=_now().isoformat(),
                             next_attempt_at=_now().isoformat(), outcome="success")
    db.record_canary_attempt("hdencode:remux", at=_now().isoformat(),
                             next_attempt_at=_now().isoformat(), outcome="success")

    runtime = authority.evaluate_runtime(config, db)
    assert authority.BLOCKER_CANARY_SOURCE_NOT_CRAWLED not in runtime["blockers"]
    assert authority.BLOCKER_CANARY_SOURCE_NOT_CRAWLED not in runtime["revocations"]


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
