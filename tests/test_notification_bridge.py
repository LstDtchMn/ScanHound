"""Tests for backend/notification_bridge.py

Covers:
- NotificationBridge.__init__: all attributes None/default
- configure: empty config (no channels), with discord webhook, with email config
- send: when no manager (early return), basic send call (mock manager and loop)
- notify_scan_complete: correct type_name, title, message format, data dict
- notify_error: correct type_name and message
- shutdown: stops loop, calls manager.shutdown, clears references
"""

import asyncio
import os
import sys
import threading
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.notification_bridge import NotificationBridge


# ===================================================================
# NotificationBridge.__init__
# ===================================================================

class TestNotificationBridgeInit:

    def test_manager_is_none(self):
        nb = NotificationBridge()
        assert nb._manager is None

    def test_loop_is_none(self):
        nb = NotificationBridge()
        assert nb._loop is None

    def test_thread_is_none(self):
        nb = NotificationBridge()
        assert nb._thread is None

    def test_ready_event_exists(self):
        nb = NotificationBridge()
        assert isinstance(nb._ready, threading.Event)

    def test_ready_event_not_set(self):
        nb = NotificationBridge()
        assert not nb._ready.is_set()


# ===================================================================
# NotificationBridge.configure
# ===================================================================

class TestNotificationBridgeConfigure:

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_empty_config_creates_manager_no_channels(self, MockManager, mock_start):
        mock_instance = MagicMock()
        MockManager.return_value = mock_instance

        nb = NotificationBridge()
        nb.configure({})

        MockManager.assert_called_once()
        assert nb._manager is mock_instance
        # configure_from_dict should be called with a dict (may have desktop_enabled)
        mock_instance.configure_from_dict.assert_called_once()
        config_arg = mock_instance.configure_from_dict.call_args[0][0]
        # Empty config means no discord, slack, pushover, webhook keys
        assert "discord_webhook" not in config_arg
        assert "slack_webhook" not in config_arg
        assert "email_enabled" not in config_arg
        assert "pushover_user" not in config_arg
        assert "webhook_url" not in config_arg

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_configure_with_discord_webhook(self, MockManager, mock_start):
        mock_instance = MagicMock()
        MockManager.return_value = mock_instance

        nb = NotificationBridge()
        nb.configure({
            "discord_webhook": "https://discord.com/api/webhooks/123/abc",
            "discord_username": "TestBot",
        })

        config_arg = mock_instance.configure_from_dict.call_args[0][0]
        assert config_arg["discord_webhook"] == "https://discord.com/api/webhooks/123/abc"
        assert config_arg["discord_username"] == "TestBot"

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_configure_with_slack_webhook(self, MockManager, mock_start):
        mock_instance = MagicMock()
        MockManager.return_value = mock_instance

        nb = NotificationBridge()
        nb.configure({"slack_webhook": "https://hooks.slack.com/services/xxx"})

        config_arg = mock_instance.configure_from_dict.call_args[0][0]
        assert config_arg["slack_webhook"] == "https://hooks.slack.com/services/xxx"

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_configure_with_email(self, MockManager, mock_start):
        mock_instance = MagicMock()
        MockManager.return_value = mock_instance

        nb = NotificationBridge()
        nb.configure({
            "email_enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_username": "user",
            "smtp_password": "pass",
            "email_from": "noreply@example.com",
            "email_to": ["admin@example.com"],
            "smtp_tls": True,
        })

        config_arg = mock_instance.configure_from_dict.call_args[0][0]
        assert config_arg["email_enabled"] is True
        assert config_arg["smtp_host"] == "smtp.example.com"
        assert config_arg["smtp_port"] == 587
        assert config_arg["smtp_username"] == "user"
        assert config_arg["smtp_password"] == "pass"
        assert config_arg["email_from"] == "noreply@example.com"
        assert config_arg["email_to"] == ["admin@example.com"]
        assert config_arg["smtp_tls"] is True

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_configure_with_pushover(self, MockManager, mock_start):
        mock_instance = MagicMock()
        MockManager.return_value = mock_instance

        nb = NotificationBridge()
        nb.configure({
            "pushover_user": "user_key",
            "pushover_token": "api_tok",
        })

        config_arg = mock_instance.configure_from_dict.call_args[0][0]
        assert config_arg["pushover_user"] == "user_key"
        assert config_arg["pushover_token"] == "api_tok"

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_configure_with_generic_webhook(self, MockManager, mock_start):
        mock_instance = MagicMock()
        MockManager.return_value = mock_instance

        nb = NotificationBridge()
        nb.configure({
            "webhook_url": "https://example.com/hook",
            "webhook_method": "PUT",
        })

        config_arg = mock_instance.configure_from_dict.call_args[0][0]
        assert config_arg["webhook_url"] == "https://example.com/hook"
        assert config_arg["webhook_method"] == "PUT"

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_configure_with_desktop_enabled_by_default(self, MockManager, mock_start):
        mock_instance = MagicMock()
        MockManager.return_value = mock_instance

        nb = NotificationBridge()
        nb.configure({"desktop_notifications": True})

        config_arg = mock_instance.configure_from_dict.call_args[0][0]
        assert config_arg["desktop_enabled"] is True

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_configure_desktop_disabled(self, MockManager, mock_start):
        mock_instance = MagicMock()
        MockManager.return_value = mock_instance

        nb = NotificationBridge()
        nb.configure({"desktop_notifications": False})

        config_arg = mock_instance.configure_from_dict.call_args[0][0]
        assert "desktop_enabled" not in config_arg

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_configure_starts_loop(self, MockManager, mock_start):
        MockManager.return_value = MagicMock()
        nb = NotificationBridge()
        nb.configure({})
        mock_start.assert_called_once()

    @patch("backend.notification_bridge.NotificationBridge._start_loop")
    @patch("backend.notifications.NotificationManager")
    def test_configure_handles_configure_from_dict_exception(self, MockManager, mock_start):
        mock_instance = MagicMock()
        mock_instance.configure_from_dict.side_effect = ValueError("bad config")
        MockManager.return_value = mock_instance

        nb = NotificationBridge()
        # Should not raise
        nb.configure({"discord_webhook": "bad"})
        mock_start.assert_called_once()


