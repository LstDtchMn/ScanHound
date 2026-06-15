"""Tests for backend/notifications.py

Covers:
- NotificationType enum values
- NotificationPriority enum values and comparisons
- Notification dataclass creation, to_dict, auto-generated id
- NotificationChannel: set_filters, should_handle
- DiscordWebhookChannel._build_embed: color, title, description, data fields, list truncation, field limit
- SlackWebhookChannel._build_blocks: header, section, context, emoji mapping, data fields
- GenericWebhookChannel._apply_template: safe_substitute, nested dicts/lists
- EmailChannel._build_email: subject, from/to, plain + HTML parts, HTML escaping, data table
- PushoverChannel: priority mapping
- NotificationManager: add_channel, remove_channel, add_callback
- NotificationManager.configure_from_dict: all channel types
- NotificationManager._combine_notifications: single pass-through, multi-same-type combination
- NotificationManager.get_history: returns dicts, limited by limit param
- NotificationManager.shutdown: cancels batch timer, clears pending
- get_notification_manager: singleton
- configure_notifications: configures global manager
- Async methods: notify, _send_notification
"""

import asyncio
import os
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.notifications import (
    DiscordWebhookChannel,
    EmailChannel,
    GenericWebhookChannel,
    Notification,
    NotificationChannel,
    NotificationManager,
    NotificationPriority,
    NotificationType,
    PushoverChannel,
    SlackWebhookChannel,
    configure_notifications,
    get_notification_manager,
)


# ===================================================================
# Helpers
# ===================================================================

class _ConcreteChannel(NotificationChannel):
    """Minimal concrete subclass for testing the abstract NotificationChannel."""

    def __init__(self, name="test_channel", enabled=True):
        super().__init__(name, enabled)
        self.sent = []

    async def send(self, notification):
        self.sent.append(notification)
        return True


def _make_notification(**overrides):
    """Create a Notification with sensible defaults, accepting overrides."""
    defaults = dict(
        type=NotificationType.INFO,
        title="Test Title",
        message="Test message body",
        priority=NotificationPriority.NORMAL,
        data={},
    )
    defaults.update(overrides)
    return Notification(**defaults)


# ===================================================================
# NotificationType
# ===================================================================

class TestNotificationType:

    def test_all_six_values_exist(self):
        expected = {
            "SCAN_COMPLETE": "scan_complete",
            "NEW_MISSING": "new_missing",
            "NEW_UPGRADE": "new_upgrade",
            "WATCHLIST_FOUND": "watchlist_found",
            "ERROR": "error",
            "INFO": "info",
        }
        for name, value in expected.items():
            member = NotificationType[name]
            assert member.value == value

    def test_enum_count(self):
        assert len(NotificationType) == 6

    def test_membership(self):
        assert NotificationType.SCAN_COMPLETE in NotificationType
        assert NotificationType.ERROR in NotificationType

    def test_iteration(self):
        values = list(NotificationType)
        assert len(values) == 6
        assert NotificationType.INFO in values


# ===================================================================
# NotificationPriority
# ===================================================================

class TestNotificationPriority:

    def test_integer_values(self):
        assert NotificationPriority.LOW == 1
        assert NotificationPriority.NORMAL == 2
        assert NotificationPriority.HIGH == 3
        assert NotificationPriority.URGENT == 4

    def test_comparison_high_greater_than_normal(self):
        assert NotificationPriority.HIGH > NotificationPriority.NORMAL

    def test_comparison_low_less_than_urgent(self):
        assert NotificationPriority.LOW < NotificationPriority.URGENT

    def test_comparison_equal(self):
        assert NotificationPriority.NORMAL == NotificationPriority.NORMAL

    def test_max_priority(self):
        priorities = [
            NotificationPriority.LOW,
            NotificationPriority.HIGH,
            NotificationPriority.NORMAL,
        ]
        assert max(priorities) == NotificationPriority.HIGH

    def test_is_int_subclass(self):
        assert isinstance(NotificationPriority.HIGH, int)


# ===================================================================
# Notification dataclass
# ===================================================================

class TestNotification:

    def test_creation_defaults(self):
        n = Notification(
            type=NotificationType.INFO,
            title="Hello",
            message="World",
        )
        assert n.type == NotificationType.INFO
        assert n.title == "Hello"
        assert n.message == "World"
        assert n.priority == NotificationPriority.NORMAL
        assert n.data == {}
        assert isinstance(n.timestamp, datetime)

    def test_id_starts_with_notif_prefix(self):
        n = _make_notification()
        assert n.id.startswith("notif_")

    def test_id_unique(self):
        ids = {_make_notification().id for _ in range(50)}
        assert len(ids) == 50

    def test_to_dict_keys(self):
        n = _make_notification(data={"key": "val"})
        d = n.to_dict()
        expected_keys = {"id", "type", "title", "message", "priority", "data", "timestamp"}
        assert set(d.keys()) == expected_keys

    def test_to_dict_values(self):
        n = _make_notification(
            type=NotificationType.ERROR,
            title="Err",
            message="Something broke",
            priority=NotificationPriority.URGENT,
            data={"details": "traceback"},
        )
        d = n.to_dict()
        assert d["type"] == "error"
        assert d["title"] == "Err"
        assert d["message"] == "Something broke"
        assert d["priority"] == 4
        assert d["data"] == {"details": "traceback"}
        assert d["id"].startswith("notif_")

    def test_to_dict_timestamp_is_isoformat(self):
        n = _make_notification()
        d = n.to_dict()
        # Should parse back without error
        datetime.fromisoformat(d["timestamp"])

    def test_custom_priority(self):
        n = _make_notification(priority=NotificationPriority.HIGH)
        assert n.priority == NotificationPriority.HIGH


