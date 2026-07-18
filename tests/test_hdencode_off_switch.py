"""Focused tests proving the HDEncode switch gates live access paths."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.api.routes.pipeline import UrlRequest as PipelineUrlRequest
from backend.api.routes.pipeline import search_sources
from backend.download_service import DownloadService
from backend.scanner_service import ScannerService
from backend.sources.base import PageResult, SourceBase, SourceCapability, SourceConfig
from backend.sources.registry import SourceRegistry


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


def _search_source_class(name, calls):
    class _SearchSource(SourceBase):
        @classmethod
        def get_config(cls):
            return SourceConfig(
                name=name,
                display_name=name,
                base_url=f"https://{name}.example",
                capabilities=SourceCapability.SEARCH,
            )

        async def fetch_page(self, page=1, mode="movies", **kwargs):
            return PageResult(releases=[])

        def parse_release(self, raw_data):
            return None

        async def search(self, query, mode="all", **kwargs):
            calls[name] += 1
            return PageResult(releases=[])

    return _SearchSource


@pytest.mark.asyncio
async def test_pipeline_search_never_calls_disabled_hdencode(monkeypatch):
    calls = {"hdencode": 0, "ddlbase": 0}
    fake_hdencode = _search_source_class("hdencode", calls)
    fake_ddlbase = _search_source_class("ddlbase", calls)

    def discover_only_test_sources(source_registry, package_path=None):
        source_registry.register(fake_hdencode)
        source_registry.register(fake_ddlbase)

    monkeypatch.setattr(
        SourceRegistry, "discover_sources", discover_only_test_sources
    )

    cursor = MagicMock()
    cursor.fetchone.return_value = {"title": "Example Movie", "season": None}
    connection = MagicMock()
    connection.execute.return_value = cursor
    database = MagicMock()
    database.get_connection.return_value = connection
    registry = SimpleNamespace(
        db=database,
        config={"hdencode_enabled": False, "ddlbase_enabled": True},
    )

    result = await search_sources(
        PipelineUrlRequest(url="https://rapidgator.net/file/original"),
        registry,
    )

    assert result == {"releases": [], "errors": []}
    assert calls == {"hdencode": 0, "ddlbase": 1}



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
