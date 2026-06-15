"""DownloadController — Handles downloads, JDownloader, clipboard, history.

Extracted from ScannerController to separate download concerns from scan logic.
Registered as a child of ScannerController; QML calls are delegated.
"""

import json
import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot, QMetaObject, Qt, Q_ARG

from backend.download_service import DownloadService
from backend.scanner_service import ScanStatus, STATUS_COLORS, STATUS_TEXTS

logger = logging.getLogger(__name__)


class DownloadItemWorker(QThread):
    """Background thread for downloading a single item."""

    logMessage = Signal(str, str)
    markDownloaded = Signal(list)

    def __init__(self, download_service, item, service_type, parent=None):
        super().__init__(parent)
        self._download_service = download_service
        self._item = item
        self._service_type = service_type

    def run(self):
        try:
            result = self._download_service.download_item(
                url=self._item.url, title=self._item.title,
                season=self._item.season, resolution=self._item.resolution,
                size=self._item.size, service_type=self._service_type,
            )
            if result["success"] and result.get("history_saved", True):
                self.markDownloaded.emit([self._item])
            self.logMessage.emit(
                result["message"], "info" if result["success"] else "error")
        except Exception as e:
            self.logMessage.emit(f"Download failed: {e}", "error")


class ScrapeAndCopyWorker(QThread):
    """Background thread for scraping download links and copying to clipboard."""

    logMessage = Signal(str, str)
    scrapeProgress = Signal(int, int, str)
    scrapeDone = Signal()
    markDownloaded = Signal(list)

    def __init__(self, download_service, selected_items, save_history_fn, parent=None):
        super().__init__(parent)
        self._download_service = download_service
        self._selected = selected_items
        self._save_history_fn = save_history_fn

    def run(self):
        try:
            all_links = []
            total = len(self._selected)
            for i, item in enumerate(self._selected, 1):
                service_type = "Nitroflare" if item.host_pref == "NF" else "Rapidgator"
                self.scrapeProgress.emit(i, total, item.title)
                self.logMessage.emit(
                    f"Scraping {i}/{total}: {item.title} ({service_type})", "info")
                try:
                    links = self._download_service.scrape_links(
                        item.url, service_type)
                    if links:
                        all_links.extend(links)
                        logger.info("Scraped %d %s links from %s",
                                    len(links), service_type, item.url)
                    else:
                        logger.warning("No %s links found for %s",
                                       service_type, item.url)
                except Exception as e:
                    logger.error("Failed to scrape %s: %s", item.url, e)

            self.scrapeDone.emit()
            if all_links:
                if self._download_service.copy_to_clipboard(all_links):
                    downloaded = []
                    self.logMessage.emit(
                        f"Copied {len(all_links)} download links to clipboard", "info")
                    for item in self._selected:
                        if self._save_history_fn(item):
                            downloaded.append(item)
                    self.markDownloaded.emit(downloaded)
                else:
                    self.logMessage.emit("Failed to copy to clipboard", "error")
            else:
                self.logMessage.emit(
                    "No download links found — try a different host", "warning")
        except Exception as e:
            self.logMessage.emit(f"Scrape and copy failed: {e}", "error")


