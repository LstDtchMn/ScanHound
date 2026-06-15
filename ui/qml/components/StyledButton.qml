import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../style" as Style

Button {
    id: root

    property string variant: "primary"   // "primary", "success", "danger"

    background: Rectangle {
        radius: Style.Theme.borderRadius
        color: {
            if (!root.enabled)
                return Style.Theme.surfaceLight
            if (root.variant === "success")
                return root.hovered ? Style.Theme.successLight : Style.Theme.success
            if (root.variant === "danger")
                return root.hovered ? "#ff6b5a" : Style.Theme.error
            // primary (default)
            return root.hovered ? Style.Theme.accentLight : Style.Theme.accent
        }
    }

    contentItem: Label {
        text: root.text
        color: root.enabled ? "#ffffff" : Style.Theme.textMuted
        font.bold: true
        font.pixelSize: Style.Theme.fontSizeSmall
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }
}
