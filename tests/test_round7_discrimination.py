"""Discrimination tests for the five round-7 counterexamples.

EVERY test here must FAIL against the pre-fix code. That is the whole point: five
review rounds in a row I wrote a test that built the exact fixture needed to expose
a defect and then asserted something the broken code also satisfied. So each test
below names the wrong answer it detects, and the ones where an inverted
implementation would still pass have been deliberately reshaped until it would not.

The five counterexamples:

  1. A run that ends WITHOUT entering the crawl inherits the previous run's
     termination, beside a seen-set that was already emptied -> stale "complete"
     over an empty listing -> every RSS URL reads as acquired.
  2. A cycle whose listing membership is contradicted still counts in the readiness
     window, so the qualification claim rests on evidence its own resolver rejects.
  3. A configured HDEncode mirror is classified one way by the queue and another by
     DownloadService, so it skips coordinator / off-switch / health ownership.
  4. A direct file-host URL is dispatched into the HDEncode reveal-page path.
  5. Historical detail failures block readiness forever, including for URLs that
     RSS did carry (duplicates) and URLs later attributed successfully.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from backend.download_outcome import _FAILURE_TITLES
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic, _MESSAGES
from backend.source_identity import SOURCE_KINDS


# ─────────────────────────────────────────────────────────────────────────────
# 1. Pre-crawl exit must not inherit the previous run's crawl authority
# ─────────────────────────────────────────────────────────────────────────────

def _scanner():
    """A ScannerService with the crawl-authority attributes, no scraping."""
    from backend.scanner_service import ScannerService
    svc = ScannerService.__new__(ScannerService)
    svc.config = {"base_url": "https://hdencode.org"}
    svc.db = None
    svc.items = []
    svc._item_counter = 0
    import threading
    svc._items_lock = threading.RLock()
    # stop_scan_flag and is_scanning are PROPERTIES over an Event and a lock, so
    # the backing objects have to exist before either is assigned.
    svc._stop_event = threading.Event()
    svc._scanning_lock = threading.RLock()
    svc._is_scanning = False
    svc.stop_scan_flag = False
    svc.is_scanning = False
    svc._last_crawl_seen_urls = set()
    svc._last_crawl_request_count = 0
    svc._last_crawl_termination = "not_run"
    svc._last_crawl_status = "not_run"
    svc._last_crawl_page_errors = 0
    svc._last_crawl_detail_scheduled = set()
    svc._last_crawl_detail_completed = set()
    svc.download_history = set()
    svc._log = lambda *a, **k: None
    svc._load_download_history = lambda: set()

    class _M:
        class app:
            download_history = set()
    svc.matching = _M()
    return svc


def _run_scan_with_async(svc, async_body):
    """Drive the real run_scan(), substituting the async phase."""
    svc._run_scan_async = async_body
    return svc.run_scan("Latest", "HDEncode", 1)


def test_pre_crawl_exception_does_not_inherit_previous_termination():
    """THE WRONG ANSWER: stale "complete" beside an emptied seen-set.

    That combination makes every RSS URL `feed_only`, which the resolver reads as
    affirmative acquisition -- i.e. mass FALSE acquisition, worse than the miss
    this machinery exists to detect.
    """
    svc = _scanner()

    # Run 1: a clean crawl that legitimately completes.
    async def good(*a, **k):
        svc._last_crawl_termination = "complete"
        svc._last_crawl_status = "complete"
        svc._last_crawl_seen_urls = {"https://hdencode.org/a/"}
    _run_scan_with_async(svc, good)
    assert svc._last_crawl_termination == "complete"

    # Run 2: raises BEFORE any crawl state is touched.
    async def boom(*a, **k):
        raise RuntimeError("Plex unreachable")
    _run_scan_with_async(svc, boom)

    assert svc._last_crawl_seen_urls == set(), "seen-set is emptied at run entry"
    # Both must move off the stale value. Asserting only != "complete" would pass
    # for any garbage, so pin the exact expected verdict.
    assert svc._last_crawl_termination == "scan_error"
    assert svc._last_crawl_status == "scan_error"


def test_pre_crawl_early_return_does_not_inherit_previous_termination():
    """The same defect with NO exception involved at all.

    _run_scan_async returns early at "No sources selected" and at "HDEncode is
    disabled in Settings" -- an ordinary configuration state. The review only
    described the exception path; this one is reachable by a user toggling a
    checkbox, which makes it the likelier of the two.
    """
    svc = _scanner()

    async def good(*a, **k):
        svc._last_crawl_termination = "complete"
        svc._last_crawl_status = "complete"
        svc._last_crawl_seen_urls = {"https://hdencode.org/a/"}
    _run_scan_with_async(svc, good)

    async def disabled(*a, **k):
        return  # exactly what the `if not sources:` branch does
    _run_scan_with_async(svc, disabled)

    assert svc._last_crawl_seen_urls == set()
    assert svc._last_crawl_termination == "not_run", (
        "a run that never crawled must report not_run, not the previous "
        "run's completion")
    assert svc._last_crawl_status == "not_run"


def test_run_entry_clears_detail_sets_and_page_errors():
    """The other authority fields reset too, not just the two the review named."""
    svc = _scanner()
    svc._last_crawl_detail_scheduled = {"https://hdencode.org/x/"}
    svc._last_crawl_detail_completed = {"https://hdencode.org/x/"}
    svc._last_crawl_page_errors = 7

    async def noop(*a, **k):
        return
    _run_scan_with_async(svc, noop)

    assert svc._last_crawl_detail_scheduled == set()
    assert svc._last_crawl_detail_completed == set()
    assert svc._last_crawl_page_errors == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. A contradicted cycle must not count in the readiness window
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    from backend.database import DatabaseManager
    mgr = DatabaseManager(str(tmp_path / "t.db"))
    yield mgr
    mgr.close()


def _cycle(db, *, listing_complete, at, outcome="success", details=None):
    """Insert one shadow cycle, filling NOT NULL columns generically."""
    info = db._query_dicts("PRAGMA table_info(hdencode_shadow_cycles)", default=[])
    names = {r["name"] for r in info}
    required = {r["name"] for r in info
                if r["notnull"] and r["dflt_value"] is None and r["name"] != "id"}
    cols = {
        "cycle_uuid": str(uuid.uuid4()), "completed_at": at, "outcome": outcome,
        "normal_feeds_complete": 1, "rss_requests": 2, "listing_requests": 1,
        "listing_complete": listing_complete,
        "normal_feed_outcomes": json.dumps(
            {"movies_all": "changed", "tv_all": "changed"}),
        "details_json": json.dumps(details or {}),
        "restart_recovery": 0, "catchup_used": 0,
    }
    for c in required:
        cols.setdefault(c, 0)
    cols = {k: v for k, v in cols.items() if k in names}
    with db.transaction() as conn:
        conn.execute(
            f"INSERT INTO hdencode_shadow_cycles ({','.join(cols)}) "
            f"VALUES ({','.join('?' for _ in cols)})", list(cols.values()))
    return cols["cycle_uuid"]


def test_contradicted_cycle_is_excluded_from_the_readiness_window(db):
    """THE WRONG ANSWER: 2 successful cycles when only 1 is trustworthy.

    Counting the count is what makes this discriminating -- an assertion that the
    summary merely "exists" passes either way.
    """
    _cycle(db, listing_complete=1, at="2026-08-01T00:00:00+00:00")
    _cycle(db, listing_complete=0, at="2026-08-02T00:00:00+00:00")

    summary = db.get_hdencode_shadow_summary()
    assert summary["successful_cycles"] == 1, (
        "the contradicted cycle must not increment successful_cycles")
    # It must not stretch the observation window to the contradicted cycle's date
    # either, which is the figure the qualification claim is measured in.
    assert str(summary.get("last_completed_at") or "").startswith("2026-08-01")


def test_legacy_null_listing_complete_still_counts(db):
    """NULL is pre-column data and must stay admissible -- stated, not implied.

    Without this the fix would silently invalidate every cycle recorded before the
    column existed, i.e. the entire historical qualification window.
    """
    _cycle(db, listing_complete=None, at="2026-07-01T00:00:00+00:00")
    assert db.get_hdencode_shadow_summary()["successful_cycles"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. DownloadService identity must follow the configured host
# ─────────────────────────────────────────────────────────────────────────────

def _service(config):
    from backend.download_service import DownloadService
    svc = DownloadService.__new__(DownloadService)
    svc.config = config
    return svc


MIRROR = "https://hdencode.example.net"


def test_configured_mirror_agrees_between_queue_and_download_service():
    """THE WRONG ANSWER: queue says hdencode, DownloadService says other.

    Asserted as an EQUALITY between the two production classifiers rather than
    against a literal, because the defect was disagreement -- a test that pinned
    each side to an expected string separately is exactly the shape that let them
    drift apart in the first place.
    """
    from backend.download_queue import _source
    url = "https://hdencode.example.net/some-movie-2160p/"
    svc = _service({"base_url": MIRROR})

    queue_side = _source(url, hdencode_host=MIRROR)
    service_side = svc._source_kind_of(url)
    assert (service_side == "hdencode") == (queue_side == "hdencode"), (
        f"queue={queue_side!r} service={service_side!r} for a configured mirror")
    assert service_side == "hdencode"


def test_old_default_host_is_not_hdencode_once_a_mirror_is_configured():
    """The reverse disagreement the review also named. Inverting the fix fails here."""
    svc = _service({"base_url": MIRROR})
    assert svc._source_kind_of("https://hdencode.org/a-movie-2160p/") != "hdencode"


def test_service_identity_falls_back_when_base_url_is_missing_or_blank():
    for cfg in ({}, {"base_url": ""}, {"base_url": None}):
        svc = _service(cfg)
        assert svc._source_kind_of("https://hdencode.org/x/") == "hdencode", cfg


def test_all_three_consumer_sites_use_the_config_aware_helper():
    """A structural guard, because the defect WAS a call site not being updated.

    The three behavioural sites (coordinator, off-switch/dispatch, health
    ownership) are unreachable without a browser, so this asserts the property the
    review actually found: no production call passes the URL alone.
    """
    import inspect
    from backend import download_service as mod
    src = inspect.getsource(mod)
    body = src.split("def _source_kind_of", 1)[1]
    body = body.split("\n    def ", 1)[1] if "\n    def " in body else body
    assert "_source_page_kind(url)" not in body, (
        "a consumer still classifies without the configured host")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Five-kind dispatch must be exhaustive, with no default-to-HDEncode
# ─────────────────────────────────────────────────────────────────────────────

def _scrape_service(config=None):
    from backend.download_service import DownloadService
    svc = DownloadService.__new__(DownloadService)
    svc.config = config or {"base_url": "https://hdencode.org",
                            "hdencode_enabled": True}
    svc._log = lambda *a, **k: None
    import threading
    svc._driver_lock = threading.RLock()
    svc._scrape_count_lock = threading.Lock()
    svc._active_scrapes = 0
    return svc


def test_direct_file_url_never_reaches_the_hdencode_scraper():
    """THE WRONG ANSWER: a Rapidgator URL run through HDEncode reveal logic.

    It clicks for a reveal control that cannot exist, then reports layout_changed
    or a reveal stall -- attributing the failure to HDEncode's source health and,
    on the throttle path, cooling down the whole source over a URL that has nothing
    to do with HDEncode.

    Discriminating by construction: the HDEncode implementation is replaced with a
    tripwire, so the pre-fix code fails this test by RAISING.
    """
    svc = _scrape_service()

    def _tripwire(*a, **k):
        raise AssertionError("HDEncode navigation reached for a direct file host")
    # The HDEncode branch's first real action. Pre-fix, a direct-file URL reached
    # it; post-fix nothing can.
    svc._navigate_with_diagnostic = _tripwire

    result = svc.scrape_links("https://rapidgator.net/file/abc/x.rar", "Rapidgator")

    # THE PROPERTY UNDER TEST IS THE TRIPWIRE ABOVE: no HDEncode navigation. That is
    # what round 7 was about, and it still holds.
    #
    # THE RETURN VALUE ASSERTION WAS REWRITTEN ON ROUND 8. It used to require
    # `list(result) == []` plus a diagnostic, because I had designed the branch
    # around download_item()'s `if not links:` fallback. Round 8 showed that
    # contract only works for that ONE caller out of five, so a supported direct
    # host now returns itself. My original assertion had codified the defect --
    # the same "my own test protected the thing that was wrong" pattern this
    # effort has hit repeatedly, and it is why the round-8 replacement asserts the
    # value every caller receives rather than one caller's recovery.
    assert list(result) == ["https://rapidgator.net/file/abc/x.rar"], (
        "a supported direct host must be returned to every caller")
    assert getattr(result, "diagnostic", None) is None, (
        "a passthrough is not a failure and must carry no diagnostic")


def test_unknown_host_is_reported_as_unsupported_not_as_hdencode():
    svc = _scrape_service()

    def _tripwire(*a, **k):
        raise AssertionError("HDEncode navigation reached for an unknown host")
    # The HDEncode branch's first real action. Pre-fix, a direct-file URL reached
    # it; post-fix nothing can.
    svc._navigate_with_diagnostic = _tripwire

    result = svc.scrape_links("https://totally-unknown.example/page/", "Rapidgator")
    assert result.diagnostic.code is ScrapeCode.UNSUPPORTED_SOURCE
    assert result.diagnostic.affects_source_health is False


def test_direct_link_diagnostic_is_not_source_wide():
    """A direct link must never pause the whole source."""
    from backend.download_outcome import is_source_wide_denial
    for code in (ScrapeCode.DIRECT_LINK_NO_SOURCE_PAGE, ScrapeCode.UNSUPPORTED_SOURCE):
        diag = ScrapeDiagnostic(code, affected_scope="item")
        assert not is_source_wide_denial(diag.to_dict()), code


def test_every_source_kind_has_explicit_dispatch():
    """No semantic default. Adding a sixth kind must break loudly, not silently.

    Reads the dispatch source rather than calling it, because three of the five
    branches need a browser. The property under test is textual by nature: the
    review's finding was that a `default:` comment stood where a branch should.
    """
    import inspect
    from backend.download_service import DownloadService
    src = inspect.getsource(DownloadService.scrape_links)
    for kind in SOURCE_KINDS:
        assert f'"{kind}"' in src, f"{kind} has no explicit branch in scrape_links"
    assert "unhandled source kind" in src, (
        "no guard against a future sixth kind falling through")


def test_every_scrape_code_has_a_message_and_a_failure_title():
    """The exhaustiveness test that did not exist.

    Both maps are hand-maintained and keyed on the enum, so a new code silently
    renders as a KeyError (public_message) or as the generic "Download Failed".
    Nothing asserted either was complete -- which is how I would have shipped the
    two new codes with a missing title.
    """
    for code in ScrapeCode:
        assert code in _MESSAGES, f"{code.value} has no user-facing message"
        assert code.value in _FAILURE_TITLES, f"{code.value} has no failure title"


def test_no_failure_title_asserts_an_unproven_cause():
    """Titles must describe what was OBSERVED, never why.

    The existing check above proves a title EXISTS; it never looked at the words.
    So `reveal_verification_stalled` rendered as "HDEncode is throttling" for two
    weeks after the reason code had been deliberately made neutral, and it is the
    title the user actually reads. On 2026-08-09 that attribution was refuted
    outright -- the exact stalled URL served links to a phone browser with almost
    no wait, so the source was fine. The sibling test on public_message forbids
    this wording already; nothing extended the rule to the rendered title.

    Deliberately narrow: it bans CAUSAL vocabulary, not the mention of a source.
    "HDEncode could not be reached" is an observation and stays legal.
    """
    banned = ("throttl", "rate-limit", "rate limit", "rate limiting",
              "blocking us", "banned", "blacklist")
    for code_value, title in _FAILURE_TITLES.items():
        lowered = title.lower()
        for word in banned:
            assert word not in lowered, (
                f"failure title for {code_value!r} asserts a cause we have not "
                f"proven: {title!r} contains {word!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Unresolved listing-only candidates, not every historical detail failure
# ─────────────────────────────────────────────────────────────────────────────

DUPE = "https://hdencode.org/dupe-movie-2160p/"
ONLY = "https://hdencode.org/listing-only-2160p/"


def test_detail_failure_on_a_duplicate_does_not_block(db):
    """THE WRONG ANSWER: a URL RSS DID carry blocks readiness.

    It is in both the feed and the listing, so it is the success case -- RSS found
    it. Its detail scrape failing says nothing about RSS coverage.
    """
    _cycle(db, listing_complete=1, at="2026-08-01T00:00:00+00:00",
           details={"detail_failed": [DUPE], "listing_only": [], "feed_only": []})
    res = db.get_hdencode_miss_resolution()
    assert res["unattributed_candidates"] == 0, res.get("unattributed_candidate_urls")


def test_listing_only_detail_failure_does_block(db):
    """The positive control. Without it, the test above passes on a function that
    always returns zero -- which is the "stubbed dependency invalidates the
    control" failure I have already made once this session."""
    _cycle(db, listing_complete=1, at="2026-08-01T00:00:00+00:00",
           details={"detail_failed": [ONLY], "listing_only": [ONLY],
                    "feed_only": []})
    res = db.get_hdencode_miss_resolution()
    assert res["unattributed_candidates"] == 1
    assert res["unattributed_candidate_urls"] == [ONLY]


