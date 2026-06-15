"""System Tray Manager — PySide6 QSystemTrayIcon wrapper for ScanHound."""

import ctypes
import logging
import platform
from PySide6.QtCore import QObject, Signal, Slot, QTimer
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction
from PySide6.QtWidgets import QSystemTrayIcon, QMenu

logger = logging.getLogger(__name__)


class TrayManager(QObject):
    """Manages the system tray icon, context menu, and minimize-to-tray behavior."""

    # Signals for QML
    showWindowRequested = Signal()
    scanRequested = Signal()
    stopScanRequested = Signal()
    settingsRequested = Signal()
    quitRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tray: QSystemTrayIcon | None = None
        self._menu: QMenu | None = None
        self._enabled = False
        self._scanning = False
        self._status_text = "Idle"
        self._scan_action: QAction | None = None
        self._stop_action: QAction | None = None
        self._status_action: QAction | None = None

    @property
    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def setup(self, enabled: bool = True):
        """Create and show the tray icon if enabled and available."""
        self._enabled = enabled
        if not enabled or not self.available:
            logger.info(f"System tray: enabled={enabled}, available={self.available}")
            return

        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(self._create_icon("idle"))
        self._tray.setToolTip("ScanHound — Idle")
        self._tray.activated.connect(self._on_activated)

        # Context menu
        self._menu = QMenu()
        show_action = self._menu.addAction("Show ScanHound")
        show_action.triggered.connect(self.showWindowRequested.emit)

        self._menu.addSeparator()

        self._scan_action = self._menu.addAction("Start Scan")
        self._scan_action.triggered.connect(self.scanRequested.emit)

        self._stop_action = self._menu.addAction("Stop Scan")
        self._stop_action.triggered.connect(self.stopScanRequested.emit)
        self._stop_action.setVisible(False)

        self._menu.addSeparator()

        settings_action = self._menu.addAction("Settings")
        settings_action.triggered.connect(self.settingsRequested.emit)

        self._menu.addSeparator()

        self._status_action = self._menu.addAction("Status: Idle")
        self._status_action.setEnabled(False)

        self._menu.addSeparator()

        quit_action = self._menu.addAction("Quit")
        quit_action.triggered.connect(self.quitRequested.emit)

        self._tray.setContextMenu(self._menu)
        self._tray.show()
        logger.info("System tray initialized")

    def teardown(self):
        """Hide and cleanup tray icon."""
        if self._tray:
            self._tray.hide()
            self._tray = None
        if self._menu:
            self._menu.deleteLater()
            self._menu = None

    @Slot(bool)
    def setScanningState(self, scanning: bool):
        """Update tray icon and menu based on scan state."""
        self._scanning = scanning
        if not self._tray:
            return

        if scanning:
            self._status_text = "Scanning..."
            self._tray.setIcon(self._create_icon("scanning"))
            self._tray.setToolTip("ScanHound — Scanning...")
            if self._scan_action:
                self._scan_action.setVisible(False)
            if self._stop_action:
                self._stop_action.setVisible(True)
        else:
            self._status_text = "Idle"
            self._tray.setIcon(self._create_icon("idle"))
            self._tray.setToolTip("ScanHound — Idle")
            if self._scan_action:
                self._scan_action.setVisible(True)
            if self._stop_action:
                self._stop_action.setVisible(False)

        if self._status_action:
            self._status_action.setText(f"Status: {self._status_text}")

    def showNotification(self, title: str, message: str, icon_type: str = "info"):
        """Show a tray notification balloon."""
        if not self._tray:
            return
        icon_map = {
            "info": QSystemTrayIcon.MessageIcon.Information,
            "warning": QSystemTrayIcon.MessageIcon.Warning,
            "error": QSystemTrayIcon.MessageIcon.Critical,
        }
        self._tray.showMessage(
            title, message,
            icon_map.get(icon_type, QSystemTrayIcon.MessageIcon.Information),
            5000
        )

    def _on_activated(self, reason):
        """Handle tray icon activation (double-click = show window)."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.showWindowRequested.emit()

    def _create_icon(self, status: str) -> QIcon:
        """Generate a small colored icon with 'M' letter based on status."""
        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Status-based color
        colors = {
            "idle": QColor("#4CAF50"),      # green
            "scanning": QColor("#FF9800"),  # orange
            "error": QColor("#F44336"),     # red
        }
        color = colors.get(status, colors["idle"])

        # Draw circle
        painter.setBrush(color)
        painter.setPen(QColor(0, 0, 0, 0))
        painter.drawEllipse(2, 2, size - 4, size - 4)

        # Draw 'S'
        painter.setPen(QColor("#ffffff"))
        font = QFont("Arial", 32, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), 0x0084, "S")  # AlignCenter

        painter.end()
        return QIcon(pixmap)


def get_idle_seconds() -> int:
    """Get system idle time in seconds (Windows only, 0 on other platforms)."""
    if platform.system() != "Windows":
        return 0
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount64() - lii.dwTime
            return millis // 1000
    except Exception:
        pass
    return 0
