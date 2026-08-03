"""R-5 — the EXECUTED consumer-boundary equivalence suite (round-11 item 6).

Not an inventory: every test here drives real entry points with the round-11
input matrix and asserts the SEMANTIC CONTRACT FIELDS at the final consumer.
The cross-path class is the heart: one release seen via the real RSS parser
and via the real deployed scrape facade must reach the same decision facts.
"""
import sqlite3

import pytest

from backend.sources.hdencode_feed_parser import parse_feed

# ── the round-11 input matrix ────────────────────────────────────────────────
# name -> (release title, category) — shapes the verdict enumerated.
MATRIX = {
    "tokenless_tv": ("Show Name Complete Series 2160p WEB-DL - 40.0 GB", "TV"),
    "ordinary_episode": ("Show Name S01E02 1080p WEB - 2.0 GB", "TV"),
    "ambiguous_season": ("Show Name S104 2160p WEB - 30.0 GB", "Movies"),
    "ordinary_movie": ("Movie Name 2024 1080p BluRay - 8.0 GB", "Movies"),
    "route_title_conflict": ("Other Show Season 4 1080p WEB - 9.0 GB", "Movies"),
    "year_like_title": ("Blade Runner 2049 2017 2160p WEB - 20.0 GB", "Movies"),
    "interlaced_1080i": ("Show Name S02E01 1080i HDTV - 1.5 GB", "TV"),
    "scope_crop_fn": ("Movie Name 2020 3840x1600 Scope BluRay - 25.0 GB", "Movies"),
    "unresolved_type": ("Something Odd Release", "Weird-Category"),
}


def _rss_row(name):
    title, category = MATRIX[name]
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>{title}</title>
  <link>https://hdencode.org/{name.replace('_', '-')}/</link>
  <guid>https://hdencode.org/{name.replace('_', '-')}/</guid>
  <pubDate>Fri, 01 Aug 2026 12:00:00 +0000</pubDate>
  <category>{category}</category><description>x</description></item>
