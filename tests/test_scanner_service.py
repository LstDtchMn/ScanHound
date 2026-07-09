"""Tests for backend/scanner_service.py — data models, enums, and service logic."""

import asyncio
import pytest
from dataclasses import fields
from unittest.mock import MagicMock, patch

from backend.scanner_service import (
    MediaItem,
    ScanStatus,
    STATUS_COLORS,
    STATUS_TEXTS,
    ScannerService,
    WatchlistItem,
)


# ── ScanStatus enum ──────────────────────────────────────────────────

class TestScanStatus:
    """Verify ScanStatus enum members and their string values."""

    def test_missing_value(self):
        assert ScanStatus.MISSING.value == "missing"

    def test_downloaded_value(self):
        assert ScanStatus.DOWNLOADED.value == "downloaded"

    def test_in_library_value(self):
        assert ScanStatus.IN_LIBRARY.value == "in_library"

    def test_upgrade_value(self):
        assert ScanStatus.UPGRADE.value == "upgrade"

    def test_dv_upgrade_value(self):
        assert ScanStatus.DV_UPGRADE.value == "dv_upgrade"

    def test_enum_has_expected_members(self):
        assert len(ScanStatus) == 7
        assert ScanStatus.DOWNLOADED_SIMILAR.value == "downloaded_similar"

    def test_enum_round_trips_from_value(self):
        for member in ScanStatus:
            assert ScanStatus(member.value) is member


# ── STATUS_COLORS / STATUS_TEXTS mappings ────────────────────────────

class TestStatusMappings:
    """Ensure every ScanStatus member has a colour and text entry."""

    def test_status_colors_covers_all_members(self):
        for member in ScanStatus:
            assert member in STATUS_COLORS, f"STATUS_COLORS missing {member}"

    def test_status_texts_covers_all_members(self):
        for member in ScanStatus:
            assert member in STATUS_TEXTS, f"STATUS_TEXTS missing {member}"

    def test_missing_color_is_red(self):
        assert STATUS_COLORS[ScanStatus.MISSING] == "#e74c3c"

    def test_downloaded_color_is_info_blue(self):
        assert STATUS_COLORS[ScanStatus.DOWNLOADED] == "#17a2b8"

    def test_in_library_color_is_green(self):
        assert STATUS_COLORS[ScanStatus.IN_LIBRARY] == "#27ae60"

    def test_upgrade_color_is_orange(self):
        assert STATUS_COLORS[ScanStatus.UPGRADE] == "#f39c12"

    def test_dv_upgrade_color_is_purple(self):
        assert STATUS_COLORS[ScanStatus.DV_UPGRADE] == "#9b59b6"

    def test_in_library_text_has_check_mark(self):
        assert "\u2713" in STATUS_TEXTS[ScanStatus.IN_LIBRARY]

    def test_dv_upgrade_text_contains_dv(self):
        assert "DV" in STATUS_TEXTS[ScanStatus.DV_UPGRADE]


# ── MediaItem dataclass ──────────────────────────────────────────────

