"""R-5 minimum closure, part 2 (round-12 Q2): FINAL consumers, not helpers.

Five sub-items the verdict named: cross-path media-type/provisional
equivalence through the REAL listing resolver composition; the real
/results/cached route (category filter + bookmark annotation); real
queue_action persistence; the cached-results route across the stale
lifecycle; exact autonomous-denial codes.
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager
from tests.test_r5_boundary_suite import MATRIX, _rss_row


# ── 1. cross-path media type + provisionality via the REAL compositions ──────

class TestCrossPathMediaType:
    """RSS verdict (the real parse_feed) vs THE listing composition —
    resolve_listing_media_type itself, the exact function _process_posts'
    worker and the rescan route call. Round-13: the previous version of this
    class restated the evidence list inline, so a drift inside
    scanner_service would have stayed green; now the production function is
    executed directly and any drift fails here."""

    ROUTE = {"TV": "tv", "Movies": "movie", "Weird-Category": None}

    @pytest.mark.parametrize("name", ["tokenless_tv", "ordinary_episode",
                                      "ambiguous_season", "ordinary_movie",
                                      "route_title_conflict", "unresolved_type"])
    def test_media_type_and_provisionality_agree(self, name):
        from backend.scanner_service import resolve_listing_media_type
        rss = _rss_row(name)
        title, category = MATRIX[name]
        verdict = resolve_listing_media_type(
            # ingest-time comparison: no detail page has been scraped yet, and
            # is_tv=False contributes NO evidence by the function's contract
            {"type": self.ROUTE[category], "title": title},
            {"is_tv": False},
        )
        assert verdict.media_type.value == rss["media_type"], name
        assert verdict.provisional == bool(rss["media_type_provisional"]), name

    def test_detail_filename_overrides_the_weaker_route_and_title(self):
        """The DETAIL evidence slot, executed on BOTH real paths: a
        movie-routed post whose detail page proves a season token resolves TV
        via detail-filename on the listing path, and the RSS path's real
        hydration composition (_candidate_updates) reaches the same
        non-provisional verdict from the same parsed details."""
        from unittest.mock import MagicMock

        from backend.hdencode_candidate_service import _candidate_updates
        from backend.scanner_service import resolve_listing_media_type
        from backend.scrapers import WebScrapers
        from tests.test_scrapers_extended import (
            MockApp, _FakeResponse, _build_detail_html)

        ws = WebScrapers(MockApp())
        fake = MagicMock()
        fake.get.return_value = _FakeResponse(
            _build_detail_html("Show.Name.S01E02.1080p.WEB.mkv"))
        details = ws.scrape_details(
            "https://example.com/d", headers={}, scraper=fake)
        assert details["is_tv"] is True  # the REAL parse produced the signal

        verdict = resolve_listing_media_type(
            {"type": "movie", "title": "Show Name 1080p WEB"}, details)
        assert verdict.media_type.value == "tv"
        assert verdict.provisional is False  # decided by DETAIL, not ROUTE
        assert any("detail-filename" in b for b in verdict.because)
        assert any("overruled" in b for b in verdict.because)  # route lost

        updates = _candidate_updates(details)
        assert updates["media_type"] == "tv"
        assert bool(updates["media_type_provisional"]) is verdict.provisional


# ── 2 + 4. the real /results/cached route ────────────────────────────────────

@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _cache_row(url, title, status="missing", media_type=None, season=None):
    data = {"url": url, "title": title, "status": status}
    if media_type is not None:
        data["media_type"] = media_type
    if season is not None:
        data["season"] = season
    return {"url": url, "title": title, "year": 2026, "status": status,
            "source_category": "HDEncode", "data": json.dumps(data)}


class TestResultsRouteBoundary:
    def test_category_facets_and_bookmark_annotation_at_the_route(self, client):
        dm = DatabaseManager()
        dm.upsert_background_cache([
            _cache_row("https://hdencode.org/tl-tv/", "Tokenless Show",
                       media_type="tv"),                      # no season!
            _cache_row("https://hdencode.org/mov/", "Some Movie",
                       media_type="movie", season=3),         # stray season
        ])
        tv = client.get("/results/cached", params={"category": "tv"}).json()
        assert {i["title"] for i in tv["items"]} == {"Tokenless Show"}
        movies = client.get("/results/cached", params={"category": "4k"}).json()
        assert {i["title"] for i in movies["items"]} == {"Some Movie"}
        # bookmark annotation executed at the route for every item
        allr = client.get("/results/cached").json()
        assert all("bookmarked" in i for i in allr["items"])

    def test_cached_route_across_the_stale_lifecycle(self, client):
        from backend.release_grammar import GRAMMAR_VERSION
        dm = DatabaseManager()
        row = _cache_row("https://hdencode.org/life/", "Lifecycle")
        dm.upsert_background_cache([row])
        # BEFORE: visible, in the skip set
        assert client.get("/results/cached").json()["total"] == 1
        assert "https://hdencode.org/life/" in dm.get_background_cache_urls()
        # WHILE STALE: still visible to the operator, OUT of the skip set
        conn = sqlite3.connect(dm.db_path)
        conn.execute("UPDATE background_scan_cache SET parse_version='old-v0'")
        conn.commit(); conn.close()
        dm.reconcile_derived_versions()
        assert client.get("/results/cached").json()["total"] == 1
        assert "https://hdencode.org/life/" not in dm.get_background_cache_urls()
        # AFTER HEAL: visible, current, back in the skip set, fresh blob served
        row["data"] = json.dumps({"url": row["url"], "title": "Lifecycle",
                                  "status": "missing", "healed": True})
        dm.upsert_background_cache([row])
        body = client.get("/results/cached").json()
        assert body["total"] == 1
        assert "https://hdencode.org/life/" in dm.get_background_cache_urls()
        conn = sqlite3.connect(dm.db_path)
        assert conn.execute("SELECT parse_version, derived_state FROM "
                            "background_scan_cache").fetchone() == (
                                GRAMMAR_VERSION, "current")
        conn.close()


# ── 3. real queue_action persistence ─────────────────────────────────────────

class TestQueueActionBoundary:
    def test_explicit_action_persists_the_package_key(self, tmp_path):
        from backend.hdencode_action_service import HDEncodeActionService
        db = DatabaseManager(str(tmp_path / "qa.db"))
        rss = _rss_row("ordinary_episode")
        db.ingest_hdencode_feed(
            feed_key="movies_all", feed_url="https://hdencode.org/feed/",
            last_modified=None, http_status=200, body_sha256="qa1",
            channel_last_build_date=None, entries=[rss],
            started_at="2026-08-01T12:00:00+00:00",
            completed_at="2026-08-01T12:00:05+00:00")
        svc = object.__new__(HDEncodeActionService)
        svc.config = {"hdencode_enabled": True}
        svc.db = db
        svc.download = object()          # present; not exercised at queue time
        out = svc.queue_action(rss["canonical_url"], action_kind="grab",
                               requested_by="explicit")
        conn = sqlite3.connect(db.db_path)
        row = conn.execute("SELECT package_name, state FROM "
                           "hdencode_actions").fetchone()
        conn.close()
        assert row is not None
        assert "s01" in row[0].lower()   # the episode shape reached the key
        assert out["package_name"] == row[0]


# ── 5. exact autonomous denial codes ─────────────────────────────────────────

class TestExactDenialCodes:
    def _validate(self, candidate):
        from backend.hdencode_action_service import (
            HDEncodeActionError, HDEncodeActionService)
        svc = object.__new__(HDEncodeActionService)
        svc.config = {"hdencode_rss_auto_grab_enabled": True}
        with pytest.raises(HDEncodeActionError) as exc:
            svc._validate_auto_action(candidate, "grab")
        return exc.value.code

    def _base(self, name):
        return dict(_rss_row(name), derived_state="current",
                    relevance_state="relevant_missing", identity_state="exact",
                    hydration_state="completed", description_complete=1,
                    dv_evidence="absent", hdr_evidence="absent")

    def test_unresolved_type_denies_with_its_exact_code(self):
        assert self._validate(self._base("unresolved_type")) == \
            "auto_media_type_unresolved"

    def test_provisional_type_denies_with_its_exact_code(self):
        candidate = self._base("ordinary_movie")
        candidate["media_type_provisional"] = 1
        assert self._validate(candidate) == "auto_media_type_provisional"

    def test_stale_row_denies_with_its_exact_code(self):
        candidate = self._base("ordinary_movie")
        candidate["derived_state"] = "stale"
        assert self._validate(candidate) == "stale_derived"