# ===================================================================
# NotificationChannel (via _ConcreteChannel)
# ===================================================================

class TestNotificationChannel:

    def test_default_filters_include_all_types(self):
        ch = _ConcreteChannel()
        for t in NotificationType:
            n = _make_notification(type=t)
            assert ch.should_handle(n) is True

    def test_set_filters_restricts_types(self):
        ch = _ConcreteChannel()
        ch.set_filters([NotificationType.ERROR, NotificationType.INFO])
        assert ch.should_handle(_make_notification(type=NotificationType.ERROR)) is True
        assert ch.should_handle(_make_notification(type=NotificationType.INFO)) is True
        assert ch.should_handle(_make_notification(type=NotificationType.SCAN_COMPLETE)) is False

    def test_disabled_channel_should_not_handle(self):
        ch = _ConcreteChannel(enabled=False)
        n = _make_notification(type=NotificationType.INFO)
        assert ch.should_handle(n) is False

    def test_enabled_channel_should_handle(self):
        ch = _ConcreteChannel(enabled=True)
        n = _make_notification(type=NotificationType.INFO)
        assert ch.should_handle(n) is True

    def test_name_attribute(self):
        ch = _ConcreteChannel(name="myname")
        assert ch.name == "myname"

    def test_set_filters_empty_list(self):
        ch = _ConcreteChannel()
        ch.set_filters([])
        for t in NotificationType:
            assert ch.should_handle(_make_notification(type=t)) is False


# ===================================================================
# DiscordWebhookChannel._build_embed
# ===================================================================

class TestDiscordWebhookChannelBuildEmbed:

    def _make_channel(self):
        return DiscordWebhookChannel(
            webhook_url="https://discord.com/api/webhooks/test",
            username="TestBot",
        )

    def test_embed_has_correct_title(self):
        ch = self._make_channel()
        n = _make_notification(title="Scan Done")
        embed = ch._build_embed(n)
        assert embed["title"] == "Scan Done"

    def test_embed_has_correct_description(self):
        ch = self._make_channel()
        n = _make_notification(message="Scanned 100 items")
        embed = ch._build_embed(n)
        assert embed["description"] == "Scanned 100 items"

    def test_embed_color_for_each_type(self):
        ch = self._make_channel()
        for notif_type, expected_color in DiscordWebhookChannel.COLORS.items():
            n = _make_notification(type=notif_type)
            embed = ch._build_embed(n)
            assert embed["color"] == expected_color, f"Wrong color for {notif_type}"

    def test_embed_default_color_for_unknown(self):
        ch = self._make_channel()
        n = _make_notification(type=NotificationType.INFO)
        # INFO is actually in COLORS, so test the fallback by removing it
        original = DiscordWebhookChannel.COLORS.pop(NotificationType.INFO, None)
        try:
            embed = ch._build_embed(n)
            assert embed["color"] == 0x7289DA
        finally:
            if original is not None:
                DiscordWebhookChannel.COLORS[NotificationType.INFO] = original

    def test_embed_has_timestamp(self):
        ch = self._make_channel()
        n = _make_notification()
        embed = ch._build_embed(n)
        assert "timestamp" in embed

    def test_embed_footer_has_priority(self):
        ch = self._make_channel()
        n = _make_notification(priority=NotificationPriority.HIGH)
        embed = ch._build_embed(n)
        assert "Priority: HIGH" in embed["footer"]["text"]

    def test_embed_string_data_creates_inline_field(self):
        ch = self._make_channel()
        n = _make_notification(data={"duration": "10.5s"})
        embed = ch._build_embed(n)
        fields = embed.get("fields", [])
        assert len(fields) == 1
        assert fields[0]["name"] == "Duration"
        assert fields[0]["value"] == "10.5s"
        assert fields[0]["inline"] is True

    def test_embed_numeric_data_creates_inline_field(self):
        ch = self._make_channel()
        n = _make_notification(data={"count": 42})
        embed = ch._build_embed(n)
        fields = embed.get("fields", [])
        assert any(f["name"] == "Count" and f["value"] == "42" for f in fields)

    def test_embed_list_data_formatted_with_bullets(self):
        ch = self._make_channel()
        items_list = ["Movie A", "Movie B", "Movie C"]
        n = _make_notification(data={"items": items_list})
        embed = ch._build_embed(n)
        fields = embed.get("fields", [])
        items_field = [f for f in fields if f["name"] == "Items"][0]
        for item in items_list:
            assert f"• {item}" in items_field["value"]
        assert items_field["inline"] is False

    def test_embed_list_truncated_beyond_10(self):
        ch = self._make_channel()
        long_list = [f"Item {i}" for i in range(15)]
        n = _make_notification(data={"items": long_list})
        embed = ch._build_embed(n)
        fields = embed.get("fields", [])
        items_field = [f for f in fields if f["name"] == "Items"][0]
        assert "and 5 more" in items_field["value"]
        # Only first 10 should appear as bullet points
        assert "• Item 0" in items_field["value"]
        assert "• Item 9" in items_field["value"]
        assert "• Item 10" not in items_field["value"]

    def test_embed_fields_limited_to_25(self):
        ch = self._make_channel()
        data = {f"field_{i}": f"value_{i}" for i in range(30)}
        n = _make_notification(data=data)
        embed = ch._build_embed(n)
        assert len(embed.get("fields", [])) <= 25

    def test_embed_empty_data_no_fields(self):
        ch = self._make_channel()
        n = _make_notification(data={})
        embed = ch._build_embed(n)
        assert "fields" not in embed

    def test_embed_empty_list_not_added_as_field(self):
        ch = self._make_channel()
        n = _make_notification(data={"items": []})
        embed = ch._build_embed(n)
        fields = embed.get("fields", [])
        # Empty list should not produce a field (the `if value` check skips it)
        assert not any(f["name"] == "Items" for f in fields)

    def test_embed_upgrades_key_formatted_as_list(self):
        ch = self._make_channel()
        n = _make_notification(data={"upgrades": ["Movie A", "Movie B"]})
        embed = ch._build_embed(n)
        fields = embed.get("fields", [])
        upgrades_field = [f for f in fields if f["name"] == "Upgrades"][0]
        assert "• Movie A" in upgrades_field["value"]

    def test_embed_missing_key_formatted_as_list(self):
        ch = self._make_channel()
        n = _make_notification(data={"missing": ["Movie X"]})
        embed = ch._build_embed(n)
        fields = embed.get("fields", [])
        missing_field = [f for f in fields if f["name"] == "Missing"][0]
        assert "• Movie X" in missing_field["value"]


