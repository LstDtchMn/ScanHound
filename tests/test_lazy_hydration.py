"""Conservative listing-first lazy hydration tests for PR 3."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.scanner_service import ScannerService


def _scanner():
    scanner = ScannerService.__new__(ScannerService)
    scanner.download_history = set()
    scanner._downloaded_titles_lookup = {}
    scanner.plex = SimpleNamespace(plex_index={"all_items": [], "by_imdb": {}, "by_title": {}})
    scanner.matching = MagicMock()
    return scanner


def _release(**overrides):
    values = {
        "display_title": "Example Movie",
        "year": 2024,
        "resolution": "2160p",
        "size": "20 GB",
        "season": None,
        "episode": None,
        "is_hdr": True,
        "is_dovi": False,
        "hdr_format": "HDR10",
        "imdb_id": None,
        "is_tv": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _post(url="https://hdencode.org/example/", title="Example.Movie.2024.2160p"):
    return {
        "url": url,
        "source": "hdencode",
        "type": "movie",
        "listing_title": title,
    }


def test_exact_downloaded_url_avoids_detail_request():
    scanner = _scanner()
    post = _post()
    scanner.download_history.add(post["url"])
    scanner._parse_hdencode_listing_candidate = MagicMock(side_effect=AssertionError("must not parse"))

    assert scanner._should_hydrate_listing_candidate(post) is False


def test_same_quality_downloaded_sibling_avoids_detail_request():
    scanner = _scanner()
    scanner._parse_hdencode_listing_candidate = MagicMock(return_value=_release())
    scanner._downloaded_titles_lookup = {
        "example movie": [{"resolution": "2160p", "dovi": False, "downloaded_at": "2026"}]
    }

    assert scanner._should_hydrate_listing_candidate(_post()) is False


def test_dolby_vision_gain_is_still_hydrated():
    scanner = _scanner()
    scanner._parse_hdencode_listing_candidate = MagicMock(
        return_value=_release(is_dovi=True)
    )
    scanner._downloaded_titles_lookup = {
        "example movie": [{"resolution": "2160p", "dovi": False, "downloaded_at": "2026"}]
    }

    assert scanner._should_hydrate_listing_candidate(_post()) is True


def test_missing_listing_resolution_fails_open():
    scanner = _scanner()
    scanner._parse_hdencode_listing_candidate = MagicMock(
        return_value=_release(resolution="")
    )
    scanner._downloaded_titles_lookup = {
        "example movie": [{"resolution": "2160p", "dovi": False, "downloaded_at": "2026"}]
    }

    assert scanner._should_hydrate_listing_candidate(_post()) is True


def test_unparseable_listing_fails_open_to_detail_request():
    scanner = _scanner()
    scanner._parse_hdencode_listing_candidate = MagicMock(return_value=None)

    assert scanner._should_hydrate_listing_candidate(_post()) is True


def test_uncertain_plex_match_fails_open():
    scanner = _scanner()
    scanner._parse_hdencode_listing_candidate = MagicMock(return_value=_release())
    scanner.plex.plex_index["all_items"] = [{"title": "Example Movie"}]
    scanner.matching.find_movie_matches.return_value = ([{"title": "Example Movie"}], True)

    assert scanner._should_hydrate_listing_candidate(_post()) is True


def test_conclusive_non_upgrade_plex_match_avoids_detail_request():
    scanner = _scanner()
    scanner._parse_hdencode_listing_candidate = MagicMock(return_value=_release())
    scanner.plex.plex_index["all_items"] = [{"title": "Example Movie"}]
    scanner.matching.find_movie_matches.return_value = ([{"title": "Example Movie"}], False)
    scanner.matching.calculate_movie_upgrade_status.return_value = (
        "In Library", "", "", "rating-key"
    )

    assert scanner._should_hydrate_listing_candidate(_post()) is False


def test_non_hdencode_sources_are_never_filtered_by_this_gate():
    scanner = _scanner()
    assert scanner._should_hydrate_listing_candidate({"source": "ddlbase"}) is True
