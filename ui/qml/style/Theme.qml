pragma Singleton
import QtQuick

QtObject {
    // ── Dark / Light mode toggle ────────────────────────────────────
    // Set from main.qml based on config "theme_mode" (dark|light|system)
    property bool darkMode: true
    property string themeMode: "dark"   // mirrors config value

    function applyThemeMode(mode) {
        themeMode = mode
        if (mode === "system") {
            darkMode = Qt.styleHints.colorScheme === Qt.ColorScheme.Dark
        } else {
            darkMode = (mode !== "light")
        }
    }

    // ── Colors (reactive to darkMode) ───────────────────────────────
    readonly property color background:       darkMode ? "#2a2a3e" : "#ffffff"   // unified with surface to prevent banding
    readonly property color surface:          darkMode ? "#2a2a3e" : "#ffffff"
    readonly property color surfaceLight:     darkMode ? "#363650" : "#e8e8f0"
    readonly property color surfaceHover:     darkMode ? "#3e3e58" : "#dddde8"
    readonly property color border:           darkMode ? "#44446a" : "#c8c8d8"

    readonly property color textPrimary:      darkMode ? "#e0e0f0" : "#1a1a2e"
    readonly property color textEmphasis:     darkMode ? "#eeeeff" : "#0a0a1e"
    readonly property color textSecondary:    darkMode ? "#a0a0c0" : "#555570"
    readonly property color textMuted:        darkMode ? "#707090" : "#8888a0"

    readonly property color accent:           "#FFB300"
    readonly property color accentLight:      "#FFCA28"
    readonly property color accentText:       "#1a1a1a"   // dark text for use on accent backgrounds

    readonly property color success:          "#27ae60"
    readonly property color successLight:     "#2ecc71"
    readonly property color warning:          "#f39c12"
    readonly property color error:            "#e74c3c"
    readonly property color info:             "#17a2b8"

    readonly property color statusDvUpgrade:  "#9b59b6"

    // ── Typography ──────────────────────────────────────────────────
    readonly property int fontSizeSmall:     11
    readonly property int fontSizeNormal:    13
    readonly property int fontSizeMedium:    15
    readonly property int fontSizeLarge:     18
    readonly property int fontSizeXLarge:   24
    readonly property int fontSizeTitle:     22

    // ── Spacing ─────────────────────────────────────────────────────
    readonly property int spacingTiny:       4
    readonly property int spacingSmall:      8
    readonly property int spacingMedium:     12
    readonly property int spacingLarge:      16

    // ── Geometry ────────────────────────────────────────────────────
    readonly property int borderRadius:      8
    readonly property int borderRadiusSmall: 4
    readonly property int toolbarHeight:     46
    readonly property int statusBarHeight:   28
}
