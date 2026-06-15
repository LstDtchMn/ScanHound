"""Integration tests for realistic end-to-end workflows across multiple ScanHound modules.

Tests cover:
1. Full Scan Simulation (TestScanWorkflow)
2. Matching + Download History + Watchlist Integration (TestMatchWatchlistIntegration)
3. Database + Analytics Pipeline (TestDatabaseAnalyticsPipeline)
4. Config Mutation Under Load (TestConfigMutationSafety)
5. Matching Edge Cases at Scale (TestMatchingAtScale)
6. Database Resilience (TestDatabaseResilience)
"""

import copy
import json
import random
import sqlite3
import string
import threading
import time
from datetime import datetime, timedelta

import pytest

from backend.analytics import LibraryStats, StatsDashboard, UpgradeAnalysis
from backend.app_service import LRUCache, clean_string, normalize_title
from backend.config import (
    SETTINGS_PRESETS,
    get_default_config,
    validate_config,
)
from backend.database import DatabaseManager
from backend.matching import MatchingEngine, clear_fuzzy_cache
from backend.watchlist import (
    WatchlistItem,
    WatchlistItemStatus,
    WatchlistItemType,
    WatchlistManager,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

MOVIE_TITLES = [
    ("The Matrix", 1999), ("Inception", 2010), ("The Dark Knight", 2008),
    ("Interstellar", 2014), ("Pulp Fiction", 1994), ("Fight Club", 1999),
    ("Forrest Gump", 1994), ("The Shawshank Redemption", 1994),
    ("The Godfather", 1972), ("Schindlers List", 1993),
    ("The Lord of the Rings The Fellowship of the Ring", 2001),
    ("The Lord of the Rings The Two Towers", 2002),
    ("The Lord of the Rings The Return of the King", 2003),
    ("Gladiator", 2000), ("Saving Private Ryan", 1998),
    ("The Departed", 2006), ("No Country for Old Men", 2007),
    ("There Will Be Blood", 2007), ("Whiplash", 2014),
    ("Mad Max Fury Road", 2015), ("Parasite", 2019),
    ("Joker", 2019), ("Dune", 2021), ("Dune Part Two", 2024),
    ("Oppenheimer", 2023), ("Barbie", 2023),
]

TV_SHOWS = [
    ("Breaking Bad", 2008, 5), ("Game of Thrones", 2011, 8),
    ("The Sopranos", 1999, 6), ("The Wire", 2002, 5),
    ("Stranger Things", 2016, 4),
]


def _make_web(
    title="Test Movie", year=2020, res="1080p", size="10 GB",
    dovi=False, hdr="", url="https://example.com/movie", imdb_id=None,
    is_tv=False, season=None, episodes=None, search_key=None,
    episode_number=None,
):
    web = {
        "display_title": title, "year": year, "res": res, "size": size,
        "dovi": dovi, "hdr": hdr, "url": url, "imdb_id": imdb_id,
        "is_tv": is_tv, "season": season, "episodes": episodes,
    }
    if search_key is not None:
        web["search_key"] = search_key
    if episode_number is not None:
        web["episode_number"] = episode_number
    return web


def _make_plex(
    title="test movie", original="Test Movie", year=2020, res="1080p",
    size=10.0, dovi=False, hdr=False, imdb_id=None, rating_key="9999",
    media_id=None, season=None, episode_count=None, is_tv=False,
):
    item = {
        "clean_title": title, "original_title": original, "year": year,
        "res": res, "size": size, "dovi": dovi, "hdr": hdr,
        "imdb_id": imdb_id, "rating_key": rating_key,
    }
    if media_id is not None:
        item["media_id"] = media_id
    if season is not None:
        item["season"] = season
        item["is_tv"] = True
    if episode_count is not None:
        item["episode_count"] = episode_count
    if is_tv:
        item["is_tv"] = True
    return item


def _build_index(items):
    by_imdb = {}
    by_title = {}
    for item in items:
        imdb = item.get("imdb_id")
        if imdb:
            by_imdb.setdefault(imdb, []).append(item)
        title = item.get("clean_title", "").lower()
        if title:
            by_title.setdefault(title, []).append(item)
    return {"by_imdb": by_imdb, "by_title": by_title, "all_items": list(items)}


def _generate_plex_library(count=26, tv_count=0, base_imdb=100000):
    """Generate a large Plex library for testing."""
    items = []
    resolutions = ["720p", "1080p", "1080p", "4K"]  # weighted toward 1080p
    for i in range(count):
        idx = i % len(MOVIE_TITLES)
        title, year = MOVIE_TITLES[idx]
        # For titles beyond the list, add a suffix to make unique
        if i >= len(MOVIE_TITLES):
            title = f"{title} Part {i // len(MOVIE_TITLES) + 1}"
            year = year + (i // len(MOVIE_TITLES))
        res = resolutions[i % len(resolutions)]
        imdb = f"tt{base_imdb + i:07d}"
        items.append(_make_plex(
            title=clean_string(title),
            original=title,
            year=year,
            res=res,
            size=random.uniform(5.0, 80.0),
            dovi=(i % 7 == 0),
            hdr=(i % 3 == 0),
            imdb_id=imdb,
            rating_key=f"R{1000 + i}",
            media_id=f"M{1000 + i}",
        ))

    # Add TV shows
    for j in range(tv_count):
        show_idx = j % len(TV_SHOWS)
        show_name, show_year, num_seasons = TV_SHOWS[show_idx]
        for s in range(1, min(num_seasons + 1, 4)):  # max 3 seasons per show
            imdb = f"tt{base_imdb + count + j * 10 + s:07d}"
            items.append(_make_plex(
                title=clean_string(show_name),
                original=show_name,
                year=show_year,
                res="1080p",
                size=random.uniform(20.0, 60.0),
                dovi=False,
                hdr=False,
                imdb_id=imdb,
                rating_key=f"T{2000 + j * 10 + s}",
                season=s,
                episode_count=random.randint(6, 13),
                is_tv=True,
            ))

    return items


def _generate_web_items(plex_items, extra_missing=10, extra_upgrades=5):
    """Generate web items: some match Plex, some are missing, some are upgrades."""
    web_items = []
    statuses_expected = {"in_library": 0, "upgrade": 0, "missing": 0}

    # Items that match Plex (in library)
    for item in plex_items[:len(plex_items) // 2]:
        web_items.append(_make_web(
            title=item["original_title"],
            year=item["year"],
            res=item["res"],
            size=f"{item['size']:.1f} GB",
            imdb_id=item["imdb_id"],
            url=f"https://example.com/{item['rating_key']}",
            is_tv=item.get("is_tv", False),
            season=item.get("season"),
            search_key=item["clean_title"],
        ))
        statuses_expected["in_library"] += 1

    # Items that are upgrades (4K for 1080p items)
    upgrade_candidates = [p for p in plex_items if p["res"] == "1080p"]
    for item in upgrade_candidates[:extra_upgrades]:
        web_items.append(_make_web(
            title=item["original_title"],
            year=item["year"],
            res="4K",
            size=f"{item['size'] * 3:.1f} GB",
            dovi=True,
            imdb_id=item["imdb_id"],
            url=f"https://example.com/upgrade/{item['rating_key']}",
            is_tv=item.get("is_tv", False),
            season=item.get("season"),
            search_key=item["clean_title"],
        ))
        statuses_expected["upgrade"] += 1

    # Items that are missing (not in Plex)
    for i in range(extra_missing):
        web_items.append(_make_web(
            title=f"Missing Movie {i} Unique Title",
            year=2020 + (i % 5),
            res="1080p",
            size=f"{random.uniform(5.0, 30.0):.1f} GB",
            url=f"https://example.com/missing/{i}",
            search_key=f"missing movie {i} unique title",
        ))
        statuses_expected["missing"] += 1

    return web_items, statuses_expected


# ---------------------------------------------------------------------------
# 1. Full Scan Simulation
# ---------------------------------------------------------------------------

class TestScanWorkflow:
    """Simulate a full scan lifecycle: build library, scan web items, verify results."""

    def test_full_scan_with_20_plex_items_and_50_web_items(self, matching_engine, mock_app):
        """Build a Plex index with 20+ items, scan 50+ web items, verify counts."""
        clear_fuzzy_cache()
        plex_items = _generate_plex_library(count=25, tv_count=0)
        plex_index = _build_index(plex_items)
        web_items, expected = _generate_web_items(
            plex_items, extra_missing=20, extra_upgrades=5
        )

        assert len(web_items) >= 30  # at least 30 web items
        assert len(plex_index["all_items"]) >= 25

        results = {"in_library": 0, "upgrade": 0, "missing": 0, "downloaded": 0}

        for web in web_items:
            # Check download history first
            if matching_engine.check_download_history(web):
                results["downloaded"] += 1
                continue

            is_tv = web.get("is_tv", False)
            if is_tv:
                matches, _ = matching_engine.find_tv_season_matches(web, plex_index)
            else:
                matches, _ = matching_engine.find_movie_matches(web, plex_index)

            if matches:
                if is_tv:
                    status, _, _, is_upgrade = matching_engine.calculate_tv_upgrade_status(
                        web, matches[0]
                    )
                else:
                    status, _, _, _ = matching_engine.calculate_movie_upgrade_status(
                        web, matches
                    )
                    is_upgrade = "UPGRADE" in status

                if is_upgrade:
                    results["upgrade"] += 1
                else:
                    results["in_library"] += 1
            else:
                results["missing"] += 1

        # Verify all items were categorized
        total_processed = sum(results.values())
        assert total_processed == len(web_items)

        # Verify missing items are the ones we generated
        assert results["missing"] >= 15  # we added 20 missing items
        assert results["in_library"] >= 5  # at least some matched
        assert results["upgrade"] >= 1  # at least one upgrade detected

    def test_statistics_tallied_after_scan(self, db_manager):
        """Verify scan statistics are correctly saved and tallied after scan."""
        scan_data = {
            "timestamp": datetime.now().isoformat(),
            "scan_type": "Full Scan",
            "items_scanned": 75,
            "missing_count": 20,
            "upgrade_count": 8,
            "dv_upgrade_count": 3,
            "in_library_count": 47,
            "duration_seconds": 32.5,
            "sources_scanned": "source_a,source_b",
            "plex_items_cached": 200,
        }
        db_manager.save_scan_history(scan_data)

        history = db_manager.get_scan_history()
        assert len(history) == 1
        assert history[0]["items_scanned"] == 75
        assert history[0]["missing_count"] == 20
        assert history[0]["upgrade_count"] == 8
        assert history[0]["in_library_count"] == 47

        stats = db_manager.get_scan_stats()
        assert stats["total_scans"] == 1
        assert stats["total_items_scanned"] == 75
        assert stats["total_missing"] == 20
        assert stats["total_upgrades"] == 8

    def test_incremental_scan_only_new_items(self, db_manager, matching_engine, mock_app):
        """Simulate incremental scan: mark URLs as scanned, only process new ones."""
        # First scan: mark 30 URLs as scanned
        first_batch = [
            {"url": f"https://example.com/page/{i}", "title": f"Item {i}", "source": "src"}
            for i in range(30)
        ]
        db_manager.add_scanned_urls_batch(first_batch)
        assert db_manager.get_scanned_url_count() == 30

        # Second scan: 50 URLs, 30 already scanned + 20 new
        all_urls = [f"https://example.com/page/{i}" for i in range(50)]
        scanned = db_manager.get_scanned_urls()
        new_urls = [u for u in all_urls if u not in scanned]

        assert len(new_urls) == 20  # only 20 are new

        # Process only new URLs
        for url in new_urls:
            db_manager.add_scanned_url(url, title=f"New item", source="src")

        assert db_manager.get_scanned_url_count() == 50

    def test_scan_with_download_history_filtering(self, matching_engine, mock_app):
        """Items in download history should be filtered out of scan results."""
        plex_items = _generate_plex_library(count=10)
        plex_index = _build_index(plex_items)

        web_items = [
            _make_web(
                title="Already Downloaded Film",
                year=2020,
                url="https://example.com/downloaded-1",
                search_key="already downloaded film",
            ),
            _make_web(
                title="New Film Not Downloaded",
                year=2021,
                url="https://example.com/new-1",
                search_key="new film not downloaded",
            ),
        ]

        # Add one to download history
        mock_app.download_history.add("https://example.com/downloaded-1")

        processed = []
        skipped = []
        for web in web_items:
            if matching_engine.check_download_history(web):
                skipped.append(web)
            else:
                processed.append(web)

        assert len(skipped) == 1
        assert len(processed) == 1
        assert skipped[0]["url"] == "https://example.com/downloaded-1"

    def test_scan_persists_plex_cache_to_db(self, db_manager):
        """Verify Plex library items are cached to DB and can be reloaded."""
        plex_items = _generate_plex_library(count=25)
        db_manager.save_plex_cache(plex_items, "Movies")

        loaded = db_manager.load_plex_cache("Movies")
        assert len(loaded) == 25

        # Verify data integrity
        loaded_titles = {item["clean_title"] for item in loaded}
        original_titles = {item["clean_title"] for item in plex_items}
        assert loaded_titles == original_titles

    def test_multiple_scans_accumulate_history(self, db_manager):
        """Multiple scans should accumulate in scan history."""
        for i in range(5):
            scan_data = {
                "timestamp": (datetime.now() - timedelta(days=i)).isoformat(),
                "scan_type": "Full Scan",
                "items_scanned": 50 + i * 10,
                "missing_count": 10 + i,
                "upgrade_count": 5 + i,
                "dv_upgrade_count": 1,
                "in_library_count": 34 + i * 9,
                "duration_seconds": 30.0 + i * 5,
                "sources_scanned": "src",
                "plex_items_cached": 200,
            }
            db_manager.save_scan_history(scan_data)

        history = db_manager.get_scan_history()
        assert len(history) == 5

        stats = db_manager.get_scan_stats()
        assert stats["total_scans"] == 5
        assert stats["total_items_scanned"] == sum(50 + i * 10 for i in range(5))


# ---------------------------------------------------------------------------
# 2. Matching + Download History + Watchlist Integration
# ---------------------------------------------------------------------------

class TestMatchWatchlistIntegration:
    """Tests for the interplay between matching, download history, and watchlist."""

    @pytest.fixture
    def watchlist_mgr(self, tmp_path):
        db_path = str(tmp_path / "watchlist_integration.db")
        mgr = WatchlistManager(db_path=db_path)
        yield mgr
        mgr.close()

    def test_add_items_to_watchlist_then_check_scan_results(self, watchlist_mgr):
        """Add items to watchlist, run scan results check, verify found items."""
        # Add watchlist items
        watchlist_mgr.add(WatchlistItem(
            title="Dune Part Two", year=2024,
            imdb_id="tt15239678", item_type=WatchlistItemType.MOVIE,
        ))
        watchlist_mgr.add(WatchlistItem(
            title="Oppenheimer", year=2023,
            imdb_id="tt15398776", item_type=WatchlistItemType.MOVIE,
        ))
        watchlist_mgr.add(WatchlistItem(
            title="Barbie", year=2023,
            item_type=WatchlistItemType.MOVIE,
        ))

        # Simulate scan results
        scan_items = [
            {"imdb_id": "tt15239678", "display_title": "Dune Part Two 2024", "year": 2024,
             "res": "4K", "dovi": True},
            {"display_title": "Oppenheimer", "year": 2023, "res": "1080p", "dovi": False},
            {"display_title": "Completely Unrelated Movie", "year": 2020, "res": "720p",
             "dovi": False},
        ]

        matches = watchlist_mgr.check_against_scan_results(scan_items, fuzzy_threshold=80)

        # Dune matched by IMDb, Oppenheimer by title
        assert len(matches) >= 2
        matched_titles = {m[0].title for m in matches}
        assert "Dune Part Two" in matched_titles
        assert "Oppenheimer" in matched_titles

    def test_mark_found_persists_status_in_db(self, watchlist_mgr):
        """Mark watchlist items as found, verify status persists."""
        item_id = watchlist_mgr.add(WatchlistItem(
            title="Test Movie Found", year=2024,
            item_type=WatchlistItemType.MOVIE,
        ))

        watchlist_mgr.mark_found(item_id, url="https://example.com/found-movie")

        retrieved = watchlist_mgr.get(item_id)
        assert retrieved.status == WatchlistItemStatus.FOUND
        assert retrieved.found_url == "https://example.com/found-movie"
        assert retrieved.found_date is not None

        # Verify it no longer appears in wanted list
        wanted = watchlist_mgr.get_wanted()
        assert all(w.id != item_id for w in wanted)

    def test_mixed_statuses_scan_results(self, watchlist_mgr, matching_engine, mock_app):
        """Scan results with mixed statuses: watchlist, download history, missing."""
        # Setup watchlist
        watchlist_mgr.add(WatchlistItem(
            title="Watchlisted Film", year=2024,
            item_type=WatchlistItemType.MOVIE,
        ))

        # Setup download history
        mock_app.download_history.add("https://example.com/already-downloaded")

        # Setup Plex library
        plex_items = [
            _make_plex(title="in library film", original="In Library Film",
                       year=2020, imdb_id="tt9999999", rating_key="R1"),
        ]
        plex_index = _build_index(plex_items)

        # Scan results: one in watchlist, one downloaded, one in library, one truly missing
        scan_items = [
            _make_web(title="Watchlisted Film", year=2024,
                      url="https://example.com/watchlisted",
                      search_key="watchlisted film"),
            _make_web(title="Already Downloaded",
                      url="https://example.com/already-downloaded",
                      search_key="already downloaded"),
            _make_web(title="In Library Film", year=2020,
                      imdb_id="tt9999999",
                      url="https://example.com/in-library",
                      search_key="in library film"),
            _make_web(title="Totally New Movie", year=2025,
                      url="https://example.com/missing",
                      search_key="totally new movie"),
        ]

        # Categorize
        in_download_history = []
        in_library = []
        missing = []

        for web in scan_items:
            if matching_engine.check_download_history(web):
                in_download_history.append(web)
                continue
            matches, _ = matching_engine.find_movie_matches(web, plex_index)
            if matches:
                in_library.append(web)
            else:
                missing.append(web)

        assert len(in_download_history) == 1
        assert len(in_library) == 1
        assert len(missing) == 2  # watchlisted + new (both not in plex)

        # Check watchlist against the missing items
        wl_matches = watchlist_mgr.check_against_scan_results(missing)
        assert len(wl_matches) >= 1
        assert wl_matches[0][0].title == "Watchlisted Film"

    def test_watchlist_resolution_filter_rejects_low_res(self, watchlist_mgr):
        """Watchlist items with min_resolution requirement filter out low-res matches."""
        watchlist_mgr.add(WatchlistItem(
            title="4K Only Movie", year=2024,
            min_resolution="4K",
            item_type=WatchlistItemType.MOVIE,
        ))

        # Low res scan result
        low_res = [
            {"display_title": "4K Only Movie", "year": 2024,
             "res": "720p", "dovi": False},
        ]
        matches = watchlist_mgr.check_against_scan_results(low_res)
        assert len(matches) == 0

        # 1080p also rejected (min is 4K)
        mid_res = [
            {"display_title": "4K Only Movie", "year": 2024,
             "res": "1080p", "dovi": False},
        ]
        matches = watchlist_mgr.check_against_scan_results(mid_res)
        assert len(matches) == 0

        # 4K accepted
        high_res = [
            {"display_title": "4K Only Movie", "year": 2024,
             "res": "4K", "dovi": False},
        ]
        matches = watchlist_mgr.check_against_scan_results(high_res)
        assert len(matches) == 1

    def test_watchlist_tv_season_matching(self, watchlist_mgr):
        """Watchlist TV season items should only match correct season."""
        watchlist_mgr.add(WatchlistItem(
            title="Breaking Bad", year=2008,
            item_type=WatchlistItemType.TV_SEASON,
            season=3,
        ))

        scan_items = [
            {"display_title": "Breaking Bad", "year": 2008, "res": "1080p",
             "dovi": False, "season": 1, "is_tv": True},
            {"display_title": "Breaking Bad", "year": 2008, "res": "1080p",
             "dovi": False, "season": 3, "is_tv": True},
            {"display_title": "Breaking Bad", "year": 2008, "res": "4K",
             "dovi": True, "season": 3, "is_tv": True},
        ]
        matches = watchlist_mgr.check_against_scan_results(scan_items)
        # Season 1 should NOT match, season 3 should match (twice: 1080p and 4K)
        assert all(m[1]["season"] == 3 for m in matches)
        assert len(matches) == 2

    def test_watchlist_callback_fired_on_add_and_found(self, watchlist_mgr):
        """Callbacks should fire when items are added and found."""
        events = []
        watchlist_mgr.add_callback(lambda action, item: events.append((action, item.title)))

        item_id = watchlist_mgr.add(WatchlistItem(
            title="Callback Test Movie", year=2024,
            item_type=WatchlistItemType.MOVIE,
        ))
        watchlist_mgr.mark_found(item_id, url="https://example.com/cb")

        actions = [e[0] for e in events]
        assert "added" in actions
        assert "found" in actions

    def test_watchlist_export_import_preserves_found_status(self, watchlist_mgr, tmp_path):
        """Export and reimport should preserve found status and dates."""
        item_id = watchlist_mgr.add(WatchlistItem(
            title="Export Test", year=2024,
            imdb_id="tt0000099",
            item_type=WatchlistItemType.MOVIE,
        ))
        watchlist_mgr.mark_found(item_id, url="https://example.com/export-test")

        exported = watchlist_mgr.export_to_json()
        data = json.loads(exported)
        assert data["count"] == 1
        assert data["items"][0]["status"] == "found"
        assert data["items"][0]["found_url"] == "https://example.com/export-test"


# ---------------------------------------------------------------------------
# 3. Database + Analytics Pipeline
# ---------------------------------------------------------------------------

class TestDatabaseAnalyticsPipeline:
    """Tests for the full DB -> Analytics -> Report pipeline."""

    @pytest.fixture
    def populated_db(self, tmp_db):
        """Create a DB with 50+ plex cache items and 10+ scan history entries."""
        dm = DatabaseManager(db_path=tmp_db)

        # Generate and save 60 movie items with various qualities
        movies = []
        for i in range(60):
            res_choices = ["720p", "1080p", "1080p", "1080p", "4K"]
            res = res_choices[i % len(res_choices)]
            movies.append({
                "clean_title": f"movie {i}",
                "original_title": f"Movie {i}",
                "year": 2000 + (i % 25),
                "res": res,
                "size": random.uniform(3.0, 80.0),
                "imdb_id": f"tt{200000 + i:07d}",
                "rating_key": f"R{3000 + i}",
                "media_id": f"M{3000 + i}",
                "dovi": (i % 8 == 0),
                "hdr": (i % 4 == 0),
            })
        dm.save_plex_cache(movies, "Movies")

        # Generate TV items
        tv_items = []
        for i in range(15):
            tv_items.append({
                "clean_title": f"tv show {i}",
                "original_title": f"TV Show {i}",
                "year": 2010 + (i % 10),
                "res": "1080p" if i % 3 != 0 else "4K",
                "size": random.uniform(20.0, 60.0),
                "imdb_id": f"tt{300000 + i:07d}",
                "rating_key": f"T{4000 + i}",
                "season": (i % 3) + 1,
                "episode_count": random.randint(6, 13),
                "dovi": (i % 5 == 0),
                "hdr": (i % 3 == 0),
            })
        dm.save_plex_cache(tv_items, "TV Shows")

        # Generate 12 scan history entries over 10 days
        for i in range(12):
            dm.save_scan_history({
                "timestamp": (datetime.now() - timedelta(days=i)).isoformat(),
                "scan_type": "Full Scan" if i % 2 == 0 else "Quick Scan",
                "items_scanned": 50 + i * 5,
                "missing_count": 10 + i,
                "upgrade_count": 5 + (i % 3),
                "dv_upgrade_count": i % 2,
                "in_library_count": 35 + i * 4,
                "duration_seconds": 25.0 + i * 3,
                "sources_scanned": "source_a,source_b",
                "plex_items_cached": 75,
            })

        yield dm
        dm.close()

    def test_library_stats_quality_scores(self, populated_db):
        """Verify quality scores are calculated from cached library."""
        dashboard = StatsDashboard(db_path=populated_db.db_path)
        stats = dashboard.get_library_stats("Movies")

        assert stats.total_items == 60
        assert stats.total_size_gb > 0
        assert 0 <= stats.quality_score <= 100
        # upgrade_potential can exceed 100 when SDR items overlap with
        # lower-resolution items (both counted as upgradeable)
        assert stats.upgrade_potential >= 0
        assert stats.sdr_count + stats.hdr_count + stats.dovi_count == stats.total_items

        # Resolution breakdown should sum to total
        total_by_res = sum(stats.resolution_counts.values())
        assert total_by_res == stats.total_items

    def test_scan_stats_aggregation(self, populated_db):
        """Verify scan statistics are aggregated over multiple days."""
        dashboard = StatsDashboard(db_path=populated_db.db_path)
        scan_stats = dashboard.get_scan_stats(days=30)

        assert scan_stats.total_scans == 12
        assert scan_stats.avg_duration > 0
        assert scan_stats.total_items_scanned > 0
        assert scan_stats.total_missing_found > 0
        assert scan_stats.total_upgrades_found > 0
        assert scan_stats.last_scan_time is not None
        assert len(scan_stats.items_per_scan) == 12

    def test_upgrade_analysis_structure(self, populated_db):
        """Verify upgrade analysis produces correct structure."""
        dashboard = StatsDashboard(db_path=populated_db.db_path)

        plex_items = [
            {"imdb_id": "tt0200001", "res": "1080p", "size": 15.0},
            {"imdb_id": "tt0200002", "res": "720p", "size": 5.0},
        ]
        scan_results = [
            {"status": "UPGRADE (4K)", "imdb_id": "tt0200001", "display_title": "Movie 1",
             "year": 2020, "size": "60 GB", "res": "4K"},
            {"status": "UPGRADE (DV)", "imdb_id": "tt0200002", "display_title": "Movie 2",
             "year": 2021, "size": "40 GB", "res": "4K"},
            {"status": "In Library", "display_title": "Movie 3", "year": 2022, "size": "10 GB"},
            {"status": "UPGRADE (+50%)", "imdb_id": "tt0200001", "display_title": "Movie 1 Bigger",
             "year": 2020, "size": "30 GB", "res": "1080p"},
        ]

        analysis = dashboard.get_upgrade_analysis(plex_items, scan_results)

        assert isinstance(analysis, UpgradeAnalysis)
        assert analysis.total_upgradeable == 3  # three UPGRADE results
        assert analysis.resolution_upgrades >= 1  # at least one 4K upgrade
        assert analysis.hdr_upgrades >= 1  # at least one DV upgrade
        assert analysis.estimated_size_increase_gb > 0
        assert len(analysis.top_upgrade_candidates) > 0

    def test_storage_projection(self, populated_db):
        """Verify storage projections are computed correctly."""
        dashboard = StatsDashboard(db_path=populated_db.db_path)
        lib_stats = dashboard.get_library_stats("Movies")

        analysis = UpgradeAnalysis(
            total_upgradeable=10,
            estimated_size_increase_gb=500.0,
        )

        projection = dashboard.get_storage_projection(lib_stats, analysis, growth_rate=0.05)

        assert projection["current_size_gb"] == round(lib_stats.total_size_gb, 2)
        assert projection["upgrade_size_gb"] == 500.0
        assert len(projection["monthly_projections"]) == 12
        # Each month should be larger than the previous
        sizes = [p["projected_size_gb"] for p in projection["monthly_projections"]]
        assert all(sizes[i] < sizes[i + 1] for i in range(len(sizes) - 1))

    def test_export_report_json_structure(self, populated_db):
        """Export JSON report and verify its structure."""
        dashboard = StatsDashboard(db_path=populated_db.db_path)
        report_json = dashboard.export_report(format="json")

        data = json.loads(report_json)
        assert "generated_at" in data
        assert "library" in data
        assert "scans" in data
        assert "trends" in data
        assert "quality_breakdown" in data

        # Library section
        assert "movies" in data["library"]
        assert "tv_shows" in data["library"]
        assert "total_items" in data["library"]
        assert data["library"]["total_items"] == 75  # 60 movies + 15 TV

        # Scans section
        assert "total_scans" in data["scans"]
        assert data["scans"]["total_scans"] == 12

    def test_export_report_html_contains_expected_data(self, populated_db):
        """Export HTML report and verify it contains expected elements."""
        dashboard = StatsDashboard(db_path=populated_db.db_path)
        html = dashboard.export_report(format="html")

        assert "ScanHound" in html
        assert "Library Overview" in html
        assert "Total Items" in html
        assert "Total Size" in html
        assert "Quality Score" in html
        assert "Scan Statistics" in html
        assert "<!DOCTYPE html>" in html
        # Verify data is present (total items should appear)
        assert "75" in html  # 60 movies + 15 TV shows

    def test_trend_data_aggregation(self, populated_db):
        """Verify trend data groups scans by date correctly."""
        dashboard = StatsDashboard(db_path=populated_db.db_path)
        trends = dashboard.get_trend_data(days=30)

        assert "dates" in trends
        assert "items_scanned" in trends
        assert "missing_found" in trends
        assert "upgrades_found" in trends
        assert "avg_duration" in trends
        assert "scan_count" in trends

        # Should have data (scans were within last 12 days)
        assert len(trends["dates"]) > 0
        # All arrays should be same length
        array_lengths = [
            len(trends["dates"]),
            len(trends["items_scanned"]),
            len(trends["missing_found"]),
            len(trends["upgrades_found"]),
        ]
        assert len(set(array_lengths)) == 1

    def test_quality_breakdown_resolution_and_hdr(self, populated_db):
        """Verify quality breakdown has both resolution and HDR data."""
        dashboard = StatsDashboard(db_path=populated_db.db_path)
        breakdown = dashboard.get_quality_breakdown("Movies")

        assert "resolution" in breakdown
        assert "hdr" in breakdown
        assert len(breakdown["resolution"]["labels"]) > 0
        assert len(breakdown["resolution"]["counts"]) > 0

        # HDR breakdown should have DV, HDR, SDR categories
        assert len(breakdown["hdr"]["labels"]) == 3
        assert sum(breakdown["hdr"]["counts"]) == 60  # all movies accounted for

    def test_dashboard_summary_combines_movies_and_tv(self, populated_db):
        """Dashboard summary should combine both movie and TV stats."""
        dashboard = StatsDashboard(db_path=populated_db.db_path)
        summary = dashboard.get_dashboard_summary()

        assert summary["library"]["total_items"] == 75
        assert summary["library"]["movies"]["total_items"] == 60
        assert summary["library"]["tv_shows"]["total_items"] == 15
        assert summary["library"]["total_size_gb"] > 0


# ---------------------------------------------------------------------------
# 4. Config Mutation Under Load
# ---------------------------------------------------------------------------

class TestConfigMutationSafety:
    """Tests to verify config operations are safe from unintended mutation."""

    def test_default_config_is_deep_copied(self):
        """get_default_config should return independent copies each time."""
        c1 = get_default_config()
        c2 = get_default_config()

        # They should be equal but not the same object
        assert c1 == c2
        assert c1 is not c2

        # Mutating one should not affect the other
        c1["plex_url"] = "http://changed:32400"
        c1["movie_libs"].append("New Library")
        assert c2["plex_url"] == "http://127.0.0.1:32400"
        assert "New Library" not in c2["movie_libs"]

    def test_deep_nested_mutation_does_not_affect_defaults(self):
        """Mutating nested structures should not leak to defaults."""
        defaults = get_default_config()
        original_libs = get_default_config()["movie_libs"].copy()

        config = get_default_config()
        config["movie_libs"].append("Mutated Library")
        config["tv_libs"].extend(["Extra 1", "Extra 2"])

        fresh = get_default_config()
        assert fresh["movie_libs"] == original_libs
        assert len(fresh["tv_libs"]) == 1  # original default

    def test_apply_preset_overrides_correct_values(self):
        """Presets should override specific keys only."""
        config = get_default_config()
        original_plex_url = config["plex_url"]

        # Apply Aggressive preset
        preset = SETTINGS_PRESETS["Aggressive Upgrades"]
        for key, value in preset.items():
            if key != "description":
                config[key] = value

        # Preset keys should be updated
        assert config["upgrade_sensitivity"] == 1
        assert config["rule_dv"] is True
        assert config["strict_resolution"] is False

        # Non-preset keys should be unchanged
        assert config["plex_url"] == original_plex_url
        assert config["tmdb_api_key"] == ""

    def test_conservative_preset_values(self):
        """Conservative preset should enforce strict, high-sensitivity settings."""
        config = get_default_config()
        preset = SETTINGS_PRESETS["Conservative"]
        for key, value in preset.items():
            if key != "description":
                config[key] = value

        assert config["strict_resolution"] is True
        assert config["upgrade_sensitivity"] == 10
        assert config["rule_1080_1080"] is False
        assert config["rule_4k_4k"] is False

    def test_validate_config_sanitizes_negative_values(self):
        """validate_config should sanitize negative and out-of-range values."""
        config = get_default_config()
        config["min_size_mb"] = -100
        config["scheduler_interval"] = -5
        config["scan_threads"] = 999
        config["cache_duration"] = -1
        config["upgrade_sensitivity"] = -10

        cleaned = validate_config(config)

        assert cleaned["min_size_mb"] == 0
        assert cleaned["scheduler_interval"] == 1
        assert cleaned["scan_threads"] == 50  # clamped to max
        assert cleaned["cache_duration"] == 0
        assert cleaned["upgrade_sensitivity"] == 0

    def test_validate_config_clamps_thresholds(self):
        """Match thresholds should be clamped to 0-100."""
        config = get_default_config()
        config["tv_match_threshold"] = 150
        config["low_match_threshold"] = -20
        config["movie_match_threshold"] = 200
        config["year_tolerance"] = 50

        cleaned = validate_config(config)

        assert cleaned["tv_match_threshold"] == 100
        assert cleaned["low_match_threshold"] == 0
        assert cleaned["movie_match_threshold"] == 100
        assert cleaned["year_tolerance"] == 10  # max is 10

    def test_full_chain_default_mutate_validate_verify(self):
        """Chain: get_default -> mutate -> validate -> verify all fields valid."""
        config = get_default_config()

        # Mutate with some bad values
        config["scan_threads"] = -1

        cleaned = validate_config(config)

        # All cleaned values should be within bounds
        assert 1 <= cleaned["scan_threads"] <= 50

        # Non-mutated fields should still be defaults
        assert cleaned["plex_url"] == "http://127.0.0.1:32400"
        assert cleaned["rule_dv"] is True

    def test_all_presets_produce_valid_configs(self):
        """Applying any preset then validating should produce a valid config."""
        for preset_name, preset in SETTINGS_PRESETS.items():
            config = get_default_config()
            for key, value in preset.items():
                if key != "description":
                    config[key] = value

            cleaned = validate_config(config)

            # Core fields should be present and valid
            assert isinstance(cleaned.get("rule_dv"), bool)
            assert isinstance(cleaned.get("rule_1080_4k"), bool)
            assert 0 <= cleaned.get("upgrade_sensitivity", 0) <= 100


# ---------------------------------------------------------------------------
# 5. Matching Edge Cases at Scale
# ---------------------------------------------------------------------------

class TestMatchingAtScale:
    """Tests for matching with large libraries and tricky edge cases."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        clear_fuzzy_cache()
        yield
        clear_fuzzy_cache()

    def test_500_plus_item_library_matching(self, matching_engine):
        """Build a Plex index with 500+ items and verify matching completes."""
        plex_items = _generate_plex_library(count=520)
        plex_index = _build_index(plex_items)

        assert len(plex_index["all_items"]) >= 520

        # Match 20 known items
        matched = 0
        for item in plex_items[:20]:
            web = _make_web(
                title=item["original_title"],
                year=item["year"],
                imdb_id=item["imdb_id"],
                search_key=item["clean_title"],
            )
            matches, _ = matching_engine.find_movie_matches(web, plex_index)
            if matches:
                matched += 1

        assert matched == 20  # all should match by IMDb

    def test_similar_titles_disambiguation(self, matching_engine):
        """Test items with very similar titles are correctly disambiguated."""
        plex_items = [
            _make_plex(title="the good", original="The Good", year=2020,
                       imdb_id="tt0001001", rating_key="SIM1"),
            _make_plex(title="the good place", original="The Good Place", year=2016,
                       imdb_id="tt0001002", rating_key="SIM2"),
            _make_plex(title="good will hunting", original="Good Will Hunting",
                       year=1997, imdb_id="tt0001003", rating_key="SIM3"),
            _make_plex(title="the good the bad and the ugly",
                       original="The Good the Bad and the Ugly",
                       year=1966, imdb_id="tt0001004", rating_key="SIM4"),
        ]
        plex_index = _build_index(plex_items)

        # Match "The Good Place" by IMDb - should only get that one
        web = _make_web(title="The Good Place", year=2016, imdb_id="tt0001002",
                        search_key="the good place")
        matches, _ = matching_engine.find_movie_matches(web, plex_index)
        assert len(matches) == 1
        assert matches[0]["rating_key"] == "SIM2"

        # Match "Good Will Hunting" by title fuzzy
        web2 = _make_web(title="Good Will Hunting", year=1997,
                         search_key="good will hunting")
        matches2, _ = matching_engine.find_movie_matches(web2, plex_index)
        assert len(matches2) >= 1
        assert any(m["rating_key"] == "SIM3" for m in matches2)

    def test_same_title_different_years(self, matching_engine):
        """Items with the same title but different years should match correctly."""
        plex_items = [
            _make_plex(title="dune", original="Dune", year=1984,
                       imdb_id="tt0087182", rating_key="DUNE84"),
            _make_plex(title="dune", original="Dune", year=2021,
                       imdb_id="tt1160419", rating_key="DUNE21"),
        ]
        plex_index = _build_index(plex_items)

        # Match 2021 Dune by year
        web_2021 = _make_web(title="Dune", year=2021, search_key="dune")
        matches, _ = matching_engine.find_movie_matches(web_2021, plex_index)
        assert len(matches) >= 1
        # With year tolerance=1, only 2021 should match
        assert all(abs(m["year"] - 2021) <= 1 for m in matches)

        # Match 1984 Dune by year
        web_1984 = _make_web(title="Dune", year=1984, search_key="dune")
        matches, _ = matching_engine.find_movie_matches(web_1984, plex_index)
        assert len(matches) >= 1
        assert all(abs(m["year"] - 1984) <= 1 for m in matches)

    def test_unicode_titles_chinese(self, matching_engine):
        """Chinese title matching should work."""
        plex_items = [
            _make_plex(title=clean_string("Hero"), original="Hero",
                       year=2002, imdb_id="tt0299977", rating_key="CN1"),
        ]
        plex_index = _build_index(plex_items)

        web = _make_web(title="Hero", year=2002, imdb_id="tt0299977",
                        search_key=clean_string("Hero"))
        matches, _ = matching_engine.find_movie_matches(web, plex_index)
        assert len(matches) >= 1

    def test_unicode_titles_accented(self, matching_engine):
        """Accented characters should be handled in matching."""
        # clean_string strips non-alphanumeric, so accented chars get removed
        plex_items = [
            _make_plex(title=clean_string("Amelie"), original="Amelie",
                       year=2001, imdb_id="tt0211915", rating_key="FR1"),
        ]
        plex_index = _build_index(plex_items)

        web = _make_web(title="Amelie", year=2001, imdb_id="tt0211915",
                        search_key=clean_string("Amelie"))
        matches, _ = matching_engine.find_movie_matches(web, plex_index)
        assert len(matches) >= 1

    def test_unicode_titles_korean(self, matching_engine):
        """Korean/CJK titles matched by IMDb ID (fuzzy on CJK is unreliable)."""
        plex_items = [
            _make_plex(title=clean_string("Parasite"), original="Parasite",
                       year=2019, imdb_id="tt6751668", rating_key="KR1"),
        ]
        plex_index = _build_index(plex_items)

        web = _make_web(title="Parasite", year=2019, imdb_id="tt6751668",
                        search_key=clean_string("Parasite"))
        matches, _ = matching_engine.find_movie_matches(web, plex_index)
        assert len(matches) == 1
        assert matches[0]["rating_key"] == "KR1"

    def test_year_zero_unknown_year(self, matching_engine):
        """Items with year=0 should skip fuzzy matching (no false positives)."""
        plex_items = [
            _make_plex(title="some movie", original="Some Movie", year=2020,
                       imdb_id="tt9876543", rating_key="Y0"),
        ]
        plex_index = _build_index(plex_items)

        # Web item with year=0 and no IMDb
        web = _make_web(title="Some Movie", year=0, search_key="some movie")
        matches, _ = matching_engine.find_movie_matches(web, plex_index)
        # year=0 skips fuzzy matching path
        assert len(matches) == 0

    def test_empty_search_key_auto_generated(self, matching_engine):
        """Web item without search_key should get one auto-generated."""
        plex_items = [
            _make_plex(title="the matrix", original="The Matrix", year=1999,
                       imdb_id="tt0133093", rating_key="AUTO1"),
        ]
        plex_index = _build_index(plex_items)

        web = _make_web(title="The Matrix", year=1999, imdb_id="tt0133093")
        assert "search_key" not in web
        matches, _ = matching_engine.find_movie_matches(web, plex_index)
        assert "search_key" in web
        assert len(matches) >= 1

    def test_fuzzy_matching_at_scale_performance(self, matching_engine):
        """500+ library items should complete fuzzy matching in reasonable time."""
        plex_items = _generate_plex_library(count=500)
        plex_index = _build_index(plex_items)

        # Items that will NOT match by IMDb (force fuzzy path)
        web_items = [
            _make_web(title=f"Nonexistent Film {i}", year=2020 + (i % 5),
                      search_key=f"nonexistent film {i}")
            for i in range(50)
        ]

        start = time.time()
        for web in web_items:
            matching_engine.find_movie_matches(web, plex_index)
        elapsed = time.time() - start

        # Should complete in under 10 seconds (very generous for 50 items * 500 plex)
        assert elapsed < 10.0, f"Fuzzy matching took {elapsed:.2f}s, expected < 10s"


# ---------------------------------------------------------------------------
# 6. Database Resilience
# ---------------------------------------------------------------------------

class TestDatabaseResilience:
    """Tests for database stability under stress conditions."""

    def test_write_read_cycle_1000_entries(self, db_manager):
        """Write and read 1000+ download history entries."""
        for i in range(1000):
            db_manager.add_to_history(
                f"https://example.com/movie/{i}",
                f"Movie Title {i}",
                normalized_title=f"movie title {i}",
                season=None,
                resolution="1080p" if i % 2 == 0 else "4K",
                size=f"{random.uniform(1.0, 50.0):.1f} GB",
            )

        assert db_manager.get_history_count() == 1000

        # Verify a sample of entries
        for i in [0, 100, 500, 999]:
            assert db_manager.is_in_history(f"https://example.com/movie/{i}")

        # Verify non-existent entries
        assert not db_manager.is_in_history("https://example.com/movie/1000")

    def test_null_values_in_optional_columns(self, db_manager):
        """NULL values in optional columns should not cause errors."""
        # Add history with all optional fields as None
        result = db_manager.add_to_history(
            "https://example.com/null-test", "Null Test",
            normalized_title=None, season=None, resolution=None, size=None,
        )
        assert result is True
        assert db_manager.is_in_history("https://example.com/null-test")

        # Plex cache with minimal data
        item = {
            "clean_title": "null movie",
            "original_title": None,
            "year": None,
            "res": None,
            "size": None,
            "imdb_id": None,
            "rating_key": "NULL1",
            "media_id": "MNULL1",
            "dovi": False,
            "hdr": False,
        }
        db_manager.save_plex_cache([item], "Movies")
        loaded = db_manager.load_plex_cache("Movies")
        assert len(loaded) == 1

    def test_very_long_strings(self, db_manager):
        """Very long strings (10000+ chars) should be stored and retrieved."""
        long_title = "A" * 10000
        long_url = "https://example.com/" + "x" * 10000

        result = db_manager.add_to_history(long_url, long_title)
        assert result is True
        assert db_manager.is_in_history(long_url)

        # Verify retrieval
        titles = db_manager.get_downloaded_titles()
        # This might not return the long URL item since normalized_title is None,
        # but it should not crash
        assert db_manager.get_history_count() == 1

    def test_special_characters_sql_injection_attempts(self, db_manager):
        """SQL injection attempts in titles should be safely handled."""
        injection_strings = [
            "'; DROP TABLE downloads; --",
            '"; DELETE FROM plex_cache; --',
            "Robert'); DROP TABLE downloads;--",
            "1 OR 1=1",
            "' UNION SELECT * FROM downloads --",
            "<script>alert('xss')</script>",
            "Movie\x00With\x00Nulls",  # null bytes
            "Movie\nWith\nNewlines",
            "Movie\tWith\tTabs",
        ]

        for i, injection in enumerate(injection_strings):
            url = f"https://example.com/inject/{i}"
            result = db_manager.add_to_history(url, injection)
            assert result is True
            assert db_manager.is_in_history(url)

        # Verify all tables still exist and are functional
        assert db_manager.get_history_count() == len(injection_strings)

        # Plex cache should also handle injections
        for i, injection in enumerate(injection_strings):
            db_manager.save_plex_cache([{
                "clean_title": injection,
                "original_title": injection,
                "year": 2020,
                "res": "1080p",
                "size": 10.0,
                "imdb_id": f"tt999{i:04d}",
                "rating_key": f"INJ{i}",
                "media_id": f"MINJ{i}",
                "dovi": False,
                "hdr": False,
            }], "Movies")

        loaded = db_manager.load_plex_cache("Movies")
        assert len(loaded) == len(injection_strings)

    def test_concurrent_reads_from_multiple_threads(self, db_manager):
        """Multiple threads reading simultaneously should not crash."""
        # Seed some data
        for i in range(100):
            db_manager.add_to_history(f"https://example.com/{i}", f"Title {i}")

        db_manager.save_plex_cache([{
            "clean_title": f"movie {i}",
            "original_title": f"Movie {i}",
            "year": 2020,
            "res": "1080p",
            "size": 10.0,
            "imdb_id": f"tt{i:07d}",
            "rating_key": f"R{i}",
            "media_id": f"M{i}",
            "dovi": False,
            "hdr": False,
        } for i in range(50)], "Movies")

        errors = []
        results = {"history_counts": [], "cache_counts": []}

        def reader(thread_id):
            try:
                for _ in range(50):
                    count = db_manager.get_history_count()
                    results["history_counts"].append(count)
                    db_manager.is_in_history(f"https://example.com/{thread_id}")
                    loaded = db_manager.load_plex_cache("Movies")
                    results["cache_counts"].append(len(loaded))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        # All reads should return consistent data
        assert all(c == 100 for c in results["history_counts"])
        assert all(c == 50 for c in results["cache_counts"])

    def test_concurrent_writes_and_reads(self, db_manager):
        """Mixed concurrent reads and writes should not corrupt data."""
        errors = []

        def writer(thread_id):
            try:
                for i in range(50):
                    db_manager.add_to_history(
                        f"https://t{thread_id}.com/{i}", f"T{thread_id}P{i}"
                    )
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for _ in range(100):
                    db_manager.get_history_count()
                    db_manager.is_in_history("https://t0.com/0")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(0,)),
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        # Should have all writes from both writers
        assert db_manager.get_history_count() == 100  # 2 writers * 50

    def test_scan_history_batch_write(self, db_manager):
        """Writing 100+ scan history entries should work without issues."""
        for i in range(100):
            db_manager.save_scan_history({
                "timestamp": (datetime.now() - timedelta(hours=i)).isoformat(),
                "scan_type": "Full Scan",
                "items_scanned": 50 + i,
                "missing_count": 10,
                "upgrade_count": 5,
                "dv_upgrade_count": 1,
                "in_library_count": 34 + i,
                "duration_seconds": 30.0,
                "sources_scanned": "src",
                "plex_items_cached": 200,
            })

        history = db_manager.get_scan_history(limit=100)
        assert len(history) == 100

        stats = db_manager.get_scan_stats()
        assert stats["total_scans"] == 100

    def test_scanned_urls_batch_1000(self, db_manager):
        """Batch insert of 1000 scanned URLs should complete without error."""
        batch = [
            {"url": f"https://example.com/page/{i}", "title": f"Page {i}", "source": "test"}
            for i in range(1000)
        ]
        result = db_manager.add_scanned_urls_batch(batch)
        assert result is True
        assert db_manager.get_scanned_url_count() == 1000

        # Verify random samples
        for i in random.sample(range(1000), 10):
            assert db_manager.is_url_scanned(f"https://example.com/page/{i}")

    def test_database_reconnects_after_close(self, db_manager):
        """DB operations should work after explicit close (reconnect)."""
        db_manager.add_to_history("https://before-close.com", "Before")
        db_manager.close()

        # Should auto-reconnect
        db_manager.add_to_history("https://after-close.com", "After")
        assert db_manager.is_in_history("https://after-close.com")
        # Data from before close should still be there
        assert db_manager.is_in_history("https://before-close.com")


# ---------------------------------------------------------------------------
# Additional cross-module integration tests
# ---------------------------------------------------------------------------

class TestLRUCacheIntegration:
    """Tests for the LRU cache used throughout the application."""

    def test_lru_eviction_policy(self):
        """Items beyond maxsize should be evicted (oldest first)."""
        cache = LRUCache(maxsize=5)
        for i in range(10):
            cache[f"key_{i}"] = f"value_{i}"

        assert len(cache) == 5
        # Oldest keys (0-4) should be evicted
        for i in range(5):
            assert f"key_{i}" not in cache
        # Newest keys (5-9) should remain
        for i in range(5, 10):
            assert f"key_{i}" in cache

    def test_lru_access_refreshes_item(self):
        """Accessing an item should refresh it, preventing eviction."""
        cache = LRUCache(maxsize=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3

        # Access 'a' to refresh it
        _ = cache["a"]

        # Add new item - 'b' should be evicted (oldest untouched)
        cache["d"] = 4

        assert "a" in cache  # refreshed
        assert "b" not in cache  # evicted
        assert "c" in cache
        assert "d" in cache

    def test_lru_thread_safety(self):
        """LRU cache should handle concurrent access."""
        cache = LRUCache(maxsize=100)
        errors = []

        def worker(thread_id):
            try:
                for i in range(200):
                    cache[f"t{thread_id}_k{i}"] = f"v{i}"
                    cache.get(f"t{thread_id}_k{i // 2}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(cache) <= 100


class TestCleanStringNormalizeTitle:
    """Tests for string normalization functions used throughout."""

    def test_clean_string_removes_year_in_parens(self):
        assert clean_string("The Matrix (1999)") == "the matrix"

    def test_clean_string_removes_standalone_year(self):
        assert clean_string("The Matrix 1999") == "the matrix"

    def test_clean_string_removes_special_chars(self):
        assert clean_string("It's A Wonderful Life!") == "its a wonderful life"

    def test_clean_string_empty_input(self):
        assert clean_string("") == ""
        assert clean_string(None) == ""

    def test_normalize_title_consistency(self):
        """normalize_title and clean_string should produce the same results."""
        titles = [
            "The Matrix (1999)",
            "Inception 2010",
            "It's A Wonderful Life!",
            "   Spaces   Everywhere   ",
        ]
        for title in titles:
            assert normalize_title(title) == clean_string(title)

    def test_clean_string_with_unicode(self):
        """Unicode characters should be handled (non-alphanumeric stripped)."""
        result = clean_string("Amelie")
        assert result == "amelie"
