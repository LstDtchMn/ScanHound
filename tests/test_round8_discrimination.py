"""Discrimination tests for the four round-8 findings.

Each must fail against `10201d7` (the round-8 head). Two of these findings exist
because I validated a change against ONE consumer and then claimed it held for all of
them, so these tests deliberately drive the OTHER consumers.

  1. HIGH  — candidate state disappeared on a later detail success even when RSS had
             still not carried the URL and no graded miss row took ownership. The
             clear rule keyed on `listing_only`, which IS the miss-candidate set: I
             was clearing an RSS-coverage blocker using evidence of a coverage gap.
  2. MED   — `duplicate` was counted, never persisted by URL, so the ordinary success
             case (RSS catches up while the release is still listed) was invisible and
             a candidate blocked forever.
  3. MED   — `direct_file` returned empty + a diagnostic, which only works for
             `download_item()`'s fallback. Four other production callers treat empty
             as failure.
  4. MED   — two API routes called the config-blind module `_source_page_kind(url)`,
             so a configured mirror's scrape health was not attributed to HDEncode.
"""
from __future__ import annotations

import os
import threading

import pytest

from backend.hdencode_shadow import canonical_url, compare_shadow
from backend.scrape_outcome import ScrapeCode

OK_FEEDS = {"movies_all": "changed", "tv_all": "changed"}
DEAD_FEEDS = {"movies_all": "failed", "tv_all": "failed"}
U = canonical_url("https://hdencode.org/listing-only-2160p/")
V = canonical_url("https://hdencode.org/second-2160p/")


@pytest.fixture()
def db(tmp_path):
    from backend.database import DatabaseManager
    mgr = DatabaseManager(str(tmp_path / "t.db"))
    yield mgr
    mgr.close()


def _detail_row(url):
    """A detail row that QUALIFIES as a relevant miss.

    Needs a status in {missing, missing_season, upgrade, dv_upgrade} AND attributable
    media type. My first fixture used a bare {"url": ...} and produced no miss row at
    all, so the ownership assertion failed against correct code.
    """
    return {"url": url, "title": "A Movie 2026 2160p", "status": "missing",
            "category": "movie", "is_tv": False}


def _cycle(db, uuid, at, *, rss, raw, detail, feeds=OK_FEEDS, failed=(),
           listing_ok=True):
    """Record one shadow cycle through the REAL producer and the REAL writer.

    `detail` is the set with detail rows; `raw` is the raw crawl set. A URL in
    `failed` must NOT be in `detail` -- a failed detail scrape produces no row, and my
    first fixture supplied both for the same URL, which cannot happen in production.
    """
    comparison = compare_shadow(
        rss_urls=list(rss),
        listing_items=[_detail_row(u) for u in detail],
        rss_requests=2, listing_requests=1,
        normal_feeds_complete=all(
            v in ("changed", "not_modified") for v in feeds.values()),
        normal_feed_outcomes=feeds, listing_complete=listing_ok,
        raw_listing_urls=list(raw), detail_failed_urls=list(failed))
    db.record_hdencode_shadow_comparison(
        cycle_uuid=uuid, started_at=at, completed_at=at,
        metrics=comparison.as_dict())
    return comparison


# ─────────────────────────────────────────────────────────────────────────────
# 1. HIGH — candidate state exits only on RSS carriage or miss-row ownership
# ─────────────────────────────────────────────────────────────────────────────

def test_detail_success_without_rss_or_miss_row_still_blocks(db):
    """THE WRONG ANSWER: the only readiness blocker is deleted and nothing owns it.

    Reproduces on 10201d7. The later cycle has a working detail scrape, so the old
    rule cleared -- but RSS still had not carried U, and the relevant feed was not
    observed, so `compare_shadow` could not admit a graded miss to take over. The
    qualification claim could then go clean on no evidence at all.
    """
    _cycle(db, "c1", "2026-08-01T00:00:00+00:00", rss=[], raw=[U], detail=[],
           failed=[U])
    assert db.get_hdencode_miss_resolution()["unattributed_candidates"] == 1, (
        "positive control: it must block first, or the rest proves nothing")

    _cycle(db, "c2", "2026-08-02T00:00:00+00:00", rss=[], raw=[U], detail=[U],
           feeds=DEAD_FEEDS)
    res = db.get_hdencode_miss_resolution()
    assert res["unattributed_candidates"] == 1, (
        "a detail success is not resolution: RSS still has not carried U and no "
        "graded miss row exists to take ownership")
    assert res["unattributed_candidate_urls"] == [U]


