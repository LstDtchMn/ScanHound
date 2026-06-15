import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "style" as Style

ApplicationWindow {
    id: root
    visible: true
    width: 1600
    height: 950
    minimumWidth: 1000
    minimumHeight: 600
    title: app ? app.windowTitle : "ScanHound"
    color: Style.Theme.surface
    background: Item {}  // disable Material background overlay to prevent double-painting banding

    // Material theme configuration
    Material.theme: Style.Theme.darkMode ? Material.Dark : Material.Light
    Material.accent: Style.Theme.accent
    Material.primary: Style.Theme.accent
    Material.background: Style.Theme.surface
    Material.foreground: Style.Theme.textPrimary

    Component.onCompleted: {
        // Apply saved theme before first paint
        var mode = settings ? settings.getString("theme_mode") : "dark"
        Style.Theme.applyThemeMode(mode || "dark")

        // Restore saved window geometry
        var sx = app.savedX()
        var sy = app.savedY()
        var sw = app.savedWidth()
        var sh = app.savedHeight()
        if (sx >= 0 && sy >= 0) {
            root.x = sx
            root.y = sy
        }
        if (sw > 0 && sh > 0) { root.width = sw; root.height = sh }
        app.initialize()
    }

    onClosing: function(close) {
        app.saveGeometry(root.x, root.y, root.width, root.height)
        // Minimize to tray instead of quitting (if enabled)
        if (app.shouldMinimizeToTray()) {
            close.accepted = false
            app.hideToTray()
            return
        }
        app.shutdown()
        close.accepted = true
    }

    // ── System theme change listener (for "system" mode) ──────────
    Connections {
        target: Qt.styleHints
        function onColorSchemeChanged() {
            if (Style.Theme.themeMode === "system")
                Style.Theme.darkMode = (Qt.styleHints.colorScheme === Qt.ColorScheme.Dark)
        }
    }

    // ── Keyboard Shortcuts ─────────────────────────────────────────
    Shortcut {
        sequence: "Ctrl+,"
        onActivated: settingsDialogLoader.active = true
    }
    Shortcut {
        sequence: "Ctrl+1"
        onActivated: app.switchTab(0)
    }
    Shortcut {
        sequence: "Ctrl+S"
        onActivated: scanner.checkCacheBeforeScan()
    }
    Shortcut {
        sequence: "Escape"
        onActivated: {
            if (scanner.scanning) scanner.stopScan()
        }
    }
    Shortcut {
        sequence: "Ctrl+A"
        onActivated: scanner.selectAll(true)
    }
    Shortcut {
        sequence: "Ctrl+L"
        onActivated: app.toggleLogPanel()
    }
    Shortcut {
        sequence: "F1"
        onActivated: shortcutsDialogLoader.active = true
    }
    Shortcut {
        sequence: "F5"
        onActivated: scanner.checkCacheBeforeScan()
    }
    Shortcut {
        sequence: "Ctrl+E"
        onActivated: scanner.exportResultsCsv("scan_results.csv")
    }
    Shortcut {
        sequence: "Ctrl+F"
        onActivated: scannerTabView.focusSearch()
    }

    // ── Top Bar + Content ─────────────────────────────────────────────
    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Content ────────────────────────────────────────────────────
        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: app ? app.currentTab : 0

            // Scanner Tab
            ScannerTab { id: scannerTabView }
        }

        // ── Log panel (collapsible) ─────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: app && app.logPanelVisible ? 180 : 0
            visible: app ? app.logPanelVisible : false
            color: "transparent"
            clip: true

            Behavior on Layout.preferredHeight {
                NumberAnimation { duration: 200; easing.type: Easing.OutCubic }
            }

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                // Header bar
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 24
                    color: Style.Theme.surfaceLight

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: Style.Theme.spacingSmall
                        anchors.rightMargin: Style.Theme.spacingSmall

                        Label {
                            text: "Log"
                            font.pixelSize: Style.Theme.fontSizeSmall
                            font.bold: true
                            color: Style.Theme.textSecondary
                        }

                        Item { Layout.fillWidth: true }

                        Rectangle {
                            width: clearLabel.implicitWidth + 8
                            height: 18
                            radius: 9
                            color: clearMouse.containsMouse ? Style.Theme.surfaceHover : "transparent"

                            Label {
                                id: clearLabel
                                anchors.centerIn: parent
                                text: "Clear"
                                font.pixelSize: 10
                                color: Style.Theme.textMuted
                            }

                            MouseArea {
                                id: clearMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: logListModel.clear()
                            }
                        }

                        Rectangle {
                            width: 18; height: 18; radius: 9
                            color: closeLogMouse.containsMouse ? Style.Theme.surfaceHover : "transparent"

                            Label {
                                anchors.centerIn: parent
                                text: "×"
                                font.pixelSize: 14
                                color: Style.Theme.textMuted
                            }

                            MouseArea {
                                id: closeLogMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: app.toggleLogPanel()
                            }
                        }
                    }
                }

                // Log entries
                ListView {
                    id: logListView
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.leftMargin: Style.Theme.spacingSmall
                    Layout.rightMargin: Style.Theme.spacingSmall
                    model: logListModel
                    clip: true
                    spacing: 1

                    delegate: Label {
                        required property string message
                        required property string level

                        width: logListView.width
                        text: message
                        font.pixelSize: 11
                        font.family: "Consolas"
                        color: level === "error" ? Style.Theme.error
                               : level === "warning" ? Style.Theme.warning
                               : Style.Theme.textMuted
                        wrapMode: Text.NoWrap
                        elide: Text.ElideRight
                    }

                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                    onCountChanged: {
                        if (count > 0) positionViewAtEnd()
                    }
                }
            }
        }
    }

    // Log list model (JS array-backed)
    ListModel {
        id: logListModel

        function appendLog(msg, level) {
            if (count > 500) remove(0, count - 400)
            append({ message: msg, level: level })
        }
    }

    function showSnackbarMessage(message, backgroundColor, foregroundColor) {
        snackbarLabel.text = message
        snackbar.color = backgroundColor || Style.Theme.surfaceLight
        snackbarLabel.color = foregroundColor || Style.Theme.textPrimary
        snackbarAnim.stop()
        snackbar.opacity = 0
        snackbarAnim.start()
    }

    Connections {
        target: app
        function onLogMessage(message, level) {
            logListModel.appendLog(message, level)
        }
        function onSnackbarMessage(message) {
            root.showSnackbarMessage(message, Style.Theme.surfaceLight, Style.Theme.textPrimary)
        }
        function onStartupWarning(message) {
            root.showSnackbarMessage(message, "#E65100", "#ffffff")
        }
    }

    // Connect scanner + FM log signals
    Connections {
        target: scanner
        function onLogMessage(message, level) {
            logListModel.appendLog(message, level)
        }
        function onScanComplete(count) {
            if (app) app.refreshPlexStats()
        }
    }

    Connections {
        target: scanner
        function onDuplicateWarning(message) {
            dupSnackLabel.text = message
            dupSnackFadeIn.start()
            dupSnackTimer.restart()
        }
    }

    // ── Duplicate warning snackbar ────────────────────────────────
    Rectangle {
        id: dupSnackbar
        anchors.bottom: parent.bottom
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottomMargin: 60
        width: Math.min(dupSnackRow.implicitWidth + 20, parent.width - 32)
        height: 40
        radius: 8
        color: "#E65100"
        opacity: 0
        z: 200
        visible: opacity > 0

        RowLayout {
            id: dupSnackRow
            anchors.fill: parent
            anchors.margins: 10
            spacing: 8

            Label {
                text: "\u26A0"
                font.pixelSize: 16
                color: "#ffffff"
            }
            Label {
                id: dupSnackLabel
                text: ""
                font.pixelSize: 12
                font.bold: true
                color: "#ffffff"
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
        }

        NumberAnimation on opacity { id: dupSnackFadeIn; from: 0; to: 1; duration: 200 }
        NumberAnimation on opacity { id: dupSnackFadeOut; from: 1; to: 0; duration: 300 }
        Timer { id: dupSnackTimer; interval: 4000; repeat: false; onTriggered: dupSnackFadeOut.start() }
    }

    // ── Snackbar ───────────────────────────────────────────────────
    Rectangle {
        id: snackbar
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 108
        width: snackbarLabel.implicitWidth + 40
        height: 36
        radius: 18
        color: Style.Theme.surfaceLight
        opacity: 0
        z: 100

        Label {
            id: snackbarLabel
            anchors.centerIn: parent
            font.pixelSize: Style.Theme.fontSizeSmall
            color: Style.Theme.textPrimary
        }

        SequentialAnimation {
            id: snackbarAnim
            NumberAnimation { target: snackbar; property: "opacity"; to: 1; duration: 200 }
            PauseAnimation { duration: 2500 }
            NumberAnimation { target: snackbar; property: "opacity"; to: 0; duration: 400 }
        }
    }

    // Settings request from embedded nav buttons
    Connections {
        target: app
        function onSettingsRequested() { settingsDialogLoader.active = true }
    }

    // ── Settings Dialog (lazy-loaded) ────────────────────────────────
    Loader {
        id: settingsDialogLoader
        active: false
        source: "SettingsDialog.qml"
        onLoaded: item.show()
    }
    Connections {
        target: settingsDialogLoader.item
        ignoreUnknownSignals: true
        function onClosing() { settingsDialogLoader.active = false }
    }

    // ── Shortcuts Dialog (lazy-loaded) ───────────────────────────────
    Loader {
        id: shortcutsDialogLoader
        active: false
        source: "components/ShortcutsDialog.qml"
        onLoaded: item.show()
    }
    Connections {
        target: shortcutsDialogLoader.item
        ignoreUnknownSignals: true
        function onClosing() { shortcutsDialogLoader.active = false }
    }

    // ── Status Bar ───────────────────────────────────────────────────
    footer: Rectangle {
        width: parent.width
        height: Style.Theme.statusBarHeight
        color: Style.Theme.surface

        Rectangle {
            anchors.top: parent.top
            width: parent.width
            height: 1
            color: Style.Theme.border
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Style.Theme.spacingMedium
            anchors.rightMargin: Style.Theme.spacingMedium

            Label {
                text: app && app.configReady ? "Ready" : "Initializing..."
                font.pixelSize: Style.Theme.fontSizeSmall
                color: app && app.configReady ? Style.Theme.success : Style.Theme.textMuted
            }

            // Scanner status
            Label {
                visible: scanner.scanning
                text: scanner.progressText
                font.pixelSize: Style.Theme.fontSizeSmall
                color: Style.Theme.info
            }

            Item { Layout.fillWidth: true }

            // Plex connection LED + library counts
            RowLayout {
                spacing: 4

                Rectangle {
                    width: 8; height: 8; radius: 4
                    color: scanner.plexConnected ? "#27ae60" : "#888888"
                }
                Label {
                    text: {
                        if (!scanner.plexConnected) return "Plex (offline)"
                        var mc = app ? app.plexMovieCount : 0
                        var tc = app ? app.plexTvSeasonCount : 0
                        if (mc > 0 || tc > 0) return mc + " movies | " + tc + " seasons"
                        return "Plex"
                    }
                    font.pixelSize: Style.Theme.fontSizeSmall
                    color: scanner.plexConnected ? Style.Theme.textSecondary : Style.Theme.textMuted
                }
            }

            Rectangle { width: 1; height: 12; color: Style.Theme.border }

            // Scheduler status
            Label {
                visible: app && app.schedulerActive
                text: "\u{23F1} Next: " + (app ? app.schedulerNextRun : "")
                font.pixelSize: Style.Theme.fontSizeSmall
                color: Style.Theme.info
            }

            Rectangle {
                visible: app && app.schedulerActive
                width: 1; height: 12
                color: Style.Theme.border
            }

            Label {
                text: "ScanHound"
                font.pixelSize: Style.Theme.fontSizeSmall
                color: Style.Theme.textMuted
            }
        }
    }
}