# ===================================================================
# NotificationBridge.send
# ===================================================================

class TestNotificationBridgeSend:

    def test_send_without_manager_returns_early(self):
        nb = NotificationBridge()
        assert nb._manager is None
        # Should not raise
        nb.send("info", "Title", "Message")

    def test_send_without_loop_returns_early(self):
        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = None
        # Should not raise
        nb.send("info", "Title", "Message")

    @patch("backend.notification_bridge.asyncio.run_coroutine_threadsafe")
    @patch("backend.notifications.NotificationType")
    def test_send_dispatches_to_async_loop(self, mock_type_enum, mock_threadsafe):
        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = MagicMock()

        nb.send("error", "Error Title", "Something broke", data={"detail": "oops"})

        mock_threadsafe.assert_called_once()
        # The first arg is the coroutine, second is the loop
        assert mock_threadsafe.call_args[0][1] is nb._loop

    @patch("backend.notification_bridge.asyncio.run_coroutine_threadsafe")
    def test_send_maps_type_names_correctly(self, mock_threadsafe):
        from backend.notifications import NotificationType

        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = MagicMock()

        type_map = {
            "scan_complete": NotificationType.SCAN_COMPLETE,
            "new_missing": NotificationType.NEW_MISSING,
            "new_upgrade": NotificationType.NEW_UPGRADE,
            "watchlist_found": NotificationType.WATCHLIST_FOUND,
            "error": NotificationType.ERROR,
            "info": NotificationType.INFO,
        }

        for type_name, expected_type in type_map.items():
            mock_threadsafe.reset_mock()
            nb.send(type_name, "T", "M")
            # Verify that manager.notify was called with the correct type
            nb._manager.notify.assert_called()
            call_args = nb._manager.notify.call_args
            assert call_args[0][0] == expected_type, f"Wrong type for {type_name}"

    @patch("backend.notification_bridge.asyncio.run_coroutine_threadsafe")
    def test_send_unknown_type_defaults_to_info(self, mock_threadsafe):
        from backend.notifications import NotificationType

        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = MagicMock()

        nb.send("unknown_type", "T", "M")
        call_args = nb._manager.notify.call_args
        assert call_args[0][0] == NotificationType.INFO

    @patch("backend.notification_bridge.asyncio.run_coroutine_threadsafe")
    def test_send_passes_data(self, mock_threadsafe):
        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = MagicMock()

        nb.send("info", "Title", "Msg", data={"key": "value"})
        call_kwargs = nb._manager.notify.call_args
        assert call_kwargs[1]["data"] == {"key": "value"}

    def test_send_swallows_exceptions(self):
        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = MagicMock()
        # Make the import inside send fail by patching
        with patch("backend.notification_bridge.asyncio.run_coroutine_threadsafe",
                    side_effect=RuntimeError("boom")):
            # Should not raise
            nb.send("info", "T", "M")


# ===================================================================
# NotificationBridge.notify_scan_complete
# ===================================================================

