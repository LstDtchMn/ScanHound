import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../../style" as Style

ScrollView {
    id: appearanceTab
    contentWidth: availableWidth
    Material.elevation: 0
    background: Rectangle { color: Style.Theme.surface }

    ColumnLayout {
        width: parent.width
        spacing: Style.Theme.spacingMedium

        Item { Layout.preferredHeight: Style.Theme.spacingSmall }

        Label {
            text: "Appearance"
            font.pixelSize: Style.Theme.fontSizeLarge
            font.bold: true
            color: Style.Theme.textPrimary
            Layout.leftMargin: Style.Theme.spacingLarge
        }

        // ── Theme Mode ──────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: Style.Theme.spacingLarge
            Layout.rightMargin: Style.Theme.spacingLarge
            Layout.preferredHeight: themeCol.implicitHeight + 24
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight

            ColumnLayout {
                id: themeCol
                anchors.fill: parent
                anchors.margins: 12
                spacing: Style.Theme.spacingMedium

                Label {
                    text: "Theme"
                    font.bold: true
                    color: Style.Theme.textPrimary
                }

                RowLayout {
                    spacing: Style.Theme.spacingLarge

                    Repeater {
                        model: [
                            { label: "Dark", value: "dark", icon: "\u{1F319}" },
                            { label: "Light", value: "light", icon: "\u{2600}" },
                            { label: "System", value: "system", icon: "\u{1F4BB}" }
                        ]

                        delegate: Rectangle {
                            required property var modelData
                            property bool isSelected: Style.Theme.themeMode === modelData.value
                            width: 120; height: 80
                            radius: Style.Theme.borderRadius
                            color: isSelected
                                   ? Qt.rgba(Style.Theme.accent.r, Style.Theme.accent.g, Style.Theme.accent.b, 0.2)
                                   : themeMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surface
                            border.color: isSelected ? Style.Theme.accent : Style.Theme.border
                            border.width: isSelected ? 2 : 1

                            ColumnLayout {
                                anchors.centerIn: parent
                                spacing: 4

                                Label {
                                    text: modelData.icon
                                    font.pixelSize: 24
                                    horizontalAlignment: Text.AlignHCenter
                                    Layout.alignment: Qt.AlignHCenter
                                }
                                Label {
                                    text: modelData.label
                                    font.pixelSize: Style.Theme.fontSizeNormal
                                    font.bold: true
                                    color: Style.Theme.textPrimary
                                    horizontalAlignment: Text.AlignHCenter
                                    Layout.alignment: Qt.AlignHCenter
                                }
                            }

                            MouseArea {
                                id: themeMouse
                                anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    settings.setString("theme_mode", modelData.value)
                                    Style.Theme.applyThemeMode(modelData.value)
                                }
                            }
                        }
                    }
                }

                Label {
                    text: "Theme changes apply instantly and are saved with your settings"
                    font.pixelSize: Style.Theme.fontSizeSmall
                    color: Style.Theme.textMuted
                }
            }
        }

        // ── System Tray ─────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: Style.Theme.spacingLarge
            Layout.rightMargin: Style.Theme.spacingLarge
            Layout.preferredHeight: trayCol.implicitHeight + 24
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight

            ColumnLayout {
                id: trayCol
                anchors.fill: parent
                anchors.margins: 12
                spacing: Style.Theme.spacingSmall

                Label {
                    text: "System Tray"
                    font.bold: true
                    color: Style.Theme.textPrimary
                }

                CheckBox {
                    text: "Enable system tray icon"
                    checked: settings.getBool("enable_system_tray")
                    onToggled: settings.setBool("enable_system_tray", checked)
                    Material.accent: Style.Theme.accent
                    ToolTip.visible: hovered
                    ToolTip.delay: 700
                    ToolTip.text: "Show ScanHound in the system tray for quick access without opening the full window"
                }

                CheckBox {
                    text: "Minimize to tray instead of closing"
                    checked: settings.getBool("minimize_to_tray")
                    onToggled: settings.setBool("minimize_to_tray", checked)
                    Material.accent: Style.Theme.accent
                    ToolTip.visible: hovered
                    ToolTip.delay: 700
                    ToolTip.text: "When you close the window, keep ScanHound running in the system tray instead of quitting"
                }

                CheckBox {
                    text: "Start minimized to tray"
                    checked: settings.getBool("start_minimized")
                    onToggled: settings.setBool("start_minimized", checked)
                    Material.accent: Style.Theme.accent
                    ToolTip.visible: hovered
                    ToolTip.delay: 700
                    ToolTip.text: "Launch ScanHound silently in the tray without showing the main window"
                }
            }
        }

        // ── Startup ─────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: Style.Theme.spacingLarge
            Layout.rightMargin: Style.Theme.spacingLarge
            Layout.preferredHeight: startupCol.implicitHeight + 24
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight

            ColumnLayout {
                id: startupCol
                anchors.fill: parent
                anchors.margins: 12
                spacing: Style.Theme.spacingSmall

                Label {
                    text: "Startup"
                    font.bold: true
                    color: Style.Theme.textPrimary
                }

                CheckBox {
                    text: "Auto-connect to Plex on startup"
                    checked: settings.getBool("auto_connect_plex")
                    onToggled: settings.setBool("auto_connect_plex", checked)
                    Material.accent: Style.Theme.accent
                    ToolTip.visible: hovered
                    ToolTip.delay: 700
                    ToolTip.text: "Automatically connect to your configured Plex server when ScanHound starts"
                }

            }
        }

        Item { Layout.fillHeight: true }
    }
}
