"""Tests for backend/analytics.py — dataclasses, StatsDashboard, and singleton."""

import json
import math
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

from backend.analytics import (
    LibraryStats,
    ScanStats,
    StatsDashboard,
    UpgradeAnalysis,
    get_analytics,
    _analytics_lock,
)
import backend.analytics as analytics_mod


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_analytics_singleton():
    """Reset the module-level singleton between tests."""
    analytics_mod._analytics = None
    yield
    analytics_mod._analytics = None


@pytest.fixture
def empty_db(tmp_path):
    """Create a minimal SQLite DB with the tables StatsDashboard expects."""
    db_path = str(tmp_path / "analytics_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plex_cache (
            id INTEGER PRIMARY KEY,
            content_type TEXT,
            title TEXT,
            year INTEGER,
            imdb_id TEXT,
            res TEXT,
            size REAL DEFAULT 0,
            dovi INTEGER DEFAULT 0,
            hdr INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_history (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            items_scanned INTEGER DEFAULT 0,
            missing_count INTEGER DEFAULT 0,
            upgrade_count INTEGER DEFAULT 0,
            duration_seconds REAL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def populated_db(empty_db):
    """Seed the DB with sample plex_cache and scan_history rows."""
    conn = sqlite3.connect(empty_db)
    # Plex cache: a mix of resolutions and HDR flags
    conn.executemany(
        "INSERT INTO plex_cache (content_type, title, year, imdb_id, res, size, dovi, hdr) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("Movies", "Movie A", 2025, "tt0000001", "4K", 60.0, 1, 1),
            ("Movies", "Movie B", 2024, "tt0000002", "4K", 50.0, 0, 1),
            ("Movies", "Movie C", 2023, "tt0000003", "1080p", 15.0, 0, 0),
            ("Movies", "Movie D", 2022, "tt0000004", "720p", 5.0, 0, 0),
            ("TV Shows", "Show A", 2025, "tt0000005", "1080p", 40.0, 0, 0),
        ],
    )
    # Scan history — use dates relative to now so they stay inside the
    # get_scan_stats(30) window (hardcoded dates rot once >30 days old).
    # Structure: 1 scan on the latest day, 2 scans on the prior day.
    _fmt = "%Y-%m-%dT%H:%M:%S"
    _latest = datetime.now() - timedelta(days=1)
    _prior = datetime.now() - timedelta(days=2)
    conn.executemany(
        "INSERT INTO scan_history (timestamp, items_scanned, missing_count, upgrade_count, duration_seconds) VALUES (?,?,?,?,?)",
        [
            (_latest.replace(hour=10, minute=0, second=0, microsecond=0).strftime(_fmt), 100, 30, 10, 120.0),
            (_prior.replace(hour=9, minute=0, second=0, microsecond=0).strftime(_fmt), 80, 20, 5, 90.0),
            (_prior.replace(hour=15, minute=0, second=0, microsecond=0).strftime(_fmt), 60, 10, 3, 60.0),
        ],
    )
    conn.commit()
    conn.close()
    return empty_db


# ── LibraryStats ─────────────────────────────────────────────────────

class TestLibraryStats:

    def test_defaults(self):
        stats = LibraryStats()
        assert stats.total_items == 0
        assert stats.total_size_gb == 0.0
        assert stats.quality_score == 0.0

    def test_to_dict_keys(self):
        d = LibraryStats().to_dict()
        expected_keys = {
            "total_items", "total_size_gb", "resolution_counts",
            "resolution_sizes", "hdr_count", "dovi_count", "sdr_count",
            "codec_counts", "quality_score", "upgrade_potential",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_rounds_floats(self):
        stats = LibraryStats(total_size_gb=123.456789, quality_score=78.91234)
        d = stats.to_dict()
        assert d["total_size_gb"] == 123.46
        assert d["quality_score"] == 78.9

    def test_to_dict_rounds_resolution_sizes(self):
        stats = LibraryStats(resolution_sizes={"4K": 100.12345, "1080p": 50.9999})
        d = stats.to_dict()
        assert d["resolution_sizes"]["4K"] == 100.12
        assert d["resolution_sizes"]["1080p"] == 51.0


# ── ScanStats ────────────────────────────────────────────────────────

class TestScanStats:

    def test_defaults(self):
        stats = ScanStats()
        assert stats.total_scans == 0
        assert stats.last_scan_time is None

    def test_to_dict_keys(self):
        d = ScanStats().to_dict()
        expected = {
            "total_scans", "avg_duration", "total_items_scanned",
            "total_missing_found", "total_upgrades_found",
            "last_scan_time", "scans_per_day", "avg_items_per_scan",
        }
        assert set(d.keys()) == expected

    def test_avg_items_per_scan_empty(self):
        d = ScanStats().to_dict()
        assert d["avg_items_per_scan"] == 0

    def test_avg_items_per_scan_calculated(self):
        stats = ScanStats(items_per_scan=[10, 20, 30])
        d = stats.to_dict()
        assert d["avg_items_per_scan"] == 20.0

    def test_avg_items_per_scan_rounds(self):
        stats = ScanStats(items_per_scan=[10, 15, 20])
        d = stats.to_dict()
        assert d["avg_items_per_scan"] == 15.0


# ── UpgradeAnalysis ──────────────────────────────────────────────────

class TestUpgradeAnalysis:

    def test_defaults(self):
        ua = UpgradeAnalysis()
        assert ua.total_upgradeable == 0
        assert ua.top_upgrade_candidates == []

    def test_to_dict_keys(self):
        d = UpgradeAnalysis().to_dict()
        expected = {
            "total_upgradeable", "resolution_upgrades", "hdr_upgrades",
            "size_upgrades", "estimated_size_increase_gb",
            "top_upgrade_candidates",
        }
        assert set(d.keys()) == expected

    def test_top_upgrade_candidates_capped_at_20(self):
        candidates = [{"title": f"Movie {i}"} for i in range(30)]
        ua = UpgradeAnalysis(top_upgrade_candidates=candidates)
        d = ua.to_dict()
        assert len(d["top_upgrade_candidates"]) == 20

    def test_estimated_size_rounds(self):
        ua = UpgradeAnalysis(estimated_size_increase_gb=99.9999)
        d = ua.to_dict()
        assert d["estimated_size_increase_gb"] == 100.0


# ── StatsDashboard._parse_size ───────────────────────────────────────

class TestParseSize:

    @pytest.fixture
    def dash(self, empty_db):
        return StatsDashboard(db_path=empty_db)

    def test_gb_integer(self, dash):
        assert dash._parse_size("15 GB") == 15.0

    def test_gb_decimal(self, dash):
        assert dash._parse_size("15.5 GB") == 15.5

    def test_mb_converts_to_gb(self, dash):
        result = dash._parse_size("500 MB")
        assert abs(result - 500 / 1024) < 0.01

    def test_tb_converts_to_gb(self, dash):
        result = dash._parse_size("1.5 TB")
        assert result == 1.5 * 1024

    def test_no_match_returns_zero(self, dash):
        assert dash._parse_size("unknown") == 0.0

    def test_empty_string(self, dash):
        assert dash._parse_size("") == 0.0

    def test_case_insensitive(self, dash):
        assert dash._parse_size("10 gb") == 10.0
        assert dash._parse_size("10 Gb") == 10.0

    def test_numeric_only_returns_zero(self, dash):
        assert dash._parse_size("42") == 0.0


# ── StatsDashboard.get_storage_projection ────────────────────────────

class TestGetStorageProjection:

    @pytest.fixture
    def dash(self, empty_db):
        return StatsDashboard(db_path=empty_db)

    def test_projection_structure(self, dash):
        stats = LibraryStats(total_size_gb=1000.0)
        ua = UpgradeAnalysis(estimated_size_increase_gb=200.0)
        proj = dash.get_storage_projection(stats, ua, growth_rate=0.05)

        assert proj["current_size_gb"] == 1000.0
        assert proj["upgrade_size_gb"] == 200.0
        assert proj["total_after_upgrades_gb"] == 1200.0
        assert len(proj["monthly_projections"]) == 12

    def test_monthly_growth_math(self, dash):
        stats = LibraryStats(total_size_gb=100.0)
        ua = UpgradeAnalysis(estimated_size_increase_gb=0.0)
        proj = dash.get_storage_projection(stats, ua, growth_rate=0.10)

        month1 = proj["monthly_projections"][0]
        assert month1["month"] == 1
        assert month1["projected_size_gb"] == round(100 * 1.10, 2)

        month2 = proj["monthly_projections"][1]
        assert month2["projected_size_gb"] == round(100 * 1.10 * 1.10, 2)

    def test_zero_growth_rate(self, dash):
        stats = LibraryStats(total_size_gb=500.0)
        ua = UpgradeAnalysis(estimated_size_increase_gb=50.0)
        proj = dash.get_storage_projection(stats, ua, growth_rate=0.0)

        for entry in proj["monthly_projections"]:
            assert entry["projected_size_gb"] == 500.0
            assert entry["with_upgrades_gb"] == 550.0


# ── StatsDashboard with empty DB ─────────────────────────────────────

class TestDashboardEmptyDB:

    @pytest.fixture
    def dash(self, empty_db):
        return StatsDashboard(db_path=empty_db)

    def test_get_library_stats_empty(self, dash):
        stats = dash.get_library_stats("Movies")
        assert stats.total_items == 0
        assert stats.total_size_gb == 0.0
        assert stats.quality_score == 0.0

    def test_get_scan_stats_empty(self, dash):
        stats = dash.get_scan_stats(30)
        assert stats.total_scans == 0
        assert stats.last_scan_time is None


# ── StatsDashboard with populated DB ─────────────────────────────────

class TestDashboardPopulated:

    @pytest.fixture
    def dash(self, populated_db):
        return StatsDashboard(db_path=populated_db)

    def test_library_stats_total_items(self, dash):
        stats = dash.get_library_stats("Movies")
        assert stats.total_items == 4  # 4 movie rows

    def test_library_stats_total_size(self, dash):
        stats = dash.get_library_stats("Movies")
        assert stats.total_size_gb == 60.0 + 50.0 + 15.0 + 5.0

    def test_library_stats_dovi_count(self, dash):
        stats = dash.get_library_stats("Movies")
        assert stats.dovi_count == 1

    def test_library_stats_hdr_count(self, dash):
        stats = dash.get_library_stats("Movies")
        assert stats.hdr_count == 1  # one HDR-only (dovi=0, hdr=1)

    def test_library_stats_sdr_count(self, dash):
        stats = dash.get_library_stats("Movies")
        assert stats.sdr_count == 2  # 1080p and 720p

    def test_library_stats_quality_score_nonzero(self, dash):
        stats = dash.get_library_stats("Movies")
        assert stats.quality_score > 0

    def test_library_stats_upgrade_potential(self, dash):
        stats = dash.get_library_stats("Movies")
        # SDR items + 1080p + 720p can be upgraded
        # 4 unique movies (by imdb_id). 2 are 4K (res_pri=4), 2 are not (1080p, 720p).
        # upgrade_potential = non_4k / total * 100 = 2/4 * 100 = 50.0
        assert stats.upgrade_potential == 50.0

    def test_scan_stats_total_scans(self, dash):
        stats = dash.get_scan_stats(30)
        assert stats.total_scans == 3

    def test_scan_stats_total_items_scanned(self, dash):
        stats = dash.get_scan_stats(30)
        assert stats.total_items_scanned == 100 + 80 + 60

    def test_scan_stats_avg_duration(self, dash):
        stats = dash.get_scan_stats(30)
        assert stats.avg_duration == (120.0 + 90.0 + 60.0) / 3

    def test_scan_stats_items_per_scan(self, dash):
        stats = dash.get_scan_stats(30)
        assert sorted(stats.items_per_scan) == [60, 80, 100]

    def test_scan_stats_scans_per_day(self, dash):
        stats = dash.get_scan_stats(30)
        # Two scans on the prior day, one on the latest day
        assert len(stats.scans_per_day) == 2
        assert sorted(stats.scans_per_day.values()) == [1, 2]

    def test_scan_stats_last_scan_time(self, dash):
        stats = dash.get_scan_stats(30)
        # Ordered DESC, so the latest scan (now - 1 day) is first
        assert stats.last_scan_time is not None
        assert stats.last_scan_time.date() == (datetime.now() - timedelta(days=1)).date()

    def test_tv_library_stats(self, dash):
        stats = dash.get_library_stats("TV Shows")
        assert stats.total_items == 1
        assert stats.total_size_gb == 40.0


# ── get_analytics singleton ──────────────────────────────────────────

class TestGetAnalyticsSingleton:

    def test_returns_instance(self, empty_db):
        instance = get_analytics(db_path=empty_db)
        assert isinstance(instance, StatsDashboard)

    def test_same_instance_on_second_call(self, empty_db):
        a = get_analytics(db_path=empty_db)
        b = get_analytics(db_path=empty_db)
        assert a is b

    def test_singleton_resets_across_tests(self, empty_db):
        # The autouse fixture resets _analytics to None, so this should succeed
        instance = get_analytics(db_path=empty_db)
        assert instance is not None


# ── Rename stats ─────────────────────────────────────────────────────

@pytest.fixture
def rename_db(empty_db):
    """Add a rename_jobs table with sample applied/needs_review/failed rows."""
    conn = sqlite3.connect(empty_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rename_jobs (
            id INTEGER PRIMARY KEY, package_name TEXT, original_path TEXT,
            status TEXT, destination_path TEXT, move_method TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO rename_jobs (package_name, original_path, status, destination_path, move_method) "
        "VALUES (?,?,?,?,?)",
        [
            ("p1", "/dl/a.mkv", "applied", "/lib/Movies/A (2020)/A.mkv", "move"),
            ("p2", "/dl/b.mkv", "applied", "/lib/Movies/B (2021)/B.mkv", "move"),
            ("p3", "/dl/c.mkv", "applied", "/lib/TV/Show/Season 01/c.mkv", "hardlink"),
            ("p4", "/dl/d.mkv", "needs_review", None, "move"),
            ("p5", "/dl/e.mkv", "failed", None, "move"),
        ],
    )
    conn.commit()
    conn.close()
    return empty_db


class TestRenameStats:
    def test_counts_and_directory_buckets(self, rename_db):
        sd = StatsDashboard(db_path=rename_db)
        stats = sd.get_rename_stats({"Movies": "/lib/Movies", "TV": "/lib/TV"})
        assert stats["applied"] == 3
        assert stats["total_jobs"] == 5
        assert stats["by_status"] == {"applied": 3, "needs_review": 1, "failed": 1}
        assert stats["by_directory"] == {"Movies": 2, "TV": 1}
        assert stats["by_method"] == {"move": 2, "hardlink": 1}

    def test_fallback_bucket_without_roots(self, rename_db):
        sd = StatsDashboard(db_path=rename_db)
        stats = sd.get_rename_stats()  # no configured roots
        # Falls back to the parent directory name of each destination.
        assert stats["applied"] == 3
        assert sum(stats["by_directory"].values()) == 3

    def test_empty_when_no_jobs(self, empty_db):
        conn = sqlite3.connect(empty_db)
        conn.execute("CREATE TABLE rename_jobs (id INTEGER PRIMARY KEY, status TEXT, "
                     "destination_path TEXT, move_method TEXT)")
        conn.commit(); conn.close()
        sd = StatsDashboard(db_path=empty_db)
        stats = sd.get_rename_stats()
        assert stats["applied"] == 0 and stats["total_jobs"] == 0
        assert stats["by_directory"] == {}


# ── Rename destination bucketing: 4K prefix collision (review fix #4) ─

class TestBucketDestination:
    _roots = {"Movies": "/library/movies", "Movies (4K)": "/library/movies-4k",
              "TV": "/library/tv"}

    def test_4k_not_swallowed_by_movies(self):
        assert StatsDashboard._bucket_destination(
            "/library/movies-4k/Film (2024)/f.mkv", self._roots) == "Movies (4K)"

    def test_plain_movies_still_buckets(self):
        assert StatsDashboard._bucket_destination(
            "/library/movies/Film (2024)/f.mkv", self._roots) == "Movies"

    def test_order_independent_longest_prefix(self):
        rev = {"Movies (4K)": "/library/movies-4k", "Movies": "/library/movies"}
        assert StatsDashboard._bucket_destination(
            "/library/movies-4k/x.mkv", rev) == "Movies (4K)"

    def test_path_boundary_no_partial_prefix(self):
        assert StatsDashboard._bucket_destination(
            "/library/movies2/x.mkv", {"Movies": "/library/movies"}) != "Movies"