def test_an_admitted_miss_row_transfers_ownership(db):
    """The other legitimate exit — and it must be TRANSFER, not erasure."""
    _cycle(db, "c1", "2026-08-01T00:00:00+00:00", rss=[], raw=[U], detail=[],
           failed=[U])
    _cycle(db, "c2", "2026-08-02T00:00:00+00:00", rss=[], raw=[U], detail=[U])

    res = db.get_hdencode_miss_resolution()
    assert res["unattributed_candidates"] == 0, "the miss row now owns U"
    held = (int(res.get("never_acquired") or 0) + int(res.get("undetermined") or 0)
            + int(res.get("not_yet_assessable") or 0))
    assert held > 0, (
        "ownership transferred, so the miss machinery must still be holding U -- if "
        "it is not, the blocker was erased rather than handed over")


def test_listing_only_alone_never_clears(db):
    """The precise defect: `listing_only` MEANS RSS did not carry it.

    Discriminating by construction — the later cycle keeps U listing-only with a
    working detail scrape and a feed too degraded to admit a miss, so the ONLY thing
    the old rule could have keyed on is the coverage gap itself.
    """
    _cycle(db, "c1", "2026-08-01T00:00:00+00:00", rss=[], raw=[U, V], detail=[V],
           failed=[U])
    _cycle(db, "c2", "2026-08-02T00:00:00+00:00", rss=[], raw=[U, V], detail=[U, V],
           feeds=DEAD_FEEDS)
    assert db.get_hdencode_miss_resolution()["unattributed_candidates"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 2. MEDIUM — RSS catch-up (a duplicate) must clear, and must be persisted
# ─────────────────────────────────────────────────────────────────────────────

def test_rss_catch_up_clears_the_candidate(db):
    """THE WRONG ANSWER: the ordinary success case blocks forever.

    RSS catching up while the release is still listed makes U a DUPLICATE — in
    neither `listing_only` nor `feed_only`. Only the count was persisted, so the
    loader could never observe the one thing that resolves the candidate.
    """
    _cycle(db, "c1", "2026-08-01T00:00:00+00:00", rss=[], raw=[U], detail=[],
           failed=[U])
    assert db.get_hdencode_miss_resolution()["unattributed_candidates"] == 1

    _cycle(db, "c2", "2026-08-02T00:00:00+00:00", rss=[U], raw=[U], detail=[U])
    res = db.get_hdencode_miss_resolution()
    assert res["unattributed_candidates"] == 0, (
        f"RSS carried U, so it is resolved: {res.get('unattributed_candidate_urls')}")


def test_duplicate_urls_survive_the_persistence_round_trip(db):
    """Verify the CONSUMER, not the component.

    A new dataclass field that never reaches `details_json` is the exact "signal
    nothing consumes" failure this review has found five times, so this asserts the
    stored JSON rather than the object.
    """
    import json
    comparison = _cycle(db, "c1", "2026-08-01T00:00:00+00:00",
                        rss=[U], raw=[U, V], detail=[U, V])
    assert comparison.duplicate_urls == (U,), comparison.duplicate_urls

    row = db._query_dicts(
        "SELECT details_json FROM hdencode_shadow_cycles WHERE cycle_uuid='c1'",
        default=[])[0]
    stored = json.loads(row["details_json"])
    assert stored.get("duplicate_urls") == [U], stored.get("duplicate_urls")


def test_a_contradicted_cycle_cannot_clear_via_rss_carriage(db):
    """The create/clear asymmetry still holds on the new evidence path."""
    _cycle(db, "c1", "2026-08-01T00:00:00+00:00", rss=[], raw=[U], detail=[],
           failed=[U])
    _cycle(db, "c2", "2026-08-02T00:00:00+00:00", rss=[U], raw=[U], detail=[U],
           listing_ok=False)
    assert db.get_hdencode_miss_resolution()["unattributed_candidates"] == 1, (
        "a membership-contradicted cycle must not clear, even with RSS carriage")


def test_legacy_cycles_without_duplicate_urls_clear_nothing(db):
    """Absent evidence must clear nothing — the conservative reading."""
    _cycle(db, "c1", "2026-08-01T00:00:00+00:00", rss=[], raw=[U], detail=[],
           failed=[U])
    with db.transaction() as conn:
        conn.execute(
            "UPDATE hdencode_shadow_cycles SET details_json = "
            "json_remove(details_json, '$.duplicate_urls')")
    assert db.get_hdencode_miss_resolution()["unattributed_candidates"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. MEDIUM — the direct_file contract must hold for every scrape_links consumer
# ─────────────────────────────────────────────────────────────────────────────

RAPIDGATOR = "https://rapidgator.net/file/abc/x.rar"
MEGA = "https://mega.nz/file/abc"


def _scrape_service(config=None):
    from backend.download_service import DownloadService
    svc = DownloadService.__new__(DownloadService)
    svc.config = config or {"base_url": "https://hdencode.org",
                            "hdencode_enabled": True}
    svc._log = lambda *a, **k: None
    svc._driver_lock = threading.RLock()
    svc._scrape_count_lock = threading.Lock()
    svc._scrapes_done = threading.Condition(svc._scrape_count_lock)
    svc._active_scrapes = 0
    return svc


def test_a_supported_direct_link_is_returned_as_a_link():
    """THE WRONG ANSWER: zero links, so four of five callers report a failure.

    Asserted on the RETURN VALUE rather than on download_item's behaviour, because
    the defect was that only download_item recovered.
    """
    svc = _scrape_service()

    def _tripwire(*a, **k):
        raise AssertionError("no browser may start for a direct link")
    svc._navigate_with_diagnostic = _tripwire

    result = svc.scrape_links(RAPIDGATOR, "Rapidgator")
    assert list(result) == [RAPIDGATOR], (
        "'give me downloadable links' must return the downloadable link to EVERY "
        "caller, not rely on one caller's fallback")
    assert getattr(result, "diagnostic", None) is None, (
        "a successful passthrough must not carry a failure diagnostic")


def test_an_identified_but_unsupported_direct_host_still_refuses():
    """The gate that keeps this from being a silent behaviour change.

    Identity knows 13 direct hosts; the downloader can hand off 4. Returning the URL
    for the other 9 would hand download_item a host it currently refuses.
    """
    svc = _scrape_service()
    svc._navigate_with_diagnostic = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no browser for a direct link"))

    result = svc.scrape_links(MEGA, "Rapidgator")
    assert list(result) == []
    assert result.diagnostic.code is ScrapeCode.DIRECT_LINK_NO_SOURCE_PAGE
    assert result.diagnostic.cause_code == "direct_link_unsupported_host"
    assert result.diagnostic.affects_source_health is False


def test_download_item_still_accepts_the_passthrough_link():
    """The caller whose behaviour must NOT change.

    Its `if not links:` fallback no longer fires, because links are now non-empty.
    That is the point — but the outcome for the user must be identical.
    """
    svc = _scrape_service()
    svc._navigate_with_diagnostic = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no browser for a direct link"))
    result = svc.scrape_links(RAPIDGATOR, "Rapidgator")
    # Exactly what download_item's fallback used to synthesise.
    assert list(result) == [RAPIDGATOR]
    assert svc._is_supported_download_link(RAPIDGATOR) is True


# ─────────────────────────────────────────────────────────────────────────────
# 4. MEDIUM — no production caller may classify with the default host
# ─────────────────────────────────────────────────────────────────────────────

MIRROR = "https://hdencode.example.net"


def test_no_production_module_calls_the_config_blind_classifier():
    """THE CLAIM I GOT WRONG, now asserted instead of asserted-about.

    I wrote "no production call site passes a URL alone any more" after grepping ONE
    file. This walks every production .py file, which is the search that would have
    caught it. Structural because the defect was a call site, not a behaviour.
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    pattern = re.compile(r"_source_page_kind\(\s*[^,)]+\s*\)")
    for path in list(root.glob("backend/**/*.py")) + list(root.glob("ui/**/*.py")):
        for num, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue          # prose about the helper, not a call
            if "def _source_page_kind" in line:
                continue          # the definition itself
            if pattern.search(line) and "hdencode_host" not in line:
                offenders.append(f"{path.relative_to(root)}:{num}: {stripped}")
    assert not offenders, (
        "these production call sites classify with the DEFAULT host, so a "
        "configured mirror is misattributed:\n  " + "\n  ".join(offenders))


def test_owns_source_health_follows_the_configured_mirror():
    svc = _scrape_service({"base_url": MIRROR})
    assert svc.owns_source_health("https://hdencode.example.net/a-movie-2160p/") is True
    assert svc.owns_source_health("https://hdencode.org/a-movie-2160p/") is False


def test_owns_source_health_agrees_with_the_queue():
    """Asserted as AGREEMENT between the two production classifiers.

    Pinning each side to its own literal is the shape that let them drift.
    """
    from backend.download_queue import _source
    svc = _scrape_service({"base_url": MIRROR})
    url = "https://hdencode.example.net/a-movie-2160p/"
    assert svc.owns_source_health(url) == (_source(url, hdencode_host=MIRROR)
                                          == "hdencode")


def test_the_route_module_no_longer_owns_a_classifier():
    """Structural guard only, and labelled as such.

    My first version of this was titled "drives the ROUTE, not the service" and did
    nothing of the kind -- it read the module source with inspect.getsource() and
    never called a route. A false claim in a docstring, inside a file written to
    catch false claims. The tests below actually execute the routes; this one keeps
    the narrow property that a future edit cannot reintroduce a route-local
    classifier without tripping something.
    """
    import inspect
    from backend.api.routes import downloads as mod
    src = inspect.getsource(mod)
    assert "_source_page_kind(" not in src.replace(
        "`_source_page_kind(url)`", ""), (
        "the route still classifies independently of the service")


# ─────────────────────────────────────────────────────────────────────────────
# The route-level tests the review actually asked for
# ─────────────────────────────────────────────────────────────────────────────

def _registry(config):
    """A ServiceRegistry stand-in whose `download` is a REAL DownloadService.

    A bare MagicMock service is what made `test_health_routing_uses_parsed_hostname`
    misbehave: `owns_source_health()` returned a truthy Mock, so the route recorded
    health for every URL. Only `scrape_links` is stubbed here, so identity and
    ownership are production code.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from backend.download_service import DownloadService

    svc = _scrape_service(config)
    db = MagicMock()
    return SimpleNamespace(download=svc, db=db), svc, db


def test_scrape_route_returns_a_direct_link_to_its_caller(monkeypatch):
    """REQUIRED BY THE REVIEW: /download/scrape with a supported direct URL.

    Executes the route. Pre-fix it returned zero links for a Rapidgator URL because
    the route has no equivalent of download_item()'s passthrough fallback.
    """
    from backend.api.routes import downloads as routes
    reg, svc, _db = _registry({"base_url": "https://hdencode.org",
                               "hdencode_enabled": True})

    def _tripwire(*a, **k):
        raise AssertionError("no browser may start for a direct link")
    svc._navigate_with_diagnostic = _tripwire
    monkeypatch.setattr(routes, "record_scrape_outcome",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                            "a direct file host is not HDEncode source health")))

    result = routes.scrape_links(routes.ScrapeRequest(url=RAPIDGATOR), reg)
    links = result.get("links") if isinstance(result, dict) else list(result)
    assert list(links) == [RAPIDGATOR], (
        f"the route must hand its caller the direct link; got {links!r}")


