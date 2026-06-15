import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../style" as Style

ApplicationWindow {
    id: shortcutsDialog
    title: "Keyboard Shortcuts"
    width: 420; height: 380
    minimumWidth: 350; minimumHeight: 300
    color: Style.Theme.background
    modality: Qt.ApplicationModal

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.Theme.spacingLarge
        spacing: Style.Theme.spacingMedium

        Label {
            text: "Keyboard Shortcuts"
            font.pixelSize: Style.Theme.fontSizeLarge
            font.bold: true
            color: Style.Theme.textPrimary
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Style.Theme.borderRadius
            color: Style.Theme.surface
            border.color: Style.Theme.border
            border.width: 1

            ListView {
                anchors.fill: parent
                anchors.margins: 8
                clip: true
                spacing: 1

                model: [
                    { key: "Ctrl+S", action: "Start scan" },
                    { key: "Escape", action: "Stop scan" },
                    { key: "Ctrl+,", action: "Open settings" },
                    { key: "Ctrl+1", action: "Switch to Scanner tab" },
                    { key: "Ctrl+A", action: "Select / deselect all results" },
                    { key: "Ctrl+L", action: "Toggle log panel" },
                    { key: "Ctrl+E", action: "Export results to CSV" },
                    { key: "Ctrl+F", action: "Focus search / filter field" },
                    { key: "F5", action: "Refresh / re-scan" },
                    { key: "F1", action: "Show this dialog" },
                ]

                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    width: ListView.view.width
                    height: 32
                    color: index % 2 === 0 ? "transparent" : Qt.rgba(Style.Theme.surfaceLight.r, Style.Theme.surfaceLight.g, Style.Theme.surfaceLight.b, 0.3)
                    radius: 4

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 12
                        anchors.rightMargin: 12

                        Rectangle {
                            width: keyLabel.implicitWidth + 16
                            height: 24
                            radius: 4
                            color: Style.Theme.surfaceLight
                            border.color: Style.Theme.border
                            border.width: 1

                            Label {
                                id: keyLabel
                                anchors.centerIn: parent
                                text: modelData.key
                                font.pixelSize: Style.Theme.fontSizeSmall
                                font.bold: true
                                font.family: "Consolas"
                                color: Style.Theme.accent
                            }
                        }

                        Label {
                            Layout.fillWidth: true
                            text: modelData.action
                            font.pixelSize: Style.Theme.fontSizeSmall
                            color: Style.Theme.textPrimary
                        }
                    }
                }
            }
        }

        Button {
            Layout.alignment: Qt.AlignRight
            text: "Close"
            onClicked: shortcutsDialog.close()

            background: Rectangle {
                radius: Style.Theme.borderRadius
                color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent
            }
            contentItem: Label {
                text: parent.text
                color: "#ffffff"
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }
    }
}
