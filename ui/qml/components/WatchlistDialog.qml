import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../style" as Style

ApplicationWindow {
    id: watchlistDialog
    title: "Watchlist"
    width: 650; height: 520
    minimumWidth: 480; minimumHeight: 380
    color: Style.Theme.background
    modality: Qt.ApplicationModal
    property bool _ready: false

    property var items: []
    property string typeFilter: "all"     // all, movie, tv
    property string sortMode: "recent"    // recent, oldest, rating, az, release

    Component.onCompleted: { _ready = true; refreshWatchlist() }

    function refreshWatchlist() {
        try {
            var json = scanner.getWatchlistJson()
            items = JSON.parse(json)
        } catch (e) {
            items = []
        }
    }

    function filteredItems() {
        var list = watchlistDialog.items
        if (typeFilter !== "all") {
            list = list.filter(function(i) { return i.media_type === typeFilter })
        }
        // Sort
        list = list.slice() // copy
        if (sortMode === "rating") list.sort(function(a,b) { return (b.vote_average||0) - (a.vote_average||0) })
        else if (sortMode === "az") list.sort(function(a,b) { return (a.title||a.name||"").localeCompare(b.title||b.name||"") })
        else if (sortMode === "oldest") list.sort(function(a,b) { return (a.added_at||"").localeCompare(b.added_at||"") })
        else if (sortMode === "release") list.sort(function(a,b) { return (b.release_date||b.first_air_date||"").localeCompare(a.release_date||a.first_air_date||"") })
        // default "recent" is natural order (newest first)
        return list
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.Theme.spacingLarge
        spacing: Style.Theme.spacingMedium

        // Header
        RowLayout {
            Layout.fillWidth: true
            Label { text: "Watchlist"; font.pixelSize: Style.Theme.fontSizeLarge; font.bold: true; color: Style.Theme.textPrimary }
            Item { Layout.fillWidth: true }
            Label {
                text: {
                    var movies = watchlistDialog.items.filter(function(i){return i.media_type==="movie"}).length
                    var tv = watchlistDialog.items.filter(function(i){return i.media_type==="tv"}).length
                    return movies + " movies, " + tv + " TV shows"
                }
                font.pixelSize: Style.Theme.fontSizeSmall; color: Style.Theme.textMuted
            }
        }

        // Filters
        RowLayout {
            Layout.fillWidth: true
            spacing: Style.Theme.spacingSmall

            Repeater {
                model: [
                    { label: "All", value: "all" },
                    { label: "Movies", value: "movie" },
                    { label: "TV Shows", value: "tv" },
                ]
                delegate: Rectangle {
                    required property var modelData
                    width: filterLabel.implicitWidth + 16; height: 26; radius: 13
                    color: watchlistDialog.typeFilter === modelData.value ? Style.Theme.accent : filterMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surfaceLight
                    Label { id: filterLabel; anchors.centerIn: parent; text: modelData.label; font.pixelSize: 11; font.bold: watchlistDialog.typeFilter === modelData.value; color: watchlistDialog.typeFilter === modelData.value ? "#ffffff" : Style.Theme.textPrimary }
                    MouseArea { id: filterMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: watchlistDialog.typeFilter = modelData.value }
                }
            }

            Item { Layout.fillWidth: true }

            ComboBox {
                id: sortCombo
                implicitWidth: 160; implicitHeight: 28
                model: ["Recently Added", "Oldest First", "Highest Rated", "A-Z", "Newest Release"]
                property var values: ["recent", "oldest", "rating", "az", "release"]
                onCurrentIndexChanged: if (watchlistDialog._ready) watchlistDialog.sortMode = values[currentIndex]
                Material.background: Style.Theme.surfaceLight
                Material.foreground: Style.Theme.textPrimary
                font.pixelSize: Style.Theme.fontSizeSmall
            }
        }

        // List
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Style.Theme.borderRadius
            color: Style.Theme.surface
            clip: true

            ListView {
                id: watchlistList
                anchors.fill: parent
                anchors.margins: 4
                spacing: 2

                model: watchlistDialog.filteredItems()

                delegate: Rectangle {
                    required property var modelData
                    required property int index
                    width: ListView.view.width
                    height: 72
                    color: wlMouse.containsMouse ? Style.Theme.surfaceHover
                           : index % 2 === 0 ? "transparent" : Qt.rgba(Style.Theme.surfaceLight.r, Style.Theme.surfaceLight.g, Style.Theme.surfaceLight.b, 0.3)
                    radius: 4

                    MouseArea { id: wlMouse; anchors.fill: parent; hoverEnabled: true }

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 6
                        spacing: 10

                        Image {
                            Layout.preferredWidth: 40; Layout.preferredHeight: 60
                            source: (modelData.poster_path || "").length > 0
                                    ? "https://image.tmdb.org/t/p/w92" + modelData.poster_path : ""
                            fillMode: Image.PreserveAspectFit; asynchronous: true; cache: true
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                spacing: 6
                                Label {
                                    text: modelData.title || modelData.name || "?"
                                    font.pixelSize: Style.Theme.fontSizeNormal; font.bold: true; color: Style.Theme.textPrimary
                                    elide: Text.ElideRight; Layout.fillWidth: true
                                }
                                Rectangle {
                                    width: wlTypeLabel.implicitWidth + 8; height: 16; radius: 8
                                    color: modelData.media_type === "tv" ? Style.Theme.info : Style.Theme.accent
                                    Label { id: wlTypeLabel; anchors.centerIn: parent; text: modelData.media_type === "tv" ? "TV" : "Movie"; font.pixelSize: 8; font.bold: true; color: "#ffffff" }
                                }
                            }

                            RowLayout {
                                spacing: 8
                                Label { text: (modelData.release_date || modelData.first_air_date || "").substring(0,4); font.pixelSize: 11; color: Style.Theme.textMuted }
                                Label { visible: (modelData.vote_average||0)>0; text: "\u2605 " + (modelData.vote_average||0).toFixed(1); font.pixelSize: 11; color: Style.Theme.warning }
                                Label { visible: (modelData.added_at||"").length>0; text: "Added: " + modelData.added_at; font.pixelSize: 10; color: Style.Theme.textMuted }
                            }
                        }

                        // TMDB link
                        Rectangle {
                            width: 26; height: 26; radius: 13
                            color: tmdbLinkMouse.containsMouse ? Style.Theme.accentLight : Style.Theme.surfaceLight
                            Label { anchors.centerIn: parent; text: "\u{1F517}"; font.pixelSize: 12 }
                            MouseArea {
                                id: tmdbLinkMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: Qt.openUrlExternally("https://www.themoviedb.org/" + (modelData.media_type||"movie") + "/" + (modelData.id||""))
                            }
                        }

                        // Remove button
                        Rectangle {
                            width: 26; height: 26; radius: 13
                            color: removeMouse.containsMouse ? Qt.rgba(Style.Theme.error.r,Style.Theme.error.g,Style.Theme.error.b,0.8) : Style.Theme.error
                            Label { anchors.centerIn: parent; text: "\u2715"; font.pixelSize: 11; color: "#ffffff"; font.bold: true }
                            MouseArea {
                                id: removeMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: { scanner.removeFromWatchlist(modelData.id, modelData.media_type || "movie"); watchlistDialog.refreshWatchlist() }
                            }
                            ToolTip.visible: removeMouse.containsMouse; ToolTip.text: "Remove from watchlist"
                        }
                    }
                }

                Label {
                    anchors.centerIn: parent
                    visible: watchlistList.count === 0
                    text: "Watchlist is empty."
                    color: Style.Theme.textMuted
                }
            }
        }

        // Footer
        RowLayout {
            Layout.fillWidth: true

            Button {
                text: "Clear All"
                onClicked: { scanner.clearWatchlist(); watchlistDialog.refreshWatchlist() }
                background: Rectangle { radius: Style.Theme.borderRadius; color: parent.hovered ? Qt.rgba(Style.Theme.error.r,Style.Theme.error.g,Style.Theme.error.b,0.8) : Style.Theme.error }
                contentItem: Label { text: parent.text; color: "#ffffff"; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            }

            Item { Layout.fillWidth: true }

            Button {
                text: "Close"
                onClicked: watchlistDialog.close()
                background: Rectangle { radius: Style.Theme.borderRadius; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent }
                contentItem: Label { text: parent.text; color: "#ffffff"; font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            }
        }
    }
}
