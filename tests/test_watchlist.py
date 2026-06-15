"""Tests for backend/watchlist.py — WatchlistItem, enums, and WatchlistManager."""

import json
import os
import sqlite3
from datetime import datetime

import pytest

from backend.watchlist import (
    WatchlistItem,
    WatchlistItemStatus,
    WatchlistItemType,
    WatchlistManager,
)


# ── Enums ────────────────────────────────────────────────────────────

class TestWatchlistItemType:

    def test_movie_value(self):
        assert WatchlistItemType.MOVIE.value == "movie"

    def test_tv_show_value(self):
        assert WatchlistItemType.TV_SHOW.value == "tv_show"

    def test_tv_season_value(self):
        assert WatchlistItemType.TV_SEASON.value == "tv_season"

    def test_member_count(self):
        assert len(WatchlistItemType) == 3

    def test_round_trip_from_value(self):
        for member in WatchlistItemType:
            assert WatchlistItemType(member.value) is member


class TestWatchlistItemStatus:

    def test_wanted_value(self):
        assert WatchlistItemStatus.WANTED.value == "wanted"

    def test_found_value(self):
        assert WatchlistItemStatus.FOUND.value == "found"

    def test_downloaded_value(self):
        assert WatchlistItemStatus.DOWNLOADED.value == "downloaded"

    def test_in_library_value(self):
        assert WatchlistItemStatus.IN_LIBRARY.value == "in_library"

    def test_member_count(self):
        assert len(WatchlistItemStatus) == 4


# ── WatchlistItem dataclass ──────────────────────────────────────────

class TestWatchlistItemDataclass:

    def test_defaults(self):
        item = WatchlistItem()
        assert item.id == 0
        assert item.title == ""
        assert item.year is None
        assert item.imdb_id is None
        assert item.item_type == WatchlistItemType.MOVIE
        assert item.status == WatchlistItemStatus.WANTED
        assert item.season is None
        assert item.min_resolution is None
        assert item.prefer_dovi is False
        assert item.notes == ""
        assert item.found_date is None
        assert item.found_url is None
        assert item.priority == 1

    def test_to_dict_keys(self):
        d = WatchlistItem().to_dict()
        expected = {
            "id", "title", "year", "imdb_id", "tmdb_id", "item_type",
            "status", "season", "min_resolution", "prefer_dovi", "notes",
            "added_date", "found_date", "found_url", "priority",
        }
        assert set(d.keys()) == expected

    def test_to_dict_enum_serialized_as_value(self):
        d = WatchlistItem().to_dict()
        assert d["item_type"] == "movie"
        assert d["status"] == "wanted"

    def test_to_dict_found_date_none(self):
        d = WatchlistItem().to_dict()
        assert d["found_date"] is None

    def test_to_dict_found_date_iso(self):
        dt = datetime(2026, 1, 15, 12, 30, 0)
        item = WatchlistItem(found_date=dt)
        d = item.to_dict()
        assert d["found_date"] == "2026-01-15T12:30:00"

    def test_from_dict_round_trip(self):
        original = WatchlistItem(
            id=5,
            title="Dune: Part Two",
            year=2024,
            imdb_id="tt15239678",
            tmdb_id="693134",
            item_type=WatchlistItemType.MOVIE,
            status=WatchlistItemStatus.WANTED,
            min_resolution="4K",
            prefer_dovi=True,
            notes="Must have DV",
            priority=3,
        )
        d = original.to_dict()
        restored = WatchlistItem.from_dict(d)
        assert restored.title == original.title
        assert restored.year == original.year
        assert restored.imdb_id == original.imdb_id
        assert restored.item_type == original.item_type
        assert restored.status == original.status
        assert restored.min_resolution == original.min_resolution
        assert restored.prefer_dovi == original.prefer_dovi
        assert restored.priority == original.priority

    def test_from_dict_minimal(self):
        item = WatchlistItem.from_dict({"title": "Minimal", "year": 2020})
        assert item.title == "Minimal"
        assert item.year == 2020
        assert item.item_type == WatchlistItemType.MOVIE


# ── WatchlistManager with tmp_path DB ────────────────────────────────

