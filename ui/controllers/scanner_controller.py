"""ScannerController — Manages scan state, results, and filtering for QML."""

import logging
import os
import re
import threading
from typing import Dict
from PySide6.QtCore import QObject, QThread, Signal, Slot, Property, QMetaObject, Qt, Q_ARG

from backend.app_service import (
    AppService, LRUCache, clean_string,
    STATUS_MISSING, STATUS_DOWNLOADED, STATUS_IN_LIBRARY, STATUS_IN_LIBRARY_CHECK,
    STATUS_UPGRADE_4K, STATUS_UPGRADE_SIZE, STATUS_UPGRADE_SIZE_DV, STATUS_DV_UPGRADE,
    COLOR_MISSING, COLOR_DOWNLOADED, COLOR_IN_LIBRARY, COLOR_UPGRADE, COLOR_DV_UPGRADE,
    RESOLUTION_ORDER,
)
from backend.scanner_service import ScannerService, MediaItem, ScanStatus
from backend.plex_service import PlexService
from backend.matching import MatchingEngine
from backend.scrapers import WebScrapers
from backend.auto_grab_service import AutoGrabService
from backend.sources.registry import get_registry
from ui.controllers.download_controller import DownloadController
from ui.models.results_model import ResultsModel

logger = logging.getLogger(__name__)

# Emoji constants (matching original app.py)
EMOJI_4K = "🎬"
EMOJI_DV = "[DV]"
EMOJI_INFO = "ℹ️"
EMOJI_WARNING = "⚠️"


class _ScannerAppBridge:
    """Adapter providing the interface MatchingEngine/WebScrapers expect from parent_app."""

    def __init__(self, backend: AppService):
        self._backend = backend
        self.tmdb_cache = backend.tmdb_cache
        self.omdb_cache = backend.omdb_cache
        self.download_history: set = set()

        # Constants expected by MatchingEngine
        self.STATUS_MISSING = STATUS_MISSING
        self.STATUS_DOWNLOADED = STATUS_DOWNLOADED
        self.STATUS_IN_LIBRARY = STATUS_IN_LIBRARY
        self.STATUS_IN_LIBRARY_CHECK = STATUS_IN_LIBRARY_CHECK
        self.STATUS_UPGRADE_4K = STATUS_UPGRADE_4K
        self.STATUS_UPGRADE_SIZE = STATUS_UPGRADE_SIZE
        self.STATUS_UPGRADE_SIZE_DV = STATUS_UPGRADE_SIZE_DV
        self.STATUS_DV_UPGRADE = STATUS_DV_UPGRADE
        self.COLOR_MISSING = COLOR_MISSING
        self.COLOR_DOWNLOADED = COLOR_DOWNLOADED
        self.COLOR_IN_LIBRARY = COLOR_IN_LIBRARY
        self.COLOR_UPGRADE = COLOR_UPGRADE
        self.COLOR_DV_UPGRADE = COLOR_DV_UPGRADE
        self.RESOLUTION_ORDER = RESOLUTION_ORDER
        self.EMOJI_4K = EMOJI_4K
        self.EMOJI_DV = EMOJI_DV
        self.EMOJI_INFO = EMOJI_INFO
        self.EMOJI_WARNING = EMOJI_WARNING

    @property
    def config(self):
        return self._backend.config

    def clean_string(self, s: str) -> str:
        return clean_string(s)

    def safe_log(self, message: str, level: str = "info"):
        getattr(logger, level if level != "success" else "info", logger.info)(message)

    def log(self, message: str, level: str = "info"):
        self.safe_log(message, level)

    @staticmethod
    def parse_size(size_str: str) -> float:
        """Parse size string to GB (float)."""
        try:
            if not size_str or size_str in ["-", "?", "Unknown"]:
                return 0.0
            s = str(size_str).upper().replace(" ", "")
            numeric = re.sub(r'[A-Z]+', '', s).replace(",", "")
            if not numeric:
                return 0.0
            if "TB" in s or "TIB" in s:
                return float(numeric) * 1024
            if "GB" in s or "GIB" in s:
                return float(numeric)
            if "MB" in s or "MIB" in s:
                return float(numeric) / 1024
            return float(s.replace(",", ""))
        except (ValueError, TypeError):
            return 0.0