# ===================================================================
# SlackWebhookChannel._build_blocks
# ===================================================================

class TestSlackWebhookChannelBuildBlocks:

    def _make_channel(self):
        return SlackWebhookChannel(webhook_url="https://hooks.slack.com/test")

    def test_blocks_has_header(self):
        ch = self._make_channel()
        n = _make_notification(title="Hello Slack")
        blocks = ch._build_blocks(n)
        header_blocks = [b for b in blocks if b["type"] == "header"]
        assert len(header_blocks) == 1
        assert "Hello Slack" in header_blocks[0]["text"]["text"]

    def test_blocks_has_section(self):
        ch = self._make_channel()
        n = _make_notification(message="Body text here")
        blocks = ch._build_blocks(n)
        section_blocks = [b for b in blocks if b["type"] == "section" and "text" in b and b["text"].get("type") == "mrkdwn"]
        assert len(section_blocks) >= 1
        assert section_blocks[0]["text"]["text"] == "Body text here"

    def test_blocks_has_context(self):
        ch = self._make_channel()
        n = _make_notification()
        blocks = ch._build_blocks(n)
        context_blocks = [b for b in blocks if b["type"] == "context"]
        assert len(context_blocks) == 1
        assert "Sent at" in context_blocks[0]["elements"][0]["text"]

    def test_emoji_mapping_for_each_type(self):
        ch = self._make_channel()
        for notif_type, emoji in SlackWebhookChannel.EMOJI.items():
            n = _make_notification(type=notif_type, title="Test")
            blocks = ch._build_blocks(n)
            header = [b for b in blocks if b["type"] == "header"][0]
            assert emoji in header["text"]["text"], f"Missing emoji for {notif_type}"

    def test_data_fields_in_section(self):
        ch = self._make_channel()
        n = _make_notification(data={"item_count": 5, "duration": "10s"})
        blocks = ch._build_blocks(n)
        # Find section with fields
        field_sections = [b for b in blocks if b.get("fields")]
        assert len(field_sections) == 1
        fields = field_sections[0]["fields"]
        field_texts = [f["text"] for f in fields]
        assert any("Item Count" in t for t in field_texts)
        assert any("Duration" in t for t in field_texts)

    def test_list_data_joined_with_comma(self):
        ch = self._make_channel()
        n = _make_notification(data={"tags": ["a", "b", "c"]})
        blocks = ch._build_blocks(n)
        field_sections = [b for b in blocks if b.get("fields")]
        assert len(field_sections) == 1
        text = field_sections[0]["fields"][0]["text"]
        assert "a, b, c" in text

    def test_list_data_truncated_at_5(self):
        ch = self._make_channel()
        long_list = [f"item{i}" for i in range(8)]
        n = _make_notification(data={"tags": long_list})
        blocks = ch._build_blocks(n)
        field_sections = [b for b in blocks if b.get("fields")]
        text = field_sections[0]["fields"][0]["text"]
        assert "..." in text

    def test_no_data_no_field_section(self):
        ch = self._make_channel()
        n = _make_notification(data={})
        blocks = ch._build_blocks(n)
        field_sections = [b for b in blocks if b.get("fields")]
        assert len(field_sections) == 0

    def test_header_emoji_true(self):
        ch = self._make_channel()
        n = _make_notification()
        blocks = ch._build_blocks(n)
        header = [b for b in blocks if b["type"] == "header"][0]
        assert header["text"]["emoji"] is True