@pytest.fixture
def manager(tmp_path):
    """Create a WatchlistManager backed by a temp DB."""
    db_path = str(tmp_path / "watchlist_test.db")
    mgr = WatchlistManager(db_path=db_path)
    yield mgr
    mgr.close()


def _make_item(**kwargs):
    """Helper to build a WatchlistItem with sensible defaults."""
    defaults = dict(
        title="Test Movie",
        year=2024,
        item_type=WatchlistItemType.MOVIE,
        status=WatchlistItemStatus.WANTED,
        priority=2,
    )
    defaults.update(kwargs)
    return WatchlistItem(**defaults)


class TestWatchlistManagerAdd:

    def test_add_returns_id(self, manager):
        item = _make_item(title="First Item")
        item_id = manager.add(item)
        assert isinstance(item_id, int)
        assert item_id > 0

    def test_add_deduplicates_by_imdb_id(self, manager):
        item1 = _make_item(title="Movie A", imdb_id="tt1234567")
        item2 = _make_item(title="Movie A duplicate", imdb_id="tt1234567")
        id1 = manager.add(item1)
        id2 = manager.add(item2)
        assert id1 == id2  # same ID returned, no duplicate inserted

    def test_add_allows_different_imdb_ids(self, manager):
        id1 = manager.add(_make_item(title="A", imdb_id="tt0000001"))
        id2 = manager.add(_make_item(title="B", imdb_id="tt0000002"))
        assert id1 != id2

    def test_add_without_imdb_id_dedup_by_title_year_type(self, manager):
        id1 = manager.add(_make_item(title="Same Title"))
        id2 = manager.add(_make_item(title="Same Title"))
        assert id1 == id2  # dedup by title+year+item_type+season when no imdb_id

    def test_add_without_imdb_id_different_season_no_dedup(self, manager):
        item1 = _make_item(title="Same Title")
        item1.season = 1
        item2 = _make_item(title="Same Title")
        item2.season = 2
        id1 = manager.add(item1)
        id2 = manager.add(item2)
        assert id1 != id2  # different seasons should not dedup


class TestWatchlistManagerGet:

    def test_get_by_id(self, manager):
        item = _make_item(title="Findable")
        item_id = manager.add(item)
        fetched = manager.get(item_id)
        assert fetched is not None
        assert fetched.title == "Findable"

    def test_get_nonexistent_returns_none(self, manager):
        assert manager.get(99999) is None


class TestWatchlistManagerGetAll:

    def test_get_all_returns_all(self, manager):
        manager.add(_make_item(title="A"))
        manager.add(_make_item(title="B"))
        manager.add(_make_item(title="C"))
        assert len(manager.get_all()) == 3

    def test_get_all_filter_by_status(self, manager):
        manager.add(_make_item(title="Wanted", status=WatchlistItemStatus.WANTED))
        manager.add(_make_item(title="Found", status=WatchlistItemStatus.FOUND))
        wanted = manager.get_all(status=WatchlistItemStatus.WANTED)
        assert len(wanted) == 1
        assert wanted[0].title == "Wanted"

    def test_get_all_filter_by_type(self, manager):
        manager.add(_make_item(title="Movie", item_type=WatchlistItemType.MOVIE))
        manager.add(_make_item(title="TV", item_type=WatchlistItemType.TV_SHOW))
        movies = manager.get_all(item_type=WatchlistItemType.MOVIE)
        assert len(movies) == 1
        assert movies[0].title == "Movie"

    def test_get_all_ordered_by_priority_desc(self, manager):
        manager.add(_make_item(title="Low", priority=1))
        manager.add(_make_item(title="High", priority=3))
        manager.add(_make_item(title="Med", priority=2))
        items = manager.get_all()
        assert items[0].title == "High"


class TestWatchlistManagerGetWanted:

    def test_get_wanted(self, manager):
        manager.add(_make_item(title="W1", status=WatchlistItemStatus.WANTED))
        manager.add(_make_item(title="W2", status=WatchlistItemStatus.WANTED))
        manager.add(_make_item(title="F1", status=WatchlistItemStatus.FOUND))
        assert len(manager.get_wanted()) == 2


