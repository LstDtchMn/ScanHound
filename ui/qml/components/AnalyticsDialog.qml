import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../style" as Style

ApplicationWindow {
    id: analyticsDialog
    title: "Analytics Dashboard"
    width: 650; height: 500
    minimumWidth: 500; minimumHeight: 400
    color: Style.Theme.background
    modality: Qt.ApplicationModal

    property var stats: ({})

    Component.onCompleted: refreshStats()

    function refreshStats() {
        try {
            var json = scanner.getAnalyticsJson()
            stats = JSON.parse(json)
        } catch (e) {
            stats = {}
        }
    }

    // Stat card component
    component StatCard: Rectangle {
        property string label: ""
        property string value: "0"
        property color valueColor: Style.Theme.accent

        Layout.fillWidth: true
        Layout.preferredHeight: 70
        radius: Style.Theme.borderRadius
        color: Style.Theme.surface
        border.color: Style.Theme.border
        border.width: 1

        ColumnLayout {
            anchors.centerIn: parent
            spacing: 4
            Label {
                Layout.alignment: Qt.AlignHCenter
                text: value
                font.pixelSize: Style.Theme.fontSizeXLarge
                font.bold: true
                color: valueColor
            }
            Label {
                Layout.alignment: Qt.AlignHCenter
                text: label
                font.pixelSize: Style.Theme.fontSizeSmall
                color: Style.Theme.textMuted
            }
        }
    }

    // Quality bar component
    component QualityBar: RowLayout {
        property string label: ""
        property int count: 0
        property int total: 1
        property color barColor: Style.Theme.accent

        Layout.fillWidth: true
        spacing: 8

        Label {
            Layout.preferredWidth: 80
            text: label
            font.pixelSize: Style.Theme.fontSizeSmall
            color: Style.Theme.textSecondary
        }

        Rectangle {
            Layout.fillWidth: true
            height: 16
            radius: 8
            color: Style.Theme.surfaceLight

            Rectangle {
                width: parent.width * Math.min(1, (total > 0 ? count / total : 0))
                height: parent.height
                radius: 8
                color: barColor

                Behavior on width { NumberAnimation { duration: 300 } }
            }
        }

        Label {
            Layout.preferredWidth: 50
            text: count.toString()
            font.pixelSize: Style.Theme.fontSizeSmall
            font.bold: true
            color: Style.Theme.textPrimary
            horizontalAlignment: Text.AlignRight
        }
    }

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: Style.Theme.spacingMedium

            Item { Layout.preferredHeight: Style.Theme.spacingSmall }

            Label {
                text: "Analytics Dashboard"
                font.pixelSize: Style.Theme.fontSizeLarge
                font.bold: true
                color: Style.Theme.textPrimary
                Layout.leftMargin: Style.Theme.spacingLarge
            }

            // ── Library Overview ──
            Label {
                text: "Library Overview"
                font.pixelSize: Style.Theme.fontSizeNormal
                font.bold: true
                color: Style.Theme.textSecondary
                Layout.leftMargin: Style.Theme.spacingLarge
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Style.Theme.spacingLarge
                Layout.rightMargin: Style.Theme.spacingLarge
                spacing: Style.Theme.spacingMedium

                StatCard { label: "Total Items"; value: (analyticsDialog.stats.total_items || 0).toString(); valueColor: Style.Theme.accent }
                StatCard { label: "4K Items"; value: (analyticsDialog.stats.items_4k || 0).toString(); valueColor: Style.Theme.warning }
                StatCard { label: "1080p Items"; value: (analyticsDialog.stats.items_1080p || 0).toString(); valueColor: Style.Theme.info }
                StatCard { label: "TV Seasons"; value: (analyticsDialog.stats.tv_seasons || 0).toString(); valueColor: Style.Theme.success }
            }

            // ── Scan History ──
            Label {
                text: "Scan History"
                font.pixelSize: Style.Theme.fontSizeNormal
                font.bold: true
                color: Style.Theme.textSecondary
                Layout.leftMargin: Style.Theme.spacingLarge
                Layout.topMargin: Style.Theme.spacingSmall
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Style.Theme.spacingLarge
                Layout.rightMargin: Style.Theme.spacingLarge
                spacing: Style.Theme.spacingMedium

                StatCard { label: "Total Scans"; value: (analyticsDialog.stats.total_scans || 0).toString(); valueColor: Style.Theme.accent }
                StatCard { label: "Items Found"; value: (analyticsDialog.stats.items_scanned || 0).toString(); valueColor: Style.Theme.textPrimary }
                StatCard { label: "Downloads"; value: (analyticsDialog.stats.downloads || 0).toString(); valueColor: Style.Theme.success }
            }

            // ── Quality Breakdown ──
            Label {
                text: "Quality Breakdown"
                font.pixelSize: Style.Theme.fontSizeNormal
                font.bold: true
                color: Style.Theme.textSecondary
                Layout.leftMargin: Style.Theme.spacingLarge
                Layout.topMargin: Style.Theme.spacingSmall
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: Style.Theme.spacingLarge
                Layout.rightMargin: Style.Theme.spacingLarge
                implicitHeight: qualityCol.implicitHeight + 24
                radius: Style.Theme.borderRadius
                color: Style.Theme.surface
                border.color: Style.Theme.border
                border.width: 1

                ColumnLayout {
                    id: qualityCol
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8

                    property int totalItems: Math.max(1, analyticsDialog.stats.total_items || 1)

                    QualityBar { label: "4K HDR"; count: analyticsDialog.stats.items_4k_hdr || 0; total: qualityCol.totalItems; barColor: "#FFD700" }
                    QualityBar { label: "4K SDR"; count: Math.max(0, (analyticsDialog.stats.items_4k || 0) - (analyticsDialog.stats.items_4k_hdr || 0)); total: qualityCol.totalItems; barColor: Style.Theme.warning }
                    QualityBar { label: "1080p"; count: analyticsDialog.stats.items_1080p || 0; total: qualityCol.totalItems; barColor: Style.Theme.info }
                    QualityBar { label: "720p / Other"; count: (analyticsDialog.stats.total_items || 0) - (analyticsDialog.stats.items_4k || 0) - (analyticsDialog.stats.items_1080p || 0); total: qualityCol.totalItems; barColor: Style.Theme.textMuted }
                }
            }

            // Footer
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Style.Theme.spacingLarge
                Layout.rightMargin: Style.Theme.spacingLarge
                Layout.topMargin: Style.Theme.spacingSmall

                Button {
                    text: "Export Report"
                    onClicked: scanner.exportAnalytics()
                    background: Rectangle { radius: Style.Theme.borderRadius; color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surfaceLight }
                    contentItem: Label { text: parent.text; color: Style.Theme.textPrimary; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                }

                Item { Layout.fillWidth: true }

                Button {
                    text: "Close"
                    onClicked: analyticsDialog.close()
                    background: Rectangle { radius: Style.Theme.borderRadius; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent }
                    contentItem: Label { text: parent.text; color: "#ffffff"; font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                }
            }

            Item { Layout.preferredHeight: Style.Theme.spacingMedium }
        }
    }
}
