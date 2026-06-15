"""ScanHound — PySide6 QML Entry Point.

Bootstraps the Qt application, creates the backend services and UI
controllers, loads the QML interface, and wires up system tray integration.
"""

import logging
import sys
import os

# Windows: set AppUserModelID so the taskbar shows our icon, not Python's
if sys.platform == "win32":
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ScanHound.ScanHound.3.0")

# Must set style BEFORE QApplication is created
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Material"

# QApplication (not QGuiApplication) required for QSystemTrayIcon / QMenu
from PySide6.QtWidgets import QApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtGui import QIcon

from backend.app_service import AppService
from backend.notification_bridge import NotificationBridge
from ui.controllers.main_controller import MainController
from ui.controllers.scanner_controller import ScannerController
from ui.controllers.settings_controller import SettingsController
from ui.controllers.source_search_controller import SourceSearchController
from ui.system_tray import TrayManager

logger = logging.getLogger(__name__)


def main():
    """Application entry point — initializes Qt, backend, and QML UI."""
    app = QApplication(sys.argv)
    app.setOrganizationName("ScanHound")
    app.setApplicationName("ScanHound")
    app.setQuitOnLastWindowClosed(False)  # Keep running in tray

    # Window icon (appears in title bar, taskbar, alt-tab)
    # Use PNG with multiple sizes for crisp rendering on Windows taskbar
    assets = os.path.join(os.path.dirname(__file__), "assets")
    icon = QIcon()
    for size in [16, 32, 48, 64, 128, 256]:
        png = os.path.join(assets, f"icon_{size}.png")
        if os.path.exists(png):
            icon.addFile(png)
    if icon.isNull():
        svg = os.path.join(assets, "icon.svg")
        if os.path.exists(svg):
            icon = QIcon(svg)
    app.setWindowIcon(icon)

    # -- Backend service layer --
    backend = AppService()

    # Notification bridge (sync wrapper for the async notification system)
    notif_bridge = NotificationBridge()

    # System tray icon and menu
    tray = TrayManager()

    # -- UI controllers (bridge between QML and backend) --
    main_ctrl = MainController(backend, tray=tray, notifications=notif_bridge)
    scanner_ctrl = ScannerController(backend, notifications=notif_bridge)
    settings_ctrl = SettingsController(backend)
    source_search_ctrl = SourceSearchController(backend)

    # -- QML engine setup --
    engine = QQmlApplicationEngine()

    qml_dir = os.path.join(os.path.dirname(__file__), "ui", "qml")
    engine.addImportPath(qml_dir)

    # Expose controllers to QML as context properties
    engine.rootContext().setContextProperty("app", main_ctrl)
    engine.rootContext().setContextProperty("scanner", scanner_ctrl)
    engine.rootContext().setContextProperty("settings", settings_ctrl)
    engine.rootContext().setContextProperty("sourceSearch", source_search_ctrl)

    # Load the root QML document
    qml_file = os.path.join(qml_dir, "main.qml")
    engine.load(qml_file)

    if not engine.rootObjects():
        logger.critical("Failed to load QML. Check console for errors.")
        sys.exit(1)

    # -- System tray signal wiring --
    def _show_window():
        """Bring the main window to the foreground when tray icon is activated."""
        roots = engine.rootObjects()
        if roots:
            roots[0].show()
            roots[0].raise_()
            roots[0].requestActivate()

    tray.showWindowRequested.connect(_show_window)
    tray.scanRequested.connect(scanner_ctrl.startScan)
    tray.stopScanRequested.connect(scanner_ctrl.stopScan)
    tray.quitRequested.connect(main_ctrl.requestQuit)

    # Update tray icon when scanning state changes
    scanner_ctrl.scanningChanged.connect(
        lambda: tray.setScanningState(scanner_ctrl.scanning)
    )

    # Forward Plex connection state from main_ctrl to scanner_ctrl
    main_ctrl.plexConnectedChanged.connect(scanner_ctrl.plexConnectedChanged.emit)

    # Store engine ref on main_ctrl for minimize-to-tray support
    main_ctrl.setEngine(engine)

    # Ensure clean shutdown when the application is about to quit
    app.aboutToQuit.connect(main_ctrl.shutdown)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
