"""Focused tests proving the HDEncode switch gates live access paths."""

from unittest.mock import MagicMock, patch

from backend.download_service import DownloadService
from backend.scanner_service import ScannerService


def _scanner_with_config(config):
    scanner = ScannerService.__new__(ScannerService)
    scanner.config = config
    return scanner


def test_build_sources_returns_no_hdencode_sources_when_disabled():
    scanner = _scanner_with_config({"hdencode_enabled": False})

    sources = scanner._build_sources(
        scan_type="Deep Scan",
        source_type="HDEncode",
        base_url="https://hdencode.org",
        flags={"4k": True, "remux": True, "tv": True},
        search_query="",
    )

    assert sources == []


def test_site_search_returns_no_hdencode_sources_when_disabled():
    scanner = _scanner_with_config({"hdencode_enabled": False})

    sources = scanner._build_sources(
        scan_type="Site Search",
        source_type="HDEncode",
        base_url="https://hdencode.org",
        flags={},
        search_query="example",
    )

    assert sources == []


def test_scrape_links_does_not_initialize_selenium_when_hdencode_disabled():
    service = DownloadService(
        config={"hdencode_enabled": False},
        db=MagicMock(),
        server_mode=True,
    )

    with patch("backend.download_service._ensure_selenium") as ensure_selenium:
        links = service.scrape_links(
            "https://hdencode.org/example-release/",
            "Rapidgator",
        )

    assert links == []
    ensure_selenium.assert_not_called()
