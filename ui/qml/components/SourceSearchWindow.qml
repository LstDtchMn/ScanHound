import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../style" as Style
import "." as Components

ApplicationWindow {
    id: sourceSearchWindow
    title: sourceSearch.windowTitle
    width: 1180
    height: 760
    minimumWidth: 820
    minimumHeight: 460
    color: Style.Theme.background
    modality: Qt.NonModal

    // ── Keyboard shortcuts ──────────────────────────────────────
    Shortcut {
        sequence: "Ctrl+A"
        onActivated: sourceSearch.selectAll(true)
    }
    Shortcut {
        sequence: "Ctrl+Shift+A"
        onActivated: sourceSearch.selectAll(false)
    }
    Shortcut {
        sequence: "Escape"
        onActivated: sourceSearchWindow.close()
    }
    Shortcut {
        sequence: "Ctrl+F"
        onActivated: queryField.forceActiveFocus()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.Theme.spacingLarge
        spacing: Style.Theme.spacingMedium

        // ── Header Row ───────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: Style.Theme.spacingMedium

            Label {
                text: sourceSearch.windowTitle
                font.pixelSize: Style.Theme.fontSizeLarge
                font.bold: true
                color: Style.Theme.textPrimary
            }

            Item { Layout.fillWidth: true }

            Rectangle {
                width: selectedLabel.implicitWidth + 18
                height: 28
                radius: 14
                color: Qt.rgba(Style.Theme.info.r, Style.Theme.info.g, Style.Theme.info.b, 0.16)
                border.color: Qt.rgba(Style.Theme.info.r, Style.Theme.info.g, Style.Theme.info.b, 0.5)
                border.width: 1

                Label {
                    id: selectedLabel
                    anchors.centerIn: parent
                    text: sourceSearch.resultsModel.selectedCount + " selected"
                    font.pixelSize: 11
                    font.bold: true
                    color: Style.Theme.info
                }
            }

            Button {
                text: "Open Selected"
                enabled: sourceSearch.resultsModel.selectedCount > 0
                onClicked: sourceSearch.downloadSelected()
            }

            Button {
                text: "Copy Links"
                enabled: sourceSearch.resultsModel.selectedCount > 0
                onClicked: sourceSearch.copySelectedToClipboard()
            }

            Button {
                text: "Close"
                onClicked: sourceSearchWindow.close()
            }
        }

        // ── Search Controls ──────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight
            border.color: Style.Theme.border
            border.width: 1
            implicitHeight: searchRow.implicitHeight + 16

            RowLayout {
                id: searchRow
                anchors.fill: parent
                anchors.margins: 8
                spacing: 8

                Label {
                    text: "Source:"
                    color: Style.Theme.textSecondary
                    font.pixelSize: Style.Theme.fontSizeSmall
                }

                ComboBox {
                    id: sourceCombo
                    Layout.preferredWidth: 150
                    model: {
                        var names = sourceSearch.sourceNames
                        var display = []
                        for (var i = 0; i < names.length; i++)
                            display.push(sourceSearch.sourceDisplayName(names[i]))
                        return display
                    }
                    currentIndex: {
                        var names = sourceSearch.sourceNames
                        for (var i = 0; i < names.length; i++) {
                            if (names[i] === sourceSearch.currentSource)
                                return i
                        }
                        return 0
                    }
                    Material.background: Style.Theme.surface
                    Material.foreground: Style.Theme.textPrimary
                }

                Label {
                    text: "Mode:"
                    color: Style.Theme.textSecondary
                    font.pixelSize: Style.Theme.fontSizeSmall
                }

                ComboBox {
                    id: modeCombo
                    Layout.preferredWidth: 100
                    model: ["All", "Movies", "TV"]
                    currentIndex: {
                        switch (sourceSearch.currentMode) {
                            case "movies": return 1
                            case "tv": return 2
                            default: return 0
                        }
                    }
                    Material.background: Style.Theme.surface
                    Material.foreground: Style.Theme.textPrimary
                }

                TextField {
                    id: queryField
                    Layout.fillWidth: true
                    placeholderText: "Search query..."
                    text: sourceSearch.queryText
                    color: Style.Theme.textPrimary
                    font.pixelSize: Style.Theme.fontSizeSmall
                    Material.accent: Style.Theme.accent
                    onAccepted: searchBtn.clicked()
                }

                // History dropdown
                Button {
                    id: historyBtn
                    text: "\u25BC"
                    flat: true
                    visible: sourceSearch.searchHistory.length > 0
                    implicitWidth: 28
                    implicitHeight: 28
                    ToolTip.text: "Search history"
                    ToolTip.visible: hovered
                    onClicked: historyMenu.open()

                    Menu {
                        id: historyMenu
                        y: historyBtn.height

                        Repeater {
                            model: sourceSearch.searchHistory

                            MenuItem {
                                text: {
                                    var entry = sourceSearch.searchHistory[index]
                                    var src = sourceSearch.sourceDisplayName(entry.source)
                                    return entry.query + "  \u2014  " + src
                                }
                                onTriggered: sourceSearch.applyHistoryEntry(index)
                            }
                        }

                        MenuSeparator {}

                        MenuItem {
                            text: "Clear History"
                            onTriggered: sourceSearch.clearHistory()
                        }
                    }
                }

                Button {
                    id: searchBtn
                    text: "Search"
                    highlighted: true
                    enabled: queryField.text.trim().length > 0 && !sourceSearch.searching
                    onClicked: {
                        var names = sourceSearch.sourceNames
                        var srcName = (sourceCombo.currentIndex >= 0 && sourceCombo.currentIndex < names.length)
                            ? names[sourceCombo.currentIndex] : ""
                        var modes = ["all", "movies", "tv"]
                        var mode = modes[modeCombo.currentIndex] || "all"
                        sourceSearch.customSearch(queryField.text, srcName, mode)
                    }
                }
            }
        }

        // ── Sort & Filter Bar ───────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            visible: sourceSearch.resultsModel.count > 0 || sourceSearch.searching

            Label {
                text: "Sort:"
                color: Style.Theme.textSecondary
                font.pixelSize: Style.Theme.fontSizeSmall
            }

            ComboBox {
                id: sortCombo
                Layout.preferredWidth: 120
                model: sourceSearch.sortOptions
                currentIndex: {
                    var opts = sourceSearch.sortOptions
                    for (var i = 0; i < opts.length; i++) {
                        if (opts[i] === sourceSearch.currentSort)
                            return i
                    }
                    return 0
                }
                onActivated: function(idx) {
                    sourceSearch.setSort(sourceSearch.sortOptions[idx])
                }
                Material.background: Style.Theme.surface
                Material.foreground: Style.Theme.textPrimary
            }

            Item { width: 8 }

            Label {
                text: "Filter:"
                color: Style.Theme.textSecondary
                font.pixelSize: Style.Theme.fontSizeSmall
            }

            Repeater {
                model: ["4K", "1080p", "HDR", "DV"]

                delegate: Button {
                    required property string modelData
                    required property int index
                    text: modelData
                    flat: true
                    checkable: true
                    checked: {
                        var filters = sourceSearch.activeFilters
                        for (var i = 0; i < filters.length; i++) {
                            if (String(filters[i]).trim() === String(modelData).trim()) return true
                        }
                        return false
                    }
                    implicitHeight: 28
                    font.pixelSize: 11
                    font.bold: checked

                    background: Rectangle {
                        radius: 14
                        color: parent.checked
                            ? Qt.rgba(Style.Theme.accent.r, Style.Theme.accent.g, Style.Theme.accent.b, 0.25)
                            : Qt.rgba(Style.Theme.surfaceLight.r, Style.Theme.surfaceLight.g, Style.Theme.surfaceLight.b, 0.6)
                        border.color: parent.checked ? Style.Theme.accent : Style.Theme.border
                        border.width: 1
                    }

                    Material.foreground: checked ? Style.Theme.accent : Style.Theme.textSecondary

                    onClicked: sourceSearch.toggleFilter(modelData)
                }
            }

            Button {
                text: "Clear"
                flat: true
                visible: sourceSearch.activeFilters.length > 0
                implicitHeight: 28
                font.pixelSize: 11
                Material.foreground: Style.Theme.textMuted
                onClicked: sourceSearch.clearFilters()
            }

            Item { Layout.fillWidth: true }

            // Select all / deselect all
            Button {
                text: "Select All"
                flat: true
                implicitHeight: 28
                font.pixelSize: 11
                Material.foreground: Style.Theme.textSecondary
                onClicked: sourceSearch.selectAll(true)
            }

            Button {
                text: "Deselect"
                flat: true
                visible: sourceSearch.resultsModel.selectedCount > 0
                implicitHeight: 28
                font.pixelSize: 11
                Material.foreground: Style.Theme.textSecondary
                onClicked: sourceSearch.selectAll(false)
            }
        }

        // ── Status Bar ───────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            radius: Style.Theme.borderRadius
            color: sourceSearch.searching
                   ? Qt.rgba(Style.Theme.info.r, Style.Theme.info.g, Style.Theme.info.b, 0.12)
                   : Qt.rgba(Style.Theme.surfaceLight.r, Style.Theme.surfaceLight.g, Style.Theme.surfaceLight.b, 0.8)
            border.color: sourceSearch.searching ? Style.Theme.info : Style.Theme.border
            border.width: 1
            implicitHeight: statusRow.implicitHeight + 16

            RowLayout {
                id: statusRow
                anchors.fill: parent
                anchors.margins: 8
                spacing: 10

                BusyIndicator {
                    visible: sourceSearch.searching
                    running: visible
                    Layout.preferredWidth: 18
                    Layout.preferredHeight: 18
                }

                Label {
                    Layout.fillWidth: true
                    text: sourceSearch.statusMessage
                    color: sourceSearch.searching ? Style.Theme.info : Style.Theme.textSecondary
                    wrapMode: Text.WordWrap
                }
            }
        }

        // ── Results List ─────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Style.Theme.borderRadius
            color: Style.Theme.surface
            clip: true

            ListView {
                id: resultsList
                anchors.fill: parent
                anchors.margins: Style.Theme.spacingSmall
                model: sourceSearch.resultsModel
                spacing: 2
                visible: !sourceSearch.searching || sourceSearch.resultsModel.count > 0

                delegate: Components.ResultRow {
                    width: resultsList.width
                    actionController: sourceSearch
                    showRelatedSearchButton: false

                    onClicked: function(idx) { sourceSearch.toggleSelection(idx) }
                    onSelectionToggled: function(idx) { sourceSearch.toggleSelection(idx) }
                    onHostPrefToggled: function(idx) { sourceSearch.toggleHostPref(idx) }
                    onGroupCollapseToggled: function(key) { sourceSearch.toggleGroupCollapse(key) }
                }

                footer: Item {
                    width: resultsList.width
                    height: sourceSearch.hasMore ? 48 : 0
                    visible: sourceSearch.hasMore

                    Button {
                        anchors.centerIn: parent
                        text: "Load More Results"
                        highlighted: true
                        enabled: !sourceSearch.searching
                        onClicked: sourceSearch.loadMore()
                    }
                }

                ScrollBar.vertical: ScrollBar {
                    policy: ScrollBar.AsNeeded
                }
            }

            Label {
                anchors.centerIn: parent
                visible: sourceSearch.resultsModel.count === 0 && !sourceSearch.searching
                text: sourceSearch.statusMessage
                color: Style.Theme.textMuted
                font.pixelSize: Style.Theme.fontSizeSmall
            }
        }
    }
}