class ScanWorker(QThread):
    """Background thread for running scans."""

    progress = Signal(float, str)   # (0.0–1.0, message)
    logMessage = Signal(str, str)   # (message, level)
    finished = Signal(list)         # List[MediaItem]
    error = Signal(str)

    def __init__(self, scanner: ScannerService, scan_type: str, source: str,
                 pages: int, flags: dict, search_query: str,
                 use_expired_cache: bool = False, plex_refresh_mode: str = "auto", parent=None):
        super().__init__(parent)
        self._scanner = scanner
        self._scan_type = scan_type
        self._source = source
        self._pages = pages
        self._flags = flags
        self._search_query = search_query
        self._use_expired_cache = use_expired_cache
        self._plex_refresh_mode = plex_refresh_mode

    def run(self):
        try:
            self._scanner.set_progress_callback(
                lambda v, t: self.progress.emit(v, t)
            )
            self._scanner.set_log_callback(
                lambda m, l: self.logMessage.emit(m, l)
            )
            results = self._scanner.run_scan(
                scan_type=self._scan_type,
                source_type=self._source,
                pages=self._pages,
                resolution_flags=self._flags,
                search_query=self._search_query,
                use_expired_cache=self._use_expired_cache,
                plex_refresh_mode=self._plex_refresh_mode,
            )
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class PrepareWorker(QThread):
    """Background thread for scan preparation (service init + cache check)."""

    cacheExpired = Signal(str)
    cacheCheckPassed = Signal()
    launchScanRequested = Signal(bool)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, controller, mode: str, use_expired: bool = False, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._mode = mode  # "check", "choice", "direct"
        self._use_expired = use_expired

    def run(self):
        try:
            self._controller._ensure_services()
            if self._mode == "check":
                scan_type = self._controller._scan_type
                if scan_type in ("Incremental", "Loaded Scan"):
                    is_valid, msg = self._controller._plex.check_cache_status()
                    if not is_valid:
                        self.cacheExpired.emit(msg)
                        self.cancelled.emit()
                        return
                self.cacheCheckPassed.emit()
            elif self._mode == "choice":
                self.launchScanRequested.emit(self._use_expired)
            elif self._mode == "direct":
                self.cacheCheckPassed.emit()
        except Exception as e:
            logger.error("Scan preparation failed: %s", e)
            self.failed.emit(str(e))


class PlexRefreshWorker(QThread):
    """Background thread for refreshing Plex connection."""

    logMessage = Signal(str, str)
    plexConnectedChanged = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller

    def run(self):
        try:
            self._controller._ensure_services()
            if self._controller._plex:
                success, msg = self._controller._plex.connect()
                self.logMessage.emit(
                    f"Plex refresh: {msg}" if msg else "Plex refreshed",
                    "info" if success else "warning"
                )
                self.plexConnectedChanged.emit()
        except Exception as e:
            self.logMessage.emit(f"Plex refresh failed: {e}", "error")


