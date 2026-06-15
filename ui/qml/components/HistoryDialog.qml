import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../style" as Style

ApplicationWindow {
    id: historyDialog
    title: "Download History"
    width: 700; height: 500
    minimumWidth: 500; minimumHeight: 350
    color: Style.Theme.background
    modality: Qt.ApplicationModal

    property var historyItems: []

    Component.onCompleted: refreshHistory()

    function refreshHistory() {
        try {
            var json = scanner.getHistoryJson(500)
            historyItems = JSON.parse(json)
        } catch (e) {
            historyItems = []
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.Theme.spacingLarge
        spacing: Style.Theme.spacingMedium

        // Header row
        RowLayout {
            Layout.fillWidth: true

            Label {
                text: "Download History"
                font.pixelSize: Style.Theme.fontSizeLarge
                font.bold: true
                color: Style.Theme.textPrimary
            }

            Item { Layout.fillWidth: true }

            Label {
                text: historyDialog.historyItems.length + " items"
                font.pixelSize: Style.Theme.fontSizeSmall
                color: Style.Theme.textMuted
            }
        }

        // Filter
        TextField {
            id: historySearch
            Layout.fillWidth: true
            placeholderText: "Search history..."
            color: Style.Theme.textPrimary
            font.pixelSize: Style.Theme.fontSizeSmall
            background: Rectangle { radius: 4; color: Style.Theme.surfaceLight; border.color: parent.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
            onTextChanged: historyFilterTimer.restart()
        }
        Timer { id: historyFilterTimer; interval: 200; onTriggered: historyList.filterText = historySearch.text.toLowerCase() }

        // Results list
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Style.Theme.borderRadius
            color: Style.Theme.surface
            clip: true

            ListView {
                id: historyList
                anchors.fill: parent
                anchors.margins: 4
                spacing: 1
                property string filterText: ""

                model: {
                    var q = historyList.filterText
                    if (q.length === 0) return historyDialog.historyItems
                    return historyDialog.historyItems.filter(function(item) {
                        return (item.title || "").toLowerCase().indexOf(q) >= 0
                    })
                }

                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    width: ListView.view.width
                    height: 48
                    color: index % 2 === 0 ? "transparent" : Qt.rgba(Style.Theme.surfaceLight.r, Style.Theme.surfaceLight.g, Style.Theme.surfaceLight.b, 0.3)
                    radius: 4

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 12
                        anchors.rightMargin: 12
                        spacing: 12

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            Label {
                                text: modelData.title || modelData.url || "Unknown"
                                font.pixelSize: Style.Theme.fontSizeSmall
                                font.bold: true
                                color: Style.Theme.textPrimary
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                            RowLayout {
                                spacing: 12
                                Label {
                                    text: modelData.resolution || ""
                                    font.pixelSize: 10
                                    color: Style.Theme.textMuted
                                    visible: text.length > 0
                                }
                                Label {
                                    text: modelData.size || ""
                                    font.pixelSize: 10
                                    color: Style.Theme.textMuted
                                    visible: text.length > 0
                                }
                                Label {
                                    text: modelData.date || ""
                                    font.pixelSize: 10
                                    color: Style.Theme.textMuted
                                    visible: text.length > 0
                                }
                            }
                        }

                        // Open URL button
                        Rectangle {
                            width: 26; height: 26; radius: 13
                            color: histUrlHover.containsMouse ? Style.Theme.accentLight : Style.Theme.surfaceLight
                            visible: (modelData.url || "").length > 0

                            Label { anchors.centerIn: parent; text: "\u{1F517}"; font.pixelSize: 12 }
                            MouseArea {
                                id: histUrlHover
                                anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: Qt.openUrlExternally(modelData.url)
                            }
                        }
                    }
                }

            }

            // Empty state — sibling of ListView, outside scrollable contentItem
            Label {
                anchors.centerIn: parent
                visible: historyList.count === 0
                text: "No download history."
                color: Style.Theme.textMuted
                font.pixelSize: Style.Theme.fontSizeNormal
            }
        }

        // Footer
        RowLayout {
            Layout.fillWidth: true

            Button {
                text: "Clear History"
                onClicked: {
                    scanner.clearHistory()
                    historyDialog.refreshHistory()
                }
                background: Rectangle {
                    radius: Style.Theme.borderRadius
                    color: parent.hovered ? Qt.rgba(Style.Theme.error.r, Style.Theme.error.g, Style.Theme.error.b, 0.8) : Style.Theme.error
                }
                contentItem: Label { text: parent.text; color: "#ffffff"; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            }

            Item { Layout.fillWidth: true }

            Button {
                text: "Close"
                onClicked: historyDialog.close()
                background: Rectangle { radius: Style.Theme.borderRadius; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent }
                contentItem: Label { text: parent.text; color: "#ffffff"; font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            }
        }
    }
}
