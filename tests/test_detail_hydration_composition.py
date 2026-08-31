"""Round-13 Q2: the PRODUCTION hydration composition, end to end.

The 17-field survival suite proved the DB sink can PERSIST detail-authority
facts; round 13 showed that is not the same claim as the production pipeline
being able to PRODUCE them (episode_end and hevc_evidence were sink-only).
Every test here drives the real chain:

    real feed ingest -> real hydration claim -> the REAL WebScrapers /
    DetailScraper parse (HTTP transport faked at the exact injection point
    hydrate_pending's hardcoded ``scraper=None`` reserves for production
    sessions) -> _candidate_updates -> complete_hdencode_hydration -> the
    actual sqlite row.

Nothing asserts against handcrafted candidate_updates dicts.
"""
import sqlite3
from unittest.mock import MagicMock

import pytest

from backend.database import DatabaseManager
from backend.detail_scraper import DETAIL_PARSE_VERSION
from backend.hdencode_candidate_service import HDEncodeCandidateService
from backend.release_grammar import GRAMMAR_VERSION
from backend.scrapers import WebScrapers
from backend.sources.hdencode_feed_parser import parse_feed
from tests.test_scrapers_extended import MockApp, _FakeResponse, _build_detail_html


def _entry(title, category, slug):
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>{title}</title>
  <link>https://hdencode.org/{slug}/</link>
  <guid>https://hdencode.org/{slug}/</guid>
  <pubDate>Fri, 01 Aug 2026 12:00:00 +0000</pubDate>
  <category>{category}</category><description>x</description></item>
