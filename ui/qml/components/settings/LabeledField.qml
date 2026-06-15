import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../../style" as Style

ColumnLayout {
    id: root

    property string label: ""
    property string configKey: ""
    property string placeholder: ""
    property bool password: false
    property string tooltip: ""

    Layout.fillWidth: true
    spacing: 4

    Label {
        text: root.label
        color: Style.Theme.textSecondary
        font.pixelSize: Style.Theme.fontSizeSmall
    }

    TextField {
        id: field
        Layout.fillWidth: true
        text: settings.getString(root.configKey)
        placeholderText: text.length > 0 ? "" : root.placeholder
        echoMode: root.password ? TextInput.Password : TextInput.Normal
        color: Style.Theme.textPrimary
        font.pixelSize: Style.Theme.fontSizeNormal
        onEditingFinished: settings.setString(root.configKey, text)
        ToolTip.visible: hovered && root.tooltip.length > 0
        ToolTip.delay: 700
        ToolTip.text: root.tooltip
        background: Rectangle {
            radius: 4
            color: Style.Theme.surfaceLight
            border.color: field.activeFocus ? Style.Theme.accent : Style.Theme.border
            border.width: 1
        }
    }
}
