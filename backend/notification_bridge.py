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
            loop = asyncio.new_event_loop()
            self._loop = loop
            # Signal ready only after the loop is actually running
            loop.call_soon(self._ready.set)
            try:
                loop.run_forever()
            finally:
                # The loop thread owns the loop's whole lifecycle, including
                # its end. Nothing else ever called close() on this loop, so
                # its default executor — populated by the desktop-toast path's
                # run_in_executor(None, ...) — was never retired, and its
                # asyncio_N workers piled up one lifespan after another.
                #
                # Mirrors what asyncio.run() does on the way out. Draining the
                # async generators and the executor here, on the loop's own
                # thread after run_forever() returns, means no coroutine has to
                # be submitted from outside to a loop that may already have
                # stopped — the race the previous version had between
                # is_running() and run_coroutine_threadsafe().
                #
                # No timeout argument: it is 3.12-only, and this runs on the
                # loop thread, which shutdown() already bounds with a join.
                # A wedged toast backend therefore strands this thread rather
                # than shutdown.
                #
                # close() alone would ALSO retire the executor — it calls
                # shutdown(wait=False) — so the explicit drain is not what
                # fixes the leak; closing the loop at all is. What the drain
                # buys is determinism: the workers are gone when this thread
                # exits, rather than shortly after, which is what a leak check
                # sampling right at teardown needs. The cost is the wedged case
                # above, where the drain blocks and this thread is stranded
                # instead of exiting promptly. Deterministic on the common path
                # was judged worth one extra daemon thread on the pathological
                # one; test_a_wedged_drain_strands_the_thread_rather_than_shutdown
                # pins that behaviour so the trade is visible, not accidental.
                try:
                    # Cancel and gather outstanding tasks FIRST, the way
                    # asyncio.run() does. send() dispatches notify() as
                    # fire-and-forget and drops the future, so a webhook POST or
                    # a batch delay can still be in flight here. Closing the loop
                    # with those pending destroys them mid-await and emits
                    # "Task was destroyed but it is pending!".
                    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(
                            *pending, return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.run_until_complete(loop.shutdown_default_executor())
                except Exception:
                    logger.debug("notification loop drain failed; closing anyway",
                                 exc_info=True)
                finally:
                    loop.close()

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
        """Ask the loop thread to stop, then wait for it, bounded.

        Only signals and waits — the loop thread drains and closes the loop
        itself (see ``_start_loop``). That keeps the whole teardown on one
        thread and removes the previous version's window between checking
        ``is_running()`` and submitting a coroutine, where the loop could stop
        in between and strand the coroutine for the full wait.
        """
        if self._loop:
            # Safe even if the loop already stopped: call_soon_threadsafe on a
            # stopped-but-open loop just queues a callback nobody runs.
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                logger.debug("notification loop already closed", exc_info=True)
        if self._thread:
            # Bounds the drain above: a wedged notification backend strands
            # this thread instead of holding shutdown open.
            self._thread.join(timeout=2.0)
        if self._manager:
            try:
                self._manager.shutdown()
            except Exception:
                pass
        self._manager = None
        self._loop = None
