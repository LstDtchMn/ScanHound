"""Notification Bridge — Synchronous wrapper for the async NotificationManager.

Provides a simple sync API for controllers to send notifications without
dealing with asyncio. Runs the async notification loop in a daemon thread.
"""

import asyncio
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
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

        try:
            self._manager.configure_from_dict(self._build_notif_config(config))
        except Exception as e:
            logger.warning(f"Failed to configure notifications: {e}")

        # Start async loop in background thread
        self._start_loop()
        logger.info("NotificationBridge configured")

    def reconfigure(self, config: Dict[str, Any]):
        """Rebuild the channel list after the config changed at runtime.

        configure() snapshots scalars out of the config dict into channel
        objects, so mutating reg.config (what PUT /settings does) can never
        reach the live channels — without this the running app keeps the
        startup channel set until the process restarts, while the Settings
        "Test" button probes the config directly and reports success.

        Rebuilds on the EXISTING manager rather than re-running configure(),
        which would replace it and discard _history, _callbacks and any
        in-flight batch.
        """
        if self._manager is None:
            self.configure(config)
            return

        try:
            self._manager.clear_channels()
            self._manager.configure_from_dict(self._build_notif_config(config))
        except Exception as e:
            logger.warning(f"Failed to reconfigure notifications: {e}")
            return

        # No-op when the loop thread is already alive; covers a bridge whose
        # thread died.
        self._start_loop()
        logger.info("NotificationBridge reconfigured")

    def _build_notif_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Map ScanHound config keys onto NotificationManager channel keys."""
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

        return notif_config

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

    def notify_error_confirmed(self, message: str, timeout: float = 15.0) -> bool:
        """Send an error alert and WAIT to find out whether it was delivered.

        Separate from :meth:`notify_error` on purpose. Ordinary notifications
        stay fire-and-forget so nothing in the hot path waits on an SMTP round
        trip; this variant exists for the one caller that must not discard
        state without proof — the database-corruption flag, which marks a
        total-history-loss event as "notified" exactly once.

        Returns True only when at least one channel accepted it. False covers
        every other outcome, all of which mean the same thing to the caller:
        do not throw the evidence away yet. That includes an unconfigured
        bridge, a dead loop, a timeout, a raised channel, no channel willing to
        handle the type, and a BATCHED send (whose delivery has not happened
        yet, and which reports None rather than a count).

        ONE confirming channel is enough (first_success_wins). Waiting for every
        selected channel meant the slowest one decided the answer: EmailChannel
        runs blocking smtplib, so a wedged SMTP server could burn the whole
        ``timeout`` while a Discord webhook had already delivered in 200ms, and
        this would report False for an alert the operator had actually received
        -- consuming a retry and duplicating the alert on the next boot.
        """
        if not self._manager or not self._loop:
            return False
        try:
            from backend.notifications import NotificationType
            future = asyncio.run_coroutine_threadsafe(
                self._manager.notify(
                    NotificationType.ERROR, "ScanHound Error", message,
                    first_success_wins=True),
                self._loop,
            )
            delivered = future.result(timeout=timeout)
        except FutureTimeoutError:
            # Deliberately NOT cancelled. Reaching here means NO channel has
            # confirmed yet, so cancelling would guarantee the alert is never
            # delivered; letting the sends finish may still get it out. The
            # caller retries either way, so the cost of not cancelling is at
            # worst a duplicate alert -- the cheaper mistake for a
            # total-history-loss event. The abandoned work is no longer
            # unbounded: each channel send is capped by
            # DEFAULT_CHANNEL_SEND_TIMEOUT and smtplib now carries an explicit
            # socket timeout, so it ends on its own. (Cancelling could not have
            # stopped an smtplib call already running in the executor anyway.)
            logger.warning(
                "Confirmed error notification not observed within %.1fs; "
                "treating as undelivered", timeout)
            return False
        except Exception as e:  # noqa: BLE001 - a failed alert is not fatal
            logger.warning(f"Confirmed error notification failed: {e}")
            return False
        return bool(delivered) and delivered is not None

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
