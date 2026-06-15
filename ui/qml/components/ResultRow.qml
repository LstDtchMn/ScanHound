import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../style" as Style

Rectangle {
    id: resultRow

    required property int index
    required property string itemId
    required property string title
    required property int year
    required property int season
    required property int episodes
    required property real rating
    required property int votes
    required property string votesSource
    required property int rtScore
    required property string statusText
    required property string statusColor
    required property string resolution
    required property string size
    required property string hdr
    required property bool dovi
    required property string genres
    required property string language
    required property string plexInfo
    required property string url
    required property bool selected
    required property string posterPath
    required property string hostPref
    required property string imdbId
    required property string description
    required property bool showGroupHeader
    required property int downloadedSiblings
    required property string groupKey
    required property bool hasDuplicateSelected
    required property bool groupCollapsed
    required property int groupItemCount
    required property bool groupIsCollapsed
    required property string plexVersions
    required property string groupSummary
    required property string duplicateDetails
    required property string downloadedSiblingsText
    required property bool groupEnd
    required property string postedDate
    property var actionController: scanner
    property bool showRelatedSearchButton: true

    // Parsed plex versions array — deduplicated by res+hdr+dovi, capped at 4
    readonly property var _plexParsed: {
        try {
            var raw = JSON.parse(plexVersions)
            if (!raw || raw.length === 0) return { list: [], overflow: 0, overflowTip: "" }
            var seen = {}
            var deduped = []
            for (var i = 0; i < raw.length; i++) {
                var v = raw[i]
                var key = (v.res || "") + "|" + (v.hdr ? "hdr" : "") + "|" + (v.dovi ? "dv" : "")
                if (seen[key]) continue
                seen[key] = true
                deduped.push(v)
            }
            var display = deduped.slice(0, 4)
            var displayKeys = {}
            for (var d = 0; d < display.length; d++) {
                var dk = (display[d].res || "") + "|" + (display[d].hdr ? "hdr" : "") + "|" + (display[d].dovi ? "dv" : "")
                displayKeys[dk] = true
            }
            // Build tooltip lines from hidden versions
            var hidden = deduped.slice(4)
            var tipLines = []
            for (var h = 0; h < hidden.length; h++) {
                var hv = hidden[h]
                var parts = []
                if (hv.res) parts.push(hv.res)
                if (hv.dovi) parts.push("DV")
                else if (hv.hdr) parts.push("HDR")
                if (hv.size) parts.push(hv.size + "GB")
                tipLines.push(parts.join(" "))
            }
            // Also list duplicates (same key as displayed, different size)
            // Precompute first-occurrence index per key to avoid O(n²) inner loop
            // TODO: Move entire _plexParsed logic to Python model (results_model.py) for better perf
            var firstOccurrence = {}
            for (var fi = 0; fi < raw.length; fi++) {
                var fk = (raw[fi].res || "") + "|" + (raw[fi].hdr ? "hdr" : "") + "|" + (raw[fi].dovi ? "dv" : "")
                if (!(fk in firstOccurrence)) firstOccurrence[fk] = fi
            }
            for (var j = 0; j < raw.length; j++) {
                var rv = raw[j]
                var rk = (rv.res || "") + "|" + (rv.hdr ? "hdr" : "") + "|" + (rv.dovi ? "dv" : "")
                if (j !== firstOccurrence[rk] && displayKeys[rk]) {
                    var dp = []
                    if (rv.res) dp.push(rv.res)
                    if (rv.dovi) dp.push("DV")
                    else if (rv.hdr) dp.push("HDR")
                    if (rv.size) dp.push(rv.size + "GB")
                    tipLines.push(dp.join(" "))
                }
            }
            var overflow = raw.length - display.length
            return { list: display, overflow: Math.max(0, overflow), overflowTip: tipLines.join("\n") }
        } catch(e) { return { list: [], overflow: 0, overflowTip: "" } }
    }
    readonly property var _plexVersionsList: _plexParsed.list
    readonly property int _plexOverflowCount: _plexParsed.overflow
    readonly property string _plexOverflowTip: _plexParsed.overflowTip
    // Parsed group summary (for collapsed header)
    readonly property var _groupSummary: {
        try { return JSON.parse(groupSummary) }
        catch(e) { return {} }
    }
    readonly property string _dvLogoPath: Qt.resolvedUrl("../../../assets/dolby_vision.svg")
    readonly property string _4kLogoPath: Qt.resolvedUrl("../../../assets/4k_uhd.svg")
    readonly property string _fhdLogoPath: Qt.resolvedUrl("../../../assets/full_hd.svg")
    readonly property string _rgLogoPath: Qt.resolvedUrl("../../../assets/rapidgator.svg")
    readonly property string _nfLogoPath: Qt.resolvedUrl("../../../assets/nitroflare.svg")
    readonly property color _hdrBadgeTop: "#ff9b4f"
    readonly property color _hdrBadgeBottom: "#b84a08"
    readonly property color _hdrBadgeBorder: "#ffbe78"
    readonly property color _hdrBadgeText: "#fff4e2"

    // Line 2 metadata text — brighter than textSecondary for easier reading
    readonly property color _line2Color: Style.Theme.textSecondary

    signal clicked(int index)
    signal selectionToggled(int index)
    signal hostPrefToggled(int index)
    signal groupCollapseToggled(string groupKey)

    height: groupCollapsed ? 0 : (showGroupHeader ? (groupIsCollapsed ? 90 : 120) : 80)
    visible: !groupCollapsed
    clip: true
    color: (showGroupHeader && groupIsCollapsed)
           ? "transparent"
           : selected
             ? Qt.rgba(Style.Theme.accent.r, Style.Theme.accent.g, Style.Theme.accent.b, 0.15)
             : mouseArea.containsMouse
               ? Style.Theme.surfaceHover
               : index % 2 === 0
                 ? "transparent"
                 : Qt.rgba(Style.Theme.surfaceLight.r, Style.Theme.surfaceLight.g, Style.Theme.surfaceLight.b, 0.3)
    radius: Style.Theme.borderRadiusSmall

    // Group header (shown above this row when it starts a new group)
    Rectangle {
        visible: resultRow.showGroupHeader
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: resultRow.groupIsCollapsed ? 86 : 26
        color: "transparent"

        // Full-width accent gradient bar
        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.Theme.spacingSmall
            anchors.rightMargin: Style.Theme.spacingMedium
            height: resultRow.groupIsCollapsed ? 80 : 22
            radius: 4
            color: Qt.rgba(Style.Theme.accent.r, Style.Theme.accent.g, Style.Theme.accent.b, 0.08)

            // Left accent stripe
            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 3
                radius: 2
                color: Style.Theme.accent
            }

            RowLayout {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.leftMargin: 4
                anchors.rightMargin: 0
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.Theme.spacingMedium

                // Expand/collapse arrow (matches checkbox column width)
                Item {
                    Layout.preferredWidth: 36
                    Layout.fillHeight: true

                    Label {
                        anchors.centerIn: parent
                        text: resultRow.groupIsCollapsed ? "\u25B6" : "\u25BC"
                        font.pixelSize: 16
                        color: Style.Theme.accent
                    }
                }

                // Poster (matches regular item poster column)
                Item {
                    visible: resultRow.groupIsCollapsed
                    Layout.preferredWidth: 60
                    Layout.fillHeight: true

                    Rectangle {
                        anchors.centerIn: parent
                        width: 44; height: 66
                        radius: 3
                        color: Style.Theme.surfaceLight
                        clip: true

                        Image {
                            anchors.fill: parent
                            source: resultRow.posterPath.length > 0
                                    ? "https://image.tmdb.org/t/p/w92" + resultRow.posterPath
                                    : ""
                            fillMode: Image.PreserveAspectCrop
                            visible: resultRow.posterPath.length > 0
                            asynchronous: true; cache: true
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: resultRow.posterPath.length === 0
                            text: "\u{1F3AC}"; font.pixelSize: 20
                        }
                    }
                }

                // Title + year
                Label {
                    text: {
                        var t = resultRow.title
                        if (resultRow.year > 0) t += " (" + resultRow.year + ")"
                        return t
                    }
                    font.pixelSize: 13
                    font.bold: true
                    color: Style.Theme.textPrimary
                }

                // Version count pill
                Rectangle {
                    width: versionCountLabel.implicitWidth + 12
                    height: 18; radius: 9
                    color: Style.Theme.accent

                    Label {
                        id: versionCountLabel
                        anchors.centerIn: parent
                        text: resultRow.groupItemCount + " versions"
                        font.pixelSize: 10; font.bold: true; color: "#1a1a1a"
                    }
                }

                // Summary badges (only when collapsed)
                RowLayout {
                    visible: resultRow.groupIsCollapsed && resultRow._groupSummary.resolutions !== undefined
                    spacing: 4

                    Rectangle { width: 1; height: 16; color: Style.Theme.border }

                    // Resolution badges from summary
                    Repeater {
                        model: resultRow._groupSummary.resolutions || []

                        Rectangle {
                            required property string modelData
                            width: modelData === "4K" ? 28 : modelData === "1080p" ? 44 : gsResFb.implicitWidth + 10
                            height: 18; radius: 3
                            color: modelData === "4K" ? "#1a1a1a"
                                   : modelData === "1080p" ? "#F5C518" : "#546E7A"
                            border.color: modelData === "4K" ? "#c49520" : "transparent"
                            border.width: modelData === "4K" ? 1 : 0

                            Row {
                                anchors.centerIn: parent
                                visible: modelData === "1080p"
                                spacing: 1
                                Label { text: "1080"; font.pixelSize: 9; font.bold: true; color: "#1a1a1a" }
                                Label { text: "HD"; font.pixelSize: 7; font.bold: true; color: "#1a1a1a"; anchors.baseline: parent.children[0].baseline }
                            }
                            Label {
                                id: gsResFb
                                anchors.centerIn: parent
                                visible: modelData !== "1080p"
                                text: modelData
                                font.pixelSize: 9; font.bold: true
                                color: modelData === "4K" ? "#d4a828" : "#ffffff"
                            }
                        }
                    }

                    // DV badge
                    Rectangle {
                        visible: resultRow._groupSummary.hasDovi === true
                        width: 28; height: 18; radius: 3; color: "#7B1FA2"
                        Image {
                            anchors.centerIn: parent; width: 16; height: 10
                            source: resultRow._dvLogoPath
                            sourceSize.width: 32; sourceSize.height: 20
                        }
                    }

                    // HDR badge
                    Rectangle {
                        visible: resultRow._groupSummary.hasHdr === true && resultRow._groupSummary.hasDovi !== true
                        width: 32; height: 18; radius: 4
                        border.color: resultRow._hdrBadgeBorder
                        border.width: 1
                        gradient: Gradient {
                            GradientStop { position: 0.0; color: resultRow._hdrBadgeTop }
                            GradientStop { position: 1.0; color: resultRow._hdrBadgeBottom }
                        }
                        Text {
                            anchors.centerIn: parent
                            text: "HDR"
                            font.pixelSize: 8
                            font.bold: true
                            font.letterSpacing: 0.8
                            color: resultRow._hdrBadgeText
                        }
                    }

                    // Size range
                    Label {
                        visible: (resultRow._groupSummary.sizeRange || "").length > 0
                        text: resultRow._groupSummary.sizeRange || ""
                        font.pixelSize: 11
                        color: Style.Theme.textMuted
                    }
                }

                Item { Layout.fillWidth: true }

                // Status summary (only when collapsed)
                RowLayout {
                    visible: resultRow.groupIsCollapsed && resultRow._groupSummary.statuses !== undefined
                    spacing: 6

                    Repeater {
                        model: {
                            var s = resultRow._groupSummary.statuses
                            if (!s) return []
                            var arr = []
                            var keys = Object.keys(s)
                            for (var i = 0; i < keys.length; i++) {
                                arr.push({ label: keys[i], count: s[keys[i]] })
                            }
                            return arr
                        }

                        Rectangle {
                            required property var modelData
                            width: gsStatusLbl.implicitWidth + 10
                            height: 18; radius: 9
                            color: modelData.label === "Missing" ? "#e74c3c"
                                   : modelData.label === "Downloaded" ? "#17a2b8"
                                   : modelData.label === "In Library" ? "#27ae60"
                                   : modelData.label === "Upgrade" ? "#f39c12"
                                   : modelData.label === "DV Upgrade" ? "#9b59b6"
                                   : "#888888"
                            opacity: 0.85

                            Label {
                                id: gsStatusLbl
                                anchors.centerIn: parent
                                text: modelData.count + " " + modelData.label
                                font.pixelSize: 9; font.bold: true; color: "#ffffff"
                            }
                        }
                    }
                }
            }
        }

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: resultRow.groupCollapseToggled(resultRow.groupKey)
        }
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        anchors.topMargin: resultRow.showGroupHeader ? (resultRow.groupIsCollapsed ? 86 : 26) : 0
        hoverEnabled: true
        visible: !(resultRow.showGroupHeader && resultRow.groupIsCollapsed)
        onClicked: resultRow.clicked(resultRow.index)
    }

    RowLayout {
        anchors.fill: parent
        anchors.topMargin: resultRow.showGroupHeader ? (resultRow.groupIsCollapsed ? 86 : 26) : 0
        visible: !(resultRow.showGroupHeader && resultRow.groupIsCollapsed)
        anchors.leftMargin: Style.Theme.spacingMedium
        anchors.rightMargin: Style.Theme.spacingMedium
        spacing: Style.Theme.spacingMedium

        // Checkbox
        CheckBox {
            Layout.preferredWidth: 36
            Layout.alignment: Qt.AlignVCenter
            checked: resultRow.selected
            onToggled: resultRow.selectionToggled(resultRow.index)
        }

        // Poster thumbnail
        Item {
            Layout.preferredWidth: 60
            Layout.fillHeight: true

            Image {
                id: posterThumb
                anchors.centerIn: parent
                width: 44; height: 66
                source: resultRow.posterPath.length > 0
                        ? "https://image.tmdb.org/t/p/w92" + resultRow.posterPath
                        : ""
                fillMode: Image.PreserveAspectFit
                visible: resultRow.posterPath.length > 0
                asynchronous: true
                cache: true
            }

            Rectangle {
                anchors.centerIn: parent
                width: 44; height: 66
                radius: 3
                color: Style.Theme.surfaceLight
                visible: resultRow.posterPath.length === 0
                Label {
                    anchors.centerIn: parent
                    text: "\u{1F3AC}"
                    font.pixelSize: 24
                }
            }

            MouseArea {
                id: posterHover
                anchors.fill: parent
                hoverEnabled: true
            }

            Popup {
                visible: posterHover.containsMouse && resultRow.posterPath.length > 0
                x: 64; y: -80
                width: 160; height: 240
                padding: 2
                closePolicy: Popup.NoAutoClose
                background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: Style.Theme.border; border.width: 1 }

                Image {
                    anchors.fill: parent
                    anchors.margins: 2
                    source: resultRow.posterPath.length > 0
                            ? "https://image.tmdb.org/t/p/w185" + resultRow.posterPath
                            : ""
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    cache: true
                }
            }
        }

        // ── Two-line content area ─────────────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            // ═══ LINE 1: Status, Host, Rating+RT, Title, Year, Actions ═══
            RowLayout {
                id: row1
                anchors.top: parent.top
                anchors.topMargin: (parent.height - 66) / 2   // align with poster top
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: 10

                // Status badge
                Rectangle {
                    id: statusBadgeRect
                    width: Math.max(
                        resultRow.statusText === "DV Upgrade"
                            ? dvUpgradeRow.implicitWidth + 20
                            : statusLabel.implicitWidth + 20,
                        108
                    )
                    height: 24; radius: 12
                    color: resultRow.statusColor

                    // DV Upgrade: logo + text
                    Row {
                        id: dvUpgradeRow
                        anchors.centerIn: parent
                        spacing: 4
                        visible: resultRow.statusText === "DV Upgrade"

                        Image {
                            width: 18; height: 11; y: 1
                            source: resultRow._dvLogoPath
                            sourceSize.width: 32; sourceSize.height: 20
                        }
                        Label {
                            text: "Upgrade"
                            font.pixelSize: 12; font.bold: true; color: "#ffffff"
                        }
                    }

                    // Other statuses: text only
                    Label {
                        id: statusLabel
                        anchors.centerIn: parent
                        text: resultRow.statusText
                        font.pixelSize: 12
                        font.bold: true
                        color: "#ffffff"
                        visible: resultRow.statusText !== "DV Upgrade"
                    }

                    MouseArea {
                        id: statusBadgeHover
                        anchors.fill: parent
                        hoverEnabled: resultRow.statusText === "Missing Season!"
                        acceptedButtons: Qt.NoButton
                    }
                    ToolTip.visible: statusBadgeHover.containsMouse && resultRow.plexInfo !== "-"
                    ToolTip.text: resultRow.plexInfo
                    ToolTip.delay: 300
                }

                // Host badge (RG/NF)
                Item {
                    width: 18; height: 18
                    opacity: hostMouse.containsMouse ? 0.8 : 1.0

                    Image {
                        anchors.fill: parent
                        source: resultRow.hostPref === "RG" ? resultRow._rgLogoPath : resultRow._nfLogoPath
                        sourceSize.width: 18; sourceSize.height: 18
                    }

                    MouseArea {
                        id: hostMouse
                        anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: resultRow.hostPrefToggled(resultRow.index)
                    }
                    ToolTip.visible: hostMouse.containsMouse
                    ToolTip.text: resultRow.hostPref === "RG" ? "Rapidgator (click to switch)" : "Nitroflare (click to switch)"
                }

                // Duplicate warning
                Rectangle {
                    visible: resultRow.hasDuplicateSelected
                    width: visible ? 22 : 0
                    height: 22; radius: 11
                    color: Style.Theme.warning

                    Label {
                        anchors.centerIn: parent
                        text: "\u26A0"
                        font.pixelSize: 13
                        color: "#000000"
                    }

                    MouseArea {
                        id: dupWarnMouse
                        anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: dupPopup.open()
                    }
                    ToolTip.visible: dupWarnMouse.containsMouse && !dupPopup.visible
                    ToolTip.text: "Click for details"

                    Popup {
                        id: dupPopup
                        x: -10; y: 30
                        width: Math.min(dupPopupCol.implicitWidth + 28, 500)
                        height: dupPopupCol.implicitHeight + 24
                        padding: 12
                        closePolicy: Popup.CloseOnPressOutside | Popup.CloseOnEscape
                        background: Rectangle {
                            radius: 8; color: Style.Theme.surface
                            border.color: Style.Theme.warning; border.width: 2
                        }

                        ColumnLayout {
                            id: dupPopupCol
                            anchors.fill: parent
                            spacing: 8

                            RowLayout {
                                spacing: 6
                                Label { text: "\u26A0"; font.pixelSize: 16; color: Style.Theme.warning }
                                Label {
                                    text: "Duplicate Versions Detected"
                                    font.pixelSize: 13; font.bold: true; color: Style.Theme.textPrimary
                                }
                                Item { Layout.fillWidth: true }
                                Rectangle {
                                    width: 20; height: 20; radius: 10
                                    color: dupCloseHover.containsMouse ? Style.Theme.surfaceHover : "transparent"
                                    Label { anchors.centerIn: parent; text: "\u2715"; font.pixelSize: 11; color: Style.Theme.textMuted }
                                    MouseArea { id: dupCloseHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: dupPopup.close() }
                                }
                            }

                            Rectangle { Layout.fillWidth: true; height: 1; color: Style.Theme.border }

                            Label {
                                text: "The following versions of this title are also selected or already in your library:"
                                font.pixelSize: 11; color: Style.Theme.textSecondary
                                wrapMode: Text.WordWrap; Layout.fillWidth: true
                            }

                            Label {
                                text: resultRow.duplicateDetails || "Another version is selected"
                                font.pixelSize: 12; color: Style.Theme.textPrimary
                                wrapMode: Text.WordWrap; Layout.fillWidth: true
                                lineHeight: 1.4
                            }
                        }
                    }
                }

                // Rating + RT score block
                Rectangle {
                    Layout.minimumWidth: ratingRow.implicitWidth + 12
                    height: 22; radius: 5
                    color: Style.Theme.darkMode
                        ? Qt.rgba(1, 1, 1, 0.05)
                        : Qt.rgba(0, 0, 0, 0.04)

                    RowLayout {
                        id: ratingRow
                        anchors.centerIn: parent
                        spacing: 3

                        Label {
                            text: resultRow.rating > 0 ? resultRow.rating.toFixed(1) : "-"
                            font.pixelSize: Style.Theme.fontSizeMedium
                            font.bold: true
                            color: resultRow.rating >= 7.0
                                   ? Style.Theme.success
                                   : resultRow.rating >= 5.0
                                     ? Style.Theme.textPrimary
                                     : Style.Theme.error
                        }

                        Label {
                            visible: resultRow.rtScore >= 0
                            text: resultRow.rtScore >= 60 ? "\u{1F345}" : "\u{1F7E9}"
                            font.pixelSize: 14
                        }
                        Label {
                            visible: resultRow.rtScore >= 0
                            text: resultRow.rtScore + "%"
                            font.pixelSize: 12; font.bold: true
                            color: resultRow.rtScore >= 60 ? "#4a7c27" : "#c74523"
                        }

                        // Vote count
                        Label {
                            visible: resultRow.votes > 0
                            text: {
                                var v = resultRow.votes
                                if (v >= 1000000) return "(" + (v / 1000000).toFixed(1) + "M)"
                                if (v >= 1000) return "(" + (v / 1000).toFixed(1) + "k)"
                                return "(" + v.toString() + ")"
                            }
                            font.pixelSize: 10
                            color: Qt.rgba(Style.Theme.textMuted.r, Style.Theme.textMuted.g, Style.Theme.textMuted.b, 0.7)
                        }
                    }
                }

                // Title (fills all remaining space!)
                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    HoverHandler { id: titleHover }

                    ToolTip.visible: titleHover.hovered && resultRow.description.length > 0
                    ToolTip.text: resultRow.description
                    ToolTip.delay: 400
                    ToolTip.timeout: 8000

                    Menu {
                        id: titleContextMenu
                        MenuItem {
                            text: "Copy Title"
                            onTriggered: { titleInput.selectAll(); titleInput.copy(); titleInput.deselect() }
                        }
                        MenuItem {
                            text: "Copy Title + Season"
                            visible: resultRow.season >= 0
                            onTriggered: {
                                titleClipHelper.text = resultRow.title + " S" + (resultRow.season < 10 ? "0" : "") + resultRow.season
                                titleClipHelper.selectAll(); titleClipHelper.copy()
                            }
                        }
                        MenuItem {
                            text: "Copy IMDb ID"
                            visible: resultRow.imdbId.length > 0
                            onTriggered: { titleClipHelper.text = resultRow.imdbId; titleClipHelper.selectAll(); titleClipHelper.copy() }
                        }
                        MenuSeparator {
                            visible: resultRow.showRelatedSearchButton && resultRow.url.length > 0
                        }
                        MenuItem {
                            text: "Search for similar"
                            visible: resultRow.showRelatedSearchButton && resultRow.url.length > 0
                            onTriggered: sourceSearch.searchForItem(resultRow.title, resultRow.year, resultRow.season, resultRow.url)
                        }
                    }
                    TextEdit { id: titleClipHelper; visible: false }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 4
                        spacing: 8

                        TextInput {
                            id: titleInput
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                            text: {
                                var t = resultRow.title
                                if (resultRow.year > 0)
                                    t += " (" + resultRow.year + ")"
                                if (resultRow.season >= 0)
                                    t += "  S" + (resultRow.season < 10 ? "0" : "") + resultRow.season
                                        + (resultRow.episodes > 0 ? " (" + resultRow.episodes + " eps)" : "")
                                return t
                            }
                            readOnly: true
                            selectByMouse: true
                            selectionColor: Style.Theme.accent
                            selectedTextColor: "#ffffff"
                            font.pixelSize: 16
                            font.bold: true
                            font.letterSpacing: 0.3
                            color: Style.Theme.textEmphasis
                            clip: true

                            MouseArea {
                                anchors.fill: parent
                                acceptedButtons: Qt.RightButton
                                onClicked: titleContextMenu.popup()
                            }
                        }

                        // Downloaded siblings badge
                        Rectangle {
                            visible: resultRow.downloadedSiblings > 0
                            width: sibLabel.implicitWidth + 10
                            height: 20; radius: 10
                            color: Style.Theme.info

                            Label {
                                id: sibLabel
                                anchors.centerIn: parent
                                text: resultRow.downloadedSiblings + "x"
                                font.pixelSize: 11; font.bold: true; color: "#ffffff"
                            }

                            MouseArea {
                                id: sibMouse
                                anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: sibPopup.open()
                            }
                            ToolTip.visible: sibMouse.containsMouse && !sibPopup.visible
                            ToolTip.text: "Click for details"

                            Popup {
                                id: sibPopup
                                x: parent.width - width; y: 26
                                width: Math.min(sibPopupCol.implicitWidth + 28, 450)
                                height: sibPopupCol.implicitHeight + 24
                                padding: 12
                                closePolicy: Popup.CloseOnPressOutside | Popup.CloseOnEscape
                                background: Rectangle {
                                    radius: 8; color: Style.Theme.surface
                                    border.color: Style.Theme.info; border.width: 2
                                }

                                ColumnLayout {
                                    id: sibPopupCol
                                    anchors.fill: parent
                                    spacing: 8

                                    RowLayout {
                                        spacing: 6
                                        Label { text: "\u{1F4E6}"; font.pixelSize: 14 }
                                        Label {
                                            text: "Previously Downloaded Versions"
                                            font.pixelSize: 13; font.bold: true; color: Style.Theme.textPrimary
                                        }
                                        Item { Layout.fillWidth: true }
                                        Rectangle {
                                            width: 20; height: 20; radius: 10
                                            color: sibCloseHover.containsMouse ? Style.Theme.surfaceHover : "transparent"
                                            Label { anchors.centerIn: parent; text: "\u2715"; font.pixelSize: 11; color: Style.Theme.textMuted }
                                            MouseArea { id: sibCloseHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: sibPopup.close() }
                                        }
                                    }

                                    Rectangle { Layout.fillWidth: true; height: 1; color: Style.Theme.border }

                                    Label {
                                        text: resultRow.downloadedSiblings + " version(s) of this title were previously downloaded:"
                                        font.pixelSize: 11; color: Style.Theme.textSecondary
                                        wrapMode: Text.WordWrap; Layout.fillWidth: true
                                    }

                                    Label {
                                        text: resultRow.downloadedSiblingsText || "Details unavailable"
                                        font.pixelSize: 12; color: Style.Theme.textPrimary
                                        wrapMode: Text.WordWrap; Layout.fillWidth: true
                                        lineHeight: 1.4
                                    }
                                }
                            }
                        }
                    }
                }

                // Action buttons
                RowLayout {
                    spacing: 3

                    Rectangle {
                        width: 26; height: 26; radius: 13
                        color: searchHover.containsMouse ? Style.Theme.info : Style.Theme.surfaceLight
                        visible: resultRow.showRelatedSearchButton && resultRow.url.length > 0 && sourceSearch.supportsSearchUrl(resultRow.url)
                        Label { anchors.centerIn: parent; text: "\u{1F50D}"; font.pixelSize: 13 }
                        MouseArea {
                            id: searchHover
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: sourceSearch.searchForItem(resultRow.title, resultRow.year, resultRow.season, resultRow.url)
                        }
                        ToolTip.visible: searchHover.containsMouse
                        ToolTip.text: "Search this title on the same source in a separate window"
                    }

                    Rectangle {
                        width: 26; height: 26; radius: 13
                        color: urlHover.containsMouse ? Style.Theme.accentLight : Style.Theme.surfaceLight
                        visible: resultRow.url.length > 0
                        Label { anchors.centerIn: parent; text: "\u{1F517}"; font.pixelSize: 13 }
                        MouseArea { id: urlHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: Qt.openUrlExternally(resultRow.url) }
                        ToolTip.visible: urlHover.containsMouse; ToolTip.text: "Open download page"
                    }

                    Rectangle {
                        width: 26; height: 26; radius: 13
                        color: imdbHover.containsMouse ? "#b8860b" : "#daa520"
                        visible: resultRow.imdbId.length > 0
                        Label { anchors.centerIn: parent; text: "IMDb"; font.pixelSize: 7; font.bold: true; color: "#000000" }
                        MouseArea { id: imdbHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: Qt.openUrlExternally("https://www.imdb.com/title/" + resultRow.imdbId) }
                        ToolTip.visible: imdbHover.containsMouse; ToolTip.text: "Open on IMDb"
                    }

                    Rectangle {
                        width: 26; height: 26; radius: 13
                        color: plexHover.containsMouse ? "#cc7a00" : "#e5a00d"
                        visible: resultRow.plexInfo !== "-" && resultRow.plexInfo.length > 0
                                 && resultRow.statusText !== "Missing Season!"
                        Label { anchors.centerIn: parent; text: "\u{1F4FA}"; font.pixelSize: 13 }
                        MouseArea { id: plexHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: resultRow.actionController.openInPlex(resultRow.index) }
                        ToolTip.visible: plexHover.containsMouse; ToolTip.text: "Open in Plex"
                    }
                }
            }

            // ═══ LINE 2: Genre, Language, Quality badges, Size ═══
            RowLayout {
                anchors.bottom: parent.bottom
                anchors.bottomMargin: (parent.height - 66) / 2   // align with poster bottom
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: Style.Theme.spacingSmall

                // Genre
                Label {
                    text: resultRow.genres
                    font.pixelSize: 15
                    color: resultRow._line2Color
                    elide: Text.ElideRight
                    Layout.preferredWidth: Math.min(implicitWidth, 250)

                    ToolTip.visible: genreMouse.containsMouse && truncated
                    ToolTip.text: resultRow.genres
                    MouseArea { id: genreMouse; anchors.fill: parent; hoverEnabled: true }
                }

                Rectangle { visible: resultRow.language.length > 0; width: 1; height: 14; color: Style.Theme.textMuted; opacity: 0.4 }

                // Language
                Label {
                    visible: resultRow.language.length > 0
                    text: resultRow.language
                    font.pixelSize: 15
                    color: resultRow._line2Color
                }

                Rectangle { visible: resultRow.resolution.length > 0; width: 1; height: 14; color: Style.Theme.textMuted; opacity: 0.4 }

                // Quality badges (source)
                RowLayout {
                    spacing: 4

                    Label {
                        visible: resultRow.resolution.length > 0
                        text: "Source"
                        font.pixelSize: 14
                        font.bold: true
                        color: resultRow._line2Color
                    }

                    // 4K badge
                    Rectangle {
                        visible: resultRow.resolution === "4K"
                        width: 38; height: 24; radius: 4
                        color: "#1a1a1a"; border.color: "#c49520"; border.width: 1
                        Label { anchors.centerIn: parent; text: "4K"; font.pixelSize: 14; font.bold: true; color: "#d4a828" }
                    }
                    // 1080p badge
                    Rectangle {
                        visible: resultRow.resolution === "1080p"
                        width: 60; height: 24; radius: 4
                        color: "#F5C518"
                        Row {
                            anchors.centerIn: parent
                            spacing: 2
                            Label { text: "1080"; font.pixelSize: 12; font.bold: true; color: "#1a1a1a" }
                            Label { text: "HD"; font.pixelSize: 9; font.bold: true; color: "#1a1a1a"; anchors.baseline: parent.children[0].baseline }
                        }
                    }
                    // Other resolution (text fallback)
                    Rectangle {
                        visible: resultRow.resolution.length > 0 && resultRow.resolution !== "4K" && resultRow.resolution !== "1080p"
                        width: resBadgeLabel.implicitWidth + 14
                        height: 24; radius: 4; color: "#546E7A"
                        Label { id: resBadgeLabel; anchors.centerIn: parent; text: resultRow.resolution; font.pixelSize: 12; font.bold: true; color: "#ffffff" }
                    }

                    // DV badge with Dolby logo
                    Rectangle {
                        visible: resultRow.dovi
                        width: 43; height: 24; radius: 4; color: "#7B1FA2"
                        Image {
                            anchors.centerIn: parent
                            width: 24; height: 15
                            source: resultRow._dvLogoPath
                            sourceSize.width: 32; sourceSize.height: 20
                        }
                    }

                    Rectangle {
                        visible: resultRow.hdr === "HDR" && !resultRow.dovi
                        width: 48; height: 24; radius: 5
                        border.color: resultRow._hdrBadgeBorder
                        border.width: 1
                        gradient: Gradient {
                            GradientStop { position: 0.0; color: resultRow._hdrBadgeTop }
                            GradientStop { position: 1.0; color: resultRow._hdrBadgeBottom }
                        }
                        Text {
                            anchors.centerIn: parent
                            text: "HDR"
                            font.pixelSize: 11
                            font.bold: true
                            font.letterSpacing: 1.2
                            color: resultRow._hdrBadgeText
                        }
                    }
                }

                // Size
                Label {
                    text: resultRow.size
                    font.pixelSize: 15
                    color: resultRow._line2Color
                }

                Item { Layout.fillWidth: true }
            }

            // Plex specs + date (bottom-right, aligned with poster bottom)
            Row {
                anchors.bottom: parent.bottom
                anchors.bottomMargin: (parent.height - 66) / 2
                anchors.right: parent.right
                spacing: 6

                // Missing Season info
                Row {
                    visible: resultRow.statusText === "Missing Season!" && resultRow.plexInfo !== "-"
                    spacing: 3
                    anchors.baseline: postedDateLabel.baseline
                    Label { text: "Plex"; font.pixelSize: 15; font.bold: true; color: Style.Theme.textSecondary }
                    Label { text: resultRow.plexInfo; font.pixelSize: 15; color: "#d35400" }
                }

                // Plex library versions
                Row {
                    visible: resultRow._plexVersionsList.length > 0
                    spacing: 3
                    anchors.verticalCenter: postedDateLabel.verticalCenter

                    Label { text: "Plex"; font.pixelSize: 15; font.bold: true; color: resultRow._line2Color; anchors.verticalCenter: parent.verticalCenter }

                    Repeater {
                        model: resultRow._plexVersionsList

                        Row {
                            spacing: 2
                            required property var modelData
                            required property int index

                            Label {
                                visible: index > 0
                                text: "\u00B7"; font.pixelSize: 16; font.bold: true
                                color: Style.Theme.textMuted
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            Rectangle {
                                visible: modelData.res === "4K"
                                width: 38; height: 24; radius: 4
                                color: "#1a1a1a"; border.color: "#c49520"; border.width: 1
                                anchors.verticalCenter: parent.verticalCenter
                                Label { anchors.centerIn: parent; text: "4K"; font.pixelSize: 14; font.bold: true; color: "#d4a828" }
                            }
                            Rectangle {
                                visible: modelData.res === "1080p"
                                width: 60; height: 24; radius: 4
                                color: "#F5C518"
                                anchors.verticalCenter: parent.verticalCenter
                                Row {
                                    anchors.centerIn: parent; spacing: 2
                                    Label { text: "1080"; font.pixelSize: 12; font.bold: true; color: "#1a1a1a" }
                                    Label { text: "HD"; font.pixelSize: 9; font.bold: true; color: "#1a1a1a"; anchors.baseline: parent.children[0].baseline }
                                }
                            }
                            Rectangle {
                                visible: modelData.res !== "4K" && modelData.res !== "1080p" && modelData.res !== "?"
                                width: plexResFallbackLabel.implicitWidth + 14
                                height: 24; radius: 4; color: "#546E7A"
                                anchors.verticalCenter: parent.verticalCenter
                                Label { id: plexResFallbackLabel; anchors.centerIn: parent; text: modelData.res || ""; font.pixelSize: 12; font.bold: true; color: "#ffffff" }
                            }

                            Rectangle {
                                visible: modelData.dovi === true
                                width: 43; height: 24; radius: 4; color: "#7B1FA2"
                                anchors.verticalCenter: parent.verticalCenter
                                Image { anchors.centerIn: parent; width: 24; height: 15; source: resultRow._dvLogoPath; sourceSize.width: 32; sourceSize.height: 20 }
                            }

                            Rectangle {
                                visible: modelData.hdr === true && modelData.dovi !== true
                                width: 48; height: 24; radius: 5
                                border.color: resultRow._hdrBadgeBorder
                                border.width: 1
                                gradient: Gradient {
                                    GradientStop { position: 0.0; color: resultRow._hdrBadgeTop }
                                    GradientStop { position: 1.0; color: resultRow._hdrBadgeBottom }
                                }
                                anchors.verticalCenter: parent.verticalCenter
                                Text {
                                    anchors.centerIn: parent
                                    text: "HDR"
                                    font.pixelSize: 11
                                    font.bold: true
                                    font.letterSpacing: 1.2
                                    color: resultRow._hdrBadgeText
                                }
                            }

                            Label {
                                text: (modelData.size !== undefined ? modelData.size : "?") + "GB"
                                font.pixelSize: 15
                                color: Style.Theme.textMuted
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }
                    }

                    Label {
                        visible: resultRow._plexOverflowCount > 0
                        text: "+" + resultRow._plexOverflowCount + " more"
                        font.pixelSize: 12
                        font.italic: true
                        color: Style.Theme.textMuted
                        anchors.verticalCenter: parent.verticalCenter

                        ToolTip.visible: resultRow._plexOverflowTip.length > 0 && plexOverflowMa.containsMouse
                        ToolTip.delay: 300
                        ToolTip.text: resultRow._plexOverflowTip

                        MouseArea {
                            id: plexOverflowMa
                            anchors.fill: parent
                            hoverEnabled: true
                            acceptedButtons: Qt.NoButton
                        }
                    }
                }

                // Separator before date
                Rectangle {
                    visible: resultRow.postedDate.length > 0 && (resultRow._plexVersionsList.length > 0 || (resultRow.statusText === "Missing Season!" && resultRow.plexInfo !== "-"))
                    width: 1; height: 18; color: Style.Theme.textMuted; opacity: 0.4
                    anchors.verticalCenter: postedDateLabel.verticalCenter
                }

                // Posted date
                Label {
                    id: postedDateLabel
                    visible: resultRow.postedDate.length > 0
                    text: {
                        var parts = resultRow.postedDate.split(" ")
                        if (parts.length >= 6) {
                            var month = parts[0].substring(0, 3)
                            var day = parts[1]
                            var yr = parts[2]
                            var time = parts[4]
                            var ampm = parts[5]
                            return month + " " + day + " " + yr + " " + time + " " + ampm
                        }
                        return resultRow.postedDate
                    }
                    font.pixelSize: 15
                    color: Style.Theme.textMuted
                }
            }
        }
    }

    // Bottom border — thicker accent line at the end of a group
    Rectangle {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: resultRow.groupEnd ? Style.Theme.spacingSmall : 0
        anchors.rightMargin: resultRow.groupEnd ? Style.Theme.spacingMedium : 0
        height: resultRow.groupEnd ? 2 : 1
        color: resultRow.groupEnd ? Style.Theme.accent : Style.Theme.border
        opacity: resultRow.groupEnd ? 0.45 : 0.3
    }
}
