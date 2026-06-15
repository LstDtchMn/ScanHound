import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../style" as Style

Rectangle {
    id: groupHeader

    required property int index
    required property string groupKey
    required property int groupLevel
    required property string groupTitle
    required property int groupCount
    required property bool groupExpanded
    required property string groupPosterPath

    signal toggled(string key)

    height: groupLevel === 0 ? 40 : groupLevel === 1 ? 36 : 30
    color: groupLevel === 0
           ? Style.Theme.surfaceLight
           : "transparent"
    radius: Style.Theme.borderRadiusSmall

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Style.Theme.spacingSmall + (groupLevel * 20)
        anchors.rightMargin: Style.Theme.spacingSmall
        spacing: Style.Theme.spacingSmall

        // Expand/collapse chevron
        Label {
            text: groupExpanded ? "▼" : "▶"
            font.pixelSize: 10
            color: Style.Theme.textMuted
            Layout.preferredWidth: 16
            horizontalAlignment: Text.AlignHCenter
        }

        // Poster thumbnail (for series headers)
        Rectangle {
            visible: groupLevel === 1 && groupPosterPath.length > 0
            Layout.preferredWidth: visible ? 24 : 0
            Layout.preferredHeight: visible ? 36 : 0
            color: Style.Theme.surfaceLight
            radius: 3
            clip: true

            Image {
                anchors.fill: parent
                source: groupPosterPath.length > 0
                        ? "https://image.tmdb.org/t/p/w92" + groupPosterPath
                        : ""
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
            }
        }

        // Group title
        Label {
            text: groupTitle
            font.pixelSize: groupLevel === 0
                            ? Style.Theme.fontSizeNormal
                            : Style.Theme.fontSizeSmall
            font.bold: groupLevel <= 1
            color: groupLevel === 0
                   ? Style.Theme.accent
                   : Style.Theme.textPrimary
            Layout.fillWidth: true
            elide: Text.ElideRight
        }
    }

    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: groupHeader.toggled(groupKey)
    }

    // Bottom border for top-level groups
    Rectangle {
        visible: groupLevel === 0
        anchors.bottom: parent.bottom
        width: parent.width
        height: 1
        color: Style.Theme.border
        opacity: 0.5
    }
}