def test_a_later_rss_observation_clears_the_candidate(db):
    """THE WRONG ANSWER: one transient scrape failure blocks readiness forever.

    A sum over history cannot subtract, so nothing could ever clear it. That part of
    round 7 stands.

    INVERTED ON ROUND 8, AND THIS TEST WAS THE PROBLEM. My original version had the
    later cycle keep the URL in `listing_only` with a working detail scrape, and
    asserted that cleared the candidate. `listing_only` MEANS RSS DID NOT CARRY IT --
    so I had written a test asserting that an RSS-coverage blocker is resolved by
    evidence of an RSS coverage gap, and the test then protected exactly the
    fail-open the reviewer found. Third time a test of mine has guarded the defect it
    was written to catch.

    The clearing evidence is now affirmative RSS carriage: `feed_only` (in RSS, not
    in the listing) or `duplicate_urls` (in RSS and in the listing).
    """
    _cycle(db, listing_complete=1, at="2026-08-01T00:00:00+00:00",
           details={"detail_failed": [ONLY], "listing_only": [ONLY],
                    "feed_only": []})
    _cycle(db, listing_complete=1, at="2026-08-02T00:00:00+00:00",
           details={"detail_failed": [], "listing_only": [],
                    "feed_only": [], "duplicate_urls": [ONLY]})
    res = db.get_hdencode_miss_resolution()
    assert res["unattributed_candidates"] == 0, res.get("unattributed_candidate_urls")


