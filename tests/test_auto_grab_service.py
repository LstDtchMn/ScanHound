"""Tests for backend/auto_grab_service.py — AutoGrabService and AutoGrabReport."""

import pytest
from unittest.mock import MagicMock

from backend.auto_grab_service import AutoGrabService, AutoGrabReport
from backend.scanner_service import MediaItem, ScanStatus


def make_item(**kwargs):
    """Helper: create a MediaItem with sensible defaults."""
    defaults = dict(
        id="test-1",
        title="Test Movie",
        year=2023,
        status=ScanStatus.MISSING,
        rating=7.5,
        votes=50000,
        genres=["Action"],
        language="en",
    )
    defaults.update(kwargs)
    return MediaItem(**defaults)


def make_service(config=None, download_service=None):
    """Helper: create an AutoGrabService with mocked download_service."""
    if config is None:
        config = {"auto_grab_enabled": True}
    if download_service is None:
        download_service = MagicMock()
        download_service.download_item.return_value = {"success": True, "method": "jdownloader"}
    return AutoGrabService(config, download_service)


# ── AutoGrabReport dataclass ─────────────────────────────────────────

class TestAutoGrabReport:

    def test_defaults(self):
        r = AutoGrabReport()
        assert r.evaluated == 0
        assert r.grabbed == 0
        assert r.skipped_rating == 0
        assert r.skipped_votes == 0
        assert r.skipped_genre == 0
        assert r.skipped_language == 0
        assert r.skipped_status == 0
        assert r.skipped_already_downloaded == 0
        assert r.failed == 0
        assert r.grabbed_items == []

    def test_grabbed_items_is_independent_list(self):
        r1 = AutoGrabReport()
        r2 = AutoGrabReport()
        r1.grabbed_items.append("x")
        assert r2.grabbed_items == []


# ── enabled property ─────────────────────────────────────────────────

class TestEnabled:

    def test_false_when_key_missing(self):
        svc = AutoGrabService({}, MagicMock())
        assert svc.enabled is False

    def test_false_when_explicitly_false(self):
        svc = AutoGrabService({"auto_grab_enabled": False}, MagicMock())
        assert svc.enabled is False

    def test_true_when_set(self):
        svc = AutoGrabService({"auto_grab_enabled": True}, MagicMock())
        assert svc.enabled is True


# ── _parse_csv ────────────────────────────────────────────────────────

class TestParseCsv:

    def test_empty_string_returns_empty_list(self):
        svc = make_service()
        svc.config["test_key"] = ""
        assert svc._parse_csv("test_key") == []

    def test_whitespace_only_returns_empty_list(self):
        svc = make_service()
        svc.config["test_key"] = "   "
        assert svc._parse_csv("test_key") == []

    def test_missing_key_returns_empty_list(self):
        svc = make_service()
        assert svc._parse_csv("nonexistent") == []

    def test_single_value(self):
        svc = make_service()
        svc.config["k"] = "action"
        assert svc._parse_csv("k") == ["action"]

    def test_multiple_values(self):
        svc = make_service()
        svc.config["k"] = "action,drama,comedy"
        assert svc._parse_csv("k") == ["action", "drama", "comedy"]

    def test_values_lowercased(self):
        svc = make_service()
        svc.config["k"] = "Action,DRAMA"
        assert svc._parse_csv("k") == ["action", "drama"]

    def test_whitespace_around_values_stripped(self):
        svc = make_service()
        svc.config["k"] = " action , drama "
        assert svc._parse_csv("k") == ["action", "drama"]

    def test_empty_segments_skipped(self):
        svc = make_service()
        svc.config["k"] = "action,,drama"
        result = svc._parse_csv("k")
        assert "" not in result
        assert "action" in result
        assert "drama" in result


# ── evaluate_item ─────────────────────────────────────────────────────