class TestMediaItem:
    """Test MediaItem dataclass fields and defaults."""

    def test_required_fields_only(self):
        item = MediaItem(id="item_0", title="Test", year=2024)
        assert item.id == "item_0"
        assert item.title == "Test"
        assert item.year == 2024

    def test_default_season_is_none(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.season is None

    def test_default_episodes_is_none(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.episodes is None

    def test_default_rating_is_zero(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.rating == 0.0

    def test_default_status_is_missing(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.status == ScanStatus.MISSING

    def test_default_color_is_red(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.color == "#e74c3c"

    def test_default_genres_is_empty_list(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.genres == []

    def test_default_web_data_is_empty_dict(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.web_data == {}

    def test_group_key_field_exists(self):
        item = MediaItem(id="x", title="X", year=2024, group_key="test|S0")
        assert item.group_key == "test|S0"

    def test_is_duplicate_group_defaults_false(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.is_duplicate_group is False

    def test_prior_grab_defaults_none(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.prior_grab is None

    def test_plex_info_default(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.plex_info == "-"

    def test_host_pref_default(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.host_pref == "RG"

    def test_all_expected_field_names(self):
        expected = {
            "id", "title", "year", "season", "episodes", "rating", "votes",
            "votes_source", "rt_score", "status", "status_text", "color",
            "resolution", "size", "hdr", "dovi", "genres", "language", "url",
            "plex_info", "plex_versions", "plex_rating_key", "selected",
            "host_pref", "poster_path", "imdb_id",
            "tile_state", "description", "posted_date", "web_data", "group_key",
            "is_duplicate_group", "prior_grab", "category",
        }
        actual = {f.name for f in fields(MediaItem)}
        assert actual == expected

    def test_plex_rating_key_defaults_none(self):
        item = MediaItem(id="x", title="X", year=2024)
        assert item.plex_rating_key is None


# ── WatchlistItem dataclass (scanner_service variant) ────────────────

class TestScannerWatchlistItem:
    """Test the WatchlistItem defined in scanner_service.py."""

    def test_required_fields(self):
        item = WatchlistItem(tmdb_id=42, media_type="movie", title="Test", year=2024)
        assert item.tmdb_id == 42
        assert item.media_type == "movie"
        assert item.title == "Test"
        assert item.year == 2024

    def test_optional_defaults(self):
        item = WatchlistItem(tmdb_id=1, media_type="tv", title="T", year=0)
        assert item.poster_path is None
        assert item.overview == ""
        assert item.rating == 0.0
        assert item.language == ""
        assert item.genres == []
        assert item.added_date == ""
        assert item.priority == 0
        assert item.notes == ""
        assert item.in_plex is False
        assert item.web_data == {}
        assert item.group_key == ""
        assert item.is_duplicate_group is False


# ── Helper: build a mocked ScannerService ────────────────────────────

def _make_service(**overrides):
    """Create a ScannerService with fully mocked dependencies."""
    config = overrides.get("config", {"tmdb_api_key": "", "omdb_api_key": ""})
    db = overrides.get("db", MagicMock())
    scrapers = overrides.get("scrapers", MagicMock())
    matching = overrides.get("matching", MagicMock())
    plex_service = overrides.get("plex_service", MagicMock())

    svc = ScannerService(
        config=config,
        db=db,
        scrapers=scrapers,
        matching=matching,
        plex_service=plex_service,
    )
    return svc


# ── ScannerService._create_media_item ────────────────────────────────

class TestCreateMediaItem:
    """Test the per-episode size calculation and basic item creation."""

    def _call(self, details, url="http://example.com/item"):
        svc = _make_service()
        svc.download_history = set()
        svc._downloaded_titles_lookup = {}
        result = {"details": details, "url": url, "is_tv": details.get("is_tv", False)}
        return svc._create_media_item(result)

    def test_basic_movie_creation(self):
        details = {
            "display_title": "Blade Runner 2049",
            "year": 2017,
            "size": "55 GB",
            "res": "4K",
            "hdr": "HDR10",
            "dovi": False,
            "genres": ["Sci-Fi"],
            "language": "English",
        }
        item = self._call(details)
        assert item is not None
        assert item.title == "Blade Runner 2049"
        assert item.year == 2017
        assert item.size == "55 GB"

    def test_tv_pack_per_episode_size_gb(self):
        details = {
            "display_title": "Breaking Bad",
            "year": 2008,
            "season": 1,
            "episodes": 10,
            "size": "45.5 GB",
            "res": "1080p",
            "hdr": "SDR",
            "dovi": False,
        }
        item = self._call(details)
        assert item is not None
        assert "~4.5 GB/ep" in item.size
        assert item.size.startswith("45.5 GB")

    def test_tv_pack_per_episode_size_tb(self):
        details = {
            "display_title": "Some Show",
            "year": 2020,
            "season": 2,
            "episodes": 8,
            "size": "1 TB",
            "res": "4K",
            "hdr": "HDR10",
            "dovi": False,
        }
        item = self._call(details)
        assert item is not None
        # 1 TB = 1024 GB, 1024 / 8 = 128 GB/ep
        assert "~128.0 GB/ep" in item.size


    def test_tv_pack_per_episode_size_mb(self):
        details = {
            "display_title": "Short Show",
            "year": 2019,
            "season": 1,
            "episodes": 4,
            "size": "2048 MB",
            "res": "720p",
            "hdr": "SDR",
            "dovi": False,
        }
        item = self._call(details)
        assert item is not None
        # 2048 MB = 2 GB, 2 / 4 = 0.5 GB/ep
        assert "~0.5 GB/ep" in item.size

    def test_single_episode_no_per_ep_suffix(self):
        details = {
            "display_title": "Show EP1",
            "year": 2021,
            "season": 1,
            "episodes": 1,
            "size": "4 GB",
            "res": "1080p",
            "hdr": "SDR",
            "dovi": False,
        }
        item = self._call(details)
        assert item is not None
        assert "GB/ep" not in item.size

    def test_missing_episodes_no_per_ep(self):
        details = {
            "display_title": "No Ep Info",
            "year": 2021,
            "season": 1,
            "episodes": None,
            "size": "20 GB",
            "res": "1080p",
            "hdr": "SDR",
            "dovi": False,
        }
        item = self._call(details)
        assert item is not None
        assert "GB/ep" not in item.size

    def test_unknown_size_skips_per_ep(self):
        details = {
            "display_title": "Unknown Sz",
            "year": 2021,
            "season": 1,
            "episodes": 5,
            "size": "?",
            "res": "1080p",
            "hdr": "SDR",
            "dovi": False,
        }
        item = self._call(details)
        assert item is not None
        assert item.size == "?"

    def test_downloaded_url_sets_downloaded_status(self):
        svc = _make_service()
        svc.download_history = {"http://example.com/dl"}
        svc._downloaded_titles_lookup = {}
        result = {
            "details": {
                "display_title": "Downloaded Movie",
                "year": 2023,
                "size": "10 GB",
                "res": "4K",
                "hdr": "SDR",
                "dovi": False,
            },
            "url": "http://example.com/dl",
            "is_tv": False,
        }
        item = svc._create_media_item(result)
        assert item is not None
        assert item.status == ScanStatus.DOWNLOADED
        assert item.status_text == STATUS_TEXTS[ScanStatus.DOWNLOADED]
        assert item.color == STATUS_COLORS[ScanStatus.DOWNLOADED]

    def test_group_key_format_for_movie(self):
        details = {
            "display_title": "Dune Part Two",
            "year": 2024,
            "size": "60 GB",
            "res": "4K",
            "hdr": "HDR10",
            "dovi": True,
        }
        item = self._call(details)
        assert item is not None
        assert "|S0" in item.group_key

    def test_group_key_format_for_tv(self):
        details = {
            "display_title": "The Bear",
            "year": 2022,
            "season": 3,
            "episodes": 10,
            "size": "30 GB",
            "res": "1080p",
            "hdr": "SDR",
            "dovi": False,
        }
        item = self._call(details)
        assert item is not None
        assert "|S3" in item.group_key


class TestAssignGroupKeys:
    """ScannerService._assign_group_keys rebuilds group_key from the CURRENT
    (post-enrichment) title/year/season, using the uniform recipe
    {normalized_title}|{year or 0}|S{season or 0} — so a title that enrichment
    corrected regroups under the right key instead of a frozen bogus one."""

    def _svc(self, items):
        svc = _make_service()
        svc.items = items
        return svc

    def test_rebuilds_from_corrected_title(self):
        # Guacamole case: parse froze a garbage key ("gua-killingfaith"),
        # enrichment fixed the title to "Killing Faith".
        svc = self._svc([
            MediaItem(id="a", title="Killing Faith", year=2025, season=None,
                      group_key="guakillingfaith|2025|S0"),
        ])
        svc._assign_group_keys()
        assert svc.items[0].group_key == "killing faith|2025|S0"

    def test_two_releases_same_title_merge_to_one_group(self):
        svc = self._svc([
            MediaItem(id="a", title="Killing Faith", year=2025,
                      group_key="guakillingfaith|2025|S0"),
            MediaItem(id="b", title="Killing Faith", year=2025,
                      group_key="killing faith|2025|S0"),
        ])
        svc._assign_group_keys()
        assert svc.items[0].group_key == svc.items[1].group_key == "killing faith|2025|S0"

    def test_movie_and_tv_uniform_format(self):
        svc = self._svc([
            MediaItem(id="m", title="Dune", year=2021, season=None),
            MediaItem(id="t", title="The Bear", year=2022, season=3),
        ])
        svc._assign_group_keys()
        assert svc.items[0].group_key == "dune|2021|S0"
        assert svc.items[1].group_key == "the bear|2022|S3"

    def test_missing_year_normalizes_to_zero(self):
        svc = self._svc([MediaItem(id="x", title="Some Show", year=0, season=1)])
        svc._assign_group_keys()
        assert svc.items[0].group_key == "some show|0|S1"


# ── ScannerService.detect_duplicate_groups ───────────────────────────

class TestMatchAgainstPlex:
    def test_movie_match_stores_selected_plex_rating_key(self):
        svc = _make_service()
        svc.plex.plex_index = {"all_items": [{"rating_key": "new-plex-key"}], "by_imdb": {}, "by_title": {}}
        svc.items = [
            MediaItem(
                id="item_0",
                title="Anaconda",
                year=2025,
                resolution="4K",
                size="20 GB",
                imdb_id="tt1234567",
                web_data={"imdb_id": "tt1234567", "size": "20 GB"},
            )
        ]
        svc.matching.find_movie_matches.return_value = (
            [{"rating_key": "new-plex-key", "imdb_id": "tt1234567", "res": "4K", "size": 20.0}],
            False,
        )
        svc.matching.calculate_movie_upgrade_status.return_value = (
            "IN_LIBRARY",
            "#27ae60",
            "Have 20GB [4K]",
            "new-plex-key",
        )

        asyncio.run(svc._match_against_plex())

        assert svc.items[0].plex_rating_key == "new-plex-key"

    def test_downloaded_similar_sibling_not_reclassified_as_upgrade(self):
        """Regression test: a DOWNLOADED_SIMILAR sibling of a just-grabbed
        release must NOT be re-classified to UPGRADE, even when its size
        would clear the upgrade threshold vs. the (stale, pre-import) Plex
        copy. See scanner_service.py _match_against_plex skip guard."""
        svc = _make_service()
        svc.plex.plex_index = {"all_items": [{"rating_key": "stale-plex-key"}], "by_imdb": {}, "by_title": {}}
        svc.items = [
            MediaItem(
                id="item_0",
                title="Anaconda",
                year=2025,
                resolution="4K",
                size="40 GB",
                status=ScanStatus.DOWNLOADED_SIMILAR,
                status_text=STATUS_TEXTS[ScanStatus.DOWNLOADED_SIMILAR],
                color=STATUS_COLORS[ScanStatus.DOWNLOADED_SIMILAR],
                imdb_id="tt1234567",
                web_data={"imdb_id": "tt1234567", "size": "40 GB"},
            )
        ]
        # Stale Plex copy is much smaller (e.g. 10 GB) — size delta would
        # clear the upgrade threshold if the matching engine were consulted.
        svc.matching.find_movie_matches.return_value = (
            [{"rating_key": "stale-plex-key", "imdb_id": "tt1234567", "res": "4K", "size": 10.0}],
            False,
        )
        svc.matching.calculate_movie_upgrade_status.return_value = (
            "UPGRADE",
            "#f39c12",
            "Have 10GB [4K] -> 40GB [4K]",
            "stale-plex-key",
        )

        asyncio.run(svc._match_against_plex())

        assert svc.items[0].status == ScanStatus.DOWNLOADED_SIMILAR
        # The matching engine must not even be consulted for a skipped item.
        svc.matching.calculate_movie_upgrade_status.assert_not_called()


class TestDetectDuplicateGroups:
    """Test grouping logic for duplicate and multi-season items."""

    @staticmethod
    def _make_item(title, season=None, resolution="4K", idx=0):
        return MediaItem(
            id=f"item_{idx}",
            title=title,
            year=2024,
            season=season,
            resolution=resolution,
        )

    def test_single_movie_not_grouped(self):
        svc = _make_service()
        items = [self._make_item("Unique Movie")]
        svc.detect_duplicate_groups(items)
        assert items[0].is_duplicate_group is False

    def test_two_movies_same_title_grouped(self):
        svc = _make_service()
        items = [
            self._make_item("Dune", resolution="1080p", idx=0),
            self._make_item("Dune", resolution="4K", idx=1),
        ]
        svc.detect_duplicate_groups(items)
        assert items[0].is_duplicate_group is True
        assert items[1].is_duplicate_group is True
        assert items[0].group_key == items[1].group_key

    def test_tv_same_season_grouped(self):
        svc = _make_service()
        items = [
            self._make_item("Show X", season=1, resolution="1080p", idx=0),
            self._make_item("Show X", season=1, resolution="4K", idx=1),
        ]
        svc.detect_duplicate_groups(items)
        assert items[0].is_duplicate_group is True
        assert items[0].group_key == items[1].group_key

    def test_tv_different_seasons_grouped_under_tv_key(self):
        svc = _make_service()
        items = [
            self._make_item("Show Y", season=1, idx=0),
            self._make_item("Show Y", season=2, idx=1),
        ]
        svc.detect_duplicate_groups(items)
        assert items[0].is_duplicate_group is True
        assert "|TV" in items[0].group_key

    def test_tv_single_season_single_item_not_grouped(self):
        svc = _make_service()
        items = [self._make_item("Show Z", season=3)]
        svc.detect_duplicate_groups(items)
        assert items[0].is_duplicate_group is False

    def test_mixed_movie_and_tv_separate_groups(self):
        svc = _make_service()
        movie = self._make_item("Title A", season=None, idx=0)
        tv = self._make_item("Title A", season=1, idx=1)
        svc.detect_duplicate_groups([movie, tv])
        # They should be in different groups because one is a movie, one is TV
        assert movie.group_key != tv.group_key

    def test_expanded_groups_updated(self):
        svc = _make_service()
        items = [
            self._make_item("Dupe", idx=0),
            self._make_item("Dupe", idx=1),
        ]
        svc.detect_duplicate_groups(items)
        # There should be at least one expanded group registered
        assert len(svc.expanded_groups) >= 1

    def test_grouped_items_dict_populated(self):
        svc = _make_service()
        items = [
            self._make_item("Film", idx=0),
            self._make_item("Film", idx=1),
        ]
        svc.detect_duplicate_groups(items)
        assert len(svc.grouped_items) >= 1
        # The group should contain both items
        for key, group in svc.grouped_items.items():
            if len(group) == 2:
                assert group[0].title == "Film"
                assert group[1].title == "Film"
                break
        else:
            pytest.fail("No group with 2 items found")