# ===================================================================
# GenericWebhookChannel._apply_template
# ===================================================================

class TestGenericWebhookChannelApplyTemplate:

    def test_simple_string_substitution(self):
        template = {"text": "$title - $message"}
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template=template,
        )
        n = _make_notification(title="Alert", message="Disk full")
        result = ch._apply_template(n)
        assert result["text"] == "Alert - Disk full"

    def test_type_and_priority_substitution(self):
        template = {"type": "$type", "prio": "$priority"}
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template=template,
        )
        n = _make_notification(type=NotificationType.ERROR, priority=NotificationPriority.HIGH)
        result = ch._apply_template(n)
        assert result["type"] == "error"
        assert result["prio"] == "HIGH"

    def test_timestamp_substitution(self):
        template = {"ts": "$timestamp"}
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template=template,
        )
        n = _make_notification()
        result = ch._apply_template(n)
        # Should be an ISO format string
        datetime.fromisoformat(result["ts"])

    def test_safe_substitute_leaves_unknown_vars(self):
        template = {"text": "$title and $unknown_var"}
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template=template,
        )
        n = _make_notification(title="Alert")
        result = ch._apply_template(n)
        assert "Alert" in result["text"]
        assert "$unknown_var" in result["text"]

    def test_nested_dict_template(self):
        template = {"outer": {"inner": "$title"}}
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template=template,
        )
        n = _make_notification(title="NestedTest")
        result = ch._apply_template(n)
        assert result["outer"]["inner"] == "NestedTest"

    def test_list_template(self):
        template = {"items": ["$title", "$message"]}
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template=template,
        )
        n = _make_notification(title="T", message="M")
        result = ch._apply_template(n)
        assert result["items"] == ["T", "M"]

    def test_data_values_available_for_substitution(self):
        template = {"count": "$total_items"}
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template=template,
        )
        n = _make_notification(data={"total_items": 42})
        result = ch._apply_template(n)
        assert result["count"] == "42"

    def test_non_string_values_pass_through(self):
        template = {"number": 42, "flag": True, "text": "$title"}
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template=template,
        )
        n = _make_notification(title="OK")
        result = ch._apply_template(n)
        assert result["number"] == 42
        assert result["flag"] is True
        assert result["text"] == "OK"

    def test_original_template_not_mutated(self):
        template = {"text": "$title"}
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template=template,
        )
        n = _make_notification(title="First")
        ch._apply_template(n)
        # Template should still have the placeholder
        assert ch.template["text"] == "$title"


# ===================================================================
# EmailChannel._build_email
# ===================================================================

