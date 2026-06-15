"""Comprehensive tests for backend/auto_grab_service.py module.

Covers:
- AutoGrabService initialization and configuration
- evaluate_item: rating, votes, genre, language, status filtering
- process_items: full pipeline with mocked download service
- AutoGrabReport correctness
- Edge cases: empty items, disabled service, all items skipped
- CSV config parsing
"""

from unittest.mock import MagicMock, patch, call

import pytest

from backend.auto_grab_service import AutoGrabService, AutoGrabReport
from backend.scanner_service import MediaItem, ScanStatus


# ── Helpers ──────────────────────────────────────────────────────────

def _make_item(**kwargs):
    """Build a MediaItem with sensible defaults, overriding with kwargs."""
    defaults = {
        "id": "item_0",
        "title": "Test Movie",
        "year": 2024,
        "rating": 7.5,
        "votes": 50000,
        "status": ScanStatus.MISSING,
        "url": "http://example.com/movie",
        "resolution": "4K",
        "size": "50 GB",
        "genres": ["Action", "Sci-Fi"],
        "language": "English",
    }
    defaults.update(kwargs)
    return MediaItem(**defaults)


def _make_service(config=None, dl_service=None):
    """Build an AutoGrabService with mocked dependencies."""
    cfg = {
        "auto_grab_enabled": True,
        "auto_grab_min_rating": 0.0,
        "auto_grab_min_votes": 0,
        "auto_grab_genres": "",
        "auto_grab_exclude_genres": "",
        "auto_grab_languages": "",
        "auto_grab_statuses": "missing,upgrade,dv_upgrade",
    }
    if config:
        cfg.update(config)
    download = dl_service or MagicMock()
    return AutoGrabService(config=cfg, download_service=download)


# ======================================================================
# Initialization
# ======================================================================

class TestInit:
    def test_enabled_true(self):
        svc = _make_service({"auto_grab_enabled": True})
        assert svc.enabled is True

    def test_enabled_false(self):
        svc = _make_service({"auto_grab_enabled": False})
        assert svc.enabled is False

    def test_enabled_default(self):
        svc = _make_service({})
        # auto_grab_enabled defaults to True from _make_service
        assert svc.enabled is True

    def test_log_callback(self):
        svc = _make_service()
        messages = []
        svc.set_log_callback(lambda msg, level: messages.append((msg, level)))
        svc._log("test message", "info")
        assert len(messages) == 1
        assert messages[0] == ("test message", "info")


# ======================================================================
# CSV parsing
# ======================================================================

class TestParseCsv:
    def test_empty_string(self):
        svc = _make_service({"auto_grab_genres": ""})
        assert svc._parse_csv("auto_grab_genres") == []

    def test_single_value(self):
        svc = _make_service({"auto_grab_genres": "Action"})
        assert svc._parse_csv("auto_grab_genres") == ["action"]

    def test_multiple_values(self):
        svc = _make_service({"auto_grab_genres": "Action, Sci-Fi, Drama"})
        result = svc._parse_csv("auto_grab_genres")
        assert result == ["action", "sci-fi", "drama"]

    def test_whitespace_only(self):
        svc = _make_service({"auto_grab_genres": "  ,  ,  "})
        assert svc._parse_csv("auto_grab_genres") == []

    def test_mixed_case(self):
        svc = _make_service({"auto_grab_genres": "ACTION, sci-fi"})
        result = svc._parse_csv("auto_grab_genres")
        assert result == ["action", "sci-fi"]


# ======================================================================
# evaluate_item — Status filtering
# ======================================================================

class TestEvaluateStatus:
    def test_missing_allowed(self):
        svc = _make_service({"auto_grab_statuses": "missing"})
        item = _make_item(status=ScanStatus.MISSING)
        assert svc.evaluate_item(item) == ""

    def test_missing_not_allowed(self):
        svc = _make_service({"auto_grab_statuses": "upgrade"})
        item = _make_item(status=ScanStatus.MISSING)
        assert svc.evaluate_item(item) == "status"

    def test_upgrade_allowed(self):
        svc = _make_service({"auto_grab_statuses": "upgrade"})
        item = _make_item(status=ScanStatus.UPGRADE)
        assert svc.evaluate_item(item) == ""

    def test_dv_upgrade_allowed(self):
        svc = _make_service({"auto_grab_statuses": "dv_upgrade"})
        item = _make_item(status=ScanStatus.DV_UPGRADE)
        assert svc.evaluate_item(item) == ""

    def test_in_library_never_grabbed(self):
        svc = _make_service({"auto_grab_statuses": "missing,upgrade,dv_upgrade"})
        item = _make_item(status=ScanStatus.IN_LIBRARY)
        assert svc.evaluate_item(item) == "status"

    def test_downloaded_skipped(self):
        svc = _make_service({"auto_grab_statuses": "missing,upgrade,dv_upgrade"})
        item = _make_item(status=ScanStatus.DOWNLOADED)
        # Downloaded is not in the allowed set
        assert svc.evaluate_item(item) == "status"

    def test_all_statuses(self):
        svc = _make_service({"auto_grab_statuses": "missing,upgrade,dv_upgrade"})
        for status in [ScanStatus.MISSING, ScanStatus.UPGRADE, ScanStatus.DV_UPGRADE]:
            item = _make_item(status=status)
            assert svc.evaluate_item(item) == ""

    def test_empty_statuses_defaults_to_all_grab_statuses(self):
        svc = _make_service({"auto_grab_statuses": ""})
        for status in [ScanStatus.MISSING, ScanStatus.UPGRADE, ScanStatus.DV_UPGRADE]:
            item = _make_item(status=status)
            assert svc.evaluate_item(item) == ""


