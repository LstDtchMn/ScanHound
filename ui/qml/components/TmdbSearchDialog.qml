import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../style" as Style

ApplicationWindow {
    id: tmdbSearchDialog
    title: "Search TMDB — Add to Watchlist"
    width: 600; height: 500
    minimumWidth: 450; minimumHeight: 350
    color: Style.Theme.background
    modality: Qt.ApplicationModal

    property var searchResults: []
    property bool searching: false

    function doSearch() {
        if (searchField.text.trim().length === 0) return
        searching = true
        searchResults = []
        var typeFilter = typeGroup.checkedButton ? typeGroup.checkedButton.filterType : "all"
        scanner.tmdbSearch(searchField.text.trim(), typeFilter)
    }

    Connections {
        target: scanner
        function onTmdbSearchResults(jsonStr) {
            try {
                tmdbSearchDialog.searchResults = JSON.parse(jsonStr)
            } catch (e) {
                tmdbSearchDialog.searchResults = []
            }
            tmdbSearchDialog.searching = false
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.Theme.spacingLarge
        spacing: Style.Theme.spacingMedium

        Label {
            text: "Search TMDB"
            font.pixelSize: Style.Theme.fontSizeLarge
            font.bold: true
            color: Style.Theme.textPrimary
        }

        // Search bar
        RowLayout {
            Layout.fillWidth: true
            spacing: Style.Theme.spacingSmall

            TextField {
                id: searchField
                Layout.fillWidth: true
                placeholderText: "Movie or TV show title..."
                color: Style.Theme.textPrimary
                font.pixelSize: Style.Theme.fontSizeNormal
                onAccepted: tmdbSearchDialog.doSearch()
                background: Rectangle { radius: 4; color: Style.Theme.surfaceLight; border.color: parent.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
            }

            Button {
                text: tmdbSearchDialog.searching ? "..." : "Search"
                enabled: !tmdbSearchDialog.searching && searchField.text.trim().length > 0
                onClicked: tmdbSearchDialog.doSearch()
                implicitWidth: 80
                background: Rectangle { radius: Style.Theme.borderRadius; color: parent.enabled ? (parent.hovered ? Style.Theme.accentLight : Style.Theme.accent) : Style.Theme.surfaceLight }
                contentItem: Label { text: parent.text; color: parent.enabled ? "#ffffff" : Style.Theme.textMuted; font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            }
        }

        // Type filter
        RowLayout {
            spacing: Style.Theme.spacingSmall
            ButtonGroup { id: typeGroup }

            Repeater {
                model: [
                    { label: "All", type: "all" },
                    { label: "Movies", type: "movie" },
                    { label: "TV Shows", type: "tv" },
                ]
                delegate: RadioButton {
                    required property var modelData
                    required property int index
                    property string filterType: modelData.type
                    text: modelData.label
                    checked: index === 0
                    ButtonGroup.group: typeGroup
                }
            }
        }

        // Results
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Style.Theme.borderRadius
            color: Style.Theme.surface
            clip: true

            BusyIndicator {
                anchors.centerIn: parent
                visible: tmdbSearchDialog.searching
                running: visible
            }

            ListView {
                id: searchResultsList
                anchors.fill: parent
                anchors.margins: 4
                spacing: 2
                visible: !tmdbSearchDialog.searching

                model: tmdbSearchDialog.searchResults

                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    width: ListView.view.width
                    height: 80
                    color: srMouse.containsMouse
                           ? Style.Theme.surfaceHover
                           : index % 2 === 0 ? "transparent" : Qt.rgba(Style.Theme.surfaceLight.r, Style.Theme.surfaceLight.g, Style.Theme.surfaceLight.b, 0.3)
                    radius: 4

                    MouseArea {
                        id: srMouse
                        anchors.fill: parent
                        hoverEnabled: true
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 10

                        // Poster
                        Image {
                            Layout.preferredWidth: 45
                            Layout.preferredHeight: 67
                            source: (modelData.poster_path || "").length > 0
                                    ? "https://image.tmdb.org/t/p/w92" + modelData.poster_path : ""
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            cache: true
                        }

                        // Info
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                spacing: 6
                                Label {
                                    text: modelData.title || modelData.name || "?"
                                    font.pixelSize: Style.Theme.fontSizeNormal
                                    font.bold: true
                                    color: Style.Theme.textPrimary
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Rectangle {
                                    width: typeLabel.implicitWidth + 8; height: 18; radius: 9
                                    color: (modelData.media_type || "") === "tv" ? Style.Theme.info : Style.Theme.accent
                                    Label {
                                        id: typeLabel
                                        anchors.centerIn: parent
                                        text: (modelData.media_type || "") === "tv" ? "TV" : "Movie"
                                        font.pixelSize: 9; font.bold: true; color: "#ffffff"
                                    }
                                }
                            }

                            RowLayout {
                                spacing: 8
                                Label {
                                    text: (modelData.release_date || modelData.first_air_date || "").substring(0, 4)
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                    color: Style.Theme.textMuted
                                }
                                Label {
                                    visible: (modelData.vote_average || 0) > 0
                                    text: "\u2605 " + (modelData.vote_average || 0).toFixed(1)
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                    color: Style.Theme.warning
                                }
                            }

                            Label {
                                text: modelData.overview || ""
                                font.pixelSize: 10
                                color: Style.Theme.textMuted
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                                maximumLineCount: 2
                                wrapMode: Text.WordWrap
                            }
                        }

                        // Add to watchlist button
                        Rectangle {
                            width: 60; height: 28; radius: Style.Theme.borderRadius
                            color: addBtnMouse.containsMouse ? Style.Theme.accentLight : Style.Theme.accent

                            Label {
                                anchors.centerIn: parent
                                text: "Add"
                                font.pixelSize: Style.Theme.fontSizeSmall
                                font.bold: true
                                color: "#ffffff"
                            }
                            MouseArea {
                                id: addBtnMouse
                                anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: scanner.addToWatchlist(JSON.stringify(modelData))
                            }
                        }
                    }
                }

                Label {
                    anchors.centerIn: parent
                    visible: searchResultsList.count === 0 && !tmdbSearchDialog.searching
                    text: "Search for movies or TV shows to add to your watchlist."
                    color: Style.Theme.textMuted
                    font.pixelSize: Style.Theme.fontSizeSmall
                }
            }
        }

        // Footer
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            Button {
                text: "Close"
                onClicked: tmdbSearchDialog.close()
                background: Rectangle { radius: Style.Theme.borderRadius; color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surfaceLight }
                contentItem: Label { text: parent.text; color: Style.Theme.textPrimary; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            }
        }
    }
}
