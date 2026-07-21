from datetime import datetime, timedelta, timezone
import sqlite3
from backend.hdencode_shadow import compare_shadow, catchup_required, jittered_interval_seconds
from backend.hdencode_candidate_service import _candidate_updates

def test_missing_hydration_fields_preserve_unknown():
    updates=_candidate_updates({})
    assert "dv_evidence" not in updates
    assert "hdr_formats" not in updates
    assert "description_complete" not in updates
    assert "identity_state" not in updates

def test_explicit_false_is_negated_and_identity_is_only_hydrated():
    updates=_candidate_updates({"url":"https://hdencode.org/a/","display_title":"A","dovi":False,"hdr":"SDR"})
    assert updates["dv_evidence"]=="negated"
    assert updates["hdr_evidence"]=="negated"
    assert updates["identity_state"]=="hydrated"

def test_shadow_comparison_detects_relevant_listing_only():
    result=compare_shadow(rss_urls=["https://hdencode.org/a/"],listing_items=[{"url":"https://hdencode.org/a","status":"in_library"},{"url":"https://hdencode.org/b/","status":"dv_upgrade","title":"B"}],rss_requests=2,listing_requests=5,normal_feeds_complete=True)
    assert result.duplicate_count==1
    assert result.relevant_miss_count==1
    assert result.outcome=="relevant_miss"
    assert result.request_reduction_pct==60.0

def test_adaptive_catchup_uses_depth_margin():
    now=datetime.now(timezone.utc)
    safe=[{"last_checked_at":(now-timedelta(hours=1)).isoformat(),"observed_depth_seconds":12*3600}]
    danger=[{"last_checked_at":(now-timedelta(hours=10)).isoformat(),"observed_depth_seconds":12*3600}]
    assert catchup_required(safe,now=now) is False
    assert catchup_required(danger,now=now) is True

def test_jitter_bounds():
    class R:
        def uniform(self,a,b): return b
    assert jittered_interval_seconds(60,rng=R())==70*60

def test_download_history_join_relationship():
    conn=sqlite3.connect(':memory:')
    conn.executescript('CREATE TABLE downloads(url TEXT PRIMARY KEY); CREATE TABLE scraped_link_map(link TEXT PRIMARY KEY, source_url TEXT);')
    conn.execute('INSERT INTO downloads VALUES (?)',('https://rapidgator.net/file/1',))
    conn.execute('INSERT INTO scraped_link_map VALUES (?,?)',('https://rapidgator.net/file/1','https://hdencode.org/post/'))
    row=conn.execute("SELECT 1 FROM scraped_link_map m JOIN downloads d ON d.url=m.link WHERE RTRIM(m.source_url,'/')=RTRIM(?,'/')",('https://hdencode.org/post',)).fetchone()
    assert row==(1,)


def test_database_context_uses_source_link_history(tmp_path):
    from backend.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "context.db"))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO downloads (url, title) VALUES (?, ?)",
            ("https://rapidgator.net/file/1", "Example"),
        )
        conn.execute(
            "INSERT INTO scraped_link_map (link, title, source_url) "
            "VALUES (?, ?, ?)",
            (
                "https://rapidgator.net/file/1",
                "Example",
                "https://hdencode.org/example/",
            ),
        )
    context = db.get_hdencode_candidate_context(
        canonical_url="https://hdencode.org/example",
        clean_title=None,
        media_type="movie",
        years=(),
        season=None,
    )
    assert context["exact_url_downloaded"] is True


def test_readiness_counts_comparisons_not_ingest_rows(tmp_path):
    from backend.database import DatabaseManager
    import json
    db = DatabaseManager(str(tmp_path / "readiness.db"))
    now = datetime.now(timezone.utc)
    with db.transaction() as conn:
        for feed in ("movies_all", "tv_all"):
            conn.execute(
                "INSERT INTO hdencode_feed_state "
                "(feed_key, feed_url, last_status, consecutive_failures, "
                "last_checked_at) VALUES (?, ?, 304, 0, ?)",
                (feed, f"https://hdencode.org/{feed}/", now.isoformat()),
            )
        # Arbitrary feed-ingest rows do not satisfy comparison readiness.
        for index in range(25):
            conn.execute(
                "INSERT INTO hdencode_ingest_cycles "
                "(feed_key, started_at, completed_at, http_status, changed, "
                "candidate_count, outcome) VALUES (?, ?, ?, 304, 0, 0, ?)",
                (
                    "movies_all",
                    now.isoformat(),
                    now.isoformat(),
                    "not_modified",
                ),
            )
    assert db.get_hdencode_rss_readiness(min_cycles=20, min_days=7)["ready"] is False

    with db.transaction() as conn:
        for index in range(20):
            completed = (
                now - timedelta(days=8) + timedelta(hours=index * 10)
            ).isoformat()
            conn.execute(
                "INSERT INTO hdencode_shadow_cycles "
                "(cycle_uuid, started_at, completed_at, normal_feeds_complete, "
                "rss_requests, listing_requests, rss_count, listing_count, "
                "duplicate_count, feed_only_count, listing_only_count, "
                "relevant_miss_count, request_reduction_pct, catchup_used, "
                "restart_recovery, outcome, details_json) "
                "VALUES (?, ?, ?, 1, 2, 5, 10, 10, 10, 0, 0, 0, 60, ?, 0, "
                "'success', ?)",
                (
                    str(index),
                    completed,
                    completed,
                    1 if index == 0 else 0,
                    json.dumps({}),
                ),
            )
    readiness = db.get_hdencode_rss_readiness(min_cycles=20, min_days=7)
    assert readiness["ready"] is True
    assert readiness["request_reduction_pct"] == 60.0