class TestWatchlistManagerRemove:

    def test_remove_deletes_item(self, manager):
        item_id = manager.add(_make_item(title="Doomed"))
        manager.remove(item_id)
        assert manager.get(item_id) is None

    def test_remove_nonexistent_no_error(self, manager):
        manager.remove(99999)  # should not raise


class TestWatchlistManagerSearch:

    def test_search_finds_substring(self, manager):
        manager.add(_make_item(title="The Matrix Reloaded"))
        manager.add(_make_item(title="Matrix Revolutions"))
        manager.add(_make_item(title="Inception"))
        results = manager.search("Matrix")
        assert len(results) == 2

    def test_search_case_insensitive(self, manager):
        manager.add(_make_item(title="Blade Runner 2049"))
        results = manager.search("blade runner")
        assert len(results) == 1

    def test_search_escapes_sql_wildcards(self, manager):
        manager.add(_make_item(title="100% Pure"))
        manager.add(_make_item(title="Pure Movie"))
        # The % should be escaped, so it only matches the literal "100%"
        results = manager.search("100%")
        assert len(results) == 1
        assert results[0].title == "100% Pure"

    def test_search_escapes_underscore(self, manager):
        manager.add(_make_item(title="file_name"))
        manager.add(_make_item(title="filename"))
        results = manager.search("file_name")
        assert len(results) == 1
        assert results[0].title == "file_name"


class TestWatchlistManagerFindByImdb:

    def test_find_existing(self, manager):
        manager.add(_make_item(title="Inception", imdb_id="tt1375666"))
        item = manager.find_by_imdb("tt1375666")
        assert item is not None
        assert item.title == "Inception"

    def test_find_missing_returns_none(self, manager):
        assert manager.find_by_imdb("tt0000000") is None


class TestWatchlistManagerMarkFound:

    def test_mark_found_updates_status_and_date(self, manager):
        item_id = manager.add(_make_item(title="Wanted Item"))
        manager.mark_found(item_id, url="http://example.com/found")
        updated = manager.get(item_id)
        assert updated.status == WatchlistItemStatus.FOUND
        assert updated.found_url == "http://example.com/found"
        assert updated.found_date is not None

    def test_mark_found_custom_status(self, manager):
        item_id = manager.add(_make_item(title="DL Item"))
        manager.mark_found(item_id, url="http://dl.com", auto_status=WatchlistItemStatus.DOWNLOADED)
        updated = manager.get(item_id)
        assert updated.status == WatchlistItemStatus.DOWNLOADED


class TestCheckAgainstScanResults:

    def test_imdb_match(self, manager):
        manager.add(_make_item(title="Dune", imdb_id="tt1160419"))
        scan_items = [
            {"imdb_id": "tt1160419", "display_title": "Dune Part Two", "year": 2024, "res": "4K", "dovi": False},
        ]
        matches = manager.check_against_scan_results(scan_items)
        assert len(matches) == 1
        assert matches[0][0].imdb_id == "tt1160419"

    def test_fuzzy_title_match(self, manager):
        manager.add(_make_item(title="The Shawshank Redemption", year=1994))
        scan_items = [
            {"display_title": "The Shawshank Redemption", "year": 1994, "res": "4K", "dovi": False},
        ]
        matches = manager.check_against_scan_results(scan_items, fuzzy_threshold=80)
        assert len(matches) == 1

    def test_no_match_different_title(self, manager):
        manager.add(_make_item(title="Inception", year=2010))
        scan_items = [
            {"display_title": "The Matrix", "year": 1999, "res": "4K", "dovi": False},
        ]
        matches = manager.check_against_scan_results(scan_items)
        assert len(matches) == 0

    def test_resolution_filter_rejects_low_res(self, manager):
        manager.add(_make_item(title="High Res Only", year=2024, min_resolution="4K"))
        scan_items = [
            {"display_title": "High Res Only", "year": 2024, "res": "1080p", "dovi": False},
        ]
        matches = manager.check_against_scan_results(scan_items)
        assert len(matches) == 0

    def test_resolution_filter_accepts_matching_res(self, manager):
        manager.add(_make_item(title="High Res Only", year=2024, min_resolution="4K"))
        scan_items = [
            {"display_title": "High Res Only", "year": 2024, "res": "4K", "dovi": False},
        ]
        matches = manager.check_against_scan_results(scan_items)
        assert len(matches) == 1

    def test_season_filter(self, manager):
        manager.add(_make_item(
            title="Breaking Bad",
            year=2008,
            item_type=WatchlistItemType.TV_SEASON,
            season=3,
        ))
        scan_items = [
            {"display_title": "Breaking Bad", "year": 2008, "res": "1080p", "dovi": False, "season": 1, "is_tv": True},
            {"display_title": "Breaking Bad", "year": 2008, "res": "1080p", "dovi": False, "season": 3, "is_tv": True},
        ]
        matches = manager.check_against_scan_results(scan_items)
        assert len(matches) == 1
        assert matches[0][1]["season"] == 3

    def test_empty_wanted_returns_empty(self, manager):
        # No items added, so get_wanted returns []
        matches = manager.check_against_scan_results([{"display_title": "X", "year": 2024, "res": "4K", "dovi": False}])
        assert matches == []