# ======================================================================
# evaluate_item — Rating filtering
# ======================================================================

class TestEvaluateRating:
    def test_no_min_rating(self):
        svc = _make_service({"auto_grab_min_rating": 0.0})
        item = _make_item(rating=1.0)
        assert svc.evaluate_item(item) == ""

    def test_above_min_rating(self):
        svc = _make_service({"auto_grab_min_rating": 6.0})
        item = _make_item(rating=7.5)
        assert svc.evaluate_item(item) == ""

    def test_below_min_rating(self):
        svc = _make_service({"auto_grab_min_rating": 8.0})
        item = _make_item(rating=6.5)
        assert svc.evaluate_item(item) == "rating"

    def test_exact_min_rating(self):
        svc = _make_service({"auto_grab_min_rating": 7.0})
        item = _make_item(rating=7.0)
        assert svc.evaluate_item(item) == ""

    def test_zero_rating_item_with_min(self):
        svc = _make_service({"auto_grab_min_rating": 5.0})
        item = _make_item(rating=0.0)
        assert svc.evaluate_item(item) == "rating"


# ======================================================================
# evaluate_item — Votes filtering
# ======================================================================

class TestEvaluateVotes:
    def test_no_min_votes(self):
        svc = _make_service({"auto_grab_min_votes": 0})
        item = _make_item(votes=5)
        assert svc.evaluate_item(item) == ""

    def test_above_min_votes(self):
        svc = _make_service({"auto_grab_min_votes": 1000})
        item = _make_item(votes=50000)
        assert svc.evaluate_item(item) == ""

    def test_below_min_votes(self):
        svc = _make_service({"auto_grab_min_votes": 10000})
        item = _make_item(votes=500)
        assert svc.evaluate_item(item) == "votes"

    def test_exact_min_votes(self):
        svc = _make_service({"auto_grab_min_votes": 5000})
        item = _make_item(votes=5000)
        assert svc.evaluate_item(item) == ""


# ======================================================================
# evaluate_item — Genre filtering
# ======================================================================

class TestEvaluateGenre:
    def test_no_genre_filter(self):
        svc = _make_service({"auto_grab_genres": ""})
        item = _make_item(genres=["Horror"])
        assert svc.evaluate_item(item) == ""

    def test_include_genre_match(self):
        svc = _make_service({"auto_grab_genres": "Action, Drama"})
        item = _make_item(genres=["Action", "Thriller"])
        assert svc.evaluate_item(item) == ""

    def test_include_genre_no_match(self):
        svc = _make_service({"auto_grab_genres": "Action, Drama"})
        item = _make_item(genres=["Horror", "Comedy"])
        assert svc.evaluate_item(item) == "genre"

    def test_exclude_genre_match(self):
        svc = _make_service({"auto_grab_exclude_genres": "Horror"})
        item = _make_item(genres=["Horror", "Thriller"])
        assert svc.evaluate_item(item) == "genre"

    def test_exclude_genre_no_match(self):
        svc = _make_service({"auto_grab_exclude_genres": "Horror"})
        item = _make_item(genres=["Action", "Drama"])
        assert svc.evaluate_item(item) == ""

    def test_include_and_exclude(self):
        # Item matches include but also matches exclude → excluded
        svc = _make_service({
            "auto_grab_genres": "Action",
            "auto_grab_exclude_genres": "Horror",
        })
        item = _make_item(genres=["Action", "Horror"])
        assert svc.evaluate_item(item) == "genre"

    def test_case_insensitive(self):
        svc = _make_service({"auto_grab_genres": "action"})
        item = _make_item(genres=["Action"])
        assert svc.evaluate_item(item) == ""

    def test_empty_genres_on_item(self):
        svc = _make_service({"auto_grab_genres": "Action"})
        item = _make_item(genres=[])
        assert svc.evaluate_item(item) == "genre"