class TestEmailChannelBuildEmail:

    def _make_channel(self):
        return EmailChannel(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="user",
            password="pass",
            from_addr="noreply@example.com",
            to_addrs=["admin@example.com", "ops@example.com"],
            use_tls=True,
        )

    def test_subject_format(self):
        ch = self._make_channel()
        n = _make_notification(title="Scan Complete")
        msg = ch._build_email(n)
        assert msg["Subject"] == "[ScanHound] Scan Complete"

    def test_from_address(self):
        ch = self._make_channel()
        n = _make_notification()
        msg = ch._build_email(n)
        assert msg["From"] == "noreply@example.com"

    def test_to_addresses(self):
        ch = self._make_channel()
        n = _make_notification()
        msg = ch._build_email(n)
        assert "admin@example.com" in msg["To"]
        assert "ops@example.com" in msg["To"]

    def test_multipart_alternative(self):
        ch = self._make_channel()
        n = _make_notification()
        msg = ch._build_email(n)
        assert msg.get_content_type() == "multipart/alternative"

    def test_has_plain_text_part(self):
        ch = self._make_channel()
        n = _make_notification(title="Test", message="Body text")
        msg = ch._build_email(n)
        parts = msg.get_payload()
        plain_parts = [p for p in parts if p.get_content_type() == "text/plain"]
        assert len(plain_parts) == 1
        plain_text = plain_parts[0].get_payload(decode=True).decode()
        assert "Test" in plain_text
        assert "Body text" in plain_text

    def test_has_html_part(self):
        ch = self._make_channel()
        n = _make_notification(title="Test", message="Body text")
        msg = ch._build_email(n)
        parts = msg.get_payload()
        html_parts = [p for p in parts if p.get_content_type() == "text/html"]
        assert len(html_parts) == 1
        html_text = html_parts[0].get_payload(decode=True).decode()
        assert "<html>" in html_text
        assert "Test" in html_text

    def test_html_escaping(self):
        ch = self._make_channel()
        n = _make_notification(title="<script>alert('xss')</script>", message="A & B")
        msg = ch._build_email(n)
        parts = msg.get_payload()
        html_parts = [p for p in parts if p.get_content_type() == "text/html"]
        html_text = html_parts[0].get_payload(decode=True).decode()
        assert "<script>" not in html_text
        assert "&lt;script&gt;" in html_text
        assert "A &amp; B" in html_text

    def test_data_in_plain_text(self):
        ch = self._make_channel()
        n = _make_notification(data={"count": 42, "status": "ok"})
        msg = ch._build_email(n)
        parts = msg.get_payload()
        plain_parts = [p for p in parts if p.get_content_type() == "text/plain"]
        plain_text = plain_parts[0].get_payload(decode=True).decode()
        assert "count" in plain_text
        assert "42" in plain_text

    def test_data_table_in_html(self):
        ch = self._make_channel()
        n = _make_notification(data={"count": 42})
        msg = ch._build_email(n)
        parts = msg.get_payload()
        html_parts = [p for p in parts if p.get_content_type() == "text/html"]
        html_text = html_parts[0].get_payload(decode=True).decode()
        assert "<table" in html_text
        assert "Count" in html_text
        assert "42" in html_text

    def test_data_list_values_joined_in_html(self):
        ch = self._make_channel()
        n = _make_notification(data={"items": ["A", "B", "C"]})
        msg = ch._build_email(n)
        parts = msg.get_payload()
        html_parts = [p for p in parts if p.get_content_type() == "text/html"]
        html_text = html_parts[0].get_payload(decode=True).decode()
        assert "A, B, C" in html_text

    def test_no_data_no_table(self):
        ch = self._make_channel()
        n = _make_notification(data={})
        msg = ch._build_email(n)
        parts = msg.get_payload()
        html_parts = [p for p in parts if p.get_content_type() == "text/html"]
        html_text = html_parts[0].get_payload(decode=True).decode()
        assert "<table" not in html_text


# ===================================================================
# PushoverChannel priority mapping
# ===================================================================

class TestPushoverChannel:

    def test_priority_mapping_low(self):
        assert PushoverChannel.PRIORITY_MAP[NotificationPriority.LOW] == -1

    def test_priority_mapping_normal(self):
        assert PushoverChannel.PRIORITY_MAP[NotificationPriority.NORMAL] == 0

    def test_priority_mapping_high(self):
        assert PushoverChannel.PRIORITY_MAP[NotificationPriority.HIGH] == 1

    def test_priority_mapping_urgent(self):
        assert PushoverChannel.PRIORITY_MAP[NotificationPriority.URGENT] == 2

    def test_all_priorities_mapped(self):
        for p in NotificationPriority:
            assert p in PushoverChannel.PRIORITY_MAP

    def test_init_attributes(self):
        ch = PushoverChannel(user_key="ukey", api_token="tok123")
        assert ch.user_key == "ukey"
        assert ch.api_token == "tok123"
        assert ch.api_url == "https://api.pushover.net/1/messages.json"
        assert ch.name == "pushover"


# ===================================================================
# NotificationManager: basic channel management
# ===================================================================

class TestNotificationManagerChannels:

    def test_add_channel(self):
        mgr = NotificationManager()
        ch = _ConcreteChannel(name="ch1")
        mgr.add_channel(ch)
        assert len(mgr._channels) == 1
        assert mgr._channels[0].name == "ch1"

    def test_add_multiple_channels(self):
        mgr = NotificationManager()
        mgr.add_channel(_ConcreteChannel(name="a"))
        mgr.add_channel(_ConcreteChannel(name="b"))
        assert len(mgr._channels) == 2

    def test_remove_channel_by_name(self):
        mgr = NotificationManager()
        mgr.add_channel(_ConcreteChannel(name="remove_me"))
        mgr.add_channel(_ConcreteChannel(name="keep_me"))
        mgr.remove_channel("remove_me")
        assert len(mgr._channels) == 1
        assert mgr._channels[0].name == "keep_me"

    def test_remove_nonexistent_channel(self):
        mgr = NotificationManager()
        mgr.add_channel(_ConcreteChannel(name="a"))
        mgr.remove_channel("nonexistent")
        assert len(mgr._channels) == 1

    def test_add_callback(self):
        mgr = NotificationManager()
        callback = MagicMock()
        mgr.add_callback(callback)
        assert callback in mgr._callbacks