</channel></rss>""".encode()
    return parse_feed(body, "movies_all").entries[0].as_database_row()


def _ingest(db, entry):
    db.ingest_hdencode_feed(
        feed_key="movies_all", feed_url="https://hdencode.org/feed/",
        last_modified=None, http_status=200, body_sha256="c1",
        channel_last_build_date=None, entries=[entry],
        started_at="2026-08-01T12:00:00+00:00",
        completed_at="2026-08-01T12:00:05+00:00")
    return entry["canonical_url"]


class _RealDetailAdapter:
    """hydrate_pending calls ``scrape_details(url, headers={}, scraper=None)``
    — scraper=None is where production creates its own HTTP session. This
    adapter keeps the ENTIRE real parse composition and swaps only that
    transport, so the payload reaching _candidate_updates is a genuinely
    parsed detail result, not a fixture dict."""

    def __init__(self, html):
        self._fake = MagicMock()
        self._fake.get.return_value = _FakeResponse(html)
        self._scrapers = WebScrapers(MockApp())

    def scrape_details(self, url, headers, scraper=None, stop_requested=None):
        return self._scrapers.scrape_details(url, headers=headers, scraper=self._fake)


def _hydrate(db, url, html):
    db.requeue_hdencode_hydration(url, reason="test", priority=50)
    svc = HDEncodeCandidateService({"hdencode_rss_hydration_limit": 1}, db)
    result = svc.hydrate_pending(_RealDetailAdapter(html), limit=1)
    assert result["completed"] == 1, result
    return _row(db, url)


def _row(db, url):
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM hdencode_candidates WHERE canonical_url = ?", (url,)
    ).fetchone()
    conn.close()
    assert row is not None
    return row


@pytest.fixture
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "hydration.db"))


# ── the facade itself produces the new facts ─────────────────────────────────

class TestScrapePayloadProducesTheFacts:
    def test_glued_range_and_codec_reach_the_payload(self):
        ws = WebScrapers(MockApp())
        fake = MagicMock()
        fake.get.return_value = _FakeResponse(_build_detail_html(
            "Show.Name.S01E01E02E03.1080p.WEB-DL.x265-GRP.mkv"))
        payload = ws.scrape_details("https://example.com/d", headers={}, scraper=fake)
        assert payload["episode_number"] == 1
        assert payload["episode_end"] == 3
        assert payload["hevc"] is True
        assert payload["episodes"] == 3  # the range spans three episodes

    def test_codec_absence_is_none_not_false(self):
        ws = WebScrapers(MockApp())
        fake = MagicMock()
        fake.get.return_value = _FakeResponse(_build_detail_html(
            "Some.Movie.2026.2160p.WEB-DL-GRP.mkv"))
        payload = ws.scrape_details("https://example.com/d", headers={}, scraper=fake)
        assert payload["hevc"] is None  # absence never means H.264


# ── the intended detail semantics (round-13: define them first) ──────────────

class TestEpisodeRangeSemantics:
    def test_glued_multi_episode_file_carries_its_parsed_range(self, db):
        url = _ingest(db, _entry("Show Name S01E01 1080p WEB - 2.0 GB", "TV", "glued"))
        row = _hydrate(db, url, _build_detail_html(
            "Show.Name.S01E01E02E03.1080p.WEB-DL.x265-GRP.mkv"))
        assert row["season"] == 1
        assert row["episode"] == 1
        assert row["episode_end"] == 3

    def test_mirrored_copies_of_one_glued_file_are_not_a_pack(self, db):
        fn = "Show.Name.S01E01E02.1080p.WEB-DL.x265-GRP.mkv"
        url = _ingest(db, _entry("Show Name S01E01 1080p WEB - 2.0 GB", "TV", "mirror"))
        row = _hydrate(db, url, _build_detail_html(fn, extra_filenames=[fn]))
        assert row["episode"] == 1
        assert row["episode_end"] == 2

    def test_separate_episode_files_stay_a_pack_with_no_invented_range(self, db):
        url = _ingest(db, _entry("Show Name S01 1080p WEB - 9.0 GB", "TV", "pack"))
        row = _hydrate(db, url, _build_detail_html(
            "Show.Name.S01E01.1080p.WEB-DL.x264-GRP.mkv",
            extra_filenames=["Show.Name.S01E02.1080p.WEB-DL.x264-GRP.mkv"]))
        assert row["season"] == 1
        assert row["episode"] is None       # a pack is not episode 1
        assert row["episode_end"] is None   # and no contiguous range is invented


# ── hevc evidence: positive token only, feed authority preserved ─────────────

class TestHevcEvidence:
    def test_detail_codec_token_asserts_hevc(self, db):
        url = _ingest(db, _entry("Some Movie 2026 2160p WEB - 20.0 GB",
                                 "Movies", "hevc-yes"))
        assert _row(db, url)["hevc_evidence"] == "unknown"  # feed had no token
        row = _hydrate(db, url, _build_detail_html(
            "Some.Movie.2026.2160p.WEB-DL.HEVC-GRP.mkv"))
        assert row["hevc_evidence"] == "asserted"

    def test_detail_without_token_leaves_feed_unknown(self, db):
        url = _ingest(db, _entry("Plain Movie 2026 2160p WEB - 20.0 GB",
                                 "Movies", "hevc-no"))
        row = _hydrate(db, url, _build_detail_html(
            "Plain.Movie.2026.2160p.WEB-DL-GRP.mkv"))
        assert row["hevc_evidence"] == "unknown"  # absence is not negation

    def test_feed_asserted_hevc_survives_a_tokenless_detail_page(self, db):
        url = _ingest(db, _entry("X265 Movie 2026 2160p x265 WEB - 20.0 GB",
                                 "Movies", "hevc-feed"))
        assert _row(db, url)["hevc_evidence"] == "asserted"  # from the title
        row = _hydrate(db, url, _build_detail_html(
            "X265.Movie.2026.2160p.WEB-DL-GRP.mkv"))
        assert row["hevc_evidence"] == "asserted"  # COALESCE preserved it


# ── the detail parse version is its own authority ────────────────────────────

class TestDetailParseVersioning:
    def test_completion_stamps_the_detail_version_not_the_grammar(self, db):
        url = _ingest(db, _entry("Stamp Movie 2026 1080p WEB - 8.0 GB",
                                 "Movies", "stamp"))
        row = _hydrate(db, url, _build_detail_html(
            "Stamp.Movie.2026.1080p.WEB-DL.x265-GRP.mkv"))
        assert row["detail_parse_version"] == DETAIL_PARSE_VERSION
        # the decoupling IS the point -- a detail-only capability change must
        # not masquerade as a grammar change (or vice versa)
        assert DETAIL_PARSE_VERSION != GRAMMAR_VERSION

    def test_reconcile_refetches_rows_stamped_by_the_old_regime(self, db):
        url = _ingest(db, _entry("Old Stamp 2026 1080p WEB - 8.0 GB",
                                 "Movies", "old-stamp"))
        _hydrate(db, url, _build_detail_html(
            "Old.Stamp.2026.1080p.WEB-DL.x265-GRP.mkv"))
        # production rows hydrated before this change carry the grammar string
        conn = sqlite3.connect(db.db_path)
        conn.execute(
            "UPDATE hdencode_candidates SET detail_parse_version = ?"
            " WHERE canonical_url = ?", (GRAMMAR_VERSION, url))
        conn.commit(); conn.close()
        counts = db.reconcile_derived_versions()
        assert counts["candidates_refetch_required"] == 1
        row = _row(db, url)
        assert row["derived_state"] == "refetch_required"
        conn = sqlite3.connect(db.db_path)
        state = conn.execute(
            "SELECT state FROM hdencode_hydration_queue WHERE canonical_url = ?",
            (url,)).fetchone()
        conn.close()
        assert state is not None and state[0] == "queued"  # requeued for refetch

    def test_reconcile_leaves_current_stamps_alone(self, db):
        url = _ingest(db, _entry("New Stamp 2026 1080p WEB - 8.0 GB",
                                 "Movies", "new-stamp"))
        _hydrate(db, url, _build_detail_html(
            "New.Stamp.2026.1080p.WEB-DL.x265-GRP.mkv"))
        counts = db.reconcile_derived_versions()
        assert counts["candidates_refetch_required"] == 0  # negative control
        assert _row(db, url)["derived_state"] == "current"


# ── round-13 separation, contract 2: production EMISSION ─────────────────────

class TestProductionEmissionContract:
    """Every field claimed detail-authoritative is actually EMITTED by the
    production adapter and reaches the row. This is contract 2 of the
    round-13 separation; contract 1 (the sink PRESERVES the fields across a
    changed poll) lives in test_feed_upsert_authority.py. Feed values are
    chosen to DIFFER from the detail page's, so every assertion observes the
    detail emission itself, not a feed value the sink happened to keep.

    One deliberate retraction, pinned here: ``title_year`` is FEED authority.
    The detail page's year maps to ``description_year`` by design
    (_candidate_updates), so title_year's post-hydration CASE guard protects
    it from LATER FEED POLLS — it is not, and never was, a detail-emitted
    fact. The round-13 review was right that claiming otherwise was wrong.
    """

    def test_tv_side_fields_come_from_the_detail_parse(self, db):
        url = _ingest(db, _entry(
            "Placeholder Show S02 720p WEB - 1.0 GB", "TV", "emit-tv"))
        row = _hydrate(db, url, _build_detail_html(
            "Emit.Show.S01E01E02.1080p.WEB-DL.x265-GRP.mkv"))
        assert row["clean_title"] == "Emit Show"      # feed said Placeholder
        assert row["season"] == 1                     # feed said 2
        assert row["episode"] == 1                    # feed had none
        assert row["episode_end"] == 2                # feed had none
        assert row["resolution"] == "1080p"           # feed said 720p
        assert row["hevc_evidence"] == "asserted"     # feed had no token
        assert "detail-filename" in (row["media_type_because"] or "")
        assert row["description_complete"] == 1
        # A TV detail page yields NO year (the scraper's absent-year sentinel
        # is 0, not None) -- it must not overwrite the feed's parse with 0.
        # Audit finding: put()'s guard rejects None/"" but 0 passes it, and
        # the sink COALESCEs, so every tv hydration was zeroing this column
        # and the year-conflict gate then read 0 as "no year" rather than as
        # a conflict.
        assert row["description_year"] != 0

    def test_movie_side_fields_come_from_the_detail_parse(self, db):
        url = _ingest(db, _entry(
            "Placeholder Film 2020 720p WEB - 1.0 GB", "Movies", "emit-mv"))
        row = _hydrate(db, url, _build_detail_html(
            "Emit.Movie.2026.2160p.WEB-DL.DV.x265-GRP.mkv",
            size_label="FileSize: 20.5 GB",
            resolution="Resolution: 3840x2160",
            color_primaries="bt.2020"))
        assert row["description_year"] == 2026        # detail's year home
        assert row["resolution"] == "4K"              # feed said 720p
        assert row["size_text"] == "20.5 GB"          # feed said 1.0 GB
        assert row["size_gb"] == pytest.approx(20.5)
        assert row["dv_evidence"] == "asserted"       # feed had no DV token
        assert row["hdr_evidence"] == "asserted"      # bt.2020 primaries
        assert (row["hdr_formats"] or "") != ""       # formats list persisted
        # THE RETRACTION: title_year stays the FEED's parse, untouched
        assert row["title_year"] == 2020

    def test_the_adapter_never_emits_title_year(self, db):
        """Pinned at the adapter boundary too: a maximally rich REAL payload
        still contains no title_year key — so the sink's COALESCE always
        binds None there on the production path, by design."""
        from unittest.mock import MagicMock
        from backend.hdencode_candidate_service import _candidate_updates

        ws = WebScrapers(MockApp())
        fake = MagicMock()
        fake.get.return_value = _FakeResponse(_build_detail_html(
            "Rich.Movie.2026.2160p.WEB-DL.DV.x265-GRP.mkv",
            size_label="FileSize: 20.5 GB",
            resolution="Resolution: 3840x2160",
            color_primaries="bt.2020"))
        payload = ws.scrape_details("https://example.com/d", headers={}, scraper=fake)
        updates = _candidate_updates(payload)
        assert "title_year" not in updates


# ── round-14: the grammar/detail dependency must be MECHANICAL ───────────────

class TestGrammarChangesInvalidateCompletedDetailRows:
    """Round 13 split the detail and feed authorities; round 14 caught that
    the split was only remembered, not enforced. DetailScraper delegates year,
    season/episode/range, size, resolution/dimension and the HEVC vocabulary
    to release_grammar -- so a grammar change alters what detail extraction
    produces. With a FIXED detail stamp, every completed row still compared
    equal and was never refetched, and a code comment asserted the opposite.

    DETAIL_PARSE_VERSION is now composite, so either authority moving
    invalidates completed rows automatically.
    """

    def test_the_stamp_composes_both_authorities(self):
        from backend.detail_scraper import DETAIL_CAPABILITY_VERSION
        assert DETAIL_CAPABILITY_VERSION in DETAIL_PARSE_VERSION
        assert GRAMMAR_VERSION in DETAIL_PARSE_VERSION
        # and it is still not simply the grammar's own version
        assert DETAIL_PARSE_VERSION != GRAMMAR_VERSION

    def _hydrated(self, db):
        url = _ingest(db, _entry("Grammar Dep 2026 1080p WEB - 8.0 GB",
                                 "Movies", "grammar-dep"))
        _hydrate(db, url, _build_detail_html(
            "Grammar.Dep.2026.1080p.WEB-DL.x265-GRP.mkv"))
        return url

    def test_a_grammar_only_change_invalidates_and_requeues(self, db, monkeypatch):
        """The reviewer's required test: move ONLY the grammar version, leave
        the detail capability alone, and a completed row must still be
        invalidated and queued for refetch."""
        url = self._hydrated(db)
        assert _row(db, url)["derived_state"] == "current"

        # simulate a future grammar bump -- capability string untouched
        import backend.detail_scraper as ds
        monkeypatch.setattr(
            ds, "DETAIL_PARSE_VERSION",
            ds.DETAIL_CAPABILITY_VERSION + "+release-grammar-vNEXT")

        counts = db.reconcile_derived_versions()

        assert counts["candidates_refetch_required"] == 1
        assert _row(db, url)["derived_state"] == "refetch_required"
        conn = sqlite3.connect(db.db_path)
        state = conn.execute(
            "SELECT state FROM hdencode_hydration_queue WHERE canonical_url = ?",
            (url,)).fetchone()
        conn.close()
        assert state is not None and state[0] == "queued"

    def test_completed_rows_feed_facts_are_rederived_not_skipped(self, db):
        """Second half of the same finding: a completed row is excluded from
        the wholesale stale sweep (that pass would destroy its detail facts),
        which left the fields detail never supplies frozen at the OLD
        grammar's parse forever. They must be re-derived from retained feed
        inputs instead of skipped."""
        url = self._hydrated(db)
        conn = sqlite3.connect(db.db_path)
        conn.execute(
            "UPDATE hdencode_candidates SET feed_parse_version='old-v0',"
            " title_year=NULL WHERE canonical_url=?", (url,))
        conn.commit(); conn.close()

        counts = db.reconcile_derived_versions()

        assert counts["completed_feed_facts_reparsed"] == 1
        row = _row(db, url)
        assert row["feed_parse_version"] == GRAMMAR_VERSION
        assert row["title_year"] == 2026          # re-derived from the title

    def test_the_narrow_pass_never_touches_detail_authority_fields(self, db):
        """It must not become the wholesale sweep by accident: a refetch may
        already be in flight, and these are the facts the authority model
        exists to protect."""
        url = self._hydrated(db)
        before = _row(db, url)
        conn = sqlite3.connect(db.db_path)
        conn.execute(
            "UPDATE hdencode_candidates SET feed_parse_version='old-v0'"
            " WHERE canonical_url=?", (url,))
        conn.commit(); conn.close()

        db.reconcile_derived_versions()

        after = _row(db, url)
        for field in ("season", "episode", "episode_end", "resolution",
                      "size_text", "size_gb", "hevc_evidence", "clean_title",
                      "description_year", "media_type"):
            assert after[field] == before[field], field

    def test_autonomous_action_is_denied_until_both_legs_are_current(self, db, monkeypatch):
        """The consequence that matters: while either leg is stale, nothing
        autonomous may act on the row."""
        from backend.hdencode_action_service import (
            HDEncodeActionError, HDEncodeActionService)
        url = self._hydrated(db)
        import backend.detail_scraper as ds
        monkeypatch.setattr(
            ds, "DETAIL_PARSE_VERSION",
            ds.DETAIL_CAPABILITY_VERSION + "+release-grammar-vNEXT")
        db.reconcile_derived_versions()

        row = dict(_row(db, url))
        svc = object.__new__(HDEncodeActionService)
        svc.config = {"hdencode_rss_auto_grab_enabled": True}
        with pytest.raises(HDEncodeActionError) as exc:
            svc._validate_auto_action(row, "grab")
        assert exc.value.code == "stale_derived"
