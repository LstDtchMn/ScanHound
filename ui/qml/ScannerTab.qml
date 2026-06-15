import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "style" as Style
import "components" as Components

Rectangle {
    id: scannerTab
    color: Style.Theme.surface
    property bool _ready: false
    Component.onCompleted: _ready = true

    property int viewMode: 0  // 0=list, 1=grid
    property int tileColumns: settings.getInt("tile_columns") || 5
    property bool toolbarCompact: (width || 0) < 1500
    property bool toolbarCollapsed: (width || 0) < 1200

    function focusSearch() {
        filterTextField.forceActiveFocus()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 0
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: scannerTab.toolbarCollapsed
                                    ? Style.Theme.toolbarHeight * 2 + 4
                                    : Style.Theme.toolbarHeight
            Layout.margins: 0
            color: "transparent"
            radius: 0
            clip: true



            ColumnLayout {
                anchors.fill: parent
                spacing: 2

            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: Style.Theme.toolbarHeight
                Layout.leftMargin: Style.Theme.spacingSmall
                Layout.rightMargin: Style.Theme.spacingSmall
                spacing: 6

                // ── Nav section ──
                Label {
                    text: "ScanHound"
                    font.pixelSize: Style.Theme.fontSizeMedium
                    font.bold: true
                    color: Style.Theme.accent
                    visible: !scannerTab.toolbarCompact
                }

                Rectangle { width: 1; Layout.fillHeight: true; Layout.topMargin: 8; Layout.bottomMargin: 8; color: Style.Theme.border }

                // ── Scan controls ──
                ComboBox {
                    id: scanTypeCombo
                    model: ["Deep Scan", "Incremental", "Loaded Scan", "Site Search"]
                    currentIndex: 1
                    implicitWidth: {
                        var maxW = 0
                        for (var i = 0; i < model.length; i++) {
                            scanTypeMetrics.text = model[i]
                            maxW = Math.max(maxW, scanTypeMetrics.width)
                        }
                        return maxW + 48  // padding + dropdown arrow
                    }
                    implicitHeight: 28
                    onCurrentTextChanged: if (_ready) scanner.setScanType(currentText)
                    Material.background: Style.Theme.surface
                    Material.foreground: Style.Theme.textPrimary
                    ToolTip.visible: hovered
                    ToolTip.text: "Select scan type"
                    TextMetrics { id: scanTypeMetrics; font: scanTypeCombo.font }
                }

                ComboBox {
                    id: sourceCombo
                    model: scanner.sourceNames
                    currentIndex: 0
                    implicitWidth: {
                        var maxW = 0
                        var items = model
                        for (var i = 0; i < items.length; i++) {
                            sourceMetrics.text = items[i]
                            maxW = Math.max(maxW, sourceMetrics.width)
                        }
                        return maxW + 48
                    }
                    implicitHeight: 28
                    onCurrentTextChanged: if (_ready) scanner.setSource(currentText)
                    Material.background: Style.Theme.surface
                    Material.foreground: Style.Theme.textPrimary
                    ToolTip.visible: hovered
                    ToolTip.text: "Select content source"
                    TextMetrics { id: sourceMetrics; font: sourceCombo.font }
                }

                RowLayout {
                    visible: !scannerTab.toolbarCollapsed
                    spacing: 2

                    Label {
                        text: "Pages:"
                        color: Style.Theme.textSecondary
                        font.pixelSize: Style.Theme.fontSizeSmall
                    }

                    Rectangle {
                        width: 22; height: 22; radius: 4
                        color: pgMinMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                        Label { anchors.centerIn: parent; text: "\u2212"; font.pixelSize: 12; color: Style.Theme.textSecondary }
                        MouseArea {
                            id: pgMinMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            onClicked: { if (pagesValue.value > 1) { pagesValue.value--; scanner.setPages(pagesValue.value) } }
                        }
                    }

                    Label {
                        id: pagesValue
                        property int value: 1
                        text: value.toString()
                        font.pixelSize: Style.Theme.fontSizeSmall
                        font.bold: true
                        color: Style.Theme.textPrimary
                        Layout.preferredWidth: 16
                        horizontalAlignment: Text.AlignHCenter
                    }

                    Rectangle {
                        width: 22; height: 22; radius: 4
                        color: pgPlusMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                        Label { anchors.centerIn: parent; text: "+"; font.pixelSize: 12; color: Style.Theme.textSecondary }
                        MouseArea {
                            id: pgPlusMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            onClicked: { if (pagesValue.value < 99) { pagesValue.value++; scanner.setPages(pagesValue.value) } }
                        }
                    }
                }

                Rectangle { width: 1; Layout.fillHeight: true; Layout.topMargin: 8; Layout.bottomMargin: 8; color: Style.Theme.border }

                // Scan / Stop button
                Button {
                    text: scanner.scanning ? "Stop" : "Scan"
                    highlighted: !scanner.scanning
                    onClicked: scanner.scanning ? scanner.stopScan() : scanner.checkCacheBeforeScan()
                    implicitWidth: 65; implicitHeight: 28

                    background: Rectangle {
                        radius: Style.Theme.borderRadius
                        color: scanner.scanning
                               ? Style.Theme.error
                               : parent.hovered ? Style.Theme.accentLight : Style.Theme.accent
                    }
                    contentItem: Label {
                        text: parent.text
                        color: scanner.scanning ? "#ffffff" : Style.Theme.accentText
                        font.bold: true; font.pixelSize: Style.Theme.fontSizeSmall
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: scanner.scanning ? "Stop scan (Escape)" : "Start scan (Ctrl+S)"
                }

                // Plex refresh mode button (cycles through Auto → Force → Cache)
                Rectangle {
                    width: 28; height: 28; radius: 14
                    color: {
                        var mode = scanner.plexRefreshMode || "auto"
                        if (mode === "force_refresh") return Style.Theme.warning
                        if (mode === "cache_only") return Style.Theme.success
                        return plexModeMouse.containsMouse ? Style.Theme.surfaceHover : "transparent"
                    }
                    Label {
                        anchors.centerIn: parent
                        text: {
                            var mode = scanner.plexRefreshMode || "auto"
                            if (mode === "force_refresh") return "↻"      // Force Refresh icon
                            if (mode === "cache_only") return "⚡"        // Cache Only icon
                            return "◉"                                     // Auto icon
                        }
                        font.pixelSize: 12
                        color: {
                            var mode = scanner.plexRefreshMode || "auto"
                            if (mode === "force_refresh") return "#ffffff"
                            if (mode === "cache_only") return "#ffffff"
                            return Style.Theme.textSecondary
                        }
                    }
                    MouseArea {
                        id: plexModeMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            var cycle = {"auto": "force_refresh", "force_refresh": "cache_only", "cache_only": "auto"}
                            var mode = scanner.plexRefreshMode || "auto"
                            scanner.setPlexRefreshMode(cycle[mode] || "auto")
                        }
                    }
                    ToolTip.visible: plexModeMouse.containsMouse
                    ToolTip.text: {
                        var mode = scanner.plexRefreshMode || "auto"
                        if (mode === "auto") return "Plex: Auto (smart 5min cooldown) — click to Force Refresh"
                        if (mode === "force_refresh") return "Plex: Force Refresh (always reload) — click to Cache Only"
                        return "Plex: Cache Only (fastest, no reload) — click to Auto"
                    }
                }

                Rectangle { width: 1; Layout.fillHeight: true; Layout.topMargin: 8; Layout.bottomMargin: 8; color: Style.Theme.border; visible: !scannerTab.toolbarCollapsed }

                // Dynamic category checkboxes (change per source)
                Repeater {
                    id: catRepeater
                    model: scanner.categoryCount
                    CheckBox {
                        required property int index
                        text: scanner.categoryLabel(index)
                        checked: scanner.categoryChecked(index)
                        onToggled: scanner.setCategoryChecked(index, checked)
                        visible: !scannerTab.toolbarCollapsed
                    }
                }
                Connections {
                    target: scanner
                    function onCategoriesChanged() { catRepeater.model = 0; catRepeater.model = scanner.categoryCount }
                }

                Item { Layout.fillWidth: true }

                // ── Right-side: Plex + Utility buttons ──
                Rectangle {
                    width: 10; height: 10; radius: 5
                    color: scanner.plexConnected ? Style.Theme.success : Style.Theme.error
                    ToolTip.visible: plexLedMouse.containsMouse
                    ToolTip.text: scanner.plexConnected ? "Plex connected" : "Plex disconnected"
                    MouseArea { id: plexLedMouse; anchors.fill: parent; hoverEnabled: true }
                }

                Rectangle {
                    width: 24; height: 24; radius: 12
                    color: plexRefreshMouse.containsMouse ? Style.Theme.surfaceHover : "transparent"
                    Label { anchors.centerIn: parent; text: "\u{21BB}"; font.pixelSize: 14; color: Style.Theme.textSecondary }
                    MouseArea { id: plexRefreshMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: scanner.refreshPlex() }
                    ToolTip.visible: plexRefreshMouse.containsMouse; ToolTip.text: "Refresh Plex library"
                }

                Rectangle {
                    width: 24; height: 24; radius: 12
                    color: moreMenuMouse.containsMouse ? Style.Theme.surfaceHover : "transparent"
                    Label { anchors.centerIn: parent; text: "\u22EE"; font.pixelSize: 16; font.bold: true; color: Style.Theme.textSecondary }
                    MouseArea { id: moreMenuMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: moreMenu.open() }

                    Menu {
                        id: moreMenu; y: parent.height
                        MenuItem { text: "Analytics"; onTriggered: analyticsDialogLoader.active = true }
                        MenuItem { text: "Watchlist"; onTriggered: watchlistDialogLoader.active = true }
                        MenuItem { text: "History"; onTriggered: historyDialogLoader.active = true }
                        MenuSeparator {}
                        MenuItem { text: "Add to Watchlist..."; onTriggered: tmdbSearchDialogLoader.active = true }
                    }
                }

                Rectangle { width: 1; Layout.fillHeight: true; Layout.topMargin: 8; Layout.bottomMargin: 8; color: Style.Theme.border }

                // Log toggle
                Rectangle {
                    width: logLbl.implicitWidth + 14; height: 24; radius: 12
                    color: logTglMouse.containsMouse ? Style.Theme.surfaceHover
                           : app && app.logPanelVisible ? Style.Theme.accent : "transparent"
                    Label { id: logLbl; anchors.centerIn: parent; text: "Log"; font.pixelSize: 10; color: app && app.logPanelVisible ? Style.Theme.accentText : Style.Theme.textMuted }
                    MouseArea { id: logTglMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: app.toggleLogPanel() }
                    ToolTip.visible: logTglMouse.containsMouse; ToolTip.text: "Toggle log panel (Ctrl+L)"
                }

                // Theme toggle
                Rectangle {
                    width: 24; height: 24; radius: 12
                    color: themeMa.containsMouse ? Style.Theme.surfaceHover : "transparent"
                    Label { anchors.centerIn: parent; text: Style.Theme.darkMode ? "\u{263E}" : "\u{2600}"; font.pixelSize: 12; color: Style.Theme.textMuted }
                    MouseArea { id: themeMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { var m = Style.Theme.darkMode ? "light" : "dark"; Style.Theme.applyThemeMode(m); if (settings) settings.setString("theme_mode", m) } }
                    ToolTip.visible: themeMa.containsMouse; ToolTip.text: Style.Theme.darkMode ? "Switch to Light mode" : "Switch to Dark mode"
                }

                // Settings
                Rectangle {
                    width: 24; height: 24; radius: 12
                    color: settMa.containsMouse ? Style.Theme.surfaceHover : "transparent"
                    Label { anchors.centerIn: parent; text: "\u2699"; font.pixelSize: 13; color: Style.Theme.textMuted }
                    MouseArea { id: settMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: app.requestSettings() }
                    ToolTip.visible: settMa.containsMouse; ToolTip.text: "Settings (Ctrl+,)"
                }
            }

            // ── Overflow row (collapsed mode) ──────────────────
            RowLayout {
                visible: scannerTab.toolbarCollapsed
                Layout.fillWidth: true
                Layout.leftMargin: Style.Theme.spacingSmall
                Layout.rightMargin: Style.Theme.spacingSmall
                spacing: 6

                Label { text: "Pages:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }

                Rectangle {
                    width: 22; height: 22; radius: 4
                    color: pgMinMouse2.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                    Label { anchors.centerIn: parent; text: "\u2212"; font.pixelSize: 12; color: Style.Theme.textSecondary }
                    MouseArea {
                        id: pgMinMouse2; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: { if (pagesValue.value > 1) { pagesValue.value--; scanner.setPages(pagesValue.value) } }
                    }
                }

                Label {
                    text: pagesValue.value.toString()
                    font.pixelSize: Style.Theme.fontSizeSmall
                    font.bold: true
                    color: Style.Theme.textPrimary
                    Layout.preferredWidth: 16
                    horizontalAlignment: Text.AlignHCenter
                }

                Rectangle {
                    width: 22; height: 22; radius: 4
                    color: pgPlusMouse2.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                    Label { anchors.centerIn: parent; text: "+"; font.pixelSize: 12; color: Style.Theme.textSecondary }
                    MouseArea {
                        id: pgPlusMouse2; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: { if (pagesValue.value < 99) { pagesValue.value++; scanner.setPages(pagesValue.value) } }
                    }
                }

                Rectangle { width: 1; Layout.fillHeight: true; Layout.topMargin: 4; Layout.bottomMargin: 4; color: Style.Theme.border }

                Repeater {
                    id: catRepeater2
                    model: scanner.categoryCount
                    CheckBox {
                        required property int index
                        text: scanner.categoryLabel(index)
                        checked: scanner.categoryChecked(index)
                        onToggled: scanner.setCategoryChecked(index, checked)
                    }
                }
                Connections {
                    target: scanner
                    function onCategoriesChanged() { catRepeater2.model = 0; catRepeater2.model = scanner.categoryCount }
                }

                Item { Layout.fillWidth: true }
            }

            }  // ColumnLayout
        }

        // ── Search bar (Site Search mode) ────────────────────────────
        TextField {
            id: searchField
            Layout.fillWidth: true
            visible: scanner.scanType === "Site Search"
            placeholderText: "Enter search query..."
            color: Style.Theme.textPrimary
            font.pixelSize: Style.Theme.fontSizeNormal
            onTextChanged: searchDebounce.restart()

            Timer {
                id: searchDebounce
                interval: 250
                onTriggered: scanner.setSearchQuery(searchField.text)
            }

            background: Rectangle {
                radius: Style.Theme.borderRadius
                color: Style.Theme.surface
                border.color: searchField.activeFocus ? Style.Theme.accent : Style.Theme.border
                border.width: 1
            }
        }

        // ── Progress bar ─────────────────────────────────────────────
        ColumnLayout {
            Layout.fillWidth: true
            visible: scanner.scanning
            spacing: 2

            ProgressBar {
                Layout.fillWidth: true
                value: scanner.progress
                from: 0; to: 1

                background: Rectangle {
                    implicitHeight: 4
                    radius: 2
                    color: Style.Theme.surfaceLight
                }
                contentItem: Item {
                    Rectangle {
                        width: parent.width * scanner.progress
                        height: parent.height
                        radius: 2
                        color: Style.Theme.accent
                    }
                }
            }

            Label {
                text: scanner.progressText
                font.pixelSize: Style.Theme.fontSizeSmall
                color: Style.Theme.textMuted
            }
        }

        // ── Stats bar ────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 30
            color: Style.Theme.surfaceLight
            radius: 0
            visible: scanner.totalCount > 0

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: Style.Theme.spacingMedium
                anchors.rightMargin: Style.Theme.spacingMedium
                spacing: Style.Theme.spacingSmall

                Label {
                    text: scanner.filteredCount !== scanner.totalCount
                          ? scanner.filteredCount + " / " + scanner.totalCount
                          : scanner.totalCount + " items"
                    font.pixelSize: Style.Theme.fontSizeSmall
                    color: Style.Theme.textPrimary
                }

                // RG / NF link tally with GB totals
                Label {
                    visible: scanner.rgCount > 0
                    text: "RG: " + scanner.rgCount + " (" + scanner.rgSize + " GB)"
                    font.pixelSize: Style.Theme.fontSizeSmall
                    color: "#e67e22"
                }
                Label {
                    visible: scanner.nfCount > 0
                    text: "NF: " + scanner.nfCount + " (" + scanner.nfSize + " GB)"
                    font.pixelSize: Style.Theme.fontSizeSmall
                    color: "#2196F3"
                }

                Label {
                    text: "Selected: " + scanner.selectedCount
                    font.pixelSize: Style.Theme.fontSizeSmall
                    visible: scanner.selectedCount > 0
                    color: Style.Theme.accent
                }

                // Select all checkbox
                CheckBox {
                    id: selectAllChk
                    text: ""
                    implicitWidth: 20
                    implicitHeight: 20
                    padding: 0
                    Layout.alignment: Qt.AlignVCenter
                    onToggled: scanner.selectAll(checked)

                    ToolTip.visible: hovered
                    ToolTip.text: "Select / Deselect All"
                }

                // Download selected
                Rectangle {
                    width: dlLabel.implicitWidth + 12
                    height: 24
                    radius: 12
                    visible: scanner.selectedCount > 0
                    color: dlMouse.containsMouse ? Style.Theme.accentLight : Style.Theme.accent

                    Label {
                        id: dlLabel
                        anchors.centerIn: parent
                        text: "Download " + scanner.selectedCount
                        font.pixelSize: 10
                        font.bold: true
                        color: Style.Theme.accentText
                    }

                    MouseArea {
                        id: dlMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: scanner.downloadSelected()
                    }

                    ToolTip.visible: dlMouse.containsMouse
                    ToolTip.text: "Download selected items to browser"
                }

                // JDownloader
                Rectangle {
                    width: jdLabel.implicitWidth + 12
                    height: 24
                    radius: 12
                    visible: scanner.selectedCount > 0
                    color: jdMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight

                    Label {
                        id: jdLabel
                        anchors.centerIn: parent
                        text: "JD"
                        font.pixelSize: 10
                        font.bold: true
                        color: Style.Theme.textSecondary
                    }

                    MouseArea {
                        id: jdMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: scanner.sendSelectedToJD()
                    }

                    ToolTip.visible: jdMouse.containsMouse
                    ToolTip.text: "Send to JDownloader"
                }

                // Copy URLs button
                Rectangle {
                    Layout.preferredWidth: 26; Layout.preferredHeight: 26
                    radius: Style.Theme.borderRadius
                    color: clipMouse.containsMouse ? Style.Theme.surfaceHover : "transparent"

                    Label {
                        anchors.centerIn: parent
                        text: "\u{1F4CB}"
                        font.pixelSize: 13
                        color: Style.Theme.textSecondary
                    }

                    MouseArea {
                        id: clipMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: scanner.copySelectedToClipboard()
                    }

                    ToolTip.visible: clipMouse.containsMouse
                    ToolTip.text: "Copy selected URLs to clipboard"
                }

                // Separator
                Rectangle { width: 1; Layout.fillHeight: true; Layout.topMargin: 6; Layout.bottomMargin: 6; color: Style.Theme.border }

                // Quick filter chips (720p / 1080p / 4K / DV)
                Repeater {
                    model: ["720p", "1080p", "4K", "DV"]
                    delegate: Rectangle {
                        required property string modelData
                        property string _state: {
                            try { return JSON.parse(scanner.quickFilters)[modelData] || "off" } catch(e) { return "off" }
                        }
                        width: qfLabel.implicitWidth + 14
                        height: 22
                        radius: 11
                        color: _state === "only"    ? "#1e6b3a"
                             : _state === "exclude" ? "#6b1e1e"
                             : qfMouse.containsMouse ? Style.Theme.surfaceHover
                             : Style.Theme.surfaceLight

                        Label {
                            id: qfLabel
                            anchors.centerIn: parent
                            text: parent.modelData
                            font.pixelSize: 10
                            font.bold: parent._state !== "off"
                            color: parent._state !== "off" ? "#ffffff" : Style.Theme.textSecondary
                        }

                        MouseArea {
                            id: qfMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: scanner.toggleQuickFilter(parent.modelData)
                        }

                        ToolTip.visible: qfMouse.containsMouse
                        ToolTip.text: parent._state === "off"    ? "Click to exclude " + parent.modelData
                                    : parent._state === "exclude" ? "Excluding " + parent.modelData + " — click to show only"
                                    :                              "Showing only " + parent.modelData + " — click to reset"
                    }
                }

                // Separator
                Rectangle { width: 1; Layout.fillHeight: true; Layout.topMargin: 6; Layout.bottomMargin: 6; color: Style.Theme.border }

                // Filter buttons
                Repeater {
                    model: [
                        { label: "All", value: "" },
                        { label: "Missing", value: "missing" },
                        { label: "Upgrade", value: "upgrade" },
                        { label: "In Library", value: "in_library" },
                    ]

                    delegate: Rectangle {
                        required property var modelData
                        width: filterLabel.implicitWidth + 16
                        height: 24
                        radius: 12
                        color: scanner.statusFilter === modelData.value
                               ? Style.Theme.accent
                               : filterMouse.containsMouse
                                 ? Style.Theme.surfaceHover
                                 : Style.Theme.surfaceLight

                        Label {
                            id: filterLabel
                            anchors.centerIn: parent
                            text: modelData.label
                            font.pixelSize: 11
                            color: scanner.statusFilter === modelData.value
                                   ? Style.Theme.accentText
                                   : Style.Theme.textSecondary
                        }

                        MouseArea {
                            id: filterMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: scanner.setStatusFilter(modelData.value)
                        }
                    }
                }

                Item { Layout.fillWidth: true }

                // Text search field
                TextField {
                    id: filterTextField
                    Layout.preferredWidth: 200
                    Layout.preferredHeight: 30
                    placeholderText: "Filter results..."
                    color: Style.Theme.textPrimary
                    font.pixelSize: Style.Theme.fontSizeSmall
                    topPadding: 0
                    bottomPadding: 0
                    leftPadding: 12
                    rightPadding: 12
                    verticalAlignment: TextInput.AlignVCenter
                    onTextChanged: filterDebounce.restart()

                    Timer {
                        id: filterDebounce
                        interval: 250
                        onTriggered: scanner.setFilterText(filterTextField.text)
                    }

                    background: Rectangle {
                        radius: 15
                        color: Style.Theme.surfaceLight
                        border.color: filterTextField.activeFocus ? Style.Theme.accent : "transparent"
                        border.width: 1
                    }
                }

                // Export CSV button
                Rectangle {
                    width: 28
                    height: 28
                    radius: 14
                    color: exportMouse.containsMouse ? Style.Theme.accentLight : Style.Theme.surfaceLight

                    Label {
                        anchors.centerIn: parent
                        text: "CSV"
                        font.pixelSize: 9
                        font.bold: true
                        color: Style.Theme.textSecondary
                    }

                    MouseArea {
                        id: exportMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: exportDialog.open()
                    }

                    ToolTip.visible: exportMouse.containsMouse
                    ToolTip.text: "Export results to CSV"
                }

                // View toggle (list / grid)
                Rectangle { width: 1; Layout.fillHeight: true; Layout.topMargin: 6; Layout.bottomMargin: 6; color: Style.Theme.border }

                Rectangle {
                    width: 26; height: 26; radius: 4
                    color: scannerTab.viewMode === 0 ? Style.Theme.accent : listViewMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight

                    Label {
                        anchors.centerIn: parent
                        text: "\u{2261}"  // ≡ list icon
                        font.pixelSize: 14; font.bold: true
                        color: scannerTab.viewMode === 0 ? Style.Theme.accentText : Style.Theme.textSecondary
                    }
                    MouseArea { id: listViewMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: scannerTab.viewMode = 0 }
                    ToolTip.visible: listViewMouse.containsMouse; ToolTip.text: "List view"
                }

                Rectangle {
                    width: 26; height: 26; radius: 4
                    color: scannerTab.viewMode === 1 ? Style.Theme.accent : gridViewMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight

                    Label {
                        anchors.centerIn: parent
                        text: "\u{2637}"  // ⚷ grid-like icon
                        font.pixelSize: 14; font.bold: true
                        color: scannerTab.viewMode === 1 ? Style.Theme.accentText : Style.Theme.textSecondary
                    }
                    MouseArea { id: gridViewMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: scannerTab.viewMode = 1 }
                    ToolTip.visible: gridViewMouse.containsMouse; ToolTip.text: "Grid view"
                }

                // Tile column count control (visible in grid mode)
                RowLayout {
                    visible: scannerTab.viewMode === 1
                    spacing: 2

                    Rectangle {
                        width: 22; height: 22; radius: 4
                        color: tileMinMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                        Label { anchors.centerIn: parent; text: "\u2212"; font.pixelSize: 12; color: Style.Theme.textSecondary }
                        MouseArea {
                            id: tileMinMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (scannerTab.tileColumns > 3) {
                                    scannerTab.tileColumns--
                                    settings.setInt("tile_columns", scannerTab.tileColumns)
                                }
                            }
                        }
                        ToolTip.visible: tileMinMouse.containsMouse; ToolTip.text: "Fewer columns"
                    }

                    Label {
                        text: scannerTab.tileColumns.toString()
                        font.pixelSize: Style.Theme.fontSizeSmall
                        font.bold: true
                        color: Style.Theme.textPrimary
                        Layout.preferredWidth: 16
                        horizontalAlignment: Text.AlignHCenter
                    }

                    Rectangle {
                        width: 22; height: 22; radius: 4
                        color: tilePlusMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                        Label { anchors.centerIn: parent; text: "+"; font.pixelSize: 12; color: Style.Theme.textSecondary }
                        MouseArea {
                            id: tilePlusMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (scannerTab.tileColumns < 10) {
                                    scannerTab.tileColumns++
                                    settings.setInt("tile_columns", scannerTab.tileColumns)
                                }
                            }
                        }
                        ToolTip.visible: tilePlusMouse.containsMouse; ToolTip.text: "More columns"
                    }
                }
            }
        }

        // ── Separator between stats bar and results ───────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: Style.Theme.border
            visible: scanner.totalCount > 0
        }

        // ── Results (list or grid) ─────────────────────────────────
        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: scannerTab.viewMode

            // List view
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: "transparent"
                radius: 0
                clip: true

                ListView {
                    id: resultsList
                    anchors.fill: parent
                    anchors.margins: Style.Theme.spacingSmall
                    model: scanner.resultsModel
                    spacing: 2

                    delegate: Components.ResultRow {
                        width: resultsList.width

                        onClicked: function(idx) { scanner.toggleSelection(idx) }
                        onSelectionToggled: function(idx) { scanner.toggleSelection(idx) }
                        onHostPrefToggled: function(idx) { scanner.toggleHostPref(idx) }
                        onGroupCollapseToggled: function(key) { scanner.toggleGroupCollapse(key) }
                    }

                    ScrollBar.vertical: ScrollBar {
                        policy: ScrollBar.AsNeeded
                    }
                }

                // Empty state — sibling of ListView so it's anchored to the
                // viewport Rectangle, not the scrollable contentItem
                Label {
                    anchors.centerIn: parent
                    visible: scanner.resultsModel.count === 0 && !scanner.scanning
                    text: "No results yet. Configure sources and click Scan."
                    color: Style.Theme.textMuted
                    font.pixelSize: Style.Theme.fontSizeMedium
                }
            }

            // Grid / Tile view
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: "transparent"
                radius: 0
                clip: true

                GridView {
                    id: resultsGrid
                    anchors.fill: parent
                    anchors.margins: Style.Theme.spacingSmall
                    model: scanner.resultsModel
                    cellWidth: Math.floor(width / scannerTab.tileColumns)
                    cellHeight: Math.floor(cellWidth * 1.65)

                    delegate: Components.ResultTile {
                        width: resultsGrid.cellWidth - 8
                        height: resultsGrid.cellHeight - 8

                        onClicked: function(idx) { scanner.toggleSelection(idx) }
                        onSelectionToggled: function(idx) { scanner.toggleSelection(idx) }
                    }

                    ScrollBar.vertical: ScrollBar {
                        policy: ScrollBar.AsNeeded
                    }
                }

                // Empty state — sibling of GridView
                Label {
                    anchors.centerIn: parent
                    visible: scanner.resultsModel.count === 0 && !scanner.scanning
                    text: "No results yet. Configure sources and click Scan."
                    color: Style.Theme.textMuted
                    font.pixelSize: Style.Theme.fontSizeMedium
                }
            }
        }

        // ── Pagination bar ──────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 32
            color: "transparent"
            radius: 0
            visible: scanner.totalPages > 1

            RowLayout {
                anchors.centerIn: parent
                spacing: Style.Theme.spacingMedium

                Rectangle {
                    width: 28; height: 28; radius: 14
                    color: prevMouse.containsMouse && scanner.page > 0
                           ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                    opacity: scanner.page > 0 ? 1.0 : 0.4

                    Label {
                        anchors.centerIn: parent
                        text: "<"
                        font.pixelSize: 14
                        font.bold: true
                        color: Style.Theme.textPrimary
                    }

                    MouseArea {
                        id: prevMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: scanner.page > 0 ? Qt.PointingHandCursor : Qt.ArrowCursor
                        onClicked: scanner.prevPage()
                    }

                    ToolTip.visible: prevMouse.containsMouse
                    ToolTip.text: "Previous page"
                }

                Label {
                    text: scanner.pageInfo
                    font.pixelSize: Style.Theme.fontSizeSmall
                    color: Style.Theme.textSecondary
                }

                Rectangle {
                    width: 28; height: 28; radius: 14
                    color: nextMouse.containsMouse && scanner.page < scanner.totalPages - 1
                           ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                    opacity: scanner.page < scanner.totalPages - 1 ? 1.0 : 0.4

                    Label {
                        anchors.centerIn: parent
                        text: ">"
                        font.pixelSize: 14
                        font.bold: true
                        color: Style.Theme.textPrimary
                    }

                    MouseArea {
                        id: nextMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: scanner.page < scanner.totalPages - 1 ? Qt.PointingHandCursor : Qt.ArrowCursor
                        onClicked: scanner.nextPage()
                    }

                    ToolTip.visible: nextMouse.containsMouse
                    ToolTip.text: "Next page"
                }
            }
        }
    }

    // ── Export CSV dialog ─────────────────────────────────────────
    Dialog {
        id: exportDialog
        title: "Export Results"
        modal: true
        anchors.centerIn: parent
        width: 400
        Material.background: Style.Theme.surface
        Material.foreground: Style.Theme.textPrimary

        ColumnLayout {
            spacing: Style.Theme.spacingMedium
            width: parent.width

            Label {
                text: "Export " + scanner.filteredCount + " results to CSV"
                font.pixelSize: Style.Theme.fontSizeNormal
                color: Style.Theme.textPrimary
            }

            TextField {
                id: exportPathField
                Layout.fillWidth: true
                text: "scan_results.csv"
                placeholderText: "File path..."
                color: Style.Theme.textPrimary
                font.pixelSize: Style.Theme.fontSizeNormal

                background: Rectangle {
                    radius: Style.Theme.borderRadiusSmall
                    color: Style.Theme.surfaceLight
                    border.color: exportPathField.activeFocus ? Style.Theme.accent : Style.Theme.border
                    border.width: 1
                }
            }
        }

        footer: DialogButtonBox {
            Material.background: Style.Theme.surface
            Button {
                text: "Export"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
                onClicked: {
                    scanner.exportResultsCsv(exportPathField.text)
                    exportDialog.close()
                }
            }
            Button {
                text: "Cancel"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
        }
    }

    // ── Cache expiry choice dialog ─────────────────────────────────
    Connections {
        target: scanner
        function onCacheExpired(message) {
            cacheExpiryDialog.cacheMessage = message
            cacheExpiryDialog.open()
        }
    }

    Dialog {
        id: cacheExpiryDialog
        property string cacheMessage: ""
        title: "Plex Cache Outdated"
        modal: true
        anchors.centerIn: parent
        width: 420
        Material.background: Style.Theme.surface
        Material.foreground: Style.Theme.textPrimary

        ColumnLayout {
            spacing: Style.Theme.spacingMedium
            width: parent.width

            Label {
                text: cacheExpiryDialog.cacheMessage
                font.pixelSize: Style.Theme.fontSizeNormal
                color: Style.Theme.textSecondary
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Label {
                text: "Use current cache (faster, may miss recent Plex changes) or reload from Plex for up-to-date matching?"
                font.pixelSize: Style.Theme.fontSizeSmall
                color: Style.Theme.textMuted
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }

        footer: DialogButtonBox {
            Material.background: Style.Theme.surface
            Button {
                text: "Use Current Cache"
                DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole
                onClicked: {
                    scanner.startScanWithCacheChoice(true)
                    cacheExpiryDialog.close()
                }
            }
            Button {
                text: "Reload Cache"
                DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
                onClicked: {
                    scanner.startScanWithCacheChoice(false)
                    cacheExpiryDialog.close()
                }
            }
            Button {
                text: "Cancel"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
        }
    }

    // ── Scanner-specific Dialogs (lazy-loaded) ─────────────────────
    Loader {
        id: historyDialogLoader
        active: false
        source: "components/HistoryDialog.qml"
        onLoaded: item.show()
    }
    Connections { target: historyDialogLoader.item; ignoreUnknownSignals: true; function onClosing() { historyDialogLoader.active = false } }

    Loader {
        id: watchlistDialogLoader
        active: false
        source: "components/WatchlistDialog.qml"
        onLoaded: item.show()
    }
    Connections { target: watchlistDialogLoader.item; ignoreUnknownSignals: true; function onClosing() { watchlistDialogLoader.active = false } }

    Loader {
        id: sourceSearchWindowLoader
        active: false
        source: "components/SourceSearchWindow.qml"
        onLoaded: {
            item.show()
            item.raise()
            item.requestActivate()
        }
    }
    Connections {
        target: sourceSearch
        function onOpenRequested() {
            if (!sourceSearchWindowLoader.active) {
                sourceSearchWindowLoader.active = true
                return
            }
            if (sourceSearchWindowLoader.item) {
                sourceSearchWindowLoader.item.show()
                sourceSearchWindowLoader.item.raise()
                sourceSearchWindowLoader.item.requestActivate()
            }
        }
    }
    Connections { target: sourceSearchWindowLoader.item; ignoreUnknownSignals: true; function onClosing() { sourceSearchWindowLoader.active = false } }

    // ── Link-scrape progress snackbar ────────────────────────────
    Rectangle {
        id: scrapeSnackbar
        anchors.bottom: parent.bottom
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottomMargin: 96
        width: Math.min(scrapeSnackRow.implicitWidth + 48, parent.width - 32)
        height: 44
        radius: 8
        color: "#1565C0"
        opacity: 0
        z: 101
        visible: opacity > 0

        RowLayout {
            id: scrapeSnackRow
            anchors.fill: parent
            anchors.leftMargin: 14
            anchors.rightMargin: 14
            spacing: 10

            Label {
                text: "⛓"
                font.pixelSize: 15
                color: "#ffffff"
            }
            Column {
                spacing: 0
                Label {
                    id: scrapeSnackTitle
                    text: ""
                    font.pixelSize: 12
                    font.bold: true
                    color: "#ffffff"
                    elide: Text.ElideRight
                }
                Label {
                    id: scrapeSnackSub
                    text: ""
                    font.pixelSize: 10
                    color: "#90CAF9"
                    elide: Text.ElideRight
                }
            }
        }

        NumberAnimation on opacity { id: scrapeSnackFadeIn;  from: 0; to: 1; duration: 150 }
        NumberAnimation on opacity { id: scrapeSnackFadeOut; from: 1; to: 0; duration: 300 }
    }

    Connections {
        target: scanner
        function onScrapeProgress(current, total, title) {
            scrapeSnackTitle.text = "Scraping links \u2014 " + current + " / " + total
            scrapeSnackSub.text   = title
            if (scrapeSnackbar.opacity < 1)
                scrapeSnackFadeIn.start()
        }
        function onScrapeDone() {
            scrapeSnackFadeOut.start()
        }
    }

    Loader {
        id: tmdbSearchDialogLoader
        active: false
        source: "components/TmdbSearchDialog.qml"
        onLoaded: item.show()
    }
    Connections { target: tmdbSearchDialogLoader.item; ignoreUnknownSignals: true; function onClosing() { tmdbSearchDialogLoader.active = false } }

    Loader {
        id: analyticsDialogLoader
        active: false
        source: "components/AnalyticsDialog.qml"
        onLoaded: item.show()
    }
    Connections { target: analyticsDialogLoader.item; ignoreUnknownSignals: true; function onClosing() { analyticsDialogLoader.active = false } }
}
