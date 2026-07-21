"""Fixture-only tests for HDEncode RSS parsing and persistence."""
import sqlite3
import pytest

from backend.database import DatabaseManager
from backend.sources.hdencode_feed_parser import parse_feed, parse_release_title


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<lastBuildDate>Sun, 19 Jul 2026 20:32:02 +0000</lastBuildDate>
<item>
<title>The.Westies.S01E03.2160p.AMZN.WEB-DL.DoVi.HDR10P.H265 \xe2\x80\x93 12.5 GB</title>
<link>https://hdencode.org/the-westies-s01e03/</link>
<guid>https://hdencode.org/the-westies-s01e03/</guid>
<pubDate>Sun, 19 Jul 2026 20:00:00 +0000</pubDate>
<category>TV Shows</category>
<description><![CDATA[Year: 2026<br>Filename: The.Westies.S01E03.mkv]]></description>
</item>
<item>
<title>Heartstopper.S03.DV.2160p.WEB.h265-BETTY \xe2\x80\x93 37.7 GB</title>
<link>https://hdencode.org/heartstopper-s03/</link>
<guid>https://hdencode.org/heartstopper-s03/</guid>
<pubDate>Sun, 19 Jul 2026 19:00:00 +0000</pubDate>
<category>TV-Packs</category>
<description><![CDATA[Season pack excerpt&hellip;]]></description>
</item>
</channel></rss>
"""


def test_parser_traps():
    parsed = parse_release_title(
        "The.Westies.S01E03.2160p.DoVi.HDR10P.H265 – 12.5 GB"
    )
    assert (parsed["season"], parsed["episode"]) == (1, 3)
    assert parsed["size_gb"] == 12.5
    assert parsed["dv"] == "asserted"
    assert parsed["hdr_formats"] == ("HDR10+",)
    assert parse_release_title(
        "Heartstopper.S03.DV.2160p.WEB.h265 – 37.7 GB"
    )["episode"] is None
    assert parse_release_title(
        "Movie.2026.2160p.WEB-DL-DVDRIP – 10 GB"
    )["dv"] == "unknown"


def test_feed_safety_and_raw_evidence():
    feed = parse_feed(RSS, "tv_all")
    assert len(feed.entries) == 2
    assert feed.entries[0].description_year == 2026
    assert feed.entries[0].raw_hash
    assert feed.entries[1].description_complete is False
    with pytest.raises(ValueError, match="DTD"):
        parse_feed(b"<!DOCTYPE rss><rss/>", "tv_all")
    with pytest.raises(ValueError, match="2 MiB"):
        parse_feed(b"x" * (2 * 1024 * 1024 + 1), "tv_all")


def rows(feed):
    return [entry.as_database_row() for entry in feed.entries]


def test_atomic_ingest_and_304(tmp_path):
    db = DatabaseManager(str(tmp_path / "crawler.db"))
    feed = parse_feed(RSS, "tv_all")
    db.ingest_hdencode_feed(
        feed_key="tv_all",
        feed_url="https://hdencode.org/tag/tv-shows/feed/",
        last_modified="validator",
        http_status=200,
        body_sha256=feed.body_sha256,
        channel_last_build_date=feed.channel_last_build_date,
        entries=rows(feed),
        started_at="2026-07-19T20:32:03+00:00",
        completed_at="2026-07-19T20:32:04+00:00",
    )
    assert len(db.list_hdencode_candidates()) == 2
    assert db.get_hdencode_feed_state("tv_all")["last_modified"] == "validator"

    empty = DatabaseManager(str(tmp_path / "empty.db"))
    empty.record_hdencode_feed_not_modified(
        feed_key="movies_all",
        feed_url="https://hdencode.org/tag/movies/feed/",
        last_modified="same",
        checked_at="2026-07-19T20:32:04+00:00",
    )
    assert empty.list_hdencode_candidates() == []


@pytest.mark.parametrize("fail_step", range(1, 8))
def test_validator_does_not_advance_on_crash(tmp_path, fail_step):
    path = tmp_path / f"crawler-{fail_step}.db"
    db = DatabaseManager(str(path))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO hdencode_feed_state "
            "(feed_key, feed_url, last_modified) VALUES (?, ?, ?)",
            ("tv_all", "https://hdencode.org/tag/tv-shows/feed/", "old"),
        )
    feed = parse_feed(RSS, "tv_all")
    with pytest.raises(RuntimeError, match="injected ingest failure"):
        db.ingest_hdencode_feed(
            feed_key="tv_all",
            feed_url="https://hdencode.org/tag/tv-shows/feed/",
            last_modified="new",
            http_status=200,
            body_sha256=feed.body_sha256,
            channel_last_build_date=feed.channel_last_build_date,
            entries=rows(feed),
            started_at="2026-07-19T20:32:03+00:00",
            completed_at="2026-07-19T20:32:04+00:00",
            _test_fail_after_step=fail_step,
        )
    db.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT last_modified FROM hdencode_feed_state "
            "WHERE feed_key='tv_all'"
        ).fetchone()[0] == "old"
        assert conn.execute(
            "SELECT COUNT(*) FROM hdencode_candidates"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM hdencode_ingest_cycles"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_additive_migration_legacy_reader(tmp_path):
    path = tmp_path / "crawler.db"
    db = DatabaseManager(str(path))
    db.close()
    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"downloads", "plex_cache", "hdencode_candidates"} <= tables
        conn.execute("SELECT url, title FROM downloads LIMIT 1").fetchall()
        conn.execute("SELECT key, title FROM plex_cache LIMIT 1").fetchall()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        conn.close()
