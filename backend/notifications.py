"""Notification System - Multi-channel notifications for scan events.

Supports:
- Desktop notifications (cross-platform)
- Webhooks (Discord, Slack, generic)
- Email notifications
- Notification history and batching
"""

import asyncio
import aiohttp
import html as html_lib
import json
import logging
import shutil
import smtplib
import string
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
from queue import Queue

logger = logging.getLogger(__name__)


class NotificationType(Enum):
    """Types of notifications."""
    SCAN_COMPLETE = "scan_complete"
    NEW_MISSING = "new_missing"
    NEW_UPGRADE = "new_upgrade"
    WATCHLIST_FOUND = "watchlist_found"
    ERROR = "error"
    INFO = "info"


class NotificationPriority(int, Enum):
    """Notification priority levels. Uses int mixin for comparison support."""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


@dataclass
class Notification:
    """A notification to be sent."""
    type: NotificationType
    title: str
    message: str
    priority: NotificationPriority = NotificationPriority.NORMAL
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    id: str = field(default_factory=lambda: f"notif_{uuid.uuid4().hex[:12]}")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'id': self.id,
            'type': self.type.value,
            'title': self.title,
            'message': self.message,
            'priority': self.priority.value,
            'data': self.data,
            'timestamp': self.timestamp.isoformat()
        }