class TestEvaluateItem:

    def test_missing_status_passes(self):
        svc = make_service()
        item = make_item(status=ScanStatus.MISSING)
        assert svc.evaluate_item(item) == ""

    def test_upgrade_status_passes(self):
        svc = make_service()
        item = make_item(status=ScanStatus.UPGRADE)
        assert svc.evaluate_item(item) == ""

    def test_dv_upgrade_status_passes(self):
        svc = make_service()
        item = make_item(status=ScanStatus.DV_UPGRADE)
        assert svc.evaluate_item(item) == ""

    def test_in_library_status_skipped(self):
        svc = make_service()
        item = make_item(status=ScanStatus.IN_LIBRARY)
        assert svc.evaluate_item(item) == "status"

    def test_downloaded_status_skipped(self):
        # A DOWNLOADED item is skipped and attributed to "already_downloaded"
        # (checked before the status gate), not lumped into "status".
        svc = make_service()
        item = make_item(status=ScanStatus.DOWNLOADED)
        assert svc.evaluate_item(item) == "already_downloaded"

    def test_rating_below_min_skipped(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_min_rating": 8.0})
        item = make_item(rating=6.5)
        assert svc.evaluate_item(item) == "rating"

    def test_rating_at_min_passes(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_min_rating": 7.5})
        item = make_item(rating=7.5)
        assert svc.evaluate_item(item) == ""

    def test_rating_zero_min_always_passes(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_min_rating": 0})
        item = make_item(rating=0.0)
        assert svc.evaluate_item(item) == ""

    def test_votes_below_min_skipped(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_min_votes": 100000})
        item = make_item(votes=5000)
        assert svc.evaluate_item(item) == "votes"

    def test_votes_at_min_passes(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_min_votes": 50000})
        item = make_item(votes=50000)
        assert svc.evaluate_item(item) == ""

    def test_votes_zero_min_always_passes(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_min_votes": 0})
        item = make_item(votes=0)
        assert svc.evaluate_item(item) == ""

    def test_genre_include_not_matched_skipped(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_genres": "horror,thriller"})
        item = make_item(genres=["Action", "Drama"])
        assert svc.evaluate_item(item) == "genre"

    def test_genre_include_matched_passes(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_genres": "action,drama"})
        item = make_item(genres=["Action"])
        assert svc.evaluate_item(item) == ""

    def test_genre_include_case_insensitive(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_genres": "ACTION"})
        item = make_item(genres=["action"])
        assert svc.evaluate_item(item) == ""

    def test_genre_exclude_matched_skipped(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_exclude_genres": "horror"})
        item = make_item(genres=["Horror", "Action"])
        assert svc.evaluate_item(item) == "genre"

    def test_genre_exclude_not_matched_passes(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_exclude_genres": "horror"})
        item = make_item(genres=["Action"])
        assert svc.evaluate_item(item) == ""

    def test_language_not_in_list_skipped(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_languages": "en,fr"})
        item = make_item(language="de")
        assert svc.evaluate_item(item) == "language"

    def test_language_in_list_passes(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_languages": "en,fr"})
        item = make_item(language="en")
        assert svc.evaluate_item(item) == ""

    def test_empty_language_passes_when_list_set(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_languages": "en"})
        item = make_item(language="")
        # Empty language is not checked (lang check requires item_lang to be truthy)
        assert svc.evaluate_item(item) == ""

    def test_no_filters_passes(self):
        svc = make_service({"auto_grab_enabled": True})
        item = make_item()
        assert svc.evaluate_item(item) == ""

    def test_custom_statuses_from_config(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_statuses": "missing"})
        upgrade_item = make_item(status=ScanStatus.UPGRADE)
        missing_item = make_item(status=ScanStatus.MISSING)
        # Only missing is allowed
        assert svc.evaluate_item(upgrade_item) == "status"
        assert svc.evaluate_item(missing_item) == ""


# ── process_items ─────────────────────────────────────────────────────

class TestProcessItems:

    def test_disabled_returns_empty_report(self):
        svc = AutoGrabService({"auto_grab_enabled": False}, MagicMock())
        report = svc.process_items([make_item()])
        assert report.evaluated == 0
        assert report.grabbed == 0

    def test_evaluates_all_items(self):
        svc = make_service()
        items = [make_item(id=str(i)) for i in range(5)]
        report = svc.process_items(items)
        assert report.evaluated == 5

    def test_grabbed_count_incremented_on_success(self):
        ds = MagicMock()
        ds.download_item.return_value = {"success": True, "method": "jdownloader"}
        svc = make_service(download_service=ds)
        report = svc.process_items([make_item()])
        assert report.grabbed == 1
        assert len(report.grabbed_items) == 1

    def test_failed_count_incremented_on_failure(self):
        ds = MagicMock()
        ds.download_item.return_value = {"success": False, "message": "timeout"}
        svc = make_service(download_service=ds)
        report = svc.process_items([make_item()])
        assert report.failed == 1
        assert report.grabbed == 0

    def test_exception_increments_failed(self):
        ds = MagicMock()
        ds.download_item.side_effect = Exception("network error")
        svc = make_service(download_service=ds)
        report = svc.process_items([make_item()])
        assert report.failed == 1

    def test_skipped_rating_count(self):
        svc = make_service({"auto_grab_enabled": True, "auto_grab_min_rating": 9.0})
        items = [make_item(rating=5.0), make_item(rating=4.0)]
        report = svc.process_items(items)
        assert report.skipped_rating == 2

    def test_skipped_status_count(self):
        svc = make_service()
        items = [
            make_item(status=ScanStatus.IN_LIBRARY),
            make_item(status=ScanStatus.IN_LIBRARY),
        ]
        report = svc.process_items(items)
        assert report.skipped_status == 2

    def test_nitroflare_service_type(self):
        ds = MagicMock()
        ds.download_item.return_value = {"success": True, "method": "nitroflare"}
        config = {"auto_grab_enabled": True, "adithd_preferred_host": "nitroflare"}
        svc = AutoGrabService(config, ds)
        svc.process_items([make_item()])
        call_kwargs = ds.download_item.call_args
        assert call_kwargs.kwargs.get("service_type") == "Nitroflare"

    def test_rapidgator_service_type_by_default(self):
        ds = MagicMock()
        ds.download_item.return_value = {"success": True, "method": "jdownloader"}
        svc = make_service(download_service=ds)
        svc.process_items([make_item()])
        call_kwargs = ds.download_item.call_args
        assert call_kwargs.kwargs.get("service_type") == "Rapidgator"

    def test_grabbed_items_list_populated(self):
        ds = MagicMock()
        ds.download_item.return_value = {"success": True, "method": "jdownloader"}
        svc = make_service(download_service=ds)
        item = make_item(title="My Movie")
        report = svc.process_items([item])
        assert len(report.grabbed_items) == 1
        assert report.grabbed_items[0].title == "My Movie"

    def test_log_callback_called(self):
        svc = make_service()
        log_calls = []
        svc.set_log_callback(lambda msg, level="info": log_calls.append(msg))
        svc.process_items([make_item()])
        assert len(log_calls) > 0

    def test_empty_items_list(self):
        svc = make_service()
        report = svc.process_items([])
        assert report.evaluated == 0
        assert report.grabbed == 0