class DownloadController(QObject):
    """Download-related operations for the Scanner tab."""

    logMessage = Signal(str, str)          # (message, level)
    scrapeProgress = Signal(int, int, str)  # (current, total, item_title)
    scrapeDone = Signal()
    markDownloadedUrlsRequested = Signal(list)

    def __init__(self, backend, results_model, all_items_getter=None,
                 plex_data_getter=None, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._results_model = results_model
        self._all_items_getter = all_items_getter  # callable → list of all items (across pages)
        self._plex_data_getter = plex_data_getter  # callable → (plex_movies, plex_tv)
        self._download_service: DownloadService | None = None
        self.markDownloadedUrlsRequested.connect(self._apply_downloaded_urls)

    @Slot(str, str)
    def _logFromThread(self, msg: str, level: str):
        """Thread-safe log emission — called via invokeMethod from background threads."""
        self.logMessage.emit(msg, level)

    def _log_threadsafe(self, msg: str, level: str = "info"):
        """Emit logMessage safely from any thread via QueuedConnection."""
        QMetaObject.invokeMethod(
            self, "_logFromThread", Qt.QueuedConnection,
            Q_ARG(str, msg), Q_ARG(str, level),
        )

    def _get_all_items(self):
        """Return all items across all pages; falls back to current-page model items."""
        if self._all_items_getter is not None:
            return self._all_items_getter()
        return self._results_model.getItems()

    def ensure_service(self):
        """Lazily create the DownloadService."""
        if self._download_service is None:
            self._download_service = DownloadService(
                self._backend.config, self._backend.db
            )

    @staticmethod
    def _set_downloaded_state(item):
        item.status = ScanStatus.DOWNLOADED
        item.status_text = STATUS_TEXTS[ScanStatus.DOWNLOADED]
        item.color = STATUS_COLORS[ScanStatus.DOWNLOADED]
        item.downloaded_siblings = []

    def _save_item_to_history(self, item) -> bool:
        return bool(self._download_service.save_to_history(
            item.url, item.title, item.season, item.resolution, item.size
        ))

    def _mark_items_downloaded(self, items):
        urls = []
        for item in items:
            if not item or not getattr(item, "url", ""):
                continue
            self._set_downloaded_state(item)
            urls.append(item.url)

        if urls:
            self.markDownloadedUrlsRequested.emit(urls)

    @Slot(list)
    def _apply_downloaded_urls(self, urls):
        self._results_model.markDownloadedUrls(urls)

    @property
    def service(self) -> DownloadService | None:
        return self._download_service

    # ── Slots ──────────────────────────────────────────────────────────

    @Slot(str)
    def openUrl(self, url):
        """Open a URL in the default browser."""
        self.ensure_service()
        self._download_service.open_url(url)

    @Slot(str, str)
    def saveToHistory(self, url, title):
        """Save a download to history."""
        self.ensure_service()
        if self._download_service.save_to_history(url, title, None, "", ""):
            self.markDownloadedUrlsRequested.emit([url])

    @Slot(str)
    def exportResultsCsv(self, filepath):
        """Export scan results to CSV."""
        self.ensure_service()
        items = self._results_model.getItems()
        self._download_service.export_results_csv(items, filepath)

    @Slot()
    def downloadSelected(self):
        """Open URLs of all selected items in browser."""
        self.ensure_service()
        items = self._get_all_items()
        selected = [i for i in items if i.selected and i.url]
        downloaded = []
        for item in selected:
            self._download_service.open_url(item.url)
            if self._save_item_to_history(item):
                downloaded.append(item)
        self._mark_items_downloaded(downloaded)
        self.logMessage.emit(f"Opened {len(selected)} URLs", "info")

    @Slot()
    def sendSelectedToJD(self):
        """Send selected item URLs to JDownloader."""
        self.ensure_service()
        items = self._get_all_items()
        selected = [i for i in items if i.selected and i.url]
        if not selected:
            self.logMessage.emit("No items selected", "warning")
            return
        links = [i.url for i in selected]
        success = self._download_service.send_to_jdownloader(links, "ScanHound Scan")
        if success:
            downloaded = []
            for item in selected:
                if self._save_item_to_history(item):
                    downloaded.append(item)
            self._mark_items_downloaded(downloaded)
            self.logMessage.emit(f"Sent {len(links)} links to JDownloader", "info")
        else:
            self.logMessage.emit("Failed to send to JDownloader", "error")

    @Slot(int)
    def downloadItem(self, row):
        """Download a single item — scrape links, send to JD or clipboard."""
        self.ensure_service()
        item = self._results_model.getItem(row)
        if not item:
            return
        if not item.url:
            self.logMessage.emit("No URL for this item", "warning")
            return

        # Use per-item host_pref instead of global config
        service_type = "Nitroflare" if item.host_pref == "NF" else "Rapidgator"

        self._download_worker = DownloadItemWorker(
            self._download_service, item, service_type, parent=self)
        self._download_worker.logMessage.connect(self._logFromThread)
        self._download_worker.markDownloaded.connect(self._mark_items_downloaded)
        self._download_worker.start()

    @Slot()
    def copySelectedToClipboard(self):
        """Scrape actual download links from selected items and copy to clipboard."""
        self.ensure_service()
        items = self._get_all_items()
        selected = [i for i in items if i.selected and i.url]
        if not selected:
            logger.warning("Copy to clipboard: no items selected (total items: %d, selected w/o url: %d)",
                           len(items), sum(1 for i in items if i.selected))
            self.logMessage.emit("No items selected", "warning")
            return

        self.logMessage.emit(f"Scraping links from {len(selected)} item(s)...", "info")

        self._scrape_worker = ScrapeAndCopyWorker(
            self._download_service, selected, self._save_item_to_history,
            parent=self)
        self._scrape_worker.logMessage.connect(self._logFromThread)
        self._scrape_worker.scrapeProgress.connect(self.scrapeProgress)
        self._scrape_worker.scrapeDone.connect(self.scrapeDone)
        self._scrape_worker.markDownloaded.connect(self._mark_items_downloaded)
        self._scrape_worker.start()

    @Slot(int)
    def openInPlex(self, row):
        """Open item in Plex web interface."""
        self.ensure_service()
        item = self._results_model.getItem(row)
        if not item:
            return
        if self._plex_data_getter:
            plex_movies, plex_tv = self._plex_data_getter()
        else:
            plex_movies, plex_tv = [], []
        self._download_service.open_in_plex(
            item.title,
            plex_movies,
            plex_tv,
            year=item.year,
            season=item.season,
            imdb_id=item.imdb_id,
            plex_rating_key=getattr(item, "plex_rating_key", None),
        )

    @Slot(int, result=str)
    def getHistoryJson(self, limit):
        """Return download history as JSON string for QML."""
        try:
            if not self._backend.db:
                return "[]"
            rows = self._backend.db.get_download_history(limit=limit)
            items = []
            for r in rows:
                items.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "resolution": r.get("resolution", ""),
                    "size": r.get("size", ""),
                    "date": r.get("downloaded_at", ""),
                })
            return json.dumps(items)
        except Exception as e:
            logger.warning(f"Failed to get history: {e}")
            return "[]"

    @Slot()
    def clearHistory(self):
        """Clear all download history."""
        try:
            if self._backend.db:
                self._backend.db.clear_history()
                self.logMessage.emit("Download history cleared", "info")
        except Exception as e:
            logger.warning(f"Failed to clear history: {e}")