# ======================================================================
# evaluate_item — Language filtering
# ======================================================================

class TestEvaluateLanguage:
    def test_no_language_filter(self):
        svc = _make_service({"auto_grab_languages": ""})
        item = _make_item(language="Korean")
        assert svc.evaluate_item(item) == ""

    def test_language_match(self):
        svc = _make_service({"auto_grab_languages": "English, French"})
        item = _make_item(language="English")
        assert svc.evaluate_item(item) == ""

    def test_language_no_match(self):
        svc = _make_service({"auto_grab_languages": "English, French"})
        item = _make_item(language="Korean")
        assert svc.evaluate_item(item) == "language"

    def test_language_case_insensitive(self):
        svc = _make_service({"auto_grab_languages": "english"})
        item = _make_item(language="English")
        assert svc.evaluate_item(item) == ""

    def test_empty_language_on_item_passes(self):
        """Items with no language data should pass language filter (not penalized)."""
        svc = _make_service({"auto_grab_languages": "English"})
        item = _make_item(language="")
        assert svc.evaluate_item(item) == ""


# ======================================================================
# evaluate_item — Combined filters
# ======================================================================

class TestEvaluateCombined:
    def test_all_filters_pass(self):
        svc = _make_service({
            "auto_grab_min_rating": 7.0,
            "auto_grab_min_votes": 5000,
            "auto_grab_genres": "Action",
            "auto_grab_languages": "English",
            "auto_grab_statuses": "missing",
        })
        item = _make_item(
            status=ScanStatus.MISSING,
            rating=8.0,
            votes=100000,
            genres=["Action"],
            language="English",
        )
        assert svc.evaluate_item(item) == ""

    def test_fails_on_first_criterion(self):
        """Status check runs first."""
        svc = _make_service({"auto_grab_statuses": "upgrade"})
        item = _make_item(status=ScanStatus.MISSING, rating=9.0)
        assert svc.evaluate_item(item) == "status"

    def test_rating_blocks_despite_good_genre(self):
        svc = _make_service({
            "auto_grab_min_rating": 8.0,
            "auto_grab_genres": "Action",
        })
        item = _make_item(rating=5.0, genres=["Action"])
        assert svc.evaluate_item(item) == "rating"


# ======================================================================
# process_items — Full pipeline
# ======================================================================

