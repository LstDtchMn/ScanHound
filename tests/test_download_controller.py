"""Focused regression tests for ui/controllers/download_controller.py."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PySide6.QtCore")

from PySide6.QtCore import QCoreApplication

from backend.scanner_service import MediaItem, ScanStatus
from ui.controllers.download_controller import DownloadController
from ui.models.results_model import ResultsModel


_APP = QCoreApplication.instance() or QCoreApplication([])


class _ImmediateThread:
    """Small thread stub that runs the target synchronously in tests."""

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _make_item(title="Test Movie", url="http://example.com/item", season=None):
    return MediaItem(
        id=f"id-{title}-{season}",
        title=title,
        year=2024,
        season=season,
        url=url,
        resolution="4K",
        size="50 GB",
        status=ScanStatus.MISSING,
        status_text="Missing",
        color="#e74c3c",
        imdb_id="tt1234567",
        plex_rating_key="plex-42",
    )


def _make_controller(items, plex_data_getter=None):
    backend = SimpleNamespace(config={}, db=MagicMock())
    model = ResultsModel()
    model.setItems(items)
    controller = DownloadController(
        backend,
        model,
        all_items_getter=lambda: items,
        plex_data_getter=plex_data_getter,
    )
    return controller, model


def test_download_selected_marks_items_downloaded_immediately():
    item = _make_item()
    item.selected = True
    controller, _model = _make_controller([item])

    service = MagicMock()
    service.save_to_history.return_value = True
    controller._download_service = service

    controller.downloadSelected()

    service.open_url.assert_called_once_with(item.url)
    assert item.status == ScanStatus.DOWNLOADED
    assert item.status_text == "Downloaded"


def test_send_selected_to_jd_does_not_mark_items_when_send_fails():
    item = _make_item()
    item.selected = True
    controller, _model = _make_controller([item])

    service = MagicMock()
    service.send_to_jdownloader.return_value = False
    controller._download_service = service

    controller.sendSelectedToJD()

    assert item.status == ScanStatus.MISSING
    service.save_to_history.assert_not_called()


def test_copy_selected_to_clipboard_marks_items_downloaded_after_success():
    item = _make_item()
    item.selected = True
    controller, _model = _make_controller([item])

    service = MagicMock()
    service.scrape_links.return_value = ["http://rapidgator.net/file"]
    service.copy_to_clipboard.return_value = True
    service.save_to_history.return_value = True
    controller._download_service = service

    # Patch QThread.start to run synchronously + process events for signal delivery
    from ui.controllers.download_controller import ScrapeAndCopyWorker
    orig_start = ScrapeAndCopyWorker.start
    def sync_start(self):
        self.run()
    with patch.object(ScrapeAndCopyWorker, 'start', sync_start):
        controller.copySelectedToClipboard()
        _APP.processEvents()

    assert item.status == ScanStatus.DOWNLOADED


def test_download_item_marks_row_downloaded_on_success():
    item = _make_item()
    controller, _model = _make_controller([item])

    service = MagicMock()
    service.download_item.return_value = {
        "success": True,
        "message": "Copied 1 links to clipboard",
        "history_saved": True,
    }
    controller._download_service = service

    from ui.controllers.download_controller import DownloadItemWorker
    def sync_start(self):
        self.run()
    with patch.object(DownloadItemWorker, 'start', sync_start):
        controller.downloadItem(0)
        _APP.processEvents()

    assert item.status == ScanStatus.DOWNLOADED


def test_open_in_plex_passes_item_metadata_to_service():
    item = _make_item(title="Anaconda", season=None)
    plex_movies = [{"clean_title": "anaconda", "year": 2025, "rating_key": "plex-42"}]
    controller, _model = _make_controller(
        [item],
        plex_data_getter=lambda: (plex_movies, []),
    )

    service = MagicMock()
    controller._download_service = service

    controller.openInPlex(0)

    service.open_in_plex.assert_called_once_with(
        item.title,
        plex_movies,
        [],
        year=item.year,
        season=item.season,
        imdb_id=item.imdb_id,
        plex_rating_key=item.plex_rating_key,
    )
