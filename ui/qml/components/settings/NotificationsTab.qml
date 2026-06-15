import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import "../../style" as Style

ScrollView {
    id: notifTab
    contentWidth: availableWidth
    Material.elevation: 0
    background: Rectangle { color: Style.Theme.surface }
    property bool _ready: false
    Component.onCompleted: _ready = true

    ColumnLayout {
        width: parent.width
        spacing: Style.Theme.spacingMedium

        Item { Layout.preferredHeight: Style.Theme.spacingSmall }

        Label {
            text: "Notification Settings"
            font.pixelSize: Style.Theme.fontSizeLarge
            font.bold: true
            color: Style.Theme.textPrimary
            Layout.leftMargin: Style.Theme.spacingLarge
        }

        // ── Desktop Notifications ────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: Style.Theme.spacingLarge
            Layout.rightMargin: Style.Theme.spacingLarge
            Layout.preferredHeight: desktopCol.implicitHeight + 24
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight

            ColumnLayout {
                id: desktopCol
                anchors.fill: parent
                anchors.margins: 12
                spacing: Style.Theme.spacingSmall

                RowLayout {
                    CheckBox {
                        id: desktopEnabled
                        text: "Desktop Notifications"
                        font.bold: true
                        checked: settings.getBool("desktop_notifications")
                        onToggled: settings.setBool("desktop_notifications", checked)
                        Material.accent: Style.Theme.accent
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Show OS desktop notifications when new items or upgrades are found"
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        text: "Test"
                        enabled: desktopEnabled.checked
                        onClicked: settings.testDesktopNotification()
                        background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent; opacity: parent.enabled ? 1 : 0.5 }
                        contentItem: Label { text: parent.text; color: "#ffffff"; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Send a test desktop notification to verify it works"
                    }
                }
            }
        }

        // ── Discord Webhook ─────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: Style.Theme.spacingLarge
            Layout.rightMargin: Style.Theme.spacingLarge
            Layout.preferredHeight: discordCol.implicitHeight + 24
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight

            ColumnLayout {
                id: discordCol
                anchors.fill: parent
                anchors.margins: 12
                spacing: Style.Theme.spacingSmall

                Label { text: "Discord Webhook"; font.bold: true; color: Style.Theme.textPrimary }

                RowLayout {
                    spacing: Style.Theme.spacingSmall
                    Label { text: "Webhook URL:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 100 }
                    TextField {
                        id: discordUrl
                        Layout.fillWidth: true
                        text: settings.getString("discord_webhook")
                        onEditingFinished: settings.setString("discord_webhook", text)
                        placeholderText: "https://discord.com/api/webhooks/..."
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: discordUrl.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Your Discord channel webhook URL — found in Discord channel Settings → Integrations → Webhooks"
                    }
                }

                RowLayout {
                    spacing: Style.Theme.spacingSmall
                    Label { text: "Username:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 100 }
                    TextField {
                        id: discordUser
                        Layout.preferredWidth: 200
                        text: settings.getString("discord_username")
                        onEditingFinished: settings.setString("discord_username", text)
                        placeholderText: "ScanHound"
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: discordUser.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Display name shown on Discord messages (defaults to 'ScanHound')"
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        text: "Test"
                        enabled: discordUrl.text.length > 0
                        onClicked: settings.testDiscordWebhook()
                        background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent; opacity: parent.enabled ? 1 : 0.5 }
                        contentItem: Label { text: parent.text; color: "#ffffff"; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Send a test message to verify your Discord webhook"
                    }
                }
            }
        }

        // ── Slack Webhook ───────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: Style.Theme.spacingLarge
            Layout.rightMargin: Style.Theme.spacingLarge
            Layout.preferredHeight: slackCol.implicitHeight + 24
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight

            ColumnLayout {
                id: slackCol
                anchors.fill: parent
                anchors.margins: 12
                spacing: Style.Theme.spacingSmall

                Label { text: "Slack Webhook"; font.bold: true; color: Style.Theme.textPrimary }

                RowLayout {
                    spacing: Style.Theme.spacingSmall
                    Label { text: "Webhook URL:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 100 }
                    TextField {
                        id: slackUrl
                        Layout.fillWidth: true
                        text: settings.getString("slack_webhook")
                        onEditingFinished: settings.setString("slack_webhook", text)
                        placeholderText: "https://hooks.slack.com/services/..."
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: slackUrl.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Your Slack incoming webhook URL from api.slack.com/apps"
                    }
                    Button {
                        text: "Test"
                        enabled: slackUrl.text.length > 0
                        onClicked: settings.testSlackWebhook()
                        background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent; opacity: parent.enabled ? 1 : 0.5 }
                        contentItem: Label { text: parent.text; color: "#ffffff"; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Send a test message to verify your Slack webhook"
                    }
                }
            }
        }

        // ── Pushover ────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: Style.Theme.spacingLarge
            Layout.rightMargin: Style.Theme.spacingLarge
            Layout.preferredHeight: pushCol.implicitHeight + 24
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight

            ColumnLayout {
                id: pushCol
                anchors.fill: parent
                anchors.margins: 12
                spacing: Style.Theme.spacingSmall

                Label { text: "Pushover"; font.bold: true; color: Style.Theme.textPrimary }

                RowLayout {
                    spacing: Style.Theme.spacingSmall
                    Label { text: "User Key:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 100 }
                    TextField {
                        id: pushUser
                        Layout.fillWidth: true
                        text: settings.getString("pushover_user")
                        onEditingFinished: settings.setString("pushover_user", text)
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: pushUser.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Your Pushover user key from pushover.net — identifies your device(s)"
                    }
                }

                RowLayout {
                    spacing: Style.Theme.spacingSmall
                    Label { text: "API Token:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 100 }
                    TextField {
                        id: pushToken
                        Layout.fillWidth: true
                        text: settings.getString("pushover_token")
                        onEditingFinished: settings.setString("pushover_token", text)
                        echoMode: TextInput.Password
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: pushToken.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Your Pushover application API token from pushover.net/apps"
                    }
                    Button {
                        text: "Test"
                        enabled: pushUser.text.length > 0 && pushToken.text.length > 0
                        onClicked: settings.testPushover()
                        background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent; opacity: parent.enabled ? 1 : 0.5 }
                        contentItem: Label { text: parent.text; color: "#ffffff"; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Send a test push notification via Pushover"
                    }
                }
            }
        }

        // ── Custom Webhook ──────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: Style.Theme.spacingLarge
            Layout.rightMargin: Style.Theme.spacingLarge
            Layout.preferredHeight: webhookCol.implicitHeight + 24
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight

            ColumnLayout {
                id: webhookCol
                anchors.fill: parent
                anchors.margins: 12
                spacing: Style.Theme.spacingSmall

                Label { text: "Custom Webhook"; font.bold: true; color: Style.Theme.textPrimary }

                RowLayout {
                    spacing: Style.Theme.spacingSmall
                    Label { text: "URL:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 100 }
                    TextField {
                        id: webhookUrl
                        Layout.fillWidth: true
                        text: settings.getString("webhook_url")
                        onEditingFinished: settings.setString("webhook_url", text)
                        placeholderText: "https://your-webhook.example.com/notify"
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: webhookUrl.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "URL that receives an HTTP request whenever ScanHound finds new items or upgrades"
                    }
                }

                RowLayout {
                    spacing: Style.Theme.spacingSmall
                    Label { text: "Method:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall; Layout.preferredWidth: 100 }
                    ComboBox {
                        id: webhookMethod
                        model: ["POST", "GET", "PUT"]
                        currentIndex: {
                            var m = settings.getString("webhook_method")
                            return m === "GET" ? 1 : m === "PUT" ? 2 : 0
                        }
                        onCurrentTextChanged: if (notifTab._ready) settings.setString("webhook_method", currentText)
                        Material.background: Style.Theme.surface
                        Material.foreground: Style.Theme.textPrimary
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "HTTP method to use when calling the webhook (POST is most common)"
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        text: "Test"
                        enabled: webhookUrl.text.length > 0
                        onClicked: settings.testWebhook()
                        background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent; opacity: parent.enabled ? 1 : 0.5 }
                        contentItem: Label { text: parent.text; color: "#ffffff"; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Send a test request to verify your webhook endpoint"
                    }
                }
            }
        }

        // ── Email (SMTP) ────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: Style.Theme.spacingLarge
            Layout.rightMargin: Style.Theme.spacingLarge
            Layout.preferredHeight: emailCol.implicitHeight + 24
            radius: Style.Theme.borderRadius
            color: Style.Theme.surfaceLight

            ColumnLayout {
                id: emailCol
                anchors.fill: parent
                anchors.margins: 12
                spacing: Style.Theme.spacingSmall

                RowLayout {
                    CheckBox {
                        id: emailEnabled
                        text: "Email Notifications (SMTP)"
                        font.bold: true
                        checked: settings.getBool("email_enabled")
                        onToggled: settings.setBool("email_enabled", checked)
                        Material.accent: Style.Theme.accent
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Send email notifications via SMTP when new items or upgrades are found"
                    }
                }

                GridLayout {
                    columns: 2
                    columnSpacing: Style.Theme.spacingSmall
                    rowSpacing: 4
                    visible: emailEnabled.checked

                    Label { text: "SMTP Host:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                    TextField {
                        id: smtpHost
                        Layout.fillWidth: true
                        text: settings.getString("smtp_host")
                        onEditingFinished: settings.setString("smtp_host", text)
                        placeholderText: "smtp.gmail.com"
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: smtpHost.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Your mail server hostname (e.g. smtp.gmail.com, smtp.office365.com)"
                    }

                    Label { text: "SMTP Port:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                    TextField {
                        id: smtpPort
                        Layout.preferredWidth: 80
                        text: settings.getInt("smtp_port") > 0 ? settings.getInt("smtp_port").toString() : "587"
                        onEditingFinished: settings.setInt("smtp_port", parseInt(text) || 587)
                        validator: IntValidator { bottom: 1; top: 65535 }
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: smtpPort.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Mail server port — 587 for TLS/STARTTLS, 465 for SSL, 25 for plain (unencrypted)"
                    }

                    Label { text: "Username:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                    TextField {
                        id: smtpUser
                        Layout.fillWidth: true
                        text: settings.getString("smtp_username")
                        onEditingFinished: settings.setString("smtp_username", text)
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: smtpUser.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "SMTP login username (usually your email address)"
                    }

                    Label { text: "Password:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                    TextField {
                        id: smtpPass
                        Layout.fillWidth: true
                        text: settings.getString("smtp_password")
                        onEditingFinished: settings.setString("smtp_password", text)
                        echoMode: TextInput.Password
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: smtpPass.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "SMTP login password (for Gmail, use an App Password if 2FA is enabled)"
                    }

                    Label { text: "From:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                    TextField {
                        id: emailFrom
                        Layout.fillWidth: true
                        text: settings.getString("email_from")
                        onEditingFinished: settings.setString("email_from", text)
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: emailFrom.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Sender address shown in the 'From' field (usually the same as your SMTP username)"
                    }

                    Label { text: "To:"; color: Style.Theme.textSecondary; font.pixelSize: Style.Theme.fontSizeSmall }
                    TextField {
                        id: emailTo
                        Layout.fillWidth: true
                        text: settings.getString("email_to")
                        onEditingFinished: settings.setString("email_to", text)
                        color: Style.Theme.textPrimary
                        background: Rectangle { radius: 4; color: Style.Theme.surface; border.color: emailTo.activeFocus ? Style.Theme.accent : Style.Theme.border; border.width: 1 }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Recipient email address(es) for scan notifications"
                    }
                }

                RowLayout {
                    visible: emailEnabled.checked
                    spacing: Style.Theme.spacingSmall

                    CheckBox {
                        text: "Use TLS"
                        checked: settings.getBool("smtp_tls")
                        onToggled: settings.setBool("smtp_tls", checked)
                        Material.accent: Style.Theme.accent
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Encrypt the SMTP connection using TLS/STARTTLS (recommended with port 587)"
                    }

                    Item { Layout.fillWidth: true }

                    Button {
                        text: "Test Email"
                        onClicked: settings.testEmail()
                        background: Rectangle { radius: 4; color: parent.hovered ? Style.Theme.accentLight : Style.Theme.accent }
                        contentItem: Label { text: parent.text; color: "#ffffff"; font.pixelSize: Style.Theme.fontSizeSmall; horizontalAlignment: Text.AlignHCenter }
                        ToolTip.visible: hovered
                        ToolTip.delay: 700
                        ToolTip.text: "Send a test email to verify your SMTP settings are correct"
                    }
                }
            }
        }

        Item { Layout.preferredHeight: Style.Theme.spacingLarge }
    }
}