class TestProcessItems:
    def test_disabled_returns_empty_report(self):
        svc = _make_service({"auto_grab_enabled": False})
        items = [_make_item()]
        report = svc.process_items(items)
        assert report.evaluated == 0
        assert report.grabbed == 0

    def test_empty_items(self):
        svc = _make_service()
        report = svc.process_items([])
        assert report.evaluated == 0
        assert report.grabbed == 0

    def test_single_qualifying_item(self):
        dl = MagicMock()
        dl.download_item.return_value = {"success": True, "method": "jdownloader", "message": "ok"}
        svc = _make_service(dl_service=dl)

        item = _make_item()
        report = svc.process_items([item])

        assert report.evaluated == 1
        assert report.grabbed == 1
        assert report.grabbed_items == [item]
        dl.download_item.assert_called_once()

    def test_item_filtered_by_rating(self):
        dl = MagicMock()
        svc = _make_service({"auto_grab_min_rating": 8.0}, dl_service=dl)

        item = _make_item(rating=5.0)
        report = svc.process_items([item])

        assert report.evaluated == 1
        assert report.grabbed == 0
        assert report.skipped_rating == 1
        dl.download_item.assert_not_called()

    def test_item_filtered_by_votes(self):
        dl = MagicMock()
        svc = _make_service({"auto_grab_min_votes": 10000}, dl_service=dl)

        item = _make_item(votes=500)
        report = svc.process_items([item])

        assert report.evaluated == 1
        assert report.grabbed == 0
        assert report.skipped_votes == 1

    def test_item_filtered_by_genre(self):
        dl = MagicMock()
        svc = _make_service({"auto_grab_genres": "Drama"}, dl_service=dl)

        item = _make_item(genres=["Action"])
        report = svc.process_items([item])

        assert report.evaluated == 1
        assert report.grabbed == 0
        assert report.skipped_genre == 1

    def test_item_filtered_by_language(self):
        dl = MagicMock()
        svc = _make_service({"auto_grab_languages": "English"}, dl_service=dl)

        item = _make_item(language="Korean")
        report = svc.process_items([item])

        assert report.evaluated == 1
        assert report.grabbed == 0
        assert report.skipped_language == 1

    def test_item_filtered_by_status(self):
        dl = MagicMock()
        svc = _make_service({"auto_grab_statuses": "upgrade"}, dl_service=dl)

        item = _make_item(status=ScanStatus.MISSING)
        report = svc.process_items([item])

        assert report.evaluated == 1
        assert report.grabbed == 0
        assert report.skipped_status == 1

    def test_mixed_items(self):
        dl = MagicMock()
        dl.download_item.return_value = {"success": True, "method": "clipboard", "message": "ok"}
        svc = _make_service({"auto_grab_min_rating": 6.0}, dl_service=dl)

        items = [
            _make_item(id="item_1", rating=8.0, title="Good Movie"),
            _make_item(id="item_2", rating=4.0, title="Bad Movie"),
            _make_item(id="item_3", rating=7.0, title="Decent Movie"),
            _make_item(id="item_4", status=ScanStatus.IN_LIBRARY, rating=9.0, title="Owned Movie"),
        ]
        report = svc.process_items(items)

        assert report.evaluated == 4
        assert report.grabbed == 2
        assert report.skipped_rating == 1
        assert report.skipped_status == 1
        assert dl.download_item.call_count == 2

    def test_download_failure_counted(self):
        dl = MagicMock()
        dl.download_item.return_value = {"success": False, "method": "", "message": "network error"}
        svc = _make_service(dl_service=dl)

        item = _make_item()
        report = svc.process_items([item])

        assert report.evaluated == 1
        assert report.grabbed == 0
        assert report.failed == 1

    def test_download_exception_counted(self):
        dl = MagicMock()
        dl.download_item.side_effect = Exception("boom")
        svc = _make_service(dl_service=dl)

        item = _make_item()
        report = svc.process_items([item])

        assert report.evaluated == 1
        assert report.grabbed == 0
        assert report.failed == 1

    def test_service_type_from_config(self):
        dl = MagicMock()
        dl.download_item.return_value = {"success": True, "method": "jdownloader", "message": "ok"}
        svc = _make_service({
            "adithd_preferred_host": "nitroflare",
        }, dl_service=dl)

        item = _make_item()
        svc.process_items([item])

        call_kwargs = dl.download_item.call_args
        assert call_kwargs[1]["service_type"] == "Nitroflare" or call_kwargs.kwargs.get("service_type") == "Nitroflare"

    def test_log_callback_receives_messages(self):
        dl = MagicMock()
        dl.download_item.return_value = {"success": True, "method": "jdownloader", "message": "ok"}
        svc = _make_service(dl_service=dl)
        messages = []
        svc.set_log_callback(lambda msg, level: messages.append((msg, level)))

        item = _make_item(title="The Matrix")
        svc.process_items([item])

        # Should have evaluation start message, grab message, and summary
        assert len(messages) >= 2
        assert any("Auto-Grab" in m for m, _ in messages)
        assert any("The Matrix" in m for m, _ in messages)


# ======================================================================
# AutoGrabReport
# ======================================================================

class TestAutoGrabReport:
    def test_defaults(self):
        report = AutoGrabReport()
        assert report.evaluated == 0
        assert report.grabbed == 0
        assert report.skipped_rating == 0
        assert report.skipped_votes == 0
        assert report.skipped_genre == 0
        assert report.skipped_language == 0
        assert report.skipped_status == 0
        assert report.skipped_already_downloaded == 0
        assert report.failed == 0
        assert report.grabbed_items == []


# ======================================================================
# _get_allowed_statuses
# ======================================================================

class TestAllowedStatuses:
    def test_default_statuses(self):
        svc = _make_service({"auto_grab_statuses": "missing,upgrade,dv_upgrade"})
        statuses = svc._get_allowed_statuses()
        assert statuses == {ScanStatus.MISSING, ScanStatus.UPGRADE, ScanStatus.DV_UPGRADE}

    def test_single_status(self):
        svc = _make_service({"auto_grab_statuses": "missing"})
        statuses = svc._get_allowed_statuses()
        assert statuses == {ScanStatus.MISSING}

    def test_empty_defaults_to_all(self):
        svc = _make_service({"auto_grab_statuses": ""})
        statuses = svc._get_allowed_statuses()
        assert ScanStatus.MISSING in statuses
        assert ScanStatus.UPGRADE in statuses
        assert ScanStatus.DV_UPGRADE in statuses

    def test_invalid_status_ignored(self):
        svc = _make_service({"auto_grab_statuses": "missing,invalid_status"})
        statuses = svc._get_allowed_statuses()
        assert statuses == {ScanStatus.MISSING}

    def test_all_invalid_defaults_to_all(self):
        svc = _make_service({"auto_grab_statuses": "foo,bar"})
        statuses = svc._get_allowed_statuses()
        assert ScanStatus.MISSING in statuses
