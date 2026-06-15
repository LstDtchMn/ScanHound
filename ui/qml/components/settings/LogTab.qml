import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../../style" as Style

Rectangle {
    id: logTab
    color: Style.Theme.surface

    property string filterLevel: ""

    // Internal log model — populated from startup buffer + live signals
    ListModel {
        id: logModel
    }

    // Pre-populate with buffered entries captured since app start (chunked to avoid UI freeze)
    property var _pendingHistory: []
    property int _historyIdx: 0

    Timer {
        id: chunkLoader
        interval: 0
        repeat: true
        running: false
        onTriggered: {
            var end = Math.min(logTab._historyIdx + 50, logTab._pendingHistory.length)
            for (var i = logTab._historyIdx; i < end; i++) {
                logModel.append({ message: logTab._pendingHistory[i].message, level: logTab._pendingHistory[i].level })
            }
            logTab._historyIdx = end
            if (logTab._historyIdx >= logTab._pendingHistory.length) {
                running = false
                logTab._pendingHistory = []
            }
        }
    }

    Component.onCompleted: {
        _pendingHistory = settings.getLogHistory()
        _historyIdx = 0
        if (_pendingHistory.length > 0)
            chunkLoader.running = true
    }

    // Connect to scanner log signal
    Connections {
        target: scanner
        function onLogMessage(message, level) {
            if (logModel.count > 1000) logModel.remove(0, logModel.count - 800)
            logModel.append({ message: message, level: level })
        }
    }

    // Connect to app log signal
    Connections {
        target: app
        function onLogMessage(message, level) {
            if (logModel.count > 1000) logModel.remove(0, logModel.count - 800)
            logModel.append({ message: message, level: level })
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.Theme.spacingMedium
        spacing: Style.Theme.spacingSmall

        // Header + controls
        RowLayout {
            Layout.fillWidth: true
            spacing: Style.Theme.spacingSmall

            Label {
                text: "Application Log"
                font.pixelSize: Style.Theme.fontSizeLarge
                font.bold: true
                color: Style.Theme.textPrimary
            }

            Item { Layout.fillWidth: true }

            // Level filter pills
            Repeater {
                model: [
                    { label: "All",     value: "" },
                    { label: "Info",    value: "info" },
                    { label: "Warning", value: "warning" },
                    { label: "Error",   value: "error" }
                ]

                delegate: Rectangle {
                    required property var modelData
                    width: pillLabel.implicitWidth + 16; height: 24
                    radius: 12
                    color: logTab.filterLevel === modelData.value
                           ? Style.Theme.accent
                           : pillMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight

                    Label {
                        id: pillLabel
                        anchors.centerIn: parent
                        text: modelData.label
                        font.pixelSize: Style.Theme.fontSizeSmall
                        color: logTab.filterLevel === modelData.value ? "#ffffff" : Style.Theme.textSecondary
                    }

                    MouseArea {
                        id: pillMouse
                        anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: logTab.filterLevel = modelData.value
                    }
                }
            }
        }

        // Search + toggles
        RowLayout {
            Layout.fillWidth: true
            spacing: Style.Theme.spacingSmall

            TextField {
                id: logSearchField
                Layout.fillWidth: true
                placeholderText: "Search logs..."
                color: Style.Theme.textPrimary
                font.pixelSize: Style.Theme.fontSizeSmall
                property string debouncedText: ""
                onTextChanged: logSearchTimer.restart()
                Timer { id: logSearchTimer; interval: 200; onTriggered: logSearchField.debouncedText = logSearchField.text }
                background: Rectangle {
                    radius: 4
                    color: Style.Theme.surfaceLight
                    border.color: logSearchField.activeFocus ? Style.Theme.accent : Style.Theme.border
                    border.width: 1
                }
            }

            CheckBox {
                id: autoScrollToggle
                text: "Auto-scroll"
                checked: true
                Material.accent: Style.Theme.accent
            }

            CheckBox {
                id: verboseToggle
                text: "Verbose"
                checked: settings.getBool("verbose_logging")
                onToggled: settings.setVerboseLogging(checked)
                Material.accent: Style.Theme.accent
                ToolTip.visible: hovered
                ToolTip.delay: 700
                ToolTip.text: "Enable DEBUG-level logging (shows scraper details, confidence scoring, cache hits). Takes effect immediately without restart."
            }
        }

        // Log view
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight
            clip: true

            ListView {
                id: logListView
                anchors.fill: parent
                anchors.margins: 4
                model: logModel
                spacing: 1
                cacheBuffer: 200

                delegate: Item {
                    required property string message
                    required property string level
                    required property int index

                    visible: {
                        var matchLevel = logTab.filterLevel === "" || level === logTab.filterLevel
                        var matchSearch = logSearchField.debouncedText.length === 0 || message.toLowerCase().indexOf(logSearchField.debouncedText.toLowerCase()) >= 0
                        return matchLevel && matchSearch
                    }
                    width: logListView.width
                    height: visible ? logLabel.implicitHeight + 4 : 0

                    Label {
                        id: logLabel
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.leftMargin: 4
                        text: message
                        font.pixelSize: 11
                        font.family: "Consolas"
                        color: level === "error"   ? "#e74c3c"
                             : level === "warning" ? "#f39c12"
                             : level === "success" ? "#27ae60"
                             : level === "debug"   ? Style.Theme.textMuted
                             : Style.Theme.textSecondary
                        wrapMode: Text.Wrap
                    }
                }

                onCountChanged: {
                    if (autoScrollToggle.checked) {
                        Qt.callLater(function() { logListView.positionViewAtEnd() })
                    }
                }
            }
        }

        // Action buttons + log settings
        RowLayout {
            Layout.fillWidth: true
            spacing: Style.Theme.spacingSmall

            Button {
                text: "Clear"
                onClicked: logModel.clear()
                background: Rectangle {
                    radius: 4
                    color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                }
                contentItem: Label {
                    text: parent.text
                    color: Style.Theme.textSecondary
                    font.pixelSize: Style.Theme.fontSizeSmall
                    horizontalAlignment: Text.AlignHCenter
                }
            }

            Button {
                text: "Reload"
                onClicked: {
                    logModel.clear()
                    var history = settings.getLogHistory()
                    for (var i = 0; i < history.length; i++) {
                        logModel.append({ message: history[i].message, level: history[i].level })
                    }
                }
                background: Rectangle {
                    radius: 4
                    color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                }
                contentItem: Label {
                    text: parent.text
                    color: Style.Theme.textSecondary
                    font.pixelSize: Style.Theme.fontSizeSmall
                    horizontalAlignment: Text.AlignHCenter
                }
                ToolTip.visible: hovered
                ToolTip.delay: 700
                ToolTip.text: "Reload the full log from the in-memory buffer (includes entries from before this tab was opened)"
            }

            Item { Layout.fillWidth: true }

            CheckBox {
                id: clearOnStartupToggle
                text: "Clear on startup"
                checked: settings.getBool("clear_logs_startup")
                onToggled: settings.setBool("clear_logs_startup", checked)
                Material.accent: Style.Theme.accent
                ToolTip.visible: hovered
                ToolTip.delay: 700
                ToolTip.text: "Delete the log file each time the app starts (keeps things tidy). Takes effect on next launch."
            }

            Label {
                text: logModel.count + " entries"
                font.pixelSize: Style.Theme.fontSizeSmall
                color: Style.Theme.textMuted
            }
        }
    }
}
