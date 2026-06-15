import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../style" as Style

Rectangle {
    id: tile

    required property int index
    required property string itemId
    required property string title
    required property int year
    required property int season
    required property int episodes
    required property real rating
    required property string statusText
    required property string statusColor
    required property string resolution
    required property string size
    required property string hdr
    required property bool dovi
    required property string genres
    required property string plexInfo
    required property string url
    required property bool selected
    required property string posterPath
    required property string imdbId
    required property bool hasDuplicateSelected
    property var actionController: scanner
    property bool showRelatedSearchButton: true

    signal clicked(int index)
    signal selectionToggled(int index)

    width: 175
    height: 295
    radius: Style.Theme.borderRadius
    color: selected
           ? Qt.rgba(Style.Theme.accent.r, Style.Theme.accent.g, Style.Theme.accent.b, 0.25)
           : tileMouse.containsMouse
             ? Style.Theme.surfaceHover
             : Style.Theme.surface
    border.color: tile.hasDuplicateSelected ? Style.Theme.warning
                  : selected ? Style.Theme.accent : "transparent"
    border.width: (selected || tile.hasDuplicateSelected) ? 2 : 0

    MouseArea {
        id: tileMouse
        anchors.fill: parent
        hoverEnabled: true
        onClicked: tile.clicked(tile.index)
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 4
        spacing: 4

        // Poster image
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Style.Theme.borderRadiusSmall
            color: Style.Theme.surfaceLight
            clip: true

            Image {
                anchors.fill: parent
                source: tile.posterPath.length > 0
                        ? "https://image.tmdb.org/t/p/w185" + tile.posterPath
                        : ""
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                cache: true
                visible: tile.posterPath.length > 0
            }

            // Fallback placeholder
            Label {
                anchors.centerIn: parent
                visible: tile.posterPath.length === 0
                text: "\u{1F3AC}"
                font.pixelSize: 40
                color: Style.Theme.textMuted
            }

            // Status badge overlay (top-left)
            Rectangle {
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.margins: 4
                width: statusLabel.implicitWidth + 10
                height: 20
                radius: 10
                color: tile.statusColor
                opacity: 0.95

                Label {
                    id: statusLabel
                    anchors.centerIn: parent
                    text: tile.statusText
                    font.pixelSize: 9
                    font.bold: true
                    color: "#ffffff"
                }
            }

            // Rating overlay (top-right)
            Rectangle {
                anchors.top: parent.top
                anchors.right: parent.right
                anchors.margins: 4
                width: ratingLabel.implicitWidth + 8
                height: 20
                radius: 10
                color: Qt.rgba(0, 0, 0, 0.7)
                visible: tile.rating > 0

                Label {
                    id: ratingLabel
                    anchors.centerIn: parent
                    text: "\u2605 " + tile.rating.toFixed(1)
                    font.pixelSize: 10
                    font.bold: true
                    color: tile.rating >= 7.0 ? "#4CAF50" : tile.rating >= 5.0 ? "#FFC107" : "#F44336"
                }
            }

            Column {
                anchors.top: parent.top
                anchors.right: parent.right
                anchors.topMargin: tile.rating > 0 ? 30 : 4
                anchors.rightMargin: 4
                spacing: 4

                Rectangle {
                    visible: tile.showRelatedSearchButton && tile.url.length > 0 && sourceSearch.supportsSearchUrl(tile.url)
                    width: 24
                    height: 24
                    radius: 12
                    color: tileSearchHover.containsMouse ? Style.Theme.info : Qt.rgba(0, 0, 0, 0.65)

                    Label {
                        anchors.centerIn: parent
                        text: "\u{1F50D}"
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: tileSearchHover
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: sourceSearch.searchForItem(tile.title, tile.year, tile.season, tile.url)
                    }

                    ToolTip.visible: tileSearchHover.containsMouse
                    ToolTip.text: "Search this title on the same source in a separate window"
                }

                Rectangle {
                    visible: tile.url.length > 0
                    width: 24
                    height: 24
                    radius: 12
                    color: tileUrlHover.containsMouse ? Style.Theme.accentLight : Qt.rgba(0, 0, 0, 0.65)

                    Label {
                        anchors.centerIn: parent
                        text: "\u{1F517}"
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: tileUrlHover
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: Qt.openUrlExternally(tile.url)
                    }

                    ToolTip.visible: tileUrlHover.containsMouse
                    ToolTip.text: "Open download page"
                }

                Rectangle {
                    visible: tile.imdbId.length > 0
                    width: 24
                    height: 24
                    radius: 12
                    color: tileImdbHover.containsMouse ? "#b8860b" : "#daa520"

                    Label {
                        anchors.centerIn: parent
                        text: "IMDb"
                        font.pixelSize: 6
                        font.bold: true
                        color: "#000000"
                    }

                    MouseArea {
                        id: tileImdbHover
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: Qt.openUrlExternally("https://www.imdb.com/title/" + tile.imdbId)
                    }

                    ToolTip.visible: tileImdbHover.containsMouse
                    ToolTip.text: "Open on IMDb"
                }

                Rectangle {
                    visible: tile.plexInfo !== "-" && tile.plexInfo.length > 0 && tile.statusText !== "Missing Season!"
                    width: 24
                    height: 24
                    radius: 12
                    color: tilePlexHover.containsMouse ? "#cc7a00" : "#e5a00d"

                    Label {
                        anchors.centerIn: parent
                        text: "\u{1F4FA}"
                        font.pixelSize: 12
                    }

                    MouseArea {
                        id: tilePlexHover
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: tile.actionController.openInPlex(tile.index)
                    }

                    ToolTip.visible: tilePlexHover.containsMouse
                    ToolTip.text: "Open in Plex"
                }
            }

            // Resolution + HDR/DV badges (bottom-right)
            Row {
                anchors.bottom: parent.bottom
                anchors.right: parent.right
                anchors.margins: 4
                spacing: 3
                layoutDirection: Qt.RightToLeft

                Rectangle {
                    visible: tile.resolution === "4K"
                    width: 26; height: 16; radius: 3
                    color: "#1a1a1a"; border.color: "#c49520"; border.width: 1
                    Label { anchors.centerIn: parent; text: "4K"; font.pixelSize: 9; font.bold: true; color: "#d4a828" }
                }
                Rectangle {
                    visible: tile.resolution === "1080p"
                    width: 42; height: 16; radius: 3; color: "#F5C518"
                    Row {
                        anchors.centerIn: parent; spacing: 1
                        Label { text: "1080"; font.pixelSize: 8; font.bold: true; color: "#1a1a1a" }
                        Label { text: "HD"; font.pixelSize: 6; font.bold: true; color: "#1a1a1a"; anchors.baseline: parent.children[0].baseline }
                    }
                }
                Rectangle {
                    visible: tile.resolution.length > 0 && tile.resolution !== "4K" && tile.resolution !== "1080p"
                    width: tileResLabel.implicitWidth + 8
                    height: 18; radius: 3; color: "#546E7A"
                    Label { id: tileResLabel; anchors.centerIn: parent; text: tile.resolution; font.pixelSize: 9; font.bold: true; color: "#ffffff" }
                }

                Rectangle {
                    visible: tile.dovi
                    width: 28; height: 18; radius: 3
                    color: "#7B1FA2"

                    Image {
                        anchors.centerIn: parent
                        width: 16; height: 10
                        source: Qt.resolvedUrl("../../../assets/dolby_vision.svg")
                        sourceSize.width: 32; sourceSize.height: 20
                    }
                }

                Rectangle {
                    visible: tile.hdr.length > 0 && !tile.dovi
                    width: 32; height: 18; radius: 4
                    border.color: "#ffbe78"
                    border.width: 1
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: "#ff9b4f" }
                        GradientStop { position: 1.0; color: "#b84a08" }
                    }
                    Text {
                        anchors.centerIn: parent
                        text: "HDR"
                        font.pixelSize: 8
                        font.bold: true
                        font.letterSpacing: 0.8
                        color: "#fff4e2"
                    }
                }
            }

            // Duplicate warning overlay (bottom-left, above checkbox)
            Rectangle {
                visible: tile.hasDuplicateSelected
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.bottomMargin: 30
                anchors.leftMargin: 4
                width: dupWarnLabel.implicitWidth + 8
                height: 18
                radius: 9
                color: Qt.rgba(Style.Theme.warning.r, Style.Theme.warning.g, Style.Theme.warning.b, 0.9)

                Label {
                    id: dupWarnLabel
                    anchors.centerIn: parent
                    text: "\u26A0 Duplicate"
                    font.pixelSize: 8
                    font.bold: true
                    color: "#000000"
                }
            }

            // Selection checkbox overlay (bottom-left)
            CheckBox {
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.margins: 2
                checked: tile.selected
                onToggled: tile.selectionToggled(tile.index)
                opacity: tileMouse.containsMouse || tile.selected ? 1 : 0

                Behavior on opacity { NumberAnimation { duration: 150 } }
            }
        }

        // Title
        Label {
            Layout.fillWidth: true
            text: {
                var t = tile.title
                if (tile.season >= 0)
                    t += " S" + (tile.season < 10 ? "0" : "") + tile.season
                return t
            }
            font.pixelSize: Style.Theme.fontSizeSmall
            font.bold: true
            color: Style.Theme.textPrimary
            elide: Text.ElideRight
            maximumLineCount: 2
            wrapMode: Text.WordWrap
        }

        // Year + Size
        RowLayout {
            Layout.fillWidth: true
            spacing: 4

            Label {
                text: tile.year > 0 ? tile.year.toString() : ""
                font.pixelSize: 10
                color: Style.Theme.textMuted
            }

            Item { Layout.fillWidth: true }

            Label {
                text: tile.size
                font.pixelSize: 10
                color: Style.Theme.textMuted
            }
        }
    }
}