class TestImportExportJson:

    def test_export_then_import_round_trip(self, manager):
        manager.add(_make_item(title="Film A", imdb_id="tt0000001"))
        manager.add(_make_item(title="Film B", imdb_id="tt0000002"))

        exported = manager.export_to_json()
        data = json.loads(exported)
        assert data["count"] == 2

        # Create a fresh manager for the import
        manager.clear()
        assert len(manager.get_all()) == 0

        count = manager.import_from_json(exported)
        assert count == 2
        assert len(manager.get_all()) == 2

    def test_import_from_json_list_format(self, manager):
        payload = json.dumps([
            {"title": "Standalone", "year": 2020, "item_type": "movie", "status": "wanted"},
        ])
        count = manager.import_from_json(payload)
        assert count == 1

    def test_import_invalid_json_returns_zero(self, manager):
        count = manager.import_from_json("not json at all {{{")
        assert count == 0

    def test_export_contains_expected_at_key(self, manager):
        exported = manager.export_to_json()
        data = json.loads(exported)
        assert "exported_at" in data
        assert "items" in data


class TestGetStats:

    def test_stats_total_count(self, manager):
        manager.add(_make_item(title="A"))
        manager.add(_make_item(title="B"))
        stats = manager.get_stats()
        assert stats["total"] == 2

    def test_stats_by_status(self, manager):
        manager.add(_make_item(title="W", status=WatchlistItemStatus.WANTED))
        manager.add(_make_item(title="F", status=WatchlistItemStatus.FOUND))
        stats = manager.get_stats()
        assert stats["by_status"]["wanted"] == 1
        assert stats["by_status"]["found"] == 1

    def test_stats_by_type(self, manager):
        manager.add(_make_item(title="M", item_type=WatchlistItemType.MOVIE))
        manager.add(_make_item(title="T", item_type=WatchlistItemType.TV_SHOW))
        stats = manager.get_stats()
        assert stats["by_type"]["movie"] == 1
        assert stats["by_type"]["tv_show"] == 1

    def test_stats_empty(self, manager):
        stats = manager.get_stats()
        assert stats["total"] == 0


class TestClear:

    def test_clear_all(self, manager):
        manager.add(_make_item(title="X"))
        manager.add(_make_item(title="Y"))
        manager.clear()
        assert len(manager.get_all()) == 0

    def test_clear_by_status(self, manager):
        manager.add(_make_item(title="W", status=WatchlistItemStatus.WANTED))
        manager.add(_make_item(title="F", status=WatchlistItemStatus.FOUND))
        manager.clear(status=WatchlistItemStatus.FOUND)
        remaining = manager.get_all()
        assert len(remaining) == 1
        assert remaining[0].title == "W"


class TestResolutionOrder:

    def test_resolution_order_values(self):
        order = WatchlistManager.RESOLUTION_ORDER
        assert order["720p"] < order["1080p"] < order["4K"]

    def test_resolution_order_has_three_entries(self):
        assert len(WatchlistManager.RESOLUTION_ORDER) == 3