class AutoGrabWorker(QThread):
    """Background thread for auto-grab after scan completes."""

    logMessage = Signal(str, str)
    autoGrabComplete = Signal(int, int)

    def __init__(self, controller, results, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._results = results

    def run(self):
        try:
            self._controller._downloads.ensure_service()
            dl_service = self._controller._downloads._download_service
            auto_grab = AutoGrabService(self._controller._backend.config, dl_service)
            auto_grab.set_log_callback(
                lambda m, l: self.logMessage.emit(m, l)
            )
            report = auto_grab.process_items(self._results)
            self.autoGrabComplete.emit(report.grabbed, report.evaluated)
        except Exception as e:
            self.logMessage.emit(f"Auto-Grab error: {e}", "error")


class ScannerController(QObject):
    """Controls the Scanner tab — scan execution, results model, filtering."""

    # Signals
    scanningChanged = Signal()
    progressChanged = Signal()
    progressTextChanged = Signal()
    logMessage = Signal(str, str)      # (message, level)
    scrapeProgress = Signal(int, int, str)  # (current, total, item_title)
    scrapeDone = Signal()
    duplicateWarning = Signal(str)     # warning about duplicate selections
    scanComplete = Signal(int)        # item count
    autoGrabComplete = Signal(int, int)  # (grabbed_count, total_evaluated)
    plexConnectedChanged = Signal()
    tmdbSearchResults = Signal(str)   # JSON string of search results
    cacheExpired = Signal(str)            # cache age message → QML shows choice dialog
    _cacheCheckPassed = Signal()          # internal: cache check OK → start scan on main thread
    _launchScanRequested = Signal(bool)   # internal: start worker on main thread after prep
    _scheduledScanRequested = Signal()  # internal: marshal scheduler thread → main thread
    _scanPrepCancelled = Signal()         # internal: cache expired → reset scanning on main thread
    _scanPrepFailed = Signal(str)         # internal: preparation error → main thread error handler

    # Scan config signals
    scanTypeChanged = Signal()
    sourceNamesChanged = Signal()
    sourceChanged = Signal()
    pagesChanged = Signal()
    searchQueryChanged = Signal()
    plexRefreshModeChanged = Signal()
    categoriesChanged = Signal()

    # Filter signals
    statusFilterChanged = Signal()
    filterTextChanged = Signal()
    quickFiltersChanged = Signal()

    # Sort signals
    sortColumnChanged = Signal()
    sortAscendingChanged = Signal()

    # Pagination signals
    pageChanged = Signal()
    totalPagesChanged = Signal()

    # Global selection signal (counts across all pages, not just current)
    selectedCountChanged = Signal()

    def __init__(self, backend: AppService, notifications=None, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._notifications = notifications

        # Scanner service (lazy init after backend startup)
        self._scanner: ScannerService = None
        self._plex: PlexService = None
        self._worker: ScanWorker = None

        # Results model
        self._results_model = ResultsModel(self)
        self._results_model.duplicateWarning.connect(self.duplicateWarning)
        # Forward per-page selection changes as global count changes
        self._results_model.selectedCountChanged.connect(self.selectedCountChanged)

        # Download controller (owns DownloadService + download slots)
        # Pass a getter so downloads can find selected items across all pages, not just current
        self._downloads = DownloadController(
            backend, self._results_model,
            all_items_getter=lambda: list(self._all_items),
            plex_data_getter=lambda: (
                self._plex.plex_movies if self._plex else [],
                self._plex.plex_tv if self._plex else [],
            ),
            parent=self,
        )
        self._downloads.logMessage.connect(self.logMessage)
        self._downloads.scrapeProgress.connect(self.scrapeProgress)
        self._downloads.scrapeDone.connect(self.scrapeDone)

        # Scan state
        self._scanning = False
        self._progress = 0.0
        self._progress_text = ""
        self._services_lock = threading.Lock()

        # Scan config
        self._scan_type = "Deep Scan"
        self._source = "HDEncode"
        self._pages = 1
        self._search_query = ""
        self._plex_refresh_mode = self._normalized_plex_refresh_mode(
            self._backend.config.get("plex_refresh_mode", "auto")
        )

        # Per-source category definitions: [{key, label, default}]
        self._SOURCE_CATEGORIES = {
            "HDEncode": [
                {"key": "4k", "label": "4K", "default": True},
                {"key": "remux", "label": "Remux", "default": False},
                {"key": "tv", "label": "TV", "default": False},
            ],
            "DDLBase": [
                {"key": "4k_webdl", "label": "4K", "default": True},
                {"key": "4k_remux", "label": "4K Remux", "default": True},
                {"key": "1080p_remux", "label": "1080p Remux", "default": True},
            ],
            "Adit-HD": [
                {"key": "4k", "label": "4K", "default": True},
                {"key": "remux", "label": "Remux", "default": False},
                {"key": "tv", "label": "TV", "default": False},
            ],
        }
        # Active category checked states {key: bool}
        self._category_flags: Dict[str, bool] = {}
        self._init_category_flags()

        # All raw results from last scan (unfiltered)
        self._all_items = []

        # Filter state
        self._status_filter = ""  # "" = all, "missing", "upgrade", etc.
        self._filter_text = ""    # text search within results
        # Quick resolution/DV filter state per chip: "off" | "exclude" | "only"
        self._quick_filters: dict = {"720p": "off", "1080p": "off", "4K": "off", "DV": "off"}

        # Sort state
        self._sort_column = ""
        self._sort_ascending = True

        # Pagination state
        self._page = 0
        self._page_size = 50
        self._filtered_items = []  # all items after status+text filter (before pagination)

        # Register shutdown hook
        self._backend.add_shutdown_hook(self._shutdown)

        # Register scheduler trigger (marshals to main thread via signal)
        self._scheduledScanRequested.connect(self.startScan)
        self._cacheCheckPassed.connect(lambda: self._launchScanWorker(False))
        self._launchScanRequested.connect(self._launchScanWorker)
        self._scanPrepCancelled.connect(self._onScanPrepCancelled)
        self._scanPrepFailed.connect(self._on_scan_error)
        self._backend.set_scan_trigger(self._on_scheduled_scan)

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

    # ── Shutdown ──────────────────────────────────────────────────────

    def _shutdown(self):
        """Stop background scan worker."""
        if self._scanner:
            self._scanner.stop_scan_flag = True
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(5000)

    def _on_scheduled_scan(self):
        """Called from scheduler background thread — emits signal to marshal to main thread."""
        self._scheduledScanRequested.emit()

    # ── Lazy init ─────────────────────────────────────────────────────

    def _ensure_services(self):
        with self._services_lock:
            if self._scanner is None:
                self._bridge = _ScannerAppBridge(self._backend)
                self._plex = PlexService(
                    self._backend.config,
                    self._backend.db,
                    self._backend.plex_manager,
                )
                # Connect Plex if not already connected
                if not self._backend.plex_manager.is_connected:
                    success, msg = self._plex.connect()
                    if not success:
                        logger.warning("Plex connection failed: %s", msg)
                scrapers = WebScrapers(self._bridge)
                matching = MatchingEngine(self._bridge)
                self._scanner = ScannerService(
                    config=self._backend.config,
                    db=self._backend.db,
                    scrapers=scrapers,
                    matching=matching,
                    plex_service=self._plex,
                    tmdb_cache=self._backend.tmdb_cache,
                    omdb_cache=self._backend.omdb_cache,
                )
                self._downloads.ensure_service()

    # ── Properties ────────────────────────────────────────────────────

    @Property(bool, notify=scanningChanged)
    def scanning(self):
        return self._scanning

    @Property(float, notify=progressChanged)
    def progress(self):
        return self._progress

    @Property(str, notify=progressTextChanged)
    def progressText(self):
        return self._progress_text

    @Property(QObject, constant=True)
    def resultsModel(self):
        return self._results_model

    @Property(bool, notify=plexConnectedChanged)
    def plexConnected(self):
        if self._backend.plex_manager:
            return self._backend.plex_manager.is_connected
        return False

    @Property("QStringList", notify=sourceNamesChanged)
    def sourceNames(self):
        """Dynamic source list from the plugin registry."""
        try:
            registry = get_registry()
            registry.sync_from_config(self._backend.config)
            sources = registry.list_sources()
            names = [s["display_name"] for s in sources if s.get("enabled", True)]
            if names:
                return names
        except Exception as e:
            logger.warning("Failed to load source registry: %s", e)
        return ["HDEncode", "DDLBase", "Adit-HD"]

    @Property(str, notify=scanTypeChanged)
    def scanType(self):
        return self._scan_type

    @scanType.setter
    def scanType(self, value):
        if self._scan_type != value:
            self._scan_type = value
            self.scanTypeChanged.emit()

    @Property(str, notify=sourceChanged)
    def source(self):
        return self._source

    @source.setter
    def source(self, value):
        if self._source != value:
            self._source = value
            self.sourceChanged.emit()

    @Property(int, notify=pagesChanged)
    def pages(self):
        return self._pages

    @pages.setter
    def pages(self, value):
        if self._pages != value:
            self._pages = max(1, min(99, value))
            self.pagesChanged.emit()

    @Property(str, notify=searchQueryChanged)
    def searchQuery(self):
        return self._search_query

    @searchQuery.setter
    def searchQuery(self, value):
        if self._search_query != value:
            self._search_query = value
            self.searchQueryChanged.emit()

    @Property(str, notify=plexRefreshModeChanged)
    def plexRefreshMode(self):
        return self._plex_refresh_mode

    @plexRefreshMode.setter
    def plexRefreshMode(self, value):
        value = self._normalized_plex_refresh_mode(value)
        if self._plex_refresh_mode != value:
            self._plex_refresh_mode = value
            self._backend.config["plex_refresh_mode"] = value
            self.plexRefreshModeChanged.emit()

    @staticmethod
    def _normalized_plex_refresh_mode(value: str) -> str:
        return value if value in ("auto", "force_refresh", "cache_only") else "auto"

    @Property(str, notify=statusFilterChanged)
    def statusFilter(self):
        return self._status_filter

    @Property(str, notify=filterTextChanged)
    def filterText(self):
        return self._filter_text

    @Property(int, notify=pageChanged)
    def page(self):
        return self._page

    @Property(int, notify=totalPagesChanged)
    def totalPages(self):
        if not self._filtered_items:
            return 1
        return max(1, (len(self._filtered_items) + self._page_size - 1) // self._page_size)

    @Property(str, notify=pageChanged)
    def pageInfo(self):
        """'1-50 of 120' style label for QML."""
        total = len(self._filtered_items)
        if total == 0:
            return "0 items"
        start = self._page * self._page_size + 1
        end = min(start + self._page_size - 1, total)
        return f"{start}-{end} of {total}"

    @Property(int, notify=totalPagesChanged)
    def filteredCount(self):
        return len(self._filtered_items)

    @Property(int, notify=totalPagesChanged)
    def totalCount(self):
        return len(self._all_items)

    @Property(int, notify=selectedCountChanged)
    def selectedCount(self):
        """Total selected items across ALL pages."""
        return sum(1 for i in self._all_items if i.selected)

    @Property(int, notify=selectedCountChanged)
    def rgCount(self):
        return sum(1 for i in self._all_items if i.host_pref == "RG" and i.selected)

    @Property(int, notify=selectedCountChanged)
    def nfCount(self):
        return sum(1 for i in self._all_items if i.host_pref == "NF" and i.selected)

    @Property(str, notify=selectedCountChanged)
    def rgSize(self):
        total = sum(self._parse_size_gb(i.size) for i in self._all_items if i.host_pref == "RG" and i.selected)
        return f"{total:.1f}" if total > 0 else "0"

    @Property(str, notify=selectedCountChanged)
    def nfSize(self):
        total = sum(self._parse_size_gb(i.size) for i in self._all_items if i.host_pref == "NF" and i.selected)
        return f"{total:.1f}" if total > 0 else "0"

    # ── Slots ─────────────────────────────────────────────────────────

    @Slot()
    def checkCacheBeforeScan(self):
        """Pre-check cache before starting scan. Shows immediate feedback."""
        if self._scanning:
            return
        self._setPreparing()

        self._prepare_worker = PrepareWorker(self, mode="check", parent=self)
        self._prepare_worker.cacheExpired.connect(self.cacheExpired)
        self._prepare_worker.cancelled.connect(self._onScanPrepCancelled)
        self._prepare_worker.cacheCheckPassed.connect(lambda: self._launchScanWorker(False))
        self._prepare_worker.failed.connect(self._on_scan_error)
        self._prepare_worker.start()

    @Slot(bool)
    def startScanWithCacheChoice(self, useExpired):
        """Start scan after user chose how to handle expired cache."""
        if self._scanning:
            return
        self._setPreparing()

        self._prepare_worker = PrepareWorker(self, mode="choice", use_expired=useExpired, parent=self)
        self._prepare_worker.launchScanRequested.connect(self._launchScanWorker)
        self._prepare_worker.failed.connect(self._on_scan_error)
        self._prepare_worker.start()

    @Slot()
    def startScan(self):
        """Start scan without cache dialog (used by scheduled scans)."""
        if self._scanning:
            return
        self._setPreparing()

        self._prepare_worker = PrepareWorker(self, mode="direct", parent=self)
        self._prepare_worker.cacheCheckPassed.connect(lambda: self._launchScanWorker(False))
        self._prepare_worker.failed.connect(self._on_scan_error)
        self._prepare_worker.start()

    def _setPreparing(self):
        """Show immediate visual feedback when a scan is initiating."""
        self._scanning = True
        self._progress = 0.0
        self._progress_text = "Preparing scan..."
        self.scanningChanged.emit()
        self.progressChanged.emit()
        self.progressTextChanged.emit()

    def _onScanPrepCancelled(self):
        """Reset scanning state on main thread when cache check leads to user dialog."""
        self._scanning = False
        self.scanningChanged.emit()

    def _launchScanWorker(self, use_expired_cache: bool = False):
        """Create and start the background scan worker."""
        self._ensure_services()
        self._progress_text = "Starting scan..."
        self.progressTextChanged.emit()

        # Disconnect previous worker signals to avoid stale callbacks
        if self._worker is not None:
            try:
                self._worker.progress.disconnect(self._on_progress)
                self._worker.logMessage.disconnect(self._on_log)
                self._worker.finished.disconnect(self._on_scan_finished)
                self._worker.error.disconnect(self._on_scan_error)
            except RuntimeError:
                pass  # Already disconnected

        flags = dict(self._category_flags)

        self._worker = ScanWorker(
            scanner=self._scanner,
            scan_type=self._scan_type,
            source=self._source,
            pages=self._pages,
            flags=flags,
            search_query=self._search_query,
            use_expired_cache=use_expired_cache,
            plex_refresh_mode=self._plex_refresh_mode,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.logMessage.connect(self._on_log)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.error.connect(self._on_scan_error)
        self._worker.start()

    @Slot()
    def stopScan(self):
        if self._scanner:
            self._scanner.stop_scan_flag = True

    @Slot()
    def refreshPlex(self):
        """Reload Plex libraries in the background."""
        self._plex_refresh_worker = PlexRefreshWorker(self, parent=self)
        self._plex_refresh_worker.logMessage.connect(self._on_log)
        self._plex_refresh_worker.plexConnectedChanged.connect(self.plexConnectedChanged)
        self._plex_refresh_worker.start()

    @Slot()
    def refreshSourceNames(self):
        """Re-read source registry and notify QML of changes."""
        self.sourceNamesChanged.emit()

    @Slot(int)
    def toggleSelection(self, row):
        self._results_model.toggleSelection(row)
        self.selectedCountChanged.emit()  # Update global RG/NF/selected tallies

    @Slot(bool)
    def selectAll(self, selected):
        """Select / deselect ALL items across all pages, not just the current page."""
        for item in self._all_items:
            item.selected = selected
        self._update_page_model()  # Refreshes visible page + emits selectedCountChanged

    @Slot(int)
    def toggleHostPref(self, row):
        self._results_model.toggleHostPref(row)
        self.selectedCountChanged.emit()  # Update RG/NF size/count tallies

    @Slot(str)
    def toggleGroupCollapse(self, groupKey):
        self._results_model.toggleGroupCollapse(groupKey)

    @Slot(str)
    def setStatusFilter(self, status):
        if self._status_filter != status:
            self._status_filter = status
            self.statusFilterChanged.emit()
            self._page = 0
            self._apply_filter()

    @Property(str, notify=sortColumnChanged)
    def sortColumn(self):
        return self._sort_column

    @Property(bool, notify=sortAscendingChanged)
    def sortAscending(self):
        return self._sort_ascending

    @Slot(str)
    def setSortColumn(self, column):
        """Toggle sort on a column. Click same column toggles direction, new column sorts ascending."""
        if self._sort_column == column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True
        self.sortColumnChanged.emit()
        self.sortAscendingChanged.emit()
        self._page = 0
        self._apply_filter()

    # ── Category system ─────────────────────────────────────────────

    def _init_category_flags(self):
        """Reset category flags to defaults for current source."""
        cats = self._SOURCE_CATEGORIES.get(self._source, [])
        self._category_flags = {c["key"]: c["default"] for c in cats}

    @Property(int, notify=categoriesChanged)
    def categoryCount(self):
        return len(self._SOURCE_CATEGORIES.get(self._source, []))

    @Slot(int, result=str)
    def categoryLabel(self, index):
        cats = self._SOURCE_CATEGORIES.get(self._source, [])
        if 0 <= index < len(cats):
            return cats[index]["label"]
        return ""

    @Slot(int, result=bool)
    def categoryChecked(self, index):
        cats = self._SOURCE_CATEGORIES.get(self._source, [])
        if 0 <= index < len(cats):
            return self._category_flags.get(cats[index]["key"], False)
        return False

    @Slot(int, bool)
    def setCategoryChecked(self, index, checked):
        cats = self._SOURCE_CATEGORIES.get(self._source, [])
        if 0 <= index < len(cats):
            self._category_flags[cats[index]["key"]] = checked

    # Legacy flag setters (kept for compatibility)
    @Slot(bool)
    def setFlag4k(self, value):
        pass

    @Slot(bool)
    def setFlag1080p(self, value):
        pass

    @Slot(bool)
    def setFlagRemux(self, value):
        pass

    @Slot(bool)
    def setFlagTv(self, value):
        pass

    @Slot(str)
    def setScanType(self, value):
        self.scanType = value

    @Slot(str)
    def setSource(self, value):
        self.source = value
        self._init_category_flags()
        self.categoriesChanged.emit()

    @Slot(int)
    def setPages(self, value):
        self.pages = value

    @Slot(str)
    def setSearchQuery(self, value):
        self.searchQuery = value

    @Slot(str)
    def setPlexRefreshMode(self, mode):
        """Set Plex refresh mode: 'auto', 'force_refresh', or 'cache_only'."""
        if mode in ("auto", "force_refresh", "cache_only"):
            self.plexRefreshMode = mode

    @Slot()
    def reloadPreferences(self):
        """Reload scanner-level preferences from the shared config."""
        self.plexRefreshMode = self._backend.config.get("plex_refresh_mode", "auto")

    @Slot(str)
    def setFilterText(self, text):
        if self._filter_text != text:
            self._filter_text = text
            self.filterTextChanged.emit()
            self._page = 0
            self._apply_filter()

    @Property(str, notify=quickFiltersChanged)
    def quickFilters(self):
        import json
        return json.dumps(self._quick_filters)

    @Slot(str)
    def toggleQuickFilter(self, name):
        """Cycle a quick filter chip: off → exclude → only → off."""
        if name not in self._quick_filters:
            return
        cycle = {"off": "exclude", "exclude": "only", "only": "off"}
        self._quick_filters[name] = cycle[self._quick_filters[name]]
        self.quickFiltersChanged.emit()
        self._page = 0
        self._apply_filter()

    @Slot()
    def nextPage(self):
        if self._page < self.totalPages - 1:
            self._page += 1
            self._update_page_model()

    @Slot()
    def prevPage(self):
        if self._page > 0:
            self._page -= 1
            self._update_page_model()

    # ── Download delegations (→ DownloadController) ─────────────────

    @Slot(str)
    def openUrl(self, url):
        self._downloads.openUrl(url)

    @Slot(str, str)
    def saveToHistory(self, url, title):
        self._downloads.saveToHistory(url, title)

    @Slot(str)
    def exportResultsCsv(self, filepath):
        self._downloads.exportResultsCsv(filepath)

    @Slot()
    def downloadSelected(self):
        self._downloads.downloadSelected()

    @Slot()
    def sendSelectedToJD(self):
        self._downloads.sendSelectedToJD()

    @Slot(int)
    def downloadItem(self, row):
        self._downloads.downloadItem(row)

    @Slot()
    def copySelectedToClipboard(self):
        self._downloads.copySelectedToClipboard()

    @Slot(int)
    def openInPlex(self, row):
        self._downloads.openInPlex(row)

    @Slot(int, result=str)
    def getHistoryJson(self, limit):
        return self._downloads.getHistoryJson(limit)

    @Slot()
    def clearHistory(self):
        self._downloads.clearHistory()

    # ── TMDB Search / Watchlist ────────────────────────────────────────

    @Slot(str, str)
    def tmdbSearch(self, query, type_filter):
        """Search TMDB in background, emit results as JSON signal."""
        import threading, json

        def _search():
            try:
                self._ensure_services()
                import requests
                api_key = self._backend.config.get("tmdb_api_key", "")
                if not api_key:
                    self.tmdbSearchResults.emit("[]")
                    return
                from urllib.parse import quote
                url = f"https://api.themoviedb.org/3/search/multi?api_key={api_key}&query={quote(query)}&page=1"
                resp = requests.get(url, timeout=10)
                data = resp.json().get("results", [])
                # Filter by type if specified
                if type_filter == "movie":
                    data = [d for d in data if d.get("media_type") == "movie"]
                elif type_filter == "tv":
                    data = [d for d in data if d.get("media_type") == "tv"]
                else:
                    data = [d for d in data if d.get("media_type") in ("movie", "tv")]
                self.tmdbSearchResults.emit(json.dumps(data[:20]))
            except Exception as e:
                logger.warning("TMDB search failed: %s", e)
                self.tmdbSearchResults.emit("[]")

        threading.Thread(target=_search, daemon=True).start()

    @Slot(str)
    def addToWatchlist(self, item_json):
        """Add a TMDB item to the watchlist."""
        import json
        from backend.watchlist import WatchlistItem
        try:
            data = json.loads(item_json)
            if self._backend.watchlist_manager:
                # Map TMDB search result fields to WatchlistItem fields
                media_type = data.get("media_type", "movie")
                item_type = "tv_show" if media_type == "tv" else "movie"
                title = data.get("title") or data.get("name", "?")
                date_str = data.get("release_date") or data.get("first_air_date", "")
                year = int(date_str[:4]) if date_str and len(date_str) >= 4 else None
                wl_item = WatchlistItem.from_dict({
                    "title": title,
                    "year": year,
                    "tmdb_id": str(data["id"]) if data.get("id") else None,
                    "imdb_id": data.get("imdb_id"),
                    "item_type": item_type,
                })
                self._backend.watchlist_manager.add(wl_item)
                self.logMessage.emit(f"Added to watchlist: {title}", "info")
        except Exception as e:
            logger.warning("Failed to add to watchlist: %s", e)

    @Slot(result=str)
    def getWatchlistJson(self):
        """Return watchlist items as JSON."""
        import json
        try:
            if self._backend.watchlist_manager:
                items = self._backend.watchlist_manager.get_all()
                return json.dumps([i.to_dict() for i in items])
        except Exception as e:
            logger.warning("Failed to get watchlist: %s", e)
        return "[]"

    @Slot(int, str)
    def removeFromWatchlist(self, item_id, media_type):
        """Remove an item from the watchlist."""
        try:
            if self._backend.watchlist_manager:
                self._backend.watchlist_manager.remove(item_id)
        except Exception as e:
            logger.warning("Failed to remove from watchlist: %s", e)

    @Slot()
    def clearWatchlist(self):
        """Clear all watchlist items."""
        try:
            if self._backend.watchlist_manager:
                self._backend.watchlist_manager.clear()
                self.logMessage.emit("Watchlist cleared", "info")
        except Exception as e:
            logger.warning("Failed to clear watchlist: %s", e)

    @Slot(result=str)
    def getAnalyticsJson(self):
        """Return analytics/stats as JSON."""
        import json
        try:
            if self._backend.stats_dashboard:
                stats = self._backend.stats_dashboard.get_dashboard_summary()
                return json.dumps(stats)
            # Fallback: basic stats from Plex if analytics module unavailable
            return json.dumps({
                "total_items": 0, "items_4k": 0, "items_1080p": 0,
                "items_4k_hdr": 0, "tv_seasons": 0,
                "total_scans": 0, "items_scanned": 0, "downloads": 0,
            })
        except Exception as e:
            logger.warning("Failed to get analytics: %s", e)
            return "{}"

    @Slot()
    def exportAnalytics(self):
        """Export analytics report to JSON file."""
        import json
        try:
            stats_json = self.getAnalyticsJson()
            filepath = os.path.join(os.path.expanduser("~"), "analytics_report.json")
            with open(filepath, "w") as f:
                json.dump(json.loads(stats_json), f, indent=2)
            self.logMessage.emit(f"Analytics exported to {filepath}", "info")
        except Exception as e:
            logger.warning("Failed to export analytics: %s", e)

    # ── Internal handlers ─────────────────────────────────────────────

    def _on_progress(self, value: float, text: str):
        self._progress = value
        self._progress_text = text
        self.progressChanged.emit()
        self.progressTextChanged.emit()

    def _on_log(self, message: str, level: str):
        self.logMessage.emit(message, level)

    def _on_scan_finished(self, results):
        self._all_items = list(results)
        # Detect and mark duplicate groups for visual grouping
        if self._scanner:
            self._scanner.detect_duplicate_groups(self._all_items)
        self._scanning = False
        self._progress = 1.0
        self._progress_text = f"Complete — {len(results)} items"
        self.scanningChanged.emit()
        self.progressChanged.emit()
        self.progressTextChanged.emit()
        self._page = 0
        self._apply_filter()
        self.scanComplete.emit(len(results))

        # Send notification
        if self._notifications:
            missing = sum(1 for i in results if i.status == ScanStatus.MISSING)
            upgrades = sum(1 for i in results
                          if i.status in (ScanStatus.UPGRADE, ScanStatus.DV_UPGRADE))
            self._notifications.notify_scan_complete(len(results), missing, upgrades)

        # Auto-Grab: process qualifying items after scan
        if self._backend.config.get("auto_grab_enabled", False):
            self._run_auto_grab(results)

    def _run_auto_grab(self, results):
        """Run auto-grab in a background thread after scan completes."""
        self._auto_grab_worker = AutoGrabWorker(self, results, parent=self)
        self._auto_grab_worker.logMessage.connect(self._on_log)
        self._auto_grab_worker.autoGrabComplete.connect(self.autoGrabComplete)
        self._auto_grab_worker.start()

    def _on_scan_error(self, error_msg: str):
        self._scanning = False
        self._progress_text = f"Error: {error_msg}"
        self.scanningChanged.emit()
        self.progressTextChanged.emit()
        self.logMessage.emit(f"Scan error: {error_msg}", "error")

    def _apply_filter(self):
        """Apply status + text filter + sort, then update paginated view."""
        items = self._all_items

        # Status filter
        if self._status_filter:
            try:
                status = ScanStatus(self._status_filter)
                items = [i for i in items if i.status == status]
            except ValueError:
                pass

        # Text filter
        if self._filter_text:
            query = self._filter_text.lower()
            items = [i for i in items if query in (i.title or "").lower()]

        # Resolution filter (exclude_720p)
        if self._backend.config.get("exclude_720p", False):
            items = [i for i in items if i.resolution != "720p"]

        # Quick filter chips (720p / 1080p / 4K / DV)
        for chip_name, chip_state in self._quick_filters.items():
            if chip_state == "off":
                continue
            if chip_name == "DV":
                match_fn = lambda i: bool(i.dovi)
            else:
                match_fn = lambda i, r=chip_name: (i.resolution or "").upper() == r.upper()
            if chip_state == "exclude":
                items = [i for i in items if not match_fn(i)]
            else:  # "only"
                items = [i for i in items if match_fn(i)]

        # Sort
        if self._sort_column:
            reverse = not self._sort_ascending
            key_map = {
                "title": lambda i: (i.title or "").lower(),
                "year": lambda i: i.year or 0,
                "rating": lambda i: i.rating or 0,
                "size": lambda i: self._parse_size_gb(i.size),
                "status": lambda i: (i.status.value if i.status else ""),
                "genre": lambda i: (i.genres[0] if i.genres else "").lower() if isinstance(i.genres, list) else (i.genres or "").lower(),
                "resolution": lambda i: (i.resolution or "").lower(),
                "date": lambda i: ScannerService._posted_date_sort_key(i),
            }
            key_fn = key_map.get(self._sort_column)
            if key_fn:
                try:
                    items = sorted(items, key=key_fn, reverse=reverse)
                except Exception:
                    pass
        else:
            # Default: preserve scan/website order (newest first) but pull all
            # versions of a duplicate group together at the first occurrence.
            seen: set = set()
            groups: dict = {}
            for item in items:
                gk = item.group_key or id(item)
                if gk not in groups:
                    groups[gk] = []
                groups[gk].append(item)
            ordered: list = []
            for item in items:
                gk = item.group_key or id(item)
                if gk not in seen:
                    seen.add(gk)
                    ordered.extend(groups[gk])
            items = ordered

        self._filtered_items = items
        self.totalPagesChanged.emit()
        self._update_page_model()

    @staticmethod
    def _parse_size_gb(size_str: str) -> float:
        """Parse size string to float GB for sorting."""
        try:
            if not size_str or size_str in ["-", "?", "Unknown"]:
                return 0.0
            # Strip per-episode suffix: "45.6 GB (~3.8 GB/ep)" → "45.6 GB"
            s = str(size_str).split("(")[0].strip().upper().replace(" ", "")
            if "TB" in s:
                return float(re.sub(r'[A-Z]+', '', s)) * 1024
            if "GB" in s:
                return float(re.sub(r'[A-Z]+', '', s))
            if "MB" in s:
                return float(re.sub(r'[A-Z]+', '', s)) / 1024
            return 0.0
        except (ValueError, TypeError):
            return 0.0

    def _update_page_model(self):
        """Slice filtered items by current page and push to model."""
        start = self._page * self._page_size
        end = start + self._page_size
        page_items = self._filtered_items[start:end]
        self._results_model.setItems(page_items)
        self.pageChanged.emit()
        self.selectedCountChanged.emit()  # Global count may differ from page count
        self.totalPagesChanged.emit()
