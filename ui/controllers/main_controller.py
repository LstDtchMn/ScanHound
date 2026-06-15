"""MainController — App-level state, navigation, and lifecycle for QML."""

import logging
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, QSettings, Signal, Slot, Property, QTimer, QMetaObject, Qt, Q_ARG
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from backend.app_service import AppService, APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)


class _QtLogHandler(logging.Handler):
    """Routes Python log records to a Qt signal for QML consumption.

    Uses QMetaObject.invokeMethod to marshal calls to the main thread,
    so this handler is safe to use from any background thread.
    """

    def __init__(self, controller):
        super().__init__()
        self._controller = controller

    def emit(self, record):
        try:
            msg = self.format(record)
            level = record.levelname.lower()
            QMetaObject.invokeMethod(
                self._controller, "_logFromThread",
                Qt.QueuedConnection,
                Q_ARG(str, msg), Q_ARG(str, level),
            )
        except Exception:
            pass


class MainController(QObject):
    """Root controller exposed to QML as 'app'."""

    # Signals
    currentTabChanged = Signal()
    configReadyChanged = Signal()
    windowTitleChanged = Signal()
    logPanelVisibleChanged = Signal()
    logMessage = Signal(str, str)  # (message, level)
    snackbarMessage = Signal(str)
    startupWarning = Signal(str)  # emitted for each startup issue
    schedulerActiveChanged = Signal()
    schedulerNextRunChanged = Signal()
    minimizeToTrayChanged = Signal()
    settingsRequested = Signal()
    plexStatsChanged = Signal()

    def __init__(self, backend: AppService, tray=None, notifications=None, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._current_tab = 0
        self._config_ready = False
        self._log_panel_visible = False
        self._settings = QSettings("ScanHound", "ScanHound")
        self._post_init_hooks: list = []
        self._tray = tray
        self._notifications = notifications
        self._engine: Optional[QQmlApplicationEngine] = None

        # Scheduler status
        self._scheduler_active = False
        self._scheduler_next_run = ""

        # Plex library counts (from DB cache)
        self._plex_movie_count = 0
        self._plex_tv_season_count = 0

        # Install Qt log handler on root logger
        self._log_handler = _QtLogHandler(self)
        self._log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(self._log_handler)

    @Slot(str, str)
    def _logFromThread(self, msg: str, level: str):
        """Slot target for _QtLogHandler — emits logMessage on the main thread."""
        self.logMessage.emit(msg, level)

    def setEngine(self, engine: QQmlApplicationEngine):
        """Store QML engine reference for window management."""
        self._engine = engine

    # ── Properties ────────────────────────────────────────────────────

    @Property(int, notify=currentTabChanged)
    def currentTab(self):
        return self._current_tab

    @currentTab.setter
    def currentTab(self, value):
        if self._current_tab != value:
            self._current_tab = value
            self.currentTabChanged.emit()

    @Property(bool, notify=configReadyChanged)
    def configReady(self):
        return self._config_ready

    @Property(str, notify=windowTitleChanged)
    def windowTitle(self):
        return f"{APP_NAME} {APP_VERSION}"

    @Property(bool, notify=logPanelVisibleChanged)
    def logPanelVisible(self):
        return self._log_panel_visible

    @Property(bool, notify=schedulerActiveChanged)
    def schedulerActive(self):
        return self._scheduler_active

    @Property(str, notify=schedulerNextRunChanged)
    def schedulerNextRun(self):
        return self._scheduler_next_run

    @Property(bool, notify=minimizeToTrayChanged)
    def minimizeToTray(self):
        return self._backend.config.get("minimize_to_tray", False)

    @Property(bool, constant=True)
    def trayAvailable(self):
        return self._tray is not None and self._tray.available

    @Property(int, notify=plexStatsChanged)
    def plexMovieCount(self):
        return self._plex_movie_count

    @Property(int, notify=plexStatsChanged)
    def plexTvSeasonCount(self):
        return self._plex_tv_season_count

    # ── Window geometry ────────────────────────────────────────────────

    @Slot(int, int, int, int)
    def saveGeometry(self, x, y, w, h):
        """Save window position and size."""
        self._settings.setValue("window/x", x)
        self._settings.setValue("window/y", y)
        self._settings.setValue("window/width", w)
        self._settings.setValue("window/height", h)

    @Slot(result=int)
    def savedX(self):
        return int(self._settings.value("window/x", -1))

    @Slot(result=int)
    def savedY(self):
        return int(self._settings.value("window/y", -1))

    @Slot(result=int)
    def savedWidth(self):
        return int(self._settings.value("window/width", 1600))

    @Slot(result=int)
    def savedHeight(self):
        return int(self._settings.value("window/height", 950))

    # ── Slots (callable from QML) ─────────────────────────────────────

    @Slot(int)
    def switchTab(self, index):
        self.currentTab = index

    def addPostInitHook(self, fn):
        """Register a function to call after backend startup completes."""
        self._post_init_hooks.append(fn)

    @Slot()
    def initialize(self):
        """Called once QML is loaded — start backend services."""
        warnings = self._backend.startup()

        # Re-install Qt log handler (setup_logging() clears root handlers)
        root_logger = logging.getLogger()
        if self._log_handler not in root_logger.handlers:
            root_logger.addHandler(self._log_handler)

        for w in warnings:
            self.startupWarning.emit(w)
            logger.warning(w)

        # Configure notifications from config
        if self._notifications:
            try:
                self._notifications.configure(self._backend.config)
            except Exception as e:
                logger.warning(f"Notification bridge config failed: {e}")

        # Setup system tray
        if self._tray:
            tray_enabled = self._backend.config.get("enable_system_tray", False)
            self._tray.setup(enabled=tray_enabled)

        # Update scheduler status
        self._update_scheduler_status()

        self._config_ready = True
        self.configReadyChanged.emit()

        # Load Plex library counts from DB cache
        self.refreshPlexStats()

        # Auto-connect to Plex in background
        self._auto_connect_plex()

        # Run post-init hooks (e.g. FM auto-watcher)
        for fn in self._post_init_hooks:
            try:
                fn()
            except Exception as e:
                logger.warning(f"Post-init hook error: {e}")

    @Slot()
    def shutdown(self):
        """Graceful cleanup."""
        if self._tray:
            self._tray.teardown()
        if self._notifications:
            self._notifications.shutdown()
        self._backend.shutdown()

    @Slot()
    def requestQuit(self):
        """Quit the application (from tray menu or Ctrl+Q)."""
        if self._engine:
            roots = self._engine.rootObjects()
            if roots:
                roots[0].close()
        QGuiApplication.quit()

    @Slot(result=bool)
    def shouldMinimizeToTray(self):
        """Check if window close should minimize to tray instead of quitting."""
        return (
            self._backend.config.get("minimize_to_tray", False)
            and self._tray is not None
            and self._tray.available
        )

    @Slot()
    def hideToTray(self):
        """Hide the main window (minimize to tray)."""
        if self._engine:
            roots = self._engine.rootObjects()
            if roots:
                roots[0].hide()
        if self._tray:
            self._tray.showNotification(
                "ScanHound", "Minimized to system tray", "info"
            )

    @Slot()
    def toggleLogPanel(self):
        self._log_panel_visible = not self._log_panel_visible
        self.logPanelVisibleChanged.emit()

    @Slot()
    def requestSettings(self):
        self.settingsRequested.emit()

    @Slot(str)
    def showSnackbar(self, message):
        self.snackbarMessage.emit(message)

    @Slot(result=str)
    def appName(self):
        return APP_NAME

    @Slot(result=str)
    def appVersion(self):
        return APP_VERSION

    # ── Plex stats ─────────────────────────────────────────────────────

    @Slot()
    def refreshPlexStats(self):
        """Reload Plex library counts from the DB cache."""
        try:
            counts = self._backend.db.plex_cache_counts()
            self._plex_movie_count = counts.get("movies", 0)
            self._plex_tv_season_count = counts.get("tv_seasons", 0)
            self.plexStatsChanged.emit()
        except Exception as e:
            logger.debug("Failed to load Plex stats: %s", e)

    # ── Plex auto-connect ──────────────────────────────────────────────

    plexConnectedChanged = Signal()

    @Slot()
    def _emitPlexConnectedChanged(self):
        """Slot wrapper so invokeMethod can trigger the signal from any thread."""
        self.plexConnectedChanged.emit()

    def _auto_connect_plex(self):
        """Attempt Plex connection in background on startup."""
        config = self._backend.config
        conn_mode = config.get("plex_connection_mode", "direct")

        # Check if credentials are configured
        if conn_mode == "account":
            if not config.get("plex_username") or not config.get("plex_password"):
                logger.info("Plex auto-connect skipped: account credentials not configured")
                return
        else:
            if not config.get("plex_url") or not config.get("plex_token"):
                logger.info("Plex auto-connect skipped: URL/token not configured")
                return

        def _connect():
            try:
                from backend.plex_service import PlexService
                plex_svc = PlexService(config, self._backend.db, self._backend.plex_manager)
                success, msg = plex_svc.connect()
                if success:
                    logger.info(f"Plex connected: {msg}")
                else:
                    logger.warning(f"Plex auto-connect failed: {msg}")
                QMetaObject.invokeMethod(self, "_emitPlexConnectedChanged", Qt.QueuedConnection)
            except Exception as e:
                logger.warning(f"Plex auto-connect error: {e}")

        threading.Thread(target=_connect, name="plex-auto-connect", daemon=True).start()

    # ── Scheduler status ──────────────────────────────────────────────

    def _update_scheduler_status(self):
        """Read scheduler state from backend config."""
        enabled = self._backend.config.get("scheduler_enabled", False)
        if enabled != self._scheduler_active:
            self._scheduler_active = enabled
            self.schedulerActiveChanged.emit()

        if enabled:
            interval = self._backend.config.get("scheduler_interval", 24)
            last = self._backend.config.get("last_scan_time", 0)
            if last > 0:
                next_ts = last + (interval * 3600)
                remaining = max(0, next_ts - time.time())
                hours = int(remaining // 3600)
                mins = int((remaining % 3600) // 60)
                self._scheduler_next_run = f"{hours}h {mins}m"
            else:
                self._scheduler_next_run = f"In {interval}h"
            self.schedulerNextRunChanged.emit()