class TestNotificationBridgeNotifyScanComplete:

    def test_calls_send_with_correct_type(self):
        nb = NotificationBridge()
        nb.send = MagicMock()

        nb.notify_scan_complete(total=100, missing=5, upgrades=3)

        nb.send.assert_called_once()
        args, kwargs = nb.send.call_args
        assert args[0] == "scan_complete"

    def test_title_is_scan_complete(self):
        nb = NotificationBridge()
        nb.send = MagicMock()

        nb.notify_scan_complete(total=10)

        args, kwargs = nb.send.call_args
        assert args[1] == "Scan Complete"

    def test_message_contains_total(self):
        nb = NotificationBridge()
        nb.send = MagicMock()

        nb.notify_scan_complete(total=42, missing=3, upgrades=1)

        args, kwargs = nb.send.call_args
        assert "42" in args[2]

    def test_message_contains_missing_and_upgrades(self):
        nb = NotificationBridge()
        nb.send = MagicMock()

        nb.notify_scan_complete(total=100, missing=7, upgrades=2)

        args, kwargs = nb.send.call_args
        message = args[2]
        assert "7" in message
        assert "2" in message

    def test_data_dict_has_expected_keys(self):
        nb = NotificationBridge()
        nb.send = MagicMock()

        nb.notify_scan_complete(total=50, missing=10, upgrades=5)

        args, kwargs = nb.send.call_args
        data = args[3]
        assert data["total"] == 50
        assert data["missing"] == 10
        assert data["upgrades"] == 5

    def test_default_missing_and_upgrades(self):
        nb = NotificationBridge()
        nb.send = MagicMock()

        nb.notify_scan_complete(total=20)

        args, kwargs = nb.send.call_args
        data = args[3]
        assert data["missing"] == 0
        assert data["upgrades"] == 0


# ===================================================================
# NotificationBridge.notify_error
# ===================================================================

class TestNotificationBridgeNotifyError:

    def test_calls_send_with_error_type(self):
        nb = NotificationBridge()
        nb.send = MagicMock()

        nb.notify_error("something went wrong")

        nb.send.assert_called_once()
        args, kwargs = nb.send.call_args
        assert args[0] == "error"

    def test_title_is_mediascout_error(self):
        nb = NotificationBridge()
        nb.send = MagicMock()

        nb.notify_error("oops")

        args, kwargs = nb.send.call_args
        assert args[1] == "ScanHound Error"

    def test_message_is_passed_through(self):
        nb = NotificationBridge()
        nb.send = MagicMock()

        nb.notify_error("Disk full")

        args, kwargs = nb.send.call_args
        assert args[2] == "Disk full"


# ===================================================================
# NotificationBridge.shutdown
# ===================================================================

class TestNotificationBridgeShutdown:

    def test_shutdown_stops_loop(self):
        nb = NotificationBridge()
        mock_loop = MagicMock()
        nb._loop = mock_loop
        nb._manager = MagicMock()

        nb.shutdown()

        mock_loop.call_soon_threadsafe.assert_called_once_with(mock_loop.stop)

    def test_shutdown_calls_manager_shutdown(self):
        nb = NotificationBridge()
        mock_manager = MagicMock()
        nb._manager = mock_manager
        nb._loop = MagicMock()

        nb.shutdown()

        mock_manager.shutdown.assert_called_once()

    def test_shutdown_clears_manager_reference(self):
        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = MagicMock()

        nb.shutdown()

        assert nb._manager is None

    def test_shutdown_clears_loop_reference(self):
        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = MagicMock()

        nb.shutdown()

        assert nb._loop is None

    def test_shutdown_without_loop_does_not_crash(self):
        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = None
        nb.shutdown()
        assert nb._manager is None

    def test_shutdown_without_manager_does_not_crash(self):
        nb = NotificationBridge()
        nb._loop = None
        nb._manager = None
        nb.shutdown()

    def test_shutdown_handles_manager_shutdown_exception(self):
        nb = NotificationBridge()
        mock_manager = MagicMock()
        mock_manager.shutdown.side_effect = RuntimeError("cleanup failed")
        nb._manager = mock_manager
        nb._loop = MagicMock()

        # Should not raise
        nb.shutdown()
        assert nb._manager is None
        assert nb._loop is None

    def test_full_lifecycle(self):
        """Configure, send, and shutdown without errors."""
        nb = NotificationBridge()
        nb._manager = MagicMock()
        nb._loop = MagicMock()

        nb.send("info", "Hello", "World")
        nb.notify_scan_complete(total=10)
        nb.notify_error("test error")
        nb.shutdown()

        assert nb._manager is None
        assert nb._loop is None