def test_copy_links_route_includes_a_direct_link_and_does_not_fail_it(monkeypatch):
    """REQUIRED BY THE REVIEW: /download/copy-links must not file it as a failure.

    The route's own logic is `if not links and diagnostic is not None: failures.append`,
    so the old empty+diagnostic contract put a perfectly good direct link into the
    failure list and contributed nothing to the clipboard.
    """
    from backend.api.routes import downloads as routes
    reg, svc, _db = _registry({"base_url": "https://hdencode.org",
                               "hdencode_enabled": True})

    def _tripwire(*a, **k):
        raise AssertionError("no browser may start for a direct link")
    svc._navigate_with_diagnostic = _tripwire

    copied = {}
    svc.copy_to_clipboard = lambda links: copied.setdefault("links", list(links)) or True
    monkeypatch.setattr(routes, "record_scrape_outcome", lambda *a, **k: None)
    monkeypatch.setattr(routes.ws_manager, "broadcast_sync", lambda *a, **k: None)

    class _Bg:
        def add_task(self, fn, *a, **k):
            fn(*a, **k)          # run the background work inline

    item = routes.ScrapeBatchRequest.model_validate(
        {"items": [{"url": RAPIDGATOR, "service_type": "Rapidgator"}]})
    routes.copy_links_batch(item, _Bg(), reg)

    assert copied.get("links") == [RAPIDGATOR], (
        f"the direct link must reach the clipboard payload; got {copied!r}")


def test_scrape_route_attributes_a_configured_mirror_to_hdencode_health(monkeypatch):
    """REQUIRED BY THE REVIEW: mirror health ownership, through the route.

    Pre-fix the route classified with the default host, so a mirror's scrape outcome
    was never persisted as HDEncode health.
    """
    from backend.api.routes import downloads as routes
    from backend.scrape_outcome import ScrapedLinks

    reg, svc, db = _registry({"base_url": MIRROR, "hdencode_enabled": True})
    recorded = []
    monkeypatch.setattr(routes, "record_scrape_outcome",
                        lambda _db, source, links: recorded.append(source))
    svc.scrape_links = lambda url, service_type, **k: ScrapedLinks(
        ["https://rapidgator.net/file/x/y.rar"])

    routes.scrape_links(
        routes.ScrapeRequest(url="https://hdencode.example.net/a-movie-2160p/"), reg)
    assert recorded == ["hdencode"], (
        f"a configured mirror's health must be attributed to HDEncode; got {recorded!r}")

    recorded.clear()
    routes.scrape_links(
        routes.ScrapeRequest(url="https://hdencode.org/a-movie-2160p/"), reg)
    assert recorded == [], (
        "once a mirror is configured, the old default host is NOT HDEncode")