OTHER = "https://hdencode.org/second-listing-only-2160p/"


def test_a_contradicted_cycle_cannot_clear_a_candidate(db):
    """The asymmetry, asserted. Clearing is permissive, so an untrusted cycle must
    not be the thing that unblocks readiness -- the same fail-open shape as HIGH 2,
    one layer down.

    TWO candidates on purpose. My first version of this test used one, asserted
    `== 1`, and passed against the pre-fix code as well -- because code that never
    clears anything cannot clear wrongly, so it got the right answer by accident.
    With two, one cleared by a TRUSTED cycle and one a contradicted cycle merely
    attempts to clear, the expected count differs from every wrong implementation:

        correct                      -> 1
        pre-fix (sums, never clears) -> 2
        clears on contradiction too  -> 0

    REBASED ONTO RSS CARRIAGE ON ROUND 8. The clearing evidence used to be "still
    listing-only, detail succeeded", which round 8 showed is not evidence of anything
    -- see test_a_later_rss_observation_clears_the_candidate. The asymmetry being
    tested is unchanged; only what counts as clearing evidence is.
    """
    _cycle(db, listing_complete=1, at="2026-08-01T00:00:00+00:00",
           details={"detail_failed": [ONLY, OTHER],
                    "listing_only": [ONLY, OTHER], "feed_only": []})
    # Trusted cycle: RSS carried ONLY -> genuinely resolved.
    _cycle(db, listing_complete=1, at="2026-08-02T00:00:00+00:00",
           details={"detail_failed": [], "listing_only": [],
                    "feed_only": [ONLY]})
    # Contradicted cycle: RSS appears to carry OTHER -> must NOT resolve it, because
    # a cycle whose membership contradicts itself cannot certify its own RSS set.
    _cycle(db, listing_complete=0, at="2026-08-03T00:00:00+00:00",
           details={"detail_failed": [], "listing_only": [],
                    "feed_only": [OTHER]})

    res = db.get_hdencode_miss_resolution()
    assert res["unattributed_candidate_urls"] == [OTHER], (
        "a membership-contradicted cycle must not clear a candidate, and a "
        "trusted one must")
    assert res["unattributed_candidates"] == 1


def test_the_same_url_failing_repeatedly_is_one_candidate(db):
    """THE WRONG ANSWER: 3, because the old code summed occurrences."""
    for day in ("01", "02", "03"):
        _cycle(db, listing_complete=1, at=f"2026-08-{day}T00:00:00+00:00",
               details={"detail_failed": [ONLY], "listing_only": [ONLY],
                        "feed_only": []})
    assert db.get_hdencode_miss_resolution()["unattributed_candidates"] == 1


def test_candidate_blocking_still_reaches_readiness(db):
    """Verify the CONSUMER, not the component.

    The count is only worth anything if the readiness gate still reads it -- and
    "the field changed name/shape and nothing consumed it" is the single most
    repeated defect of this whole effort.
    """
    _cycle(db, listing_complete=1, at="2026-08-01T00:00:00+00:00",
           details={"detail_failed": [ONLY], "listing_only": [ONLY],
                    "feed_only": []})
    readiness = db.get_hdencode_rss_readiness()
    assert "unattributed_listing_candidates" in readiness["reasons"]
    assert readiness["ready"] is False
