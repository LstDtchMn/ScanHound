"""Focused Commit-5 regressions for config, queue, browser, and route polish."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException
import pytest

from backend import app_service as app_service_module
from backend.background_scanner import BackgroundScanner
from backend.api.routes.downloads import (
    BatchDownloadRequest,
    DownloadRequest,
    download_batch,
)
from backend.app_service import AppService
from backend.browser_adapter import prune_profile_caches
from backend.config import source_enabled, validate_config
from backend.database import DatabaseManager
from backend.download_queue import (
    DownloadQueueConflict,
    DownloadQueueService,
)
from backend.download_service import DownloadService
from backend.scanner_service import ScannerService
from backend.sources.registry import SourceRegistry


def _item(index: int) -> dict:
    return {
        "url": f"https://hdencode.org/release/{index}",
        "title": f"Title {index}",
        "year": 2026,
        "season": None,
        "resolution": "2160p",
        "size": "20 GB",
        "hdr": "HDR",
        "dovi": True,
        "service_type": "Rapidgator",
    }


def test_source_enabled_fails_closed_for_non_boolean_values():
    for value in ("false", "0", "off", "true", 1, 0, [], {}, object(), None):
        assert source_enabled({"hdencode_enabled": value}, "hdencode_enabled") is False

    assert source_enabled({"hdencode_enabled": True}, "hdencode_enabled") is True
    assert source_enabled({}, "hdencode_enabled", missing_default=True) is True
    assert source_enabled({}, "hdencode_enabled", missing_default=False) is False
    assert source_enabled(None, "hdencode_enabled") is False


def test_validate_config_normalizes_only_explicit_source_flag():
    assert validate_config({"hdencode_enabled": "false"})["hdencode_enabled"] is False
    assert validate_config({"hdencode_enabled": True})["hdencode_enabled"] is True
    assert validate_config({}) == {}


def test_corrupt_config_disables_hdencode(monkeypatch, tmp_path):
    corrupt = tmp_path / "config.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(app_service_module, "CONFIG_FILE", str(corrupt))
    monkeypatch.setattr(
        app_service_module,
        "_LEGACY_CONFIG_FILE",
        str(tmp_path / "missing-legacy.json"),
    )
    monkeypatch.setattr(app_service_module, "load_dotenv", lambda: None)
    monkeypatch.delenv("SCANHOUND_HDENCODE_ENABLED", raising=False)

    config = AppService.__new__(AppService).load_config()

    assert config["hdencode_enabled"] is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("garbage", False),
        ("", False),
    ],
)
def test_hdencode_environment_override_is_strict(
    monkeypatch,
    tmp_path,
    raw,
    expected,
):
    monkeypatch.setattr(
        app_service_module,
        "CONFIG_FILE",
        str(tmp_path / "missing-config.json"),
    )
    monkeypatch.setattr(
        app_service_module,
        "_LEGACY_CONFIG_FILE",
        str(tmp_path / "missing-legacy.json"),
    )
    monkeypatch.setattr(app_service_module, "load_dotenv", lambda: None)
    monkeypatch.setenv("SCANHOUND_HDENCODE_ENABLED", raw)

    config = AppService.__new__(AppService).load_config()

    assert config["hdencode_enabled"] is expected


def test_truthy_string_false_is_rejected_across_source_gates():
    scanner = ScannerService.__new__(ScannerService)
    scanner.config = {"hdencode_enabled": "false"}
    assert scanner._build_sources(
        scan_type="Deep Scan",
        source_type="HDEncode",
        base_url="https://hdencode.org",
        flags={"4k": True},
        search_query="",
    ) == []

    service = DownloadService(
        config={
            "hdencode_enabled": "false",
            "hdencode_browser_profile_mode": "temporary",
        },
        db=MagicMock(),
        server_mode=True,
    )
    with patch("backend.download_service._ensure_selenium") as ensure_selenium:
        assert service.scrape_links(
            "https://hdencode.org/example-release/",
            "Rapidgator",
        ) == []
    ensure_selenium.assert_not_called()

    registry = SourceRegistry()
    registry._enabled = {"hdencode": True}
    registry.sync_from_config({"hdencode_enabled": "false"})
    assert registry._enabled["hdencode"] is False


def test_disabled_hdencode_never_starts_rss_cycle(monkeypatch, tmp_path):
    db = DatabaseManager(str(tmp_path / "background.db"))
    rss_constructor = MagicMock(
        side_effect=AssertionError("disabled HDEncode entered the RSS transport path")
    )
    monkeypatch.setattr(
        "backend.hdencode_rss_service.HDEncodeRSSService",
        rss_constructor,
    )

    class FakeScanner:
        def __init__(self):
            self.calls = []
            self._last_crawl_early_stopped = False

        def try_acquire_scan(self):
            return True

        def release_scan(self):
            pass

        def run_scan(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return []

    class FakeRegistry:
        lifespan_generation = 1

        def __init__(self):
            self.config = {
                "background_scan_enabled": True,
                "background_scan_sources": ["HDEncode"],
                "background_scan_pages": 1,
                "background_scan_retain_days": 7,
                "hdencode_enabled": "false",
                "hdencode_discovery_mode": "rss_shadow",
            }
            self.scanner = FakeScanner()
            self.db = db
            self.backend = SimpleNamespace(save_config=lambda: None)
            self.download = None

        def owns_lifespan(self, generation):
            return generation == self.lifespan_generation

    registry = FakeRegistry()
    background = BackgroundScanner(registry)
    try:
        result = background.scan_once()
        rss_constructor.assert_not_called()
        assert registry.scanner.calls == []
        assert result["scanned"] == 0
        assert background.last_run["sources"][0]["skipped"] == "disabled"
    finally:
        db.close()


def test_profile_cache_pruning_preserves_cookie_data(tmp_path):
    profile = tmp_path / "profile"
    cache = profile / "Default" / "Cache"
    code_cache = profile / "Default" / "Code Cache"
    service_cache = profile / "Default" / "Service Worker" / "CacheStorage"
    cache.mkdir(parents=True)
    code_cache.mkdir(parents=True)
    service_cache.mkdir(parents=True)
    (cache / "data.bin").write_bytes(b"x" * 64)
    (code_cache / "code.bin").write_bytes(b"y" * 64)
    (service_cache / "cached-response.bin").write_bytes(b"z" * 64)
    cookies = profile / "Default" / "Cookies"
    cookies.write_text("keep-session", encoding="utf-8")

    removed = prune_profile_caches(
        {
            "hdencode_browser_profile_mode": "persistent",
            "hdencode_browser_profile_dir": str(profile),
        },
        max_bytes=1,
    )

    assert str(cache) in removed
    assert str(code_cache) in removed
    assert str(service_cache) in removed
    assert not cache.exists()
    assert not code_cache.exists()
    assert not service_cache.exists()
    assert cookies.read_text(encoding="utf-8") == "keep-session"


def test_active_duplicate_is_filtered_before_stagger_times_are_assigned(tmp_path):
    db = DatabaseManager(str(tmp_path / "queue.db"))
    try:
        service = DownloadQueueService({}, db, MagicMock())
        service.schedule_batch([_item(2)], interval_minutes=10)

        batch = service.schedule_batch(
            [_item(1), _item(2), _item(3)],
            interval_minutes=10,
        )

        assert batch["total_items"] == 2
        assert [row["canonical_url"] for row in batch["items"]] == [
            _item(1)["url"],
            _item(3)["url"],
        ]
        assert [row["sequence_number"] for row in batch["items"]] == [0, 1]
        first = datetime.fromisoformat(batch["items"][0]["scheduled_for"])
        second = datetime.fromisoformat(batch["items"][1]["scheduled_for"])
        assert int((second - first).total_seconds()) == 600
    finally:
        db.close()


def test_durable_queue_restores_aggregate_batch_progress(tmp_path):
    db = DatabaseManager(str(tmp_path / "progress.db"))
    events = []
    fake = MagicMock()
    fake.download_item.return_value = {
        "success": True,
        "method": "duplicate",
        "link_count": 0,
        "message": "Already grabbed.",
    }
    try:
        service = DownloadQueueService(
            {},
            db,
            fake,
            broadcast=events.append,
        )
        batch = service.schedule_batch(
            [_item(1), _item(2)],
            interval_minutes=0,
            mode="immediate",
        )
        for _ in range(2):
            claimed = service._claim_due()
            assert claimed is not None
            service._execute(claimed)

        progress = [
            event["data"]
            for event in events
            if event["type"] == "download:batch_progress"
            and event["data"]["batch_uuid"] == batch["batch_uuid"]
        ]
        assert progress[0]["completed"] == 0
        assert any(
            event["completed"] == 0 and event["current_title"] == "Title 1"
            for event in progress
        )
        assert any(event["completed"] == 1 for event in progress)
        assert progress[-1]["completed"] == 2
        assert progress[-1]["total"] == 2
    finally:
        db.close()


def test_standard_browser_preflight_warning_is_adapter_specific():
    service = DownloadService(
        config={
            "hdencode_browser_adapter": "selenium_chromium",
            "hdencode_browser_profile_mode": "temporary",
        },
        db=MagicMock(),
        server_mode=True,
    )
    service._detect_chrome_major = MagicMock(return_value=None)
    service._log = MagicMock()

    service.driver_preflight()

    messages = [call.args[0] for call in service._log.call_args_list]
    warning = next(message for message in messages if "could NOT detect" in message)
    assert "standard Selenium" in warning
    assert "undetected-chromedriver" not in warning


def test_batch_route_preserves_conflict_and_propagates_unexpected_errors():
    request = BatchDownloadRequest(
        items=[
            DownloadRequest(
                url="https://hdencode.org/release/1",
                title="Title 1",
            )
        ]
    )
    queue = MagicMock()
    registry = SimpleNamespace(
        download=MagicMock(),
        download_queue=queue,
        config={"download_batch_interval_minutes": 10},
    )

    queue.schedule_batch.side_effect = DownloadQueueConflict("already active")
    with pytest.raises(HTTPException) as conflict:
        download_batch(request, BackgroundTasks(), registry)
    assert conflict.value.status_code == 409

    queue.schedule_batch.side_effect = RuntimeError("unexpected database fault")
    with pytest.raises(RuntimeError, match="unexpected database fault"):
        download_batch(request, BackgroundTasks(), registry)
