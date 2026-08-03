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


def _row(db):
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM hdencode_candidates WHERE canonical_url = ?",
        (URL,)).fetchone()
    conn.close()
    return dict(row)


DETAIL_FACTS = {
    "clean_title": "Movie Detail Cut",
    "title_year": 2027,
    "season": 1,
    "resolution": "2160P",
    "size_text": "50.0 GB",
    "size_gb": 50.0,
    "dv_evidence": "present",
    "media_type": "tv",
    "media_type_provisional": 0,
    "media_type_because": '["detail"]',
}


class TestHydratedFactsSurviveChangedPolls:
    def test_detail_authority_survives_and_feed_facts_update(self, db):
        _ingest(db, _body("Movie 2026 2160p WEB-DL - 12.0 GB"), "sha-v1")
        updates = dict(DETAIL_FACTS)
        updates["media_type_because"] = ["detail"]  # hydration JSON-encodes it
        db.complete_hdencode_hydration(
            URL, payload={"url": URL}, candidate_updates=updates)
        assert _row(db)["hydration_state"] == "completed"

        # the changed poll: different title text, size, and raw hash
        _ingest(db, _body("Movie 2026 1080p WEB-DL - 4.0 GB", "?v=2"), "sha-v2")

        row = _row(db)
        # 1. every detail-authority fact SURVIVES
        for field, expected in DETAIL_FACTS.items():
            assert row[field] == expected, (field, row[field])
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
