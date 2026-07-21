"""Real-boundary tests for closed public exception payloads."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.public_errors import capture_public_exception
from backend.api.routes import downloads, rename


_SENTINEL = (
    r"C:\private\library\movie.mkv?"
    "token=top-secret webdriver-internal=DevToolsActivePort"
)


def test_mapper_logs_raw_detail_but_returns_closed_payload(caplog):
    exc = RuntimeError(_SENTINEL)
    with caplog.at_level(logging.ERROR):
        public = capture_public_exception(
            logging.getLogger("test.public"),
            exc,
            code="operation_failed",
            message="The operation failed.",
            context="synthetic operation",
        )

    detail = public.as_detail()
    assert detail["code"] == "operation_failed"
    assert detail["message"] == "The operation failed."
    assert detail["correlation_id"]
    assert _SENTINEL not in str(detail)
    assert _SENTINEL in caplog.text
    assert detail["correlation_id"] in caplog.text


def test_download_websocket_body_does_not_expose_raw_exception(monkeypatch):
    notifications = []
    monkeypatch.setattr(downloads.ws_manager, "broadcast_sync", notifications.append)

    class FailingDownload:
        def download_item(self, **_kwargs):
            raise RuntimeError(_SENTINEL)
        def save_to_history(self, *_args, **_kwargs):
            return None

    req = downloads.DownloadRequest(url="https://hdencode.org/post", title="Example")
    downloads._run_grab(FailingDownload(), SimpleNamespace(), req)

    data = notifications[-1]["data"]
    assert data["code"] == "download_failed"
    assert data["correlation_id"]
    assert _SENTINEL not in str(data)
    assert "Reference:" in data["body"]


def test_http_exception_detail_does_not_expose_raw_exception():
    class FailingDownload:
        def scrape_links(self, *_args, **_kwargs):
            raise RuntimeError(_SENTINEL)

    reg = SimpleNamespace(download=FailingDownload(), db=None)
    request = downloads.ScrapeRequest(url="https://hdencode.org/post")

    with pytest.raises(HTTPException) as caught:
        downloads.scrape_links(request, reg)

    detail = caught.value.detail
    assert detail["code"] == "scrape_failed"
    assert detail["correlation_id"]
    assert _SENTINEL not in str(detail)


def test_process_folder_background_route_closes_exception(monkeypatch, tmp_path):
    notifications = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target
        def start(self):
            self.target()

    class FailingService:
        def _translate_path(self, folder):
            return folder
        def process_folder(self, *_args, **_kwargs):
            raise RuntimeError(_SENTINEL)

    monkeypatch.setattr(rename, "_service", lambda _reg: FailingService())
    monkeypatch.setattr(rename.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(rename.ws_manager, "broadcast_sync", notifications.append)

    root = tmp_path / "library"
    root.mkdir()
    reg = SimpleNamespace(config={
        "auto_rename_movie_library": str(root),
        "auto_rename_movie_library_4k": "",
        "auto_rename_tv_library": "",
    })
    result = rename.process_folder(
        rename.ProcessFolderRequest(folder=str(root), dry_run=False),
        reg,
    )

    assert result["status"] == "started"
    data = notifications[-1]["data"]
    assert data["code"] == "process_folder_failed"
    assert data["correlation_id"]
    assert _SENTINEL not in str(data)