# ===================================================================
# NotificationManager.configure_from_dict
# ===================================================================

class TestNotificationManagerConfigureFromDict:

    def test_empty_config_no_channels(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({})
        assert len(mgr._channels) == 0

    @patch("backend.notifications.DesktopNotificationChannel")
    def test_desktop_enabled(self, mock_desktop_cls):
        mock_instance = MagicMock()
        mock_desktop_cls.return_value = mock_instance
        mgr = NotificationManager()
        mgr.configure_from_dict({"desktop_enabled": True})
        mock_desktop_cls.assert_called_once()
        assert mock_instance in mgr._channels

    def test_discord_webhook_creates_channel(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({
            "discord_webhook": "https://discord.com/api/webhooks/123/abc",
            "discord_username": "TestBot",
        })
        assert len(mgr._channels) == 1
        ch = mgr._channels[0]
        assert isinstance(ch, DiscordWebhookChannel)
        assert ch.webhook_url == "https://discord.com/api/webhooks/123/abc"
        assert ch.username == "TestBot"

    def test_discord_with_type_filters(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({
            "discord_webhook": "https://discord.com/api/webhooks/123/abc",
            "discord_types": ["ERROR", "SCAN_COMPLETE"],
        })
        ch = mgr._channels[0]
        assert ch.should_handle(_make_notification(type=NotificationType.ERROR)) is True
        assert ch.should_handle(_make_notification(type=NotificationType.INFO)) is False

    def test_slack_webhook_creates_channel(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({"slack_webhook": "https://hooks.slack.com/test"})
        assert len(mgr._channels) == 1
        assert isinstance(mgr._channels[0], SlackWebhookChannel)

    def test_email_config_creates_channel(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({
            "email_enabled": True,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_username": "user@gmail.com",
            "smtp_password": "secret",
            "email_from": "user@gmail.com",
            "email_to": ["admin@example.com"],
        })
        assert len(mgr._channels) == 1
        ch = mgr._channels[0]
        assert isinstance(ch, EmailChannel)
        assert ch.smtp_host == "smtp.gmail.com"

    def test_email_without_smtp_host_no_channel(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({"email_enabled": True})
        assert len(mgr._channels) == 0

    def test_pushover_creates_channel(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({
            "pushover_user": "user123",
            "pushover_token": "token456",
        })
        assert len(mgr._channels) == 1
        ch = mgr._channels[0]
        assert isinstance(ch, PushoverChannel)
        assert ch.user_key == "user123"
        assert ch.api_token == "token456"

    def test_generic_webhook_creates_channel(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({
            "webhook_url": "https://example.com/hook",
            "webhook_method": "PUT",
        })
        assert len(mgr._channels) == 1
        ch = mgr._channels[0]
        assert isinstance(ch, GenericWebhookChannel)
        assert ch.webhook_url == "https://example.com/hook"
        assert ch.method == "PUT"

    def test_multiple_channels_from_single_config(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({
            "discord_webhook": "https://discord.com/api/webhooks/x/y",
            "slack_webhook": "https://hooks.slack.com/services/x",
            "pushover_user": "u",
            "pushover_token": "t",
        })
        assert len(mgr._channels) == 3
        names = {ch.name for ch in mgr._channels}
        assert names == {"discord", "slack", "pushover"}


# ===================================================================
# NotificationManager._combine_notifications
# ===================================================================

class TestNotificationManagerCombine:

    def test_single_notification_passes_through(self):
        mgr = NotificationManager()
        n = _make_notification(title="Only One")
        result = mgr._combine_notifications([n])
        assert len(result) == 1
        assert result[0].title == "Only One"

    def test_multiple_same_type_combined(self):
        mgr = NotificationManager()
        n1 = _make_notification(type=NotificationType.ERROR, title="Error 1")
        n2 = _make_notification(type=NotificationType.ERROR, title="Error 2")
        n3 = _make_notification(type=NotificationType.ERROR, title="Error 3")
        result = mgr._combine_notifications([n1, n2, n3])
        assert len(result) == 1
        combined = result[0]
        assert "3" in combined.title
        assert "Error" in combined.title

    def test_combined_message_has_bullet_points(self):
        mgr = NotificationManager()
        n1 = _make_notification(type=NotificationType.INFO, title="Info A")
        n2 = _make_notification(type=NotificationType.INFO, title="Info B")
        result = mgr._combine_notifications([n1, n2])
        assert len(result) == 1
        assert "Info A" in result[0].message
        assert "Info B" in result[0].message

    def test_different_types_not_combined(self):
        mgr = NotificationManager()
        n1 = _make_notification(type=NotificationType.ERROR, title="Err")
        n2 = _make_notification(type=NotificationType.INFO, title="Inf")
        result = mgr._combine_notifications([n1, n2])
        assert len(result) == 2

    def test_combined_gets_max_priority(self):
        mgr = NotificationManager()
        n1 = _make_notification(
            type=NotificationType.ERROR,
            title="Low",
            priority=NotificationPriority.LOW,
        )
        n2 = _make_notification(
            type=NotificationType.ERROR,
            title="Urgent",
            priority=NotificationPriority.URGENT,
        )
        result = mgr._combine_notifications([n1, n2])
        assert result[0].priority == NotificationPriority.URGENT

    def test_combined_merges_data(self):
        mgr = NotificationManager()
        n1 = _make_notification(type=NotificationType.INFO, title="A", data={"key1": "val1"})
        n2 = _make_notification(type=NotificationType.INFO, title="B", data={"key2": "val2"})
        result = mgr._combine_notifications([n1, n2])
        assert "key1" in result[0].data
        assert "key2" in result[0].data

    def test_empty_list_returns_empty(self):
        mgr = NotificationManager()
        result = mgr._combine_notifications([])
        assert result == []


# ===================================================================
# NotificationManager.get_history
# ===================================================================

class TestNotificationManagerGetHistory:

    def test_empty_history(self):
        mgr = NotificationManager()
        assert mgr.get_history() == []

    def test_history_returns_dicts(self):
        mgr = NotificationManager()
        n = _make_notification()
        mgr._history.append(n)
        history = mgr.get_history()
        assert len(history) == 1
        assert isinstance(history[0], dict)
        assert "id" in history[0]

    def test_history_limited_by_limit(self):
        mgr = NotificationManager()
        for i in range(10):
            mgr._history.append(_make_notification(title=f"N{i}"))
        history = mgr.get_history(limit=3)
        assert len(history) == 3
        # Should return the last 3 (most recent)
        assert history[-1]["title"] == "N9"
        assert history[0]["title"] == "N7"

    def test_history_default_limit(self):
        mgr = NotificationManager()
        for i in range(60):
            mgr._history.append(_make_notification(title=f"N{i}"))
        history = mgr.get_history()
        assert len(history) == 50  # default limit


# ===================================================================
# NotificationManager.shutdown
# ===================================================================

class TestNotificationManagerShutdown:

    def test_shutdown_clears_pending(self):
        mgr = NotificationManager()
        mgr._pending.append(_make_notification())
        mgr.shutdown()
        assert len(mgr._pending) == 0

    def test_shutdown_cancels_batch_timer(self):
        mgr = NotificationManager()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mgr._batch_timer = mock_task
        mgr.shutdown()
        mock_task.cancel.assert_called_once()

    def test_shutdown_does_not_cancel_completed_timer(self):
        mgr = NotificationManager()
        mock_task = MagicMock()
        mock_task.done.return_value = True
        mgr._batch_timer = mock_task
        mgr.shutdown()
        mock_task.cancel.assert_not_called()

    def test_shutdown_with_no_timer(self):
        mgr = NotificationManager()
        mgr._batch_timer = None
        mgr.shutdown()  # Should not raise

    def test_shutdown_calls_close_on_channels_with_close(self):
        mgr = NotificationManager()
        ch = _ConcreteChannel()
        ch.close = MagicMock()
        mgr.add_channel(ch)
        mgr.shutdown()
        ch.close.assert_called_once()


# ===================================================================
# Async: notify and _send_notification
# ===================================================================

class TestNotificationManagerAsync:

    def test_notify_sends_to_channel(self):
        mgr = NotificationManager()
        ch = _ConcreteChannel()
        mgr.add_channel(ch)

        asyncio.run(mgr.notify(
            NotificationType.INFO,
            "Test",
            "Message",
        ))
        assert len(ch.sent) == 1
        assert ch.sent[0].title == "Test"

    def test_notify_adds_to_history(self):
        mgr = NotificationManager()
        ch = _ConcreteChannel()
        mgr.add_channel(ch)

        asyncio.run(mgr.notify(
            NotificationType.INFO,
            "Hist",
            "Check history",
        ))
        assert len(mgr._history) == 1

    def test_notify_triggers_callback(self):
        mgr = NotificationManager()
        ch = _ConcreteChannel()
        mgr.add_channel(ch)
        callback = MagicMock()
        mgr.add_callback(callback)

        asyncio.run(mgr.notify(
            NotificationType.INFO,
            "CB Test",
            "Trigger callback",
        ))
        callback.assert_called_once()
        notif_arg = callback.call_args[0][0]
        assert isinstance(notif_arg, Notification)
        assert notif_arg.title == "CB Test"

    def test_notify_respects_channel_filters(self):
        mgr = NotificationManager()
        ch = _ConcreteChannel()
        ch.set_filters([NotificationType.ERROR])
        mgr.add_channel(ch)

        asyncio.run(mgr.notify(
            NotificationType.INFO,
            "Filtered Out",
            "Should not arrive",
        ))
        assert len(ch.sent) == 0

    def test_notify_with_data(self):
        mgr = NotificationManager()
        ch = _ConcreteChannel()
        mgr.add_channel(ch)

        asyncio.run(mgr.notify(
            NotificationType.SCAN_COMPLETE,
            "Done",
            "Scan finished",
            data={"count": 100},
        ))
        assert ch.sent[0].data == {"count": 100}

    def test_notify_with_priority(self):
        mgr = NotificationManager()
        ch = _ConcreteChannel()
        mgr.add_channel(ch)

        asyncio.run(mgr.notify(
            NotificationType.ERROR,
            "Critical",
            "Something bad",
            priority=NotificationPriority.URGENT,
        ))
        assert ch.sent[0].priority == NotificationPriority.URGENT

    def test_send_notification_trims_history_to_max(self):
        mgr = NotificationManager()
        mgr._max_history = 5
        ch = _ConcreteChannel()
        mgr.add_channel(ch)

        async def send_many():
            for i in range(10):
                await mgr.notify(NotificationType.INFO, f"N{i}", "msg")

        asyncio.run(send_many())
        assert len(mgr._history) == 5
        # Most recent should be last
        assert mgr._history[-1].title == "N9"

    def test_callback_exception_does_not_prevent_send(self):
        mgr = NotificationManager()
        ch = _ConcreteChannel()
        mgr.add_channel(ch)

        bad_callback = MagicMock(side_effect=RuntimeError("callback broke"))
        mgr.add_callback(bad_callback)

        asyncio.run(mgr.notify(
            NotificationType.INFO,
            "Still Sent",
            "Despite callback error",
        ))
        assert len(ch.sent) == 1

    def test_discord_send_uses_post_webhook(self):
        ch = DiscordWebhookChannel(
            webhook_url="https://discord.com/api/webhooks/test",
        )

        with patch.object(ch, "_post_webhook", new_callable=AsyncMock, return_value=True) as mock_post:
            n = _make_notification()
            result = asyncio.run(ch.send(n))
            assert result is True
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert call_args[0][0] == "https://discord.com/api/webhooks/test"
            payload = call_args[0][1]
            assert "embeds" in payload

    def test_slack_send_uses_post_webhook(self):
        ch = SlackWebhookChannel(webhook_url="https://hooks.slack.com/test")

        with patch.object(ch, "_post_webhook", new_callable=AsyncMock, return_value=True) as mock_post:
            n = _make_notification()
            result = asyncio.run(ch.send(n))
            assert result is True
            mock_post.assert_called_once()
            payload = mock_post.call_args[0][1]
            assert "blocks" in payload

    def test_generic_webhook_send_uses_to_dict_without_template(self):
        ch = GenericWebhookChannel(webhook_url="https://example.com/hook")

        with patch.object(ch, "_post_webhook", new_callable=AsyncMock, return_value=True) as mock_post:
            n = _make_notification(title="Test")
            result = asyncio.run(ch.send(n))
            assert result is True
            payload = mock_post.call_args[0][1]
            assert payload["title"] == "Test"

    def test_generic_webhook_send_uses_template_when_set(self):
        ch = GenericWebhookChannel(
            webhook_url="https://example.com/hook",
            template={"msg": "$title"},
        )

        with patch.object(ch, "_post_webhook", new_callable=AsyncMock, return_value=True) as mock_post:
            n = _make_notification(title="Tmpl")
            asyncio.run(ch.send(n))
            payload = mock_post.call_args[0][1]
            assert payload["msg"] == "Tmpl"

    def test_pushover_send_uses_data_form(self):
        ch = PushoverChannel(user_key="u", api_token="t")

        with patch.object(ch, "_post_webhook", new_callable=AsyncMock, return_value=True) as mock_post:
            n = _make_notification(priority=NotificationPriority.HIGH)
            asyncio.run(ch.send(n))
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert call_kwargs[1]["use_data"] is True
            payload = call_kwargs[0][1]
            assert payload["token"] == "t"
            assert payload["user"] == "u"
            assert payload["priority"] == 1  # HIGH -> 1


# ===================================================================
# Module-level singletons and configuration
# ===================================================================

class TestModuleLevelFunctions:

    def test_get_notification_manager_returns_instance(self):
        import backend.notifications as mod
        original = mod._notification_manager
        try:
            mod._notification_manager = None
            mgr = get_notification_manager()
            assert isinstance(mgr, NotificationManager)
        finally:
            mod._notification_manager = original

    def test_get_notification_manager_returns_singleton(self):
        import backend.notifications as mod
        original = mod._notification_manager
        try:
            mod._notification_manager = None
            mgr1 = get_notification_manager()
            mgr2 = get_notification_manager()
            assert mgr1 is mgr2
        finally:
            mod._notification_manager = original

    def test_configure_notifications_adds_channels(self):
        import backend.notifications as mod
        original = mod._notification_manager
        try:
            mod._notification_manager = None
            configure_notifications({
                "discord_webhook": "https://discord.com/api/webhooks/test",
            })
            mgr = get_notification_manager()
            assert len(mgr._channels) == 1
            assert isinstance(mgr._channels[0], DiscordWebhookChannel)
        finally:
            mod._notification_manager = original