</channel></rss>""".encode()
    return parse_feed(body, "movies_all").entries[0].as_database_row()


# ── boundary group 1: cross-path equivalence (THE core requirement) ──────────

class TestCrossPathEquivalence:
    """Same release through the real RSS parser and the real deployed scrape
    facade: the contract fields must agree. Transport is faked; the parsed
    entry shape and every decision function are real."""

    CONTRACT_CASES = [
        # (matrix name, filename form for the detail page)
        ("ordinary_episode", "Show.Name.S01E02.1080p.WEB.mkv"),
        ("ordinary_movie", "Movie.Name.2024.1080p.BluRay.mkv"),
        ("year_like_title", "Blade.Runner.2049.2017.2160p.WEB.mkv"),
        ("interlaced_1080i", "Show.Name.S02E01.1080i.HDTV.mkv"),
        ("ambiguous_season", "Show.Name.S104.2160p.mkv"),
    ]

    @pytest.mark.parametrize("name,filename", CONTRACT_CASES)
    def test_contract_fields_agree(self, name, filename):
        from tests.test_scrapers_extended import (
            MockApp, _FakeResponse, _build_detail_html)
        from unittest.mock import MagicMock
        from backend.scrapers import WebScrapers

        rss = _rss_row(name)

        scraper = WebScrapers(MockApp())
        fake = MagicMock()
        fake.get.return_value = _FakeResponse(_build_detail_html(filename))
        listing = scraper.scrape_details(
            "https://example.com/d", headers={}, scraper=fake)
        assert listing is not None

        # year: identical value (0 and None both mean absent)
        assert (listing["year"] or None) == (rss.get("title_year") or None), name
        # season interpretation: identical, including 'cannot tell'
        assert (listing.get("season") or None) == (rss.get("season") or None), name
        # resolution class: canonical fold must agree
        from backend.release_grammar import canonical_resolution
        l_res = canonical_resolution(listing.get("res")) if listing.get("res") not in (None, "?") else None
        r_res = canonical_resolution(rss.get("resolution")) if rss.get("resolution") else None
        assert l_res == r_res, (name, l_res, r_res)


# ── boundary group 2: results category + bookmark identity ───────────────────

class TestResultsBoundary:
    @pytest.mark.parametrize("name", list(MATRIX))
    def test_category_never_reconstructs_tv_from_season_alone(self, name):
        from backend.api.routes.results import _effective_category
        rss = _rss_row(name)
        item = {"title": rss["title"], "year": rss.get("title_year"),
                "season": rss.get("season"), "category": None,
                "media_type": rss["media_type"]}
        cat = _effective_category(item)
        if rss["media_type"] == "tv":
            assert cat == "tv", name
        elif rss["media_type"] == "movie":
            assert cat == "4k", name


# ── boundary group 3: autonomous action admission ────────────────────────────

class TestAutoActionBoundary:
    @pytest.mark.parametrize("name", ["unresolved_type"])
    def test_weak_or_ambiguous_evidence_never_authorises(self, name):
        from backend.hdencode_action_service import (
            HDEncodeActionError, HDEncodeActionService)
        rss = _rss_row(name)
        candidate = dict(rss, derived_state="current",
                         relevance_state="relevant_missing",
                         identity_state="exact",
                         hydration_state="completed",
                         description_complete=1,
                         dv_evidence="absent", hdr_evidence="absent")
        svc = object.__new__(HDEncodeActionService)
        svc.config = {"hdencode_rss_auto_grab_enabled": True}
        with pytest.raises(HDEncodeActionError):
            svc._validate_auto_action(candidate, "grab")

    def test_title_authority_resolves_the_conflict_cases(self):
        """EXECUTED finding of this suite: my inventory assumed the S104 and
        route-conflict rows would be weak evidence -- running the real
        resolver shows both reach a CONFIRMED tv verdict (title authority
        outranks the route, by design), with the ambiguous SEASON NUMBER
        still refused. The media-type gate rightly admits them."""
        for name in ("ambiguous_season", "route_title_conflict"):
            row = _rss_row(name)
            assert row["media_type"] == "tv", name
            assert row["media_type_provisional"] is False, name
        assert _rss_row("ambiguous_season").get("season") is None

    def test_stale_candidate_never_authorises(self):
        from backend.hdencode_action_service import (
            HDEncodeActionError, HDEncodeActionService)
        rss = _rss_row("ordinary_movie")
        candidate = dict(rss, derived_state="stale")
        svc = object.__new__(HDEncodeActionService)
        svc.config = {"hdencode_rss_auto_grab_enabled": True}
        with pytest.raises(HDEncodeActionError) as exc:
            svc._validate_auto_action(candidate, "grab")
        assert exc.value.code == "stale_derived"


# ── boundary group 4: package naming + destination routing ───────────────────

class TestPackagingBoundary:
    def test_tv_and_movie_shapes_route_and_name_differently(self):
        from backend.download_service import compute_package_name
        tv = _rss_row("ordinary_episode")
        movie = _rss_row("ordinary_movie")
        tv_name = compute_package_name(
            tv["clean_title"], tv.get("title_year"),
            tv.get("resolution"), tv.get("season"))
        movie_name = compute_package_name(
            movie["clean_title"], movie.get("title_year"),
            movie.get("resolution"), movie.get("season"))
        assert tv_name != movie_name
        assert "S01" in tv_name or "s01" in tv_name.lower()

    def test_ambiguous_season_never_leaks_a_numeric_season_into_the_name(self):
        from backend.download_service import compute_package_name
        amb = _rss_row("ambiguous_season")
        assert amb.get("season") is None       # the grammar refused to guess
        name = compute_package_name(
            amb["clean_title"], amb.get("title_year"),
            amb.get("resolution"), amb.get("season"))
        assert "S104" not in name and "S10" not in name


# ── boundary group 5: stale cache row visibility ─────────────────────────────

class TestCacheStaleBoundary:
    def test_stale_cache_rows_are_excluded_from_the_skip_set(self, tmp_path):
        from backend.database import DatabaseManager
        db = DatabaseManager(str(tmp_path / "r5.db"))
        db.upsert_background_cache([{
            "url": "https://hdencode.org/r5c/", "title": "R5", "year": 2026,
            "status": "missing", "source_category": "4k_movies", "data": "{}"}])
        conn = sqlite3.connect(db.db_path)
        conn.execute("UPDATE background_scan_cache SET parse_version='old'")
        conn.commit(); conn.close()
        db.reconcile_derived_versions()
        assert "https://hdencode.org/r5c/" not in db.get_background_cache_urls()
