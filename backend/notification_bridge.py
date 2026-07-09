"""Notification Bridge — Synchronous wrapper for the async NotificationManager.

Provides a simple sync API for controllers to send notifications without
dealing with asyncio. Runs the async notification loop in a daemon thread.
"""

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class NotificationBridge:
    """Thread-safe sync wrapper around NotificationManager."""

    def __init__(self):
        self._manager = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def configure(self, config: Dict[str, Any]):
        """Initialize the notification manager from config.

        Creates a background event loop thread for async notification dispatch.
        """
        try:
            from backend.notifications import NotificationManager
        except ImportError:
            logger.debug("NotificationManager not available")
            return

        self._manager = NotificationManager()

        # Map config keys to notification channels
        notif_config = {}

        # Desktop — default OFF: ScanHound runs headless (Docker), where there
        # is no desktop notification backend. Aligns with the channel registry
        # default and prevents fresh installs from spamming gdbus errors.
        if config.get("desktop_notifications", False):
            notif_config["desktop_enabled"] = True

        # Discord
        discord_url = config.get("discord_webhook", "")
        if discord_url:
            notif_config["discord_webhook"] = discord_url
            notif_config["discord_username"] = config.get("discord_username", "ScanHound")

        # Slack
        slack_url = config.get("slack_webhook", "")
        if slack_url:
            notif_config["slack_webhook"] = slack_url

        # Email
        if config.get("email_enabled", False):
            notif_config["email_enabled"] = True
            for k in ("smtp_host", "smtp_port", "smtp_username", "smtp_password",
                       "email_from", "email_to", "smtp_tls"):
                if k in config:
                    notif_config[k] = config[k]

        # Pushover
        if config.get("pushover_user", ""):
            notif_config["pushover_user"] = config["pushover_user"]
            notif_config["pushover_token"] = config.get("pushover_token", "")

        # Webhook
        if config.get("webhook_url", ""):
            notif_config["webhook_url"] = config["webhook_url"]
            notif_config["webhook_method"] = config.get("webhook_method", "POST")

        try:
            self._manager.configure_from_dict(notif_config)
        except Exception as e:
            logger.warning(f"Failed to configure notifications: {e}")

        # Start async loop in background thread
        self._start_loop()
        logger.info("NotificationBridge configured")

    def _start_loop(self):
        """Start the background asyncio event loop."""
        if self._thread and self._thread.is_alive():
            return

        def _run():
            self._loop = asyncio.new_event_loop()
            # Signal ready only after the loop is actually running
            self._loop.call_soon(self._ready.set)
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, name="notif-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def send(self, type_name: str, title: str, message: str, data: Optional[Dict] = None):
        """Send a notification synchronously (dispatched to async loop).

        Args:
            type_name: One of 'scan_complete', 'new_missing', 'new_upgrade',
                       'watchlist_found', 'error', 'info'
            title: Notification title
            message: Notification body
            data: Optional extra data dict
        """
        if not self._manager or not self._loop:
            return

        try:
            from backend.notifications import NotificationType
            type_map = {
                "scan_complete": NotificationType.SCAN_COMPLETE,
                "new_missing": NotificationType.NEW_MISSING,
                "new_upgrade": NotificationType.NEW_UPGRADE,
                "watchlist_found": NotificationType.WATCHLIST_FOUND,
                "error": NotificationType.ERROR,
                "info": NotificationType.INFO,
            }
            notif_type = type_map.get(type_name, NotificationType.INFO)
            future = asyncio.run_coroutine_threadsafe(
                self._manager.notify(notif_type, title, message, data=data),
                self._loop,
            )
            # Don't block — fire and forget
        except Exception as e:
            logger.warning(f"Failed to send notification: {e}")

    def notify_scan_complete(self, total: int, missing: int = 0, upgrades: int = 0):
        """Convenience: send scan-complete notification."""
        self.send(
            "scan_complete",
            "Scan Complete",
            f"Found {total} items ({missing} missing, {upgrades} upgrades)",
            {"total": total, "missing": missing, "upgrades": upgrades},
        )

    def notify_error(self, message: str):
        """Convenience: send error notification."""
        self.send("error", "ScanHound Error", message)

    def shutdown(self):
        """Stop the async loop and cleanup."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._manager:
            try:
                self._manager.shutdown()
            except Exception:
                pass
        self._manager = None
        self._loop = None
