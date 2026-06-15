import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../style" as Style

Rectangle {
    id: root
    property bool _ready: false
    Component.onCompleted: _ready = true

    property string title: ""
    property string description: ""
    property string configKey: ""
    property var labels: []           // display labels for ComboBox
    property var values: []           // config values for each option
    property string dataType: "int"   // "int", "float", or "string"
    property string tooltip: ""

    Layout.fillWidth: true
    Layout.preferredHeight: rowLayout.implicitHeight + 16
    color: "transparent"

    RowLayout {
        id: rowLayout
        anchors.fill: parent
        anchors.leftMargin: 12; anchors.rightMargin: 12
        anchors.topMargin: 8; anchors.bottomMargin: 8
        spacing: 12

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2
            Label { text: root.title; color: Style.Theme.textPrimary; font.pixelSize: Style.Theme.fontSizeSmall; font.bold: true }
            Label { text: root.description; color: Style.Theme.textMuted; font.pixelSize: 10; wrapMode: Text.WordWrap; Layout.fillWidth: true }
        }

        ComboBox {
            id: combo
            Layout.preferredWidth: 150
            Layout.preferredHeight: 28
            model: root.labels
            property var values: root.values
            ToolTip.visible: hovered && root.tooltip.length > 0
            ToolTip.delay: 700
            ToolTip.text: root.tooltip

            currentIndex: {
                var v
                if (root.dataType === "float")
                    v = settings.getFloat(root.configKey)
                else if (root.dataType === "string")
                    v = settings.getString(root.configKey)
                else
                    v = settings.getInt(root.configKey)

                if (root.dataType === "string") {
                    for (var i = 0; i < values.length; i++) {
                        if (values[i] === v) return i
                    }
                    return 0
                }

                var best = 0
                for (var j = 0; j < values.length; j++) {
                    if (Math.abs(values[j] - v) < Math.abs(values[best] - v)) best = j
                }
                return best
            }

            onCurrentIndexChanged: {
                if (!root._ready || currentIndex < 0) return
                if (root.dataType === "float")
                    settings.setFloat(root.configKey, values[currentIndex])
                else if (root.dataType === "string")
                    settings.setString(root.configKey, values[currentIndex])
                else
                    settings.setInt(root.configKey, values[currentIndex])
            }

            font.pixelSize: Style.Theme.fontSizeSmall
            background: Rectangle { radius: 4; color: combo.hovered ? Style.Theme.surfaceHover : Style.Theme.surface; border.color: combo.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
            contentItem: Label { leftPadding: 8; rightPadding: 20; text: combo.displayText; font.pixelSize: Style.Theme.fontSizeSmall; color: Style.Theme.textPrimary; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
            indicator: Label { x: combo.width - width - 6; y: (combo.height - height) / 2; text: "\u25BE"; font.pixelSize: 10; color: Style.Theme.textMuted }
        }
    }
}
