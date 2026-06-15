import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "style" as Style
import "components/settings" as SettingsTabs

ApplicationWindow {
    id: settingsDialog
    title: "Settings"
    width: 800
    height: 600
    minimumWidth: 650
    minimumHeight: 500
    color: Style.Theme.surface
    modality: Qt.ApplicationModal

    Material.theme: Style.Theme.darkMode ? Material.Dark : Material.Light
    Material.accent: Style.Theme.accent
    Material.primary: Style.Theme.accent
    Material.background: Style.Theme.surface
    Material.foreground: Style.Theme.textPrimary

    property bool _slidersReady: false
    Component.onCompleted: { settings.loadLibraries(); _slidersReady = true }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Tab bar (scrollable) ──────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 40
            color: Style.Theme.surface

            property bool overflows: tabFlickable.contentWidth > tabFlickable.width

            // Left scroll arrow
            Rectangle {
                id: leftArrow
                anchors.left: parent.left
                anchors.top: parent.top; anchors.bottom: parent.bottom
                anchors.bottomMargin: 1
                width: 24
                visible: parent.overflows && tabFlickable.contentX > 0
                color: leftArrowMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surface
                z: 2

                Label {
                    anchors.centerIn: parent
                    text: "\u25C0"
                    font.pixelSize: 10
                    color: Style.Theme.textSecondary
                }

                MouseArea {
                    id: leftArrowMouse
                    anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: tabFlickable.contentX = Math.max(0, tabFlickable.contentX - 120)
                }
            }

            // Right scroll arrow
            Rectangle {
                id: rightArrow
                anchors.right: parent.right
                anchors.top: parent.top; anchors.bottom: parent.bottom
                anchors.bottomMargin: 1
                width: 24
                visible: parent.overflows && tabFlickable.contentX < tabFlickable.contentWidth - tabFlickable.width - 1
                color: rightArrowMouse.containsMouse ? Style.Theme.surfaceHover : Style.Theme.surface
                z: 2

                Label {
                    anchors.centerIn: parent
                    text: "\u25B6"
                    font.pixelSize: 10
                    color: Style.Theme.textSecondary
                }

                MouseArea {
                    id: rightArrowMouse
                    anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: tabFlickable.contentX = Math.min(tabFlickable.contentWidth - tabFlickable.width, tabFlickable.contentX + 120)
                }
            }

            Flickable {
                id: tabFlickable
                anchors.left: leftArrow.visible ? leftArrow.right : parent.left
                anchors.right: rightArrow.visible ? rightArrow.left : parent.right
                anchors.top: parent.top; anchors.bottom: parent.bottom
                anchors.bottomMargin: 1
                contentWidth: tabRow.width
                contentHeight: height
                clip: true
                flickableDirection: Flickable.HorizontalFlick
                boundsBehavior: Flickable.StopAtBounds

                Behavior on contentX { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }

                Row {
                    id: tabRow
                    height: parent.height
                    spacing: 2
                    leftPadding: Style.Theme.spacingSmall
                    rightPadding: Style.Theme.spacingSmall

                    Repeater {
                        model: ["Plex", "API Keys", "Matching", "Sources", "JDownloader", "Scheduler", "Log", "Notifications", "Appearance", "Auto-Grab"]

                        delegate: Rectangle {
                            required property string modelData
                            required property int index
                            width: tabLabel.implicitWidth + 16
                            height: tabRow.height - 4
                            anchors.verticalCenter: parent.verticalCenter
                            radius: Style.Theme.borderRadiusSmall
                            color: tabStack.currentIndex === index
                                   ? Style.Theme.accent
                                   : tabMouse.containsMouse
                                     ? Style.Theme.surfaceHover
                                     : Style.Theme.surface

                            Label {
                                id: tabLabel
                                anchors.centerIn: parent
                                text: modelData
                                font.pixelSize: Style.Theme.fontSizeSmall
                                font.bold: tabStack.currentIndex === index
                                color: tabStack.currentIndex === index
                                       ? "#ffffff"
                                       : Style.Theme.textSecondary
                            }

                            MouseArea {
                                id: tabMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: tabStack.currentIndex = index
                            }
                        }
                    }
                }
            }

            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width; height: 1
                color: Style.Theme.border
            }
        }

        // ── Tab content ─────────────────────────────────────────────
        StackLayout {
            id: tabStack
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: 0

            // ── TAB 0: PLEX ─────────────────────────────────────────
            ScrollView {
                contentWidth: availableWidth
                Material.elevation: 0
                background: Rectangle { color: Style.Theme.surface }

                ColumnLayout {
                    width: parent.width
                    spacing: Style.Theme.spacingMedium

                    Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                    // Connection mode selector
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        Layout.preferredHeight: modeCol.implicitHeight + 24
                        radius: Style.Theme.borderRadius
                        color: Style.Theme.surfaceLight

                        ColumnLayout {
                            id: modeCol
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: Style.Theme.spacingSmall

                            Label {
                                text: "Connection Mode"
                                font.bold: true
                                color: Style.Theme.textPrimary
                            }

                            RowLayout {
                                spacing: Style.Theme.spacingLarge

                                RadioButton {
                                    id: directMode
                                    text: "Direct (URL + Token)"
                                    checked: settings.getString("plex_connection_mode") !== "account"
                                    onToggled: if (checked) settings.setString("plex_connection_mode", "direct")
                                    Material.accent: Style.Theme.accent
                                    ToolTip.visible: hovered
                                    ToolTip.delay: 700
                                    ToolTip.text: "Connect directly to a local Plex server using its URL and API token"
                                }

                                RadioButton {
                                    id: accountMode
                                    text: "Plex Account (plex.tv)"
                                    checked: settings.getString("plex_connection_mode") === "account"
                                    onToggled: if (checked) settings.setString("plex_connection_mode", "account")
                                    Material.accent: Style.Theme.accent
                                    ToolTip.visible: hovered
                                    ToolTip.delay: 700
                                    ToolTip.text: "Sign in via plex.tv to discover your servers — works for remote servers without port forwarding"
                                }
                            }
                        }
                    }

                    // ── Direct mode fields ────────────────────────────────
                    SettingsTabs.LabeledField {
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        visible: directMode.checked
                        label: "Plex URL"
                        configKey: "plex_url"
                        placeholder: "http://192.168.1.100:32400"
                        tooltip: "Full URL to your Plex server including port (e.g. http://192.168.1.100:32400)"
                    }

                    SettingsTabs.LabeledField {
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        visible: directMode.checked
                        label: "Plex Token"
                        configKey: "plex_token"
                        password: true
                        tooltip: "Your Plex authentication token — find it via plex.tv/devices or your Plex media server URL parameters"
                    }

                    // ── Account mode fields ───────────────────────────────
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        Layout.preferredHeight: acctCol.implicitHeight + 24
                        radius: Style.Theme.borderRadius
                        color: Style.Theme.surfaceLight
                        visible: accountMode.checked

                        ColumnLayout {
                            id: acctCol
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: Style.Theme.spacingSmall

                            Label {
                                text: "Plex.tv Credentials"
                                font.bold: true
                                color: Style.Theme.textPrimary
                            }

                            RowLayout {
                                spacing: Style.Theme.spacingSmall
                                Label { text: "Username / Email:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 120 }
                                TextField {
                                    id: plexUsername
                                    Layout.fillWidth: true
                                    text: settings.getString("plex_username")
                                    onEditingFinished: settings.setString("plex_username", text)
                                    placeholderText: "your@email.com"
                                    color: Style.Theme.textPrimary
                                    background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: plexUsername.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                                }
                            }

                            RowLayout {
                                spacing: Style.Theme.spacingSmall
                                Label { text: "Password:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 120 }
                                TextField {
                                    id: plexPassword
                                    Layout.fillWidth: true
                                    text: settings.getString("plex_password")
                                    onEditingFinished: settings.setString("plex_password", text)
                                    echoMode: TextInput.Password
                                    placeholderText: "password"
                                    color: Style.Theme.textPrimary
                                    background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: plexPassword.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                                }
                            }

                            RowLayout {
                                spacing: Style.Theme.spacingSmall

                                Button {
                                    text: "Login & Discover Servers"
                                    onClicked: settings.testPlexAccount()
                                    background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent }
                                    contentItem: Label { text: parent.text; color: "#ffffff"; font.bold: true; horizontalAlignment: Text.AlignHCenter }
                                }

                                Label {
                                    id: plexAccountStatus
                                    text: ""
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                    color: Style.Theme.textMuted
                                }

                                Connections {
                                    target: settings
                                    function onPlexAccountResult(success, message) {
                                        plexAccountStatus.text = message
                                        plexAccountStatus.color = success ? Style.Theme.accent : "#e74c3c"
                                        if (success) serverCombo.applyServers(settings.getPlexServers())
                                    }
                                }
                            }
                        }
                    }

                    // Server selection (account mode)
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        Layout.preferredHeight: serverCol.implicitHeight + 24
                        radius: Style.Theme.borderRadius
                        color: Style.Theme.surfaceLight
                        visible: accountMode.checked

                        ColumnLayout {
                            id: serverCol
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: Style.Theme.spacingSmall

                            Label {
                                text: "Server Selection"
                                font.bold: true
                                color: Style.Theme.textPrimary
                            }

                            RowLayout {
                                spacing: Style.Theme.spacingSmall
                                Label { text: "Server:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 120 }
                                ComboBox {
                                    id: serverCombo
                                    Layout.fillWidth: true
                                    property bool ready: false
                                    property string savedServerName: settings.getString("plex_server_name")
                                    function applyServers(servers) {
                                        ready = false
                                        model = servers
                                        var idx = servers.indexOf(savedServerName)
                                        currentIndex = idx >= 0 ? idx : -1
                                        ready = true
                                    }
                                    model: []
                                    Material.background: Style.Theme.surface
                                    Material.foreground: Style.Theme.textPrimary
                                    Component.onCompleted: applyServers(settings.getPlexServers())
                                    onCurrentTextChanged: {
                                        if (!ready || currentIndex < 0 || currentText.length === 0)
                                            return
                                        savedServerName = currentText
                                        settings.setString("plex_server_name", currentText)
                                    }
                                }
                            }

                            Label {
                                text: "After selecting a server, save settings and the app will connect automatically"
                                font.pixelSize: Style.Theme.fontSizeSmall
                                color: Style.Theme.textMuted
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }
                        }
                    }

                    // Test + status (works for both modes)
                    RowLayout {
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: Style.Theme.spacingMedium
                        visible: directMode.checked

                        Button {
                            text: "Test Connection"
                            enabled: settings.plexTestStatus !== "testing"
                            onClicked: settings.testPlex()
                            ToolTip.visible: hovered
                            ToolTip.delay: 700
                            ToolTip.text: "Verify that ScanHound can reach your Plex server with the current URL and token"

                            background: Rectangle {
                                radius: 4
                                color: parent.enabled
                                       ? parent.hovered ? Style.Theme.accentLight : Style.Theme.accent
                                       : Style.Theme.surfaceLight
                            }
                            contentItem: Label {
                                text: parent.text
                                color: parent.enabled ? "#ffffff" : Style.Theme.textMuted
                                font.pixelSize: Style.Theme.fontSizeSmall
                                font.bold: true
                                horizontalAlignment: Text.AlignHCenter
                            }
                        }

                        Rectangle {
                            width: 10; height: 10; radius: 5
                            color: settings.plexTestStatus === "success" ? Style.Theme.success
                                   : settings.plexTestStatus === "error" ? Style.Theme.error
                                   : settings.plexTestStatus === "testing" ? Style.Theme.info
                                   : Style.Theme.textMuted
                        }

                        Label {
                            text: settings.plexTestMessage
                            font.pixelSize: Style.Theme.fontSizeSmall
                            color: Style.Theme.textSecondary
                        }
                    }

                    // Library assignments (dynamic)
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: 6
                        visible: settings.libraryCount > 0

                        Label {
                            text: "Library Assignments"
                            color: Style.Theme.textPrimary
                            font.pixelSize: Style.Theme.fontSizeNormal
                            font.bold: true
                        }

                        // Table card
                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: libTableCol.implicitHeight
                            radius: Style.Theme.borderRadius
                            color: Style.Theme.surfaceLight
                            border.color: Style.Theme.border
                            border.width: 1

                            ColumnLayout {
                                id: libTableCol
                                anchors.fill: parent
                                spacing: 0

                                // Header row
                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 28
                                    color: "transparent"
                                    radius: Style.Theme.borderRadius

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.leftMargin: 12
                                        anchors.rightMargin: 12
                                        spacing: 8

                                        Label {
                                            text: "Library"
                                            Layout.fillWidth: true
                                            font.pixelSize: 10
                                            font.bold: true
                                            color: Style.Theme.textMuted
                                            font.capitalization: Font.AllUppercase
                                        }

                                        Label {
                                            text: "Type"
                                            Layout.preferredWidth: 70
                                            font.pixelSize: 10
                                            font.bold: true
                                            color: Style.Theme.textMuted
                                            font.capitalization: Font.AllUppercase
                                        }

                                        Label {
                                            text: "Assign"
                                            Layout.preferredWidth: 90
                                            font.pixelSize: 10
                                            font.bold: true
                                            color: Style.Theme.textMuted
                                            font.capitalization: Font.AllUppercase
                                        }
                                    }
                                }

                                // Separator
                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 1
                                    color: Style.Theme.border
                                    opacity: 0.5
                                }

                                // Library rows
                                Repeater {
                                    model: settings.libraryCount

                                    delegate: Rectangle {
                                        required property int index
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 34
                                        color: index % 2 === 1
                                               ? Qt.rgba(Style.Theme.surface.r, Style.Theme.surface.g, Style.Theme.surface.b, 0.4)
                                               : "transparent"
                                        radius: index === settings.libraryCount - 1 ? Style.Theme.borderRadius : 0

                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 12
                                            anchors.rightMargin: 12
                                            spacing: 8

                                            // Library name
                                            Label {
                                                text: settings.libraryName(index)
                                                color: Style.Theme.textPrimary
                                                font.pixelSize: Style.Theme.fontSizeSmall
                                                Layout.fillWidth: true
                                                elide: Text.ElideRight
                                            }

                                            // Item count
                                            Label {
                                                property int count: settings.libraryItemCount(index)
                                                visible: count > 0
                                                text: count.toLocaleString()
                                                font.pixelSize: Style.Theme.fontSizeSmall
                                                color: Style.Theme.textMuted
                                            }

                                            // Type badge
                                            Rectangle {
                                                Layout.preferredWidth: 70
                                                height: 20
                                                radius: 10
                                                color: {
                                                    var t = settings.libraryType(index)
                                                    return t === "movie" ? Qt.rgba(0.13, 0.59, 0.95, 0.15)
                                                         : t === "show" ? Qt.rgba(0.30, 0.69, 0.31, 0.15)
                                                         : Qt.rgba(1, 1, 1, 0.06)
                                                }

                                                Label {
                                                    anchors.centerIn: parent
                                                    text: settings.libraryType(index)
                                                    font.pixelSize: 10
                                                    font.bold: true
                                                    color: {
                                                        var t = settings.libraryType(index)
                                                        return t === "movie" ? "#42A5F5"
                                                             : t === "show" ? "#66BB6A"
                                                             : Style.Theme.textMuted
                                                    }
                                                }
                                            }

                                            // Assignment ComboBox (compact)
                                            ComboBox {
                                                Layout.preferredWidth: 90
                                                Layout.preferredHeight: 26
                                                model: ["none", "movies", "tv"]
                                                currentIndex: {
                                                    var a = settings.libraryAssignment(index)
                                                    return a === "movies" ? 1 : a === "tv" ? 2 : 0
                                                }
                                                // Guard against the init-time signal fire: only
                                                // write back once the component has finished loading.
                                                property bool _ready: false
                                                Component.onCompleted: _ready = true
                                                onCurrentTextChanged: if (_ready) settings.setLibraryAssignment(index, currentText)
                                                font.pixelSize: Style.Theme.fontSizeSmall

                                                background: Rectangle {
                                                    radius: 4
                                                    color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surface
                                                    border.color: parent.activeFocus ? Style.Theme.accent : Style.Theme.border
                                                    border.width: 1
                                                }

                                                contentItem: Label {
                                                    leftPadding: 8
                                                    rightPadding: 20
                                                    text: parent.displayText
                                                    font.pixelSize: Style.Theme.fontSizeSmall
                                                    color: parent.currentText === "none" ? Style.Theme.textMuted : Style.Theme.accent
                                                    font.bold: parent.currentText !== "none"
                                                    verticalAlignment: Text.AlignVCenter
                                                    elide: Text.ElideRight
                                                }

                                                indicator: Label {
                                                    x: parent.width - width - 6
                                                    y: (parent.height - height) / 2
                                                    text: "\u25BE"
                                                    font.pixelSize: 10
                                                    color: Style.Theme.textMuted
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Label {
                        text: "Caching"
                        color: Style.Theme.textPrimary
                        font.pixelSize: Style.Theme.fontSizeNormal
                        font.bold: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.topMargin: Style.Theme.spacingMedium
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: Style.Theme.spacingSmall

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Style.Theme.spacingMedium

                            Label {
                                text: "Plex cache duration"
                                color: Style.Theme.textSecondary
                                font.pixelSize: Style.Theme.fontSizeSmall
                                Layout.preferredWidth: 140
                            }

                            Label {
                                text: cacheDurationSlider.value === 0
                                      ? "Never expire"
                                      : Math.round(cacheDurationSlider.value) + " hours"
                                color: Style.Theme.accent
                                font.pixelSize: Style.Theme.fontSizeSmall
                                font.bold: true
                                Layout.preferredWidth: 110
                            }

                            Slider {
                                id: cacheDurationSlider
                                Layout.fillWidth: true
                                from: 0
                                to: 168
                                stepSize: 1
                                value: settings.getInt("cache_duration")
                                onValueChanged: if (_slidersReady) settings.setInt("cache_duration", Math.round(value))
                                ToolTip.visible: hovered
                                ToolTip.delay: 700
                                ToolTip.text: "How long the saved Plex library cache can be reused before it is considered stale"
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Style.Theme.spacingMedium

                            Label {
                                text: "Default refresh mode"
                                color: Style.Theme.textSecondary
                                font.pixelSize: Style.Theme.fontSizeSmall
                                Layout.preferredWidth: 140
                            }

                            ComboBox {
                                id: refreshModeCombo
                                Layout.preferredWidth: 190
                                model: [
                                    { label: "Auto", value: "auto" },
                                    { label: "Force Refresh", value: "force_refresh" },
                                    { label: "Cache Only", value: "cache_only" }
                                ]
                                textRole: "label"
                                valueRole: "value"
                                currentIndex: {
                                    var mode = settings.getString("plex_refresh_mode")
                                    if (mode === "force_refresh") return 1
                                    if (mode === "cache_only") return 2
                                    return 0
                                }
                                onActivated: settings.setString("plex_refresh_mode", currentValue)
                                ToolTip.visible: hovered
                                ToolTip.delay: 700
                                ToolTip.text: "Choose how scans should treat Plex cache by default"
                            }
                        }

                        CheckBox {
                            text: "Invalidate cache when Plex has newly added items"
                            checked: settings.getBool("plex_invalidate_on_new_content")
                            onToggled: settings.setBool("plex_invalidate_on_new_content", checked)
                            ToolTip.visible: hovered
                            ToolTip.delay: 700
                            ToolTip.text: "When enabled, ScanHound checks Plex for content added after the last cache update and refreshes if needed"
                        }
                    }

                    Label {
                        text: "Maintenance"
                        color: Style.Theme.textPrimary
                        font.pixelSize: Style.Theme.fontSizeNormal
                        font.bold: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.topMargin: Style.Theme.spacingMedium
                    }

                    RowLayout {
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: Style.Theme.spacingSmall

                        Button {
                            text: "Clear Plex Cache"
                            onClicked: settings.purgeCache()
                            background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surfaceLight }
                            contentItem: Label { text: parent.text; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                            ToolTip.visible: hovered
                            ToolTip.delay: 700
                            ToolTip.text: "Delete the saved Plex library cache from the local database so the next scan rebuilds it"
                        }

                        Button {
                            text: "Clear Metadata Cache"
                            onClicked: settings.purgeMetadataCache()
                            background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surfaceLight }
                            contentItem: Label { text: parent.text; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                            ToolTip.visible: hovered
                            ToolTip.delay: 700
                            ToolTip.text: "Clear cached TMDB and OMDb metadata kept in memory for the current session"
                        }

                        Button {
                            text: "Purge History"
                            onClicked: settings.purgeHistory()
                            background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surfaceLight }
                            contentItem: Label { text: parent.text; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                            ToolTip.visible: hovered
                            ToolTip.delay: 700
                            ToolTip.text: "Delete all scan result history from the local database"
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }

            // ── TAB 1: API KEYS ─────────────────────────────────────
            ScrollView {
                contentWidth: availableWidth
                Material.elevation: 0
                background: Rectangle { color: Style.Theme.surface }

                ColumnLayout {
                    width: parent.width
                    spacing: Style.Theme.spacingMedium

                    Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                    // TMDB
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: 4

                        SettingsTabs.LabeledField {
                            label: "TMDB API Key"
                            configKey: "tmdb_api_key"
                            password: true
                            tooltip: "Your TMDB API key for fetching movie and TV metadata. Get a free key at themoviedb.org/settings/api"
                        }

                        RowLayout {
                            spacing: Style.Theme.spacingMedium

                            Button {
                                text: "Test TMDB"
                                enabled: settings.tmdbTestStatus !== "testing"
                                onClicked: settings.testTmdb()
                                background: Rectangle { radius: 4; color: parent.enabled ? parent.hovered ? Style.Theme.accentLight : Style.Theme.accent : Style.Theme.surfaceLight }
                                contentItem: Label { text: parent.text; color: parent.enabled ? "#ffffff" : Style.Theme.textMuted; font.pixelSize: Style.Theme.fontSizeSmall; font.bold: true; horizontalAlignment: Text.AlignHCenter }
                            }

                            Rectangle { width: 10; height: 10; radius: 5; color: settings.tmdbTestStatus === "success" ? Style.Theme.success : settings.tmdbTestStatus === "error" ? Style.Theme.error : settings.tmdbTestStatus === "testing" ? Style.Theme.info : Style.Theme.textMuted }
                            Label { text: settings.tmdbTestMessage; font.pixelSize: Style.Theme.fontSizeSmall; color: Style.Theme.textSecondary }
                        }
                    }

                    // OMDb
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: 4

                        SettingsTabs.LabeledField {
                            label: "OMDb API Key"
                            configKey: "omdb_api_key"
                            password: true
                            tooltip: "Your OMDb API key for fetching IMDb ratings and data. Get a free key at omdbapi.com"
                        }

                        RowLayout {
                            spacing: Style.Theme.spacingMedium

                            Button {
                                text: "Test OMDb"
                                enabled: settings.omdbTestStatus !== "testing"
                                onClicked: settings.testOmdb()
                                background: Rectangle { radius: 4; color: parent.enabled ? parent.hovered ? Style.Theme.accentLight : Style.Theme.accent : Style.Theme.surfaceLight }
                                contentItem: Label { text: parent.text; color: parent.enabled ? "#ffffff" : Style.Theme.textMuted; font.pixelSize: Style.Theme.fontSizeSmall; font.bold: true; horizontalAlignment: Text.AlignHCenter }
                            }

                            Rectangle { width: 10; height: 10; radius: 5; color: settings.omdbTestStatus === "success" ? Style.Theme.success : settings.omdbTestStatus === "error" ? Style.Theme.error : settings.omdbTestStatus === "testing" ? Style.Theme.info : Style.Theme.textMuted }
                            Label { text: settings.omdbTestMessage; font.pixelSize: Style.Theme.fontSizeSmall; color: Style.Theme.textSecondary }
                        }
                    }

                    // Use TMDB toggle
                    CheckBox {
                        Layout.leftMargin: Style.Theme.spacingLarge
                        text: "Use TMDB as primary source"
                        checked: settings.getBool("use_tmdb")
                        onToggled: settings.setBool("use_tmdb", checked)
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "When enabled, TMDB is preferred over OMDb for title matching and metadata enrichment"
                    }

                    Item { Layout.fillHeight: true }
                }
            }

            // ── TAB 2: MATCHING ─────────────────────────────────────
            ScrollView {
                contentWidth: availableWidth
                Material.elevation: 0
                background: Rectangle { color: Style.Theme.surface }

                ColumnLayout {
                    width: parent.width
                    spacing: Style.Theme.spacingSmall

                    Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                    Label {
                        text: "Upgrade Rules"
                        color: Style.Theme.textPrimary
                        font.pixelSize: Style.Theme.fontSizeNormal
                        font.bold: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                    }

                    Repeater {
                        model: [
                            { key: "rule_dv", label: "Flag Dolby Vision upgrades", tip: "Detect when a Dolby Vision version is available to replace a non-DV copy" },
                            { key: "rule_1080_4k", label: "Flag 1080p → 4K upgrades", tip: "Flag library items that have a 4K version available when you only have 1080p" },
                            { key: "rule_1080_4k_size", label: "Only if 4K is larger", tip: "Only flag 1080p→4K upgrades when the 4K file is larger (avoids flagging low-bitrate upscales)" },
                            { key: "rule_1080_1080", label: "Flag 1080p size upgrades", tip: "Flag when a larger/better 1080p version is available for an existing 1080p item" },
                            { key: "rule_4k_4k", label: "Flag 4K size upgrades", tip: "Flag when a larger/better 4K version is available for an existing 4K item" },
                            { key: "strict_resolution", label: "Strict resolution matching", tip: "Only match items at the same resolution — never suggest cross-resolution upgrades" },
                        ]

                        delegate: CheckBox {
                            required property var modelData
                            Layout.leftMargin: Style.Theme.spacingLarge + 8
                            text: modelData.label
                            checked: settings.getBool(modelData.key)
                            onToggled: settings.setBool(modelData.key, checked)
                            ToolTip.visible: hovered
                            ToolTip.delay: 700
                            ToolTip.text: modelData.tip
                            Connections {
                                target: settings
                                function onConfigChanged() { checked = settings.getBool(modelData.key) }
                            }
                        }
                    }

                    Label {
                        text: "Thresholds"
                        color: Style.Theme.textPrimary
                        font.pixelSize: Style.Theme.fontSizeNormal
                        font.bold: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.topMargin: Style.Theme.spacingMedium
                    }

                    // Sliders
                    Repeater {
                        model: [
                            { key: "upgrade_sensitivity", label: "Upgrade Sensitivity", min: 1, max: 50, suffix: "%", tip: "How aggressively size differences are flagged as upgrades. Higher = more upgrade flags from smaller size differences" },
                            { key: "tv_match_threshold", label: "TV Match Score", min: 70, max: 100, suffix: "", tip: "Minimum fuzzy-match score (0–100) required to accept a TV title match. Higher = stricter, fewer false positives" },
                            { key: "movie_match_threshold", label: "Movie Match Score", min: 70, max: 100, suffix: "", tip: "Minimum fuzzy-match score (0–100) required to accept a movie title match. Higher = stricter, fewer false positives" },
                            { key: "year_tolerance", label: "Year Tolerance", min: 0, max: 5, suffix: " yrs", tip: "Allow this many years of difference when matching release years. Useful for remasters or regional release date differences" },
                        ]

                        delegate: ColumnLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.leftMargin: Style.Theme.spacingLarge + 8
                            Layout.rightMargin: Style.Theme.spacingLarge
                            spacing: 2

                            Connections {
                                target: settings
                                function onConfigChanged() { threshSlider.value = settings.getInt(modelData.key) }
                            }

                            RowLayout {
                                Label {
                                    text: modelData.label
                                    color: Style.Theme.textSecondary
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                    Layout.fillWidth: true
                                }
                                Label {
                                    text: Math.round(threshSlider.value) + modelData.suffix
                                    color: Style.Theme.accent
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                    font.bold: true
                                }
                            }

                            Slider {
                                id: threshSlider
                                Layout.fillWidth: true
                                from: modelData.min
                                to: modelData.max
                                stepSize: 1
                                value: settings.getInt(modelData.key)
                                onValueChanged: if (_slidersReady) settings.setInt(modelData.key, Math.round(value))
                                ToolTip.visible: hovered
                                ToolTip.delay: 700
                                ToolTip.text: modelData.tip
                            }
                        }
                    }

                    // Presets
                    Label {
                        text: "Presets"
                        color: Style.Theme.textPrimary
                        font.pixelSize: Style.Theme.fontSizeNormal
                        font.bold: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.topMargin: Style.Theme.spacingMedium
                    }

                    Flow {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge + 8
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: Style.Theme.spacingSmall

                        Repeater {
                            model: ["Aggressive Upgrades", "Conservative", "4K Only", "Quality Seeker", "Balanced"]

                            delegate: Button {
                                required property string modelData
                                text: modelData
                                onClicked: settings.applyPreset(modelData)
                                background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surfaceLight }
                                contentItem: Label { text: parent.text; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }

            // ── TAB 3: SOURCES ──────────────────────────────────────
            ScrollView {
                contentWidth: availableWidth
                Material.elevation: 0
                background: Rectangle { color: Style.Theme.surface }

                ColumnLayout {
                    width: parent.width
                    spacing: Style.Theme.spacingMedium

                    Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                    CheckBox {
                        Layout.leftMargin: Style.Theme.spacingLarge
                        text: "Exclude 720p results"
                        checked: settings.getBool("exclude_720p")
                        onToggled: settings.setBool("exclude_720p", checked)
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Filter out 720p results from all source searches"
                    }

                    Label {
                        text: "Adit-HD Forum"
                        color: Style.Theme.textPrimary
                        font.pixelSize: Style.Theme.fontSizeNormal
                        font.bold: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge + 8
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: 4

                        Label { text: "Username"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                        TextField {
                            Layout.fillWidth: true
                            text: settings.getString("adithd_username")
                            color: Style.Theme.textPrimary
                            onEditingFinished: settings.setString("adithd_username", text)
                            background: Rectangle { radius: 4; color: Style.Theme.surfaceLight; border.color: activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        }

                        Label { text: "Password"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                        TextField {
                            Layout.fillWidth: true
                            text: settings.getString("adithd_password")
                            echoMode: TextInput.Password
                            color: Style.Theme.textPrimary
                            onEditingFinished: settings.setString("adithd_password", text)
                            background: Rectangle { radius: 4; color: Style.Theme.surfaceLight; border.color: activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        }

                        Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                        RowLayout {
                            spacing: Style.Theme.spacingSmall

                            Button {
                                text: settings.adithdTestStatus === "testing" ? "Testing..." : "Test Login"
                                enabled: settings.adithdTestStatus !== "testing"
                                implicitWidth: 120

                                background: Rectangle {
                                    radius: Style.Theme.borderRadius
                                    color: parent.enabled
                                           ? parent.hovered ? Style.Theme.accentLight : Style.Theme.accent
                                           : Style.Theme.surfaceLight
                                }
                                contentItem: Label {
                                    text: parent.text
                                    color: parent.enabled ? "#ffffff" : Style.Theme.textMuted
                                    font.bold: true
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }

                                onClicked: settings.testAditHd()
                            }

                            Label {
                                visible: settings.adithdTestStatus !== "untested"
                                text: settings.adithdTestStatus === "testing" ? "..." : settings.adithdTestMessage
                                font.pixelSize: Style.Theme.fontSizeSmall
                                color: settings.adithdTestStatus === "success" ? Style.Theme.success
                                       : settings.adithdTestStatus === "error" ? Style.Theme.error
                                       : Style.Theme.textMuted
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }

                    // ── Cuty.io / Shortlink Credentials ──────────────────
                    Label {
                        text: "Cuty.io / Shortlinks"
                        color: Style.Theme.textPrimary
                        font.pixelSize: Style.Theme.fontSizeNormal
                        font.bold: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.topMargin: Style.Theme.spacingMedium
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge + 8
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: 4

                        Label { text: "Email"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                        TextField {
                            Layout.fillWidth: true
                            text: settings.getString("cuty_email")
                            color: Style.Theme.textPrimary
                            onEditingFinished: settings.setString("cuty_email", text)
                            background: Rectangle { radius: 4; color: Style.Theme.surfaceLight; border.color: activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        }

                        Label { text: "Password"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                        TextField {
                            Layout.fillWidth: true
                            text: settings.getString("cuty_password")
                            echoMode: TextInput.Password
                            color: Style.Theme.textPrimary
                            onEditingFinished: settings.setString("cuty_password", text)
                            background: Rectangle { radius: 4; color: Style.Theme.surfaceLight; border.color: activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        }

                        Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                        RowLayout {
                            spacing: Style.Theme.spacingSmall

                            Button {
                                text: settings.cutyTestStatus === "testing" ? "Testing..." : "Test Login"
                                enabled: settings.cutyTestStatus !== "testing"
                                implicitWidth: 120

                                background: Rectangle {
                                    radius: Style.Theme.borderRadius
                                    color: parent.enabled
                                           ? parent.hovered ? Style.Theme.accentLight : Style.Theme.accent
                                           : Style.Theme.surfaceLight
                                }
                                contentItem: Label {
                                    text: parent.text
                                    color: parent.enabled ? "#ffffff" : Style.Theme.textMuted
                                    font.bold: true
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }

                                onClicked: settings.testCuty()
                            }

                            Label {
                                visible: settings.cutyTestStatus !== "untested"
                                text: settings.cutyTestStatus === "testing" ? "..." : settings.cutyTestMessage
                                font.pixelSize: Style.Theme.fontSizeSmall
                                color: settings.cutyTestStatus === "success" ? Style.Theme.success
                                       : settings.cutyTestStatus === "error" ? Style.Theme.error
                                       : Style.Theme.textMuted
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }

            // ── TAB 4: JDOWNLOADER ──────────────────────────────────
            ScrollView {
                contentWidth: availableWidth
                Material.elevation: 0
                background: Rectangle { color: Style.Theme.surface }

                ColumnLayout {
                    width: parent.width
                    spacing: Style.Theme.spacingMedium

                    Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                    CheckBox {
                        Layout.leftMargin: Style.Theme.spacingLarge
                        text: "Enable JDownloader Integration"
                        checked: settings.getBool("jd_enabled")
                        onToggled: settings.setBool("jd_enabled", checked)
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Send download links directly to JDownloader via the MyJDownloader cloud API"
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge + 8
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: 4

                        Label { text: "MyJDownloader Email"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                        TextField {
                            Layout.fillWidth: true
                            text: settings.getString("jd_email")
                            color: Style.Theme.textPrimary
                            onEditingFinished: settings.setString("jd_email", text)
                            background: Rectangle { radius: 4; color: Style.Theme.surfaceLight; border.color: activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        }

                        Label { text: "MyJDownloader Password"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                        TextField {
                            Layout.fillWidth: true
                            text: settings.getString("jd_password")
                            echoMode: TextInput.Password
                            color: Style.Theme.textPrimary
                            onEditingFinished: settings.setString("jd_password", text)
                            background: Rectangle { radius: 4; color: Style.Theme.surfaceLight; border.color: activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        }

                        Label { text: "Device Name"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                        TextField {
                            Layout.fillWidth: true
                            text: settings.getString("jd_device")
                            color: Style.Theme.textPrimary
                            onEditingFinished: settings.setString("jd_device", text)
                            background: Rectangle { radius: 4; color: Style.Theme.surfaceLight; border.color: activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        }

                        Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                        RowLayout {
                            spacing: Style.Theme.spacingSmall

                            Button {
                                text: settings.jdTestStatus === "testing" ? "Testing..." : "Test Connection"
                                enabled: settings.jdTestStatus !== "testing"
                                implicitWidth: 140

                                background: Rectangle {
                                    radius: Style.Theme.borderRadius
                                    color: parent.enabled
                                           ? parent.hovered ? Style.Theme.accentLight : Style.Theme.accent
                                           : Style.Theme.surfaceLight
                                }
                                contentItem: Label {
                                    text: parent.text
                                    color: parent.enabled ? "#ffffff" : Style.Theme.textMuted
                                    font.bold: true
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }

                                onClicked: settings.testJd()
                            }

                            Label {
                                visible: settings.jdTestStatus !== "untested"
                                text: settings.jdTestStatus === "testing" ? "..." : settings.jdTestMessage
                                font.pixelSize: Style.Theme.fontSizeSmall
                                color: settings.jdTestStatus === "success" ? Style.Theme.success
                                       : settings.jdTestStatus === "error" ? Style.Theme.error
                                       : Style.Theme.textMuted
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }

            // ── TAB 5: SCHEDULER ────────────────────────────────────
            ScrollView {
                contentWidth: availableWidth
                Material.elevation: 0
                background: Rectangle { color: Style.Theme.surface }

                ColumnLayout {
                    width: parent.width
                    spacing: Style.Theme.spacingMedium

                    Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                    CheckBox {
                        Layout.leftMargin: Style.Theme.spacingLarge
                        text: "Enable Scheduled Scanning"
                        checked: settings.getBool("scheduler_enabled")
                        onToggled: settings.setBool("scheduler_enabled", checked)
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Automatically run a full scan at the configured interval without manual intervention"
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge + 8
                        Layout.rightMargin: Style.Theme.spacingLarge
                        spacing: 2

                        RowLayout {
                            Label {
                                text: "Scan every"
                                color: Style.Theme.textSecondary
                                font.pixelSize: Style.Theme.fontSizeSmall
                            }
                            Label {
                                text: Math.round(schedSlider.value) + " hours"
                                color: Style.Theme.accent
                                font.pixelSize: Style.Theme.fontSizeSmall
                                font.bold: true
                            }
                        }

                        Slider {
                            id: schedSlider
                            Layout.fillWidth: true
                            from: 1; to: 24; stepSize: 1
                            value: settings.getInt("scheduler_interval")
                            onValueChanged: if (_slidersReady) settings.setInt("scheduler_interval", Math.round(value))
                            ToolTip.visible: hovered
                            ToolTip.delay: 700
                            ToolTip.text: "How often the automatic scan runs (in hours)"
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }

            // ── TAB 6: LOG ────────────────────────────────────────
            SettingsTabs.LogTab {}

            // ── TAB 7: NOTIFICATIONS ───────────────────────────────
            SettingsTabs.NotificationsTab {}

            // ── TAB 8: APPEARANCE ─────────────────────────────────
            SettingsTabs.AppearanceTab {}

            // ── TAB 9: AUTO-GRAB ────────────────────────────────────
            ScrollView {
                contentWidth: availableWidth
                Material.elevation: 0
                background: Rectangle { color: Style.Theme.surface }

                ColumnLayout {
                    width: parent.width
                    spacing: Style.Theme.spacingMedium

                    Item { Layout.preferredHeight: Style.Theme.spacingSmall }

                    // Description
                    Label {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        text: "Automatically grab links for items that match your criteria after each scan completes."
                        color: Style.Theme.textSecondary
                        font.pixelSize: Style.Theme.fontSizeSmall
                        wrapMode: Text.WordWrap
                    }

                    // Enable toggle
                    CheckBox {
                        Layout.leftMargin: Style.Theme.spacingLarge
                        text: "Enable Auto-Grab"
                        checked: settings.getBool("auto_grab_enabled")
                        onToggled: settings.setBool("auto_grab_enabled", checked)
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "After each scan, automatically send download links to JDownloader for items matching your quality and filter criteria"
                    }

                    // ── Rating & Votes ──
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        Layout.preferredHeight: ratingCol.implicitHeight + 24
                        radius: Style.Theme.borderRadius
                        color: Style.Theme.surfaceLight

                        ColumnLayout {
                            id: ratingCol
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 8

                            Label {
                                text: "Quality Criteria"
                                color: Style.Theme.textPrimary
                                font.pixelSize: Style.Theme.fontSizeNormal
                                font.bold: true
                            }

                            // Min rating slider
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2

                                RowLayout {
                                    Label {
                                        text: "Minimum Rating"
                                        color: Style.Theme.textSecondary
                                        font.pixelSize: Style.Theme.fontSizeSmall
                                    }
                                    Label {
                                        text: agRatingSlider.value.toFixed(1) + " / 10"
                                        color: Style.Theme.accent
                                        font.pixelSize: Style.Theme.fontSizeSmall
                                        font.bold: true
                                    }
                                    Label {
                                        text: agRatingSlider.value === 0 ? "(disabled)" : ""
                                        color: Style.Theme.textSecondary
                                        font.pixelSize: Style.Theme.fontSizeSmall
                                        font.italic: true
                                    }
                                }

                                Slider {
                                    id: agRatingSlider
                                    Layout.fillWidth: true
                                    from: 0; to: 10; stepSize: 0.5
                                    value: settings.getFloat("auto_grab_min_rating")
                                    onValueChanged: if (_slidersReady) settings.setFloat("auto_grab_min_rating", value)
                                    ToolTip.visible: hovered
                                    ToolTip.delay: 700
                                    ToolTip.text: "Only auto-grab items with an IMDb rating at or above this value. Set to 0 to disable the rating filter"
                                }
                            }

                            // Min votes
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2

                                RowLayout {
                                    Label {
                                        text: "Minimum IMDb Votes"
                                        color: Style.Theme.textSecondary
                                        font.pixelSize: Style.Theme.fontSizeSmall
                                    }
                                    Label {
                                        text: agVotesSlider.value === 0 ? "(disabled)" : Math.round(agVotesSlider.value).toLocaleString()
                                        color: agVotesSlider.value === 0 ? Style.Theme.textSecondary : Style.Theme.accent
                                        font.pixelSize: Style.Theme.fontSizeSmall
                                        font.bold: agVotesSlider.value > 0
                                        font.italic: agVotesSlider.value === 0
                                    }
                                }

                                Slider {
                                    id: agVotesSlider
                                    Layout.fillWidth: true
                                    from: 0; to: 100000; stepSize: 1000
                                    value: settings.getInt("auto_grab_min_votes")
                                    onValueChanged: if (_slidersReady) settings.setInt("auto_grab_min_votes", Math.round(value))
                                    ToolTip.visible: hovered
                                    ToolTip.delay: 700
                                    ToolTip.text: "Only auto-grab items with at least this many IMDb votes — helps filter out obscure titles with artificially high ratings. Set to 0 to disable"
                                }
                            }
                        }
                    }

                    // ── Genre & Language ──
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        Layout.preferredHeight: filterCol.implicitHeight + 24
                        radius: Style.Theme.borderRadius
                        color: Style.Theme.surfaceLight

                        ColumnLayout {
                            id: filterCol
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 8

                            Label {
                                text: "Content Filters"
                                color: Style.Theme.textPrimary
                                font.pixelSize: Style.Theme.fontSizeNormal
                                font.bold: true
                            }

                            // Include genres
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 4

                                Label {
                                    text: "Include Genres (comma-separated, empty = all)"
                                    color: Style.Theme.textSecondary
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                }

                                TextField {
                                    id: agGenresField
                                    Layout.fillWidth: true
                                    text: settings.getString("auto_grab_genres")
                                    placeholderText: text.length > 0 ? "" : "e.g. Action, Sci-Fi, Drama"
                                    color: Style.Theme.textPrimary
                                    font.pixelSize: Style.Theme.fontSizeNormal
                                    onEditingFinished: settings.setString("auto_grab_genres", text)
                                    background: Rectangle {
                                        radius: 4
                                        color: Style.Theme.surfaceLight
                                        border.color: agGenresField.activeFocus ? Style.Theme.accent : Style.Theme.border
                                        border.width: 1
                                    }
                                }
                            }

                            // Exclude genres
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 4

                                Label {
                                    text: "Exclude Genres (comma-separated)"
                                    color: Style.Theme.textSecondary
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                }

                                TextField {
                                    id: agExclGenresField
                                    Layout.fillWidth: true
                                    text: settings.getString("auto_grab_exclude_genres")
                                    placeholderText: text.length > 0 ? "" : "e.g. Horror, Documentary"
                                    color: Style.Theme.textPrimary
                                    font.pixelSize: Style.Theme.fontSizeNormal
                                    onEditingFinished: settings.setString("auto_grab_exclude_genres", text)
                                    background: Rectangle {
                                        radius: 4
                                        color: Style.Theme.surfaceLight
                                        border.color: agExclGenresField.activeFocus ? Style.Theme.accent : Style.Theme.border
                                        border.width: 1
                                    }
                                }
                            }

                            // Languages
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 4

                                Label {
                                    text: "Languages (comma-separated, empty = all)"
                                    color: Style.Theme.textSecondary
                                    font.pixelSize: Style.Theme.fontSizeSmall
                                }

                                TextField {
                                    id: agLanguagesField
                                    Layout.fillWidth: true
                                    text: settings.getString("auto_grab_languages")
                                    placeholderText: text.length > 0 ? "" : "e.g. English, French"
                                    color: Style.Theme.textPrimary
                                    font.pixelSize: Style.Theme.fontSizeNormal
                                    onEditingFinished: settings.setString("auto_grab_languages", text)
                                    background: Rectangle {
                                        radius: 4
                                        color: Style.Theme.surfaceLight
                                        border.color: agLanguagesField.activeFocus ? Style.Theme.accent : Style.Theme.border
                                        border.width: 1
                                    }
                                }
                            }
                        }
                    }

                    // ── Status filters ──
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: Style.Theme.spacingLarge
                        Layout.rightMargin: Style.Theme.spacingLarge
                        Layout.preferredHeight: statusCol.implicitHeight + 24
                        radius: Style.Theme.borderRadius
                        color: Style.Theme.surfaceLight

                        ColumnLayout {
                            id: statusCol
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 8

                            Label {
                                text: "Grab items with these statuses"
                                color: Style.Theme.textPrimary
                                font.pixelSize: Style.Theme.fontSizeNormal
                                font.bold: true
                            }

                            // Parse comma-separated statuses into checkbox states
                            property string rawStatuses: settings.getString("auto_grab_statuses")

                            function updateStatuses() {
                                var parts = []
                                if (agMissingCb.checked) parts.push("missing")
                                if (agUpgradeCb.checked) parts.push("upgrade")
                                if (agDvUpgradeCb.checked) parts.push("dv_upgrade")
                                settings.setString("auto_grab_statuses", parts.join(","))
                            }

                            CheckBox {
                                id: agMissingCb
                                text: "Missing (not in Plex)"
                                checked: statusCol.rawStatuses.indexOf("missing") >= 0
                                onToggled: statusCol.updateStatuses()
                                ToolTip.visible: hovered
                                ToolTip.delay: 700
                                ToolTip.text: "Auto-grab items that are completely absent from your Plex library"
                            }

                            CheckBox {
                                id: agUpgradeCb
                                text: "Upgrades (resolution/size)"
                                checked: statusCol.rawStatuses.split(",").indexOf("upgrade") >= 0
                                onToggled: statusCol.updateStatuses()
                                ToolTip.visible: hovered
                                ToolTip.delay: 700
                                ToolTip.text: "Auto-grab items where a better resolution or larger file is available for an item already in your Plex library"
                            }

                            CheckBox {
                                id: agDvUpgradeCb
                                text: "Dolby Vision Upgrades"
                                checked: statusCol.rawStatuses.indexOf("dv_upgrade") >= 0
                                onToggled: statusCol.updateStatuses()
                                ToolTip.visible: hovered
                                ToolTip.delay: 700
                                ToolTip.text: "Auto-grab Dolby Vision versions to replace non-DV copies already in your Plex library"
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }
        }

        // ── Footer (Save / Cancel) ──────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 52
            color: Style.Theme.surface

            Rectangle {
                anchors.top: parent.top
                width: parent.width; height: 1
                color: Style.Theme.border
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: Style.Theme.spacingLarge
                anchors.rightMargin: Style.Theme.spacingLarge
                spacing: Style.Theme.spacingMedium

                Item { Layout.fillWidth: true }

                Button {
                    text: "Cancel"
                    onClicked: {
                        settings.cancel()
                        scanner.reloadPreferences()
                        // Revert live theme preview to saved value
                        var saved = settings.getString("theme_mode")
                        Style.Theme.applyThemeMode(saved || "dark")
                        settingsDialog.close()
                    }
                    background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.surfaceHover : Style.Theme.surfaceLight }
                    contentItem: Label { text: parent.text; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeNormal; horizontalAlignment: Text.AlignHCenter }
                }

                Button {
                    text: "Save"
                    onClicked: {
                        settings.saveLibraryAssignments()
                        settings.save()
                        scanner.reloadPreferences()
                        settingsDialog.close()
                    }
                    background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent }
                    contentItem: Label { text: parent.text; color: "#ffffff"; font.pixelSize: Style.Theme.fontSizeNormal; font.bold: true; horizontalAlignment: Text.AlignHCenter }
                }
            }
        }
    }
}