class NotificationChannel(ABC):
    """Abstract base class for notification channels."""

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled
        self._filters: Set[NotificationType] = set(NotificationType)

    def set_filters(self, types: List[NotificationType]):
        """Set which notification types this channel handles."""
        self._filters = set(types)

    def should_handle(self, notification: Notification) -> bool:
        """Check if this channel should handle the notification."""
        return self.enabled and notification.type in self._filters

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Send a notification. Returns True on success."""
        pass

    async def _post_webhook(self, url: str, payload: dict,
                            expected: tuple = (200, 204),
                            use_data: bool = False,
                            method: str = "POST",
                            headers: dict = None) -> bool:
        """Common webhook POST helper. Returns True on success."""
        try:
            async with aiohttp.ClientSession() as session:
                kwargs = {"timeout": aiohttp.ClientTimeout(total=10)}
                if use_data:
                    kwargs["data"] = payload
                else:
                    kwargs["json"] = payload
                if headers:
                    kwargs["headers"] = headers
                async with session.request(method, url, **kwargs) as response:
                    if response.status in expected:
                        logger.debug(f"{self.name} notification sent")
                        return True
                    logger.error(f"{self.name} webhook failed: {response.status}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send {self.name} notification: {e}")
            return False


class DesktopNotificationChannel(NotificationChannel):
    """Desktop notifications using plyer or native fallbacks."""

    def __init__(self, app_name: str = "ScanHound"):
        super().__init__("desktop")
        self.app_name = app_name
        self._notifier = self._get_notifier()

    def _get_notifier(self) -> Optional[Callable]:
        """Get the appropriate notifier for the platform."""
        try:
            from plyer import notification as plyer_notification
        except ImportError:
            logger.warning("plyer not installed, desktop notifications disabled")
            return None
        # In a headless container plyer's Linux backend shells out to gdbus /
        # notify-send, which aren't installed → every send raises
        # FileNotFoundError and spams ERROR logs. Disable the channel when
        # neither backend exists. (DISPLAY is set for Xvfb, so it can't gate
        # this — probe the actual binaries.)
        if sys.platform.startswith("linux") and not (
                shutil.which("gdbus") or shutil.which("notify-send")):
            logger.info("No desktop notification backend (gdbus/notify-send) "
                        "present — desktop notifications disabled")
            return None
        return plyer_notification.notify

    async def send(self, notification: Notification) -> bool:
        """Send desktop notification."""
        if not self._notifier:
            return False

        try:
            # Run in executor to avoid blocking
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self._notifier(
                    title=notification.title,
                    message=notification.message,
                    app_name=self.app_name,
                    timeout=10
                )
            )
            logger.debug(f"Desktop notification sent: {notification.title}")
            return True
        except FileNotFoundError as e:
            # The notification backend vanished (headless host) — self-disable so
            # every subsequent send doesn't re-raise and spam the log. Belt-and-
            # suspenders behind the _get_notifier probe.
            self._notifier = None
            logger.debug(f"Desktop notification backend missing; disabling channel: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send desktop notification: {e}")
            return False


class DiscordWebhookChannel(NotificationChannel):
    """Discord webhook notifications."""

    COLORS = {
        NotificationType.SCAN_COMPLETE: 0x2ECC71,  # Green
        NotificationType.NEW_MISSING: 0xE74C3C,    # Red
        NotificationType.NEW_UPGRADE: 0xF39C12,    # Orange
        NotificationType.WATCHLIST_FOUND: 0x9B59B6,  # Purple
        NotificationType.ERROR: 0xE74C3C,          # Red
        NotificationType.INFO: 0x3498DB,           # Blue
    }

    def __init__(self, webhook_url: str, username: str = "ScanHound"):
        super().__init__("discord")
        self.webhook_url = webhook_url
        self.username = username

    def _build_embed(self, notification: Notification) -> Dict[str, Any]:
        """Build Discord embed from notification."""
        embed = {
            "title": notification.title,
            "description": notification.message,
            "color": self.COLORS.get(notification.type, 0x7289DA),
            "timestamp": notification.timestamp.isoformat(),
            "footer": {"text": f"Priority: {notification.priority.name}"}
        }

        # Add fields from data
        if notification.data:
            fields = []
            for key, value in notification.data.items():
                if key in ('items', 'upgrades', 'missing'):
                    # Format lists
                    if isinstance(value, list) and value:
                        formatted = "\n".join(f"• {item}" for item in value[:10])
                        if len(value) > 10:
                            formatted += f"\n... and {len(value) - 10} more"
                        fields.append({
                            "name": key.replace('_', ' ').title(),
                            "value": formatted,
                            "inline": False
                        })
                elif isinstance(value, (str, int, float)):
                    fields.append({
                        "name": key.replace('_', ' ').title(),
                        "value": str(value),
                        "inline": True
                    })
            embed["fields"] = fields[:25]  # Discord limit

        return embed

    async def send(self, notification: Notification) -> bool:
        """Send Discord webhook notification."""
        payload = {
            "username": self.username,
            "embeds": [self._build_embed(notification)]
        }
        return await self._post_webhook(self.webhook_url, payload)


class SlackWebhookChannel(NotificationChannel):
    """Slack webhook notifications."""

    EMOJI = {
        NotificationType.SCAN_COMPLETE: ":white_check_mark:",
        NotificationType.NEW_MISSING: ":x:",
        NotificationType.NEW_UPGRADE: ":arrow_up:",
        NotificationType.WATCHLIST_FOUND: ":star:",
        NotificationType.ERROR: ":warning:",
        NotificationType.INFO: ":information_source:",
    }

    def __init__(self, webhook_url: str):
        super().__init__("slack")
        self.webhook_url = webhook_url

    def _build_blocks(self, notification: Notification) -> List[Dict[str, Any]]:
        """Build Slack blocks from notification."""
        emoji = self.EMOJI.get(notification.type, ":bell:")
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} {notification.title}",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": notification.message
                }
            }
        ]

        # Add data fields
        if notification.data:
            fields = []
            for key, value in list(notification.data.items())[:10]:
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value[:5])
                    if len(notification.data.get(key, [])) > 5:
                        value += "..."
                fields.append({
                    "type": "mrkdwn",
                    "text": f"*{key.replace('_', ' ').title()}:* {value}"
                })

            if fields:
                blocks.append({
                    "type": "section",
                    "fields": fields[:10]
                })

        # Add timestamp
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Sent at {notification.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
                }
            ]
        })

        return blocks

    async def send(self, notification: Notification) -> bool:
        """Send Slack webhook notification."""
        payload = {
            "blocks": self._build_blocks(notification)
        }

        return await self._post_webhook(self.webhook_url, payload, expected=(200,))


class GenericWebhookChannel(NotificationChannel):
    """Generic webhook for custom integrations."""

    def __init__(
        self,
        webhook_url: str,
        method: str = "POST",
        headers: Optional[Dict[str, str]] = None,
        template: Optional[Dict[str, Any]] = None
    ):
        super().__init__("webhook")
        self.webhook_url = webhook_url
        self.method = method
        self.headers = headers or {"Content-Type": "application/json"}
        self.template = template

    async def send(self, notification: Notification) -> bool:
        """Send generic webhook notification."""
        payload = self._apply_template(notification) if self.template else notification.to_dict()
        return await self._post_webhook(
            self.webhook_url, payload,
            expected=tuple(range(200, 300)),
            method=self.method, headers=self.headers,
        )

    def _apply_template(self, notification: Notification) -> Dict[str, Any]:
        """Apply template with notification data using safe substitution."""
        import copy
        result = copy.deepcopy(self.template)

        # Build safe substitution dict with only string values
        safe_vars = {
            'title': str(notification.title),
            'message': str(notification.message),
            'type': notification.type.value,
            'priority': notification.priority.name,
            'timestamp': notification.timestamp.isoformat(),
        }
        # Only include safe string/number values from data (no objects)
        for k, v in notification.data.items():
            if isinstance(k, str) and k.isidentifier() and isinstance(v, (str, int, float, bool)):
                safe_vars[k] = str(v)

        def replace_vars(obj):
            if isinstance(obj, str):
                try:
                    return string.Template(obj).safe_substitute(safe_vars)
                except (ValueError, KeyError):
                    return obj
            elif isinstance(obj, dict):
                return {k: replace_vars(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [replace_vars(item) for item in obj]
            return obj

        return replace_vars(result)


class EmailChannel(NotificationChannel):
    """Email notifications via SMTP."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: List[str],
        use_tls: bool = True
    ):
        super().__init__("email")
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        self.use_tls = use_tls

    def _build_email(self, notification: Notification) -> MIMEMultipart:
        """Build email message."""
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[ScanHound] {notification.title}"
        msg['From'] = self.from_addr
        msg['To'] = ", ".join(self.to_addrs)

        # Plain text version
        text = f"{notification.title}\n\n{notification.message}"
        if notification.data:
            text += "\n\nDetails:\n"
            for key, value in notification.data.items():
                text += f"- {key}: {value}\n"

        # HTML version
        safe_title = html_lib.escape(str(notification.title))
        safe_message = html_lib.escape(str(notification.message))
        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #2c3e50; color: white; padding: 20px; border-radius: 5px 5px 0 0;">
                <h2 style="margin: 0;">{safe_title}</h2>
            </div>
            <div style="background: #ecf0f1; padding: 20px; border-radius: 0 0 5px 5px;">
                <p style="font-size: 16px; color: #2c3e50;">{safe_message}</p>
        """

        if notification.data:
            html += '<table style="width: 100%; border-collapse: collapse; margin-top: 15px;">'
            for key, value in notification.data.items():
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value[:10])
                safe_key = html_lib.escape(str(key).replace('_', ' ').title())
                safe_value = html_lib.escape(str(value))
                html += f'''
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #bdc3c7; font-weight: bold;">
                        {safe_key}
                    </td>
                    <td style="padding: 8px; border-bottom: 1px solid #bdc3c7;">{safe_value}</td>
                </tr>
                '''
            html += '</table>'

        html += f"""
                <p style="color: #7f8c8d; font-size: 12px; margin-top: 20px;">
                    Sent at {notification.timestamp.strftime('%Y-%m-%d %H:%M:%S')}
                </p>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(text, 'plain'))
        msg.attach(MIMEText(html, 'html'))

        return msg

    async def send(self, notification: Notification) -> bool:
        """Send email notification."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._send_sync, notification)
            logger.debug(f"Email notification sent: {notification.title}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email notification: {e}")
            return False

    def _send_sync(self, notification: Notification):
        """Synchronous email sending."""
        msg = self._build_email(notification)

        if self.use_tls:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.sendmail(self.from_addr, self.to_addrs, msg.as_string())
        else:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) as server:
                server.login(self.username, self.password)
                server.sendmail(self.from_addr, self.to_addrs, msg.as_string())


class PushoverChannel(NotificationChannel):
    """Pushover push notifications."""

    PRIORITY_MAP = {
        NotificationPriority.LOW: -1,
        NotificationPriority.NORMAL: 0,
        NotificationPriority.HIGH: 1,
        NotificationPriority.URGENT: 2
    }

    def __init__(self, user_key: str, api_token: str):
        super().__init__("pushover")
        self.user_key = user_key
        self.api_token = api_token
        self.api_url = "https://api.pushover.net/1/messages.json"

    async def send(self, notification: Notification) -> bool:
        """Send Pushover notification."""
        payload = {
            "token": self.api_token,
            "user": self.user_key,
            "title": notification.title,
            "message": notification.message,
            "priority": self.PRIORITY_MAP.get(notification.priority, 0),
            "timestamp": int(notification.timestamp.timestamp())
        }
        return await self._post_webhook(self.api_url, payload, expected=(200,), use_data=True)


class NotificationManager:
    """Manages notification channels and delivery."""

    def __init__(self, batch_delay: float = 5.0):
        self._channels: List[NotificationChannel] = []
        self._history: List[Notification] = []
        self._max_history = 100
        self._batch_delay = batch_delay
        self._pending: List[Notification] = []
        self._batch_timer: Optional[asyncio.Task] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._callbacks: List[Callable[[Notification], None]] = []

    def add_channel(self, channel: NotificationChannel):
        """Add a notification channel."""
        self._channels.append(channel)
        logger.info(f"Added notification channel: {channel.name}")

    def remove_channel(self, name: str):
        """Remove a notification channel by name."""
        self._channels = [c for c in self._channels if c.name != name]

    def add_callback(self, callback: Callable[[Notification], None]):
        """Add a callback to be called when notifications are sent."""
        self._callbacks.append(callback)

    def configure_from_dict(self, config: Dict[str, Any]):
        """Configure channels from a configuration dictionary."""
        # Desktop notifications
        if config.get('desktop_enabled', False):
            self.add_channel(DesktopNotificationChannel())

        # Discord
        if config.get('discord_webhook'):
            channel = DiscordWebhookChannel(
                config['discord_webhook'],
                config.get('discord_username', 'ScanHound')
            )
            if config.get('discord_types'):
                channel.set_filters([
                    NotificationType[t] for t in config['discord_types']
                ])
            self.add_channel(channel)

        # Slack
        if config.get('slack_webhook'):
            channel = SlackWebhookChannel(config['slack_webhook'])
            if config.get('slack_types'):
                channel.set_filters([
                    NotificationType[t] for t in config['slack_types']
                ])
            self.add_channel(channel)

        # Email
        if config.get('email_enabled') and config.get('smtp_host'):
            channel = EmailChannel(
                smtp_host=config['smtp_host'],
                smtp_port=config.get('smtp_port', 587),
                username=config.get('smtp_username', ''),
                password=config.get('smtp_password', ''),
                from_addr=config.get('email_from', ''),
                to_addrs=config.get('email_to', []),
                use_tls=config.get('smtp_tls', True)
            )
            if config.get('email_types'):
                channel.set_filters([
                    NotificationType[t] for t in config['email_types']
                ])
            self.add_channel(channel)

        # Pushover
        if config.get('pushover_user') and config.get('pushover_token'):
            channel = PushoverChannel(
                config['pushover_user'],
                config['pushover_token']
            )
            if config.get('pushover_types'):
                channel.set_filters([
                    NotificationType[t] for t in config['pushover_types']
                ])
            self.add_channel(channel)

        # Generic webhook
        if config.get('webhook_url'):
            channel = GenericWebhookChannel(
                config['webhook_url'],
                config.get('webhook_method', 'POST'),
                config.get('webhook_headers'),
                config.get('webhook_template')
            )
            self.add_channel(channel)

    async def notify(
        self,
        type: NotificationType,
        title: str,
        message: str,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        data: Optional[Dict[str, Any]] = None,
        batch: bool = False
    ):
        """Send a notification to all configured channels.

        Args:
            type: Notification type
            title: Notification title
            message: Notification message
            priority: Priority level
            data: Additional data
            batch: If True, batch with other notifications
        """
        notification = Notification(
            type=type,
            title=title,
            message=message,
            priority=priority,
            data=data or {}
        )

        if batch:
            await self._add_to_batch(notification)
        else:
            await self._send_notification(notification)

    def send_notification(
        self,
        notification_type: NotificationType,
        title: str,
        message: str,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        data: Optional[Dict[str, Any]] = None
    ):
        """Synchronous wrapper for notify (compatibility layer)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.notify(notification_type, title, message, priority, data),
                loop
            )
        else:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    self.notify(notification_type, title, message, priority, data)
                )
            finally:
                loop.close()

    async def _add_to_batch(self, notification: Notification):
        """Add notification to batch queue."""
        async with self._lock:
            self._pending.append(notification)

            if self._batch_timer is None:
                self._batch_timer = asyncio.create_task(self._batch_sender())

    async def _batch_sender(self):
        """Send batched notifications after delay."""
        await asyncio.sleep(self._batch_delay)

        async with self._lock:
            if not self._pending:
                self._batch_timer = None
                return

            # Combine similar notifications
            combined = self._combine_notifications(self._pending)
            self._pending.clear()
            self._batch_timer = None

        for notification in combined:
            await self._send_notification(notification)

    def _combine_notifications(
        self,
        notifications: List[Notification]
    ) -> List[Notification]:
        """Combine similar notifications into batches."""
        by_type: Dict[NotificationType, List[Notification]] = {}
        for n in notifications:
            by_type.setdefault(n.type, []).append(n)

        combined = []
        for type, notifs in by_type.items():
            if len(notifs) == 1:
                combined.append(notifs[0])
            else:
                # Combine into summary
                titles = [n.title for n in notifs]
                all_data = {}
                for n in notifs:
                    all_data.update(n.data)

                combined.append(Notification(
                    type=type,
                    title=f"{len(notifs)} {type.value.replace('_', ' ').title()} Notifications",
                    message="\n".join(f"• {t}" for t in titles[:10]),
                    priority=max(n.priority for n in notifs),
                    data=all_data
                ))

        return combined

    async def _send_notification(self, notification: Notification):
        """Send notification to all applicable channels."""
        # Add to history
        self._history.append(notification)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # Call callbacks
        for callback in self._callbacks:
            try:
                callback(notification)
            except Exception as e:
                logger.error(f"Notification callback error: {e}")

        # Send to channels
        tasks = []
        for channel in self._channels:
            if channel.should_handle(notification):
                tasks.append(channel.send(notification))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            successes = sum(1 for r in results if r is True)
            logger.debug(f"Notification sent to {successes}/{len(tasks)} channels")

    def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get notification history."""
        return [n.to_dict() for n in self._history[-limit:]]

    # Convenience methods for common notifications
    async def notify_scan_complete(
        self,
        items_scanned: int,
        missing_count: int,
        upgrade_count: int,
        duration: float
    ):
        """Send scan complete notification."""
        await self.notify(
            NotificationType.SCAN_COMPLETE,
            "Scan Complete",
            f"Scanned {items_scanned} items in {duration:.1f}s",
            data={
                'items_scanned': items_scanned,
                'missing': missing_count,
                'upgrades': upgrade_count,
                'duration': f"{duration:.1f}s"
            }
        )

    async def notify_watchlist_found(self, items: List[Dict[str, Any]]):
        """Send notification when watchlist items are found."""
        if not items:
            return

        titles = [item.get('display_title', 'Unknown') for item in items]
        await self.notify(
            NotificationType.WATCHLIST_FOUND,
            f"Watchlist Items Found ({len(items)})",
            f"Found: {', '.join(titles[:5])}" + ("..." if len(titles) > 5 else ""),
            priority=NotificationPriority.HIGH,
            data={'items': titles}
        )

    async def notify_error(self, error: str, details: Optional[str] = None):
        """Send error notification."""
        await self.notify(
            NotificationType.ERROR,
            "Error Occurred",
            error,
            priority=NotificationPriority.HIGH,
            data={'details': details} if details else {}
        )

    def shutdown(self):
        """Shutdown notification manager and cleanup resources."""
        # Cancel any pending batch timer
        if self._batch_timer and not self._batch_timer.done():
            self._batch_timer.cancel()

        # Clear pending notifications
        self._pending.clear()

        # Close any channel connections
        for channel in self._channels:
            if hasattr(channel, 'close'):
                try:
                    channel.close()
                except Exception:
                    pass

        logger.info("Notification manager shutdown complete")


# Global notification manager instance
_notification_manager: Optional[NotificationManager] = None


_notification_lock = threading.Lock()


def get_notification_manager() -> NotificationManager:
    """Get the global notification manager (thread-safe)."""
    global _notification_manager
    if _notification_manager is None:
        with _notification_lock:
            if _notification_manager is None:
                _notification_manager = NotificationManager()
    return _notification_manager


def configure_notifications(config: Dict[str, Any]):
    """Configure the global notification manager."""
    manager = get_notification_manager()
    manager.configure_from_dict(config)
