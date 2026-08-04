"""Round-10 Q3 P0: a changed feed poll must not revert DETAIL-authority facts.

Confirmed broader than media type: the ingest upsert unconditionally replaced
clean_title, years, season/episode, resolution, size, DV/HDR/HEVC evidence,
completeness and the media-type triple with lower-authority feed values while
leaving the row marked hydrated — silently undoing completed hydration on
every changed poll. Real parsed entries, real file-backed DB, per the
verdict's test recipe; no fakes on the entry or persistence boundary.
"""
import sqlite3

import pytest

from backend.database import DatabaseManager
from backend.sources.hdencode_feed_parser import parse_feed

URL = "https://hdencode.org/movie-2026-2160p-web-dl/"


def _body(title, guid_suffix=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>HDEncode</title>
<item><title>{title}</title>
  <link>{URL}</link>
  <guid>{URL}{guid_suffix}</guid>
  <pubDate>Fri, 01 Aug 2026 12:00:00 +0000</pubDate>
  <category>Movies</category><description>x</description></item>
</channel></rss>""".encode()


@pytest.fixture
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "auth.db"))


def _ingest(db, body, sha):
    parsed = parse_feed(body, "movies_all")
    db.ingest_hdencode_feed(
        feed_key="movies_all", feed_url="https://hdencode.org/feed/",
        last_modified=None, http_status=200, body_sha256=sha,
        channel_last_build_date=None,
        entries=[e.as_database_row() for e in parsed.entries],
        started_at="2026-08-01T12:00:00+00:00",
        completed_at="2026-08-01T12:00:05+00:00")


def _row2(db):
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM hdencode_candidates LIMIT 1").fetchone()
    conn.close()
    return dict(row)


def _row(db):
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM hdencode_candidates WHERE canonical_url = ?",
        (URL,)).fetchone()
    conn.close()
    return dict(row)


#: ALL SEVENTEEN protected fields, each with a distinct hydrated value --
#: round-12 hardening: a future CASE omission in the upsert guard must fail
#: a named field assertion, not hide behind a subset.
#:
#: Round-13 separation: this is CONTRACT 1 -- the SINK preserves every
#: protected field across a changed poll, proven with handcrafted updates
#: precisely so each field can carry a distinct sentinel value. Whether the
#: production adapter can actually PRODUCE a field is CONTRACT 2, proven
#: end-to-end in test_detail_hydration_composition.py
#: (TestProductionEmissionContract) -- which is also where title_year's
#: feed-authority retraction is pinned. Passing here says nothing about
#: emission, by design.
DETAIL_FACTS = {
    "media_type": "tv",
    "media_type_provisional": 0,
    "media_type_because": '["detail"]',
    "clean_title": "Movie Detail Cut",
    "title_year": 2027,
    "description_year": 2028,
    "season": 1,
    "episode": 4,
    "episode_end": 6,
    "resolution": "2160P",
    "size_text": "50.0 GB",
    "size_gb": 50.0,
    "dv_evidence": "present",
    "hdr_evidence": "present",
    "hevc_evidence": "present",
    "hdr_formats": '["DV", "HDR10"]',
    "description_complete": 1,
}

PROTECTED_FIELDS = tuple(DETAIL_FACTS)


class TestHydratedFactsSurviveChangedPolls:
    def test_detail_authority_survives_and_feed_facts_update(self, db):
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 12.0 GB"), "sha-v1")
        updates = dict(DETAIL_FACTS)
        # hydration JSON-encodes these two itself -- pass the raw shapes
        updates["media_type_because"] = ["detail"]
        updates["hdr_formats"] = ["DV", "HDR10"]
        db.complete_hdencode_hydration(
            URL, payload={"url": URL}, candidate_updates=updates)
        assert _row(db)["hydration_state"] == "completed"

        # the changed poll: different title text, size, and raw hash
        _ingest(db, _body("Movie 2026 1080p WEB-DL - 4.0 GB", "?v=2"), "sha-v2")

        row = _row(db)
        # 1. every one of the SEVENTEEN detail-authority facts SURVIVES
        assert len(PROTECTED_FIELDS) == 17
        for field in PROTECTED_FIELDS:
            assert row[field] == DETAIL_FACTS[field], (field, row[field])
        # meta-guard: the SQL carries exactly one CASE per protected field
        import inspect
        from backend import database as _dbmod
        src = inspect.getsource(_dbmod)
        for field in PROTECTED_FIELDS:
            assert (f"{field} = CASE WHEN hdencode_candidates.hydration_state"
                    in src), f"upsert guard missing for {field}"
        assert row["hydration_state"] == "completed"
        # 2. raw feed-only facts DID update — this is not a frozen row
        assert "1080p" in row["title"]
        assert row["raw_hash"] != ""

    def test_pre_hydration_rows_still_refresh_normally(self, db):
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 12.0 GB"), "sha-v1")
        before = _row(db)
        assert before["hydration_state"] != "completed"
        _ingest(db, _body("Movie 2026 1080p WEB-DL - 4.0 GB", "?v=2"), "sha-v2")
        after = _row(db)
        assert after["resolution"] != before["resolution"]
        assert after["size_gb"] != before["size_gb"]


class TestR4VersionStamps:
    """R-4 commit 1: every write boundary stamps the grammar version it
    parsed with -- dedicated columns, behaviour-neutral (nothing reads them
    yet). The reconciler (commit 2) turns mismatches into staleness."""

    def test_ingest_stamps_feed_parse_version(self, db):
        from backend.release_grammar import GRAMMAR_VERSION
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 12.0 GB"), "sha-v1")
        row = _row(db)
        assert row["feed_parse_version"] == GRAMMAR_VERSION
        assert row["derived_state"] == "current"
        assert row["detail_parse_version"] is None

    def test_hydration_stamps_detail_parse_version(self, db):
        # Round-13: the detail stamp is the SCRAPER's own version, decoupled
        # from the grammar's (see DETAIL_PARSE_VERSION's doc comment).
        from backend.detail_scraper import DETAIL_PARSE_VERSION
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 12.0 GB"), "sha-v1")
        db.complete_hdencode_hydration(
            URL, payload={"url": URL}, candidate_updates={"media_type": "movie"})
        assert _row(db)["detail_parse_version"] == DETAIL_PARSE_VERSION

    def test_cache_upsert_stamps_parse_version(self, db):
        from backend.release_grammar import GRAMMAR_VERSION
        assert db.upsert_background_cache([{
            "url": "https://hdencode.org/c1/", "title": "C", "year": 2026,
            "status": "missing", "source_category": "4k_movies",
            "data": "{}"}])
        conn = sqlite3.connect(db.db_path)
        row = conn.execute("SELECT parse_version, derived_state "
                           "FROM background_scan_cache").fetchone()
        conn.close()
        assert row[0] == GRAMMAR_VERSION and row[1] == "current"


class TestR4Reconciler:
    """R-4 commit 2: version mismatches become visible staleness with the
    ratified consequences -- refetch transition, auto-action exclusion,
    skip-set bypass, cache generation bump."""

    def _age_stamps(self, db):
        conn = sqlite3.connect(db.db_path)
        conn.execute("UPDATE hdencode_candidates SET feed_parse_version='old-v0',"
                     " detail_parse_version=CASE WHEN hydration_state='completed'"
                     " THEN 'old-v0' ELSE detail_parse_version END")
        conn.execute("UPDATE background_scan_cache SET parse_version='old-v0'")
        conn.commit(); conn.close()

    def test_hydrated_mismatch_becomes_refetch_and_requeues(self, db):
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 12.0 GB"), "sha-v1")
        db.complete_hdencode_hydration(
            URL, payload={"url": URL}, candidate_updates={"media_type": "movie"})
        self._age_stamps(db)
        out = db.reconcile_derived_versions()
        assert out["candidates_refetch_required"] == 1
        row = _row(db)
        assert row["derived_state"] == "refetch_required"
        assert row["relevance_state"] == "unclassified"
        assert row["hydration_state"] == "queued"      # the completed->queued transition
        # re-hydration under the current grammar returns the row to current
        db.complete_hdencode_hydration(
            URL, payload={"url": URL}, candidate_updates={"media_type": "movie"})
        assert _row(db)["derived_state"] == "current"

    def test_nonhydrated_mismatch_is_marked_then_healed_offline(self, db):
        # Commit 3 made non-hydrated staleness TRANSIENT: the same
        # reconciliation pass reparses the retained inputs and returns the
        # row to current -- so the durable observable is the reparse count
        # plus the fresh stamp, not a lingering 'stale' state.
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 12.0 GB"), "sha-v1")
        self._age_stamps(db)
        out = db.reconcile_derived_versions()
        assert out["candidates_stale"] == 1
        assert out["candidates_reparsed"] == 1
        assert _row(db)["derived_state"] == "current"

    def test_auto_action_rejects_a_stale_row_outright(self):
        # Unit-level: whatever leaves a row non-current (refetch_required
        # rows, or a future marker), the autonomous authorizer refuses it
        # before any other consideration.
        import pytest as _pytest
        from backend.hdencode_action_service import (
            HDEncodeActionError, HDEncodeActionService)
        svc = object.__new__(HDEncodeActionService)
        svc.config = {"hdencode_rss_auto_grab_enabled": True}
        with _pytest.raises(HDEncodeActionError) as exc:
            svc._validate_auto_action({"derived_state": "refetch_required"}, "grab")
        assert exc.value.code == "stale_derived"

    def test_stale_cache_rows_leave_the_skip_set_and_bump_the_rev(self, db):
        assert db.upsert_background_cache([{
            "url": "https://hdencode.org/c1/", "title": "C", "year": 2026,
            "status": "missing", "source_category": "4k_movies", "data": "{}"}])
        assert "https://hdencode.org/c1/" in db.get_background_cache_urls()
        self._age_stamps(db)
        rev_before = db.get_background_cache_version()
        out = db.reconcile_derived_versions()
        assert out["cache_stale"] == 1
        assert "https://hdencode.org/c1/" not in db.get_background_cache_urls()
        assert db.get_background_cache_version() != rev_before

    def test_reconcile_is_idempotent_and_current_rows_untouched(self, db):
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 12.0 GB"), "sha-v1")
        out1 = db.reconcile_derived_versions()
        assert out1 == {"candidates_refetch_required": 0,
                        "candidates_stale": 0, "cache_stale": 0,
                        "candidates_reparsed": 0}
        assert _row(db)["derived_state"] == "current"


class TestR4OfflineReparse:
    """R-4 commit 3: stale non-hydrated rows are re-derived IN PLACE through
    the same composition live ingest uses -- including healing a real
    old-grammar artifact without any network."""

    def test_stale_row_heals_offline_with_corrected_facts(self, db):
        # A genuine pre-boundary-fix artifact: the group name S0MEGRP once
        # parsed as TV season 0. Plant that wrong verdict with an old stamp;
        # the reconciler must reparse it into a movie under the current
        # grammar and return the row to 'current'.
        _ingest(db, _body("Movie 2020 1080p x264-S0MEGRP - 4.0 GB"), "sha-v1")
        conn = sqlite3.connect(db.db_path)
        conn.execute("UPDATE hdencode_candidates SET media_type='tv', season=0,"
                     " feed_parse_version='old-v0'")
        conn.commit(); conn.close()
        out = db.reconcile_derived_versions()
        assert out["candidates_stale"] == 1
        assert out["candidates_reparsed"] == 1
        row = _row2(db)
        assert row["derived_state"] == "current"
        assert row["media_type"] != "tv"
        assert row["season"] is None
        from backend.release_grammar import GRAMMAR_VERSION
        assert row["feed_parse_version"] == GRAMMAR_VERSION

    def test_ingest_and_reparse_agree_exactly(self, db):
        # THE anti-drift property: reparsing a fresh row changes nothing.
        _ingest(db, _body("Show Complete Series 2160p WEB - 40.0 GB"), "sha-v1")
        before = _row2(db)
        conn = sqlite3.connect(db.db_path)
        conn.execute("UPDATE hdencode_candidates SET feed_parse_version='old-v0'")
        conn.commit(); conn.close()
        db.reconcile_derived_versions()
        after = _row2(db)
        for field in ("media_type", "clean_title", "title_year", "season",
                      "resolution", "size_gb", "dv_evidence", "hdr_formats",
                      "description_complete", "media_type_provisional"):
            assert after[field] == before[field], field
        assert after["derived_state"] == "current"


class TestR4CacheHealing:
    """Round-11 Finding 2 (P1): a stale cache row that gets re-scraped must
    HEAL -- fresh data + current version + derived_state back to 'current' +
    re-enters the skip set. Without the heal it re-scrapes forever."""

    CACHE_ROW = {"url": "https://hdencode.org/heal1/", "title": "H",
                 "year": 2026, "status": "missing",
                 "source_category": "4k_movies", "data": "{}"}

    def test_rescrape_heals_a_stale_row_end_to_end(self, db):
        assert db.upsert_background_cache([dict(self.CACHE_ROW)])
        conn = sqlite3.connect(db.db_path)
        conn.execute("UPDATE background_scan_cache SET parse_version='old-v0'")
        conn.commit(); conn.close()
        out = db.reconcile_derived_versions()
        assert out["cache_stale"] == 1
        assert self.CACHE_ROW["url"] not in db.get_background_cache_urls()
        # the re-scrape lands as a fresh upsert of the same URL
        assert db.upsert_background_cache([dict(self.CACHE_ROW, data='{"v":2}')])
        conn = sqlite3.connect(db.db_path)
        row = conn.execute("SELECT parse_version, derived_state, data "
                           "FROM background_scan_cache").fetchone()
        conn.close()
        from backend.release_grammar import GRAMMAR_VERSION
        assert row[0] == GRAMMAR_VERSION
        assert row[1] == "current"                 # THE heal
        assert row[2] == '{"v":2}'
        assert self.CACHE_ROW["url"] in db.get_background_cache_urls()

    def test_race_both_serialized_orders_end_current(self, db):
        """Round-12 F2 remainder -- the REAL interleaving property, proven
        deterministically over TWO independent database handles in both
        serialized orders. Final state must be current version + current
        state + the fresh blob either way."""
        from backend.database import DatabaseManager
        from backend.release_grammar import GRAMMAR_VERSION
        db2 = DatabaseManager(db.db_path)   # second, independent handle

        def _cache_row():
            conn = sqlite3.connect(db.db_path)
            r = conn.execute("SELECT parse_version, derived_state, data "
                             "FROM background_scan_cache WHERE url = ?",
                             (self.CACHE_ROW["url"],)).fetchone()
            conn.close()
            return r

        # ORDER 1: fresh upsert commits BEFORE the reconciliation pass.
        assert db.upsert_background_cache([dict(self.CACHE_ROW)])
        conn = sqlite3.connect(db.db_path)
        conn.execute("UPDATE background_scan_cache SET parse_version='old-v0'")
        conn.commit(); conn.close()
        healed = dict(self.CACHE_ROW)
        healed["data"] = '{"o1": 1}'
        assert db2.upsert_background_cache([healed])      # heal lands first
        out = db.reconcile_derived_versions()             # late pass second
        assert out["cache_stale"] == 0                    # nothing to mark
        assert _cache_row() == (GRAMMAR_VERSION, "current", '{"o1": 1}')

        # ORDER 2: reconciliation marks stale BEFORE the upsert lands.
        conn = sqlite3.connect(db.db_path)
        conn.execute("UPDATE background_scan_cache SET parse_version='old-v0'")
        conn.commit(); conn.close()
        out = db.reconcile_derived_versions()             # marks stale first
        assert out["cache_stale"] == 1
        assert _cache_row()[1] == "stale"
        healed["data"] = '{"o2": 2}'
        assert db2.upsert_background_cache([healed])      # heal lands second
        assert _cache_row() == (GRAMMAR_VERSION, "current", '{"o2": 2}')
