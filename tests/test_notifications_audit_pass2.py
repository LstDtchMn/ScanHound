"""Regression tests for the audit pass 2 notification findings.

Covers three confirmed defects, each of which failed SILENTLY in production:

- #5  settings.py:310 — notification settings never reached the running
      NotificationBridge (channels are built from a startup config snapshot),
      while the Settings "Test" button probed reg.config directly and reported
      success.
- #23 notifications.py:574 — email_to is a str in the config schema but
      EmailChannel treated it as a list, producing a per-character To: header
      and a single malformed envelope recipient for multi-address values.
- #24 notifications.py:339 — webhook_method="GET" sent the payload as a JSON
      request body, which receivers ignore, yet any 2xx counted as success.

Every transport is mocked; nothing here sends a real email, webhook or
notification.
"""

import asyncio
import json
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.notification_bridge import NotificationBridge
from backend.notifications import (
    DiscordWebhookChannel,
    EmailChannel,
    GenericWebhookChannel,
    Notification,
    NotificationManager,
    NotificationPriority,
    NotificationType,
    PushoverChannel,
    _normalize_addrs,
)


# ===================================================================
# Test doubles — no real transport is ever touched
# ===================================================================

class _RecordingSMTP:
    """Stand-in for smtplib.SMTP / SMTP_SSL.

    Replicates the one behaviour that hid finding #23: smtplib wraps a bare
    string to_addrs into a SINGLE envelope recipient rather than rejecting it,
    so a malformed value is delivered to a nonsense address instead of raising.
    """

    calls = []

    def __init__(self, host, port, *a, **k):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, from_addr, to_addrs, msg):
        if isinstance(to_addrs, str):
            to_addrs = [to_addrs]
        _RecordingSMTP.calls.append({
            "from": from_addr,
            "rcpt": list(to_addrs),
            "raw": msg,
        })


def _make_recording_session(status=200):
    """Return (FakeSessionClass, calls list) for patching aiohttp.ClientSession."""
    calls = []

    class _Resp:
        def __init__(self):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def request(self, method, url, **kwargs):
            calls.append({
                "method": method,
                "url": url,
                "kwargs": {k: v for k, v in kwargs.items() if k != "timeout"},
            })
            return _Resp()

    return _Session, calls


def _notification(**kw):
    kw.setdefault("type", NotificationType.SCAN_COMPLETE)
    kw.setdefault("title", "Scan Complete")
    kw.setdefault("message", "Found 3 items")
    return Notification(**kw)


# ===================================================================
# Finding #23 — email_to string vs list
# ===================================================================

class TestEmailRecipientNormalization:
    """email_to is typed `str` in config.py but consumed as List[str]."""

    def setup_method(self):
        _RecordingSMTP.calls = []

    def _channel(self, to_addrs):
        return EmailChannel(
            smtp_host="smtp.example.com", smtp_port=587,
            username="u", password="p",
            from_addr="scanhound@example.com",
            to_addrs=to_addrs,
        )

    # -- single address (the common case) ----------------------------------

    def test_single_address_string_becomes_one_element_list(self):
        ch = self._channel("jesse@example.com")
        assert ch.to_addrs == ["jesse@example.com"]

    def test_single_address_string_to_header_is_the_address(self):
        # Pre-fix this was 'j, e, s, s, e, @, ...'. Assert exact equality
        # rather than a substring check: 'jesse@example.com' is NOT a
        # substring of the per-character form, but a weaker check like
        # `"jesse" in header` would have passed the buggy implementation.
        ch = self._channel("jesse@example.com")
        msg = ch._build_email(_notification())
        assert msg["To"] == "jesse@example.com"

    # -- comma-separated multi-address (a free-text field invites this) -----

    def test_comma_separated_string_splits_into_separate_addresses(self):
        ch = self._channel("jesse@example.com, alerts@example.com")
        assert ch.to_addrs == ["jesse@example.com", "alerts@example.com"]

    def test_comma_separated_string_to_header_is_rfc_correct(self):
        ch = self._channel("jesse@example.com, alerts@example.com")
        msg = ch._build_email(_notification())
        assert msg["To"] == "jesse@example.com, alerts@example.com"

    def test_semicolon_separated_string_also_splits(self):
        ch = self._channel("jesse@example.com;alerts@example.com")
        assert ch.to_addrs == ["jesse@example.com", "alerts@example.com"]

    def test_comma_separated_produces_two_envelope_recipients(self):
        # THE disagreeing case. An implementation that only wrapped the string
        # (`[to_addrs]` when isinstance str) fixes the To: header for a single
        # address and still passes every single-address test above — but it
        # hands smtplib ONE recipient 'a@x, b@x', which is the 501 the finding
        # describes. Splitting is what makes this assertion hold.
        ch = self._channel("jesse@example.com, alerts@example.com")
        with patch("backend.notifications.smtplib.SMTP", _RecordingSMTP):
            ch._send_sync(_notification())
        assert _RecordingSMTP.calls[0]["rcpt"] == [
            "jesse@example.com", "alerts@example.com"
        ]

    # -- back-compat: existing callers pass a real list --------------------

    def test_list_input_is_preserved(self):
        ch = self._channel(["admin@example.com", "ops@example.com"])
        assert ch.to_addrs == ["admin@example.com", "ops@example.com"]

    def test_list_input_to_header_unchanged(self):
        ch = self._channel(["admin@example.com", "ops@example.com"])
        msg = ch._build_email(_notification())
        assert msg["To"] == "admin@example.com, ops@example.com"

    # -- degenerate values --------------------------------------------------

    def test_empty_string_yields_no_recipients(self):
        assert self._channel("").to_addrs == []

    def test_none_yields_no_recipients(self):
        assert self._channel(None).to_addrs == []

    def test_whitespace_and_empty_segments_are_dropped(self):
        ch = self._channel(" jesse@example.com , , alerts@example.com ")
        assert ch.to_addrs == ["jesse@example.com", "alerts@example.com"]

    # -- POSITIVE CONTROL ---------------------------------------------------

    def test_positive_control_single_recipient_still_sends(self):
        """A correctly configured email channel must still deliver.

        Guards against a "fix" that broke sending outright — every
        failure-only assertion above would still pass in that case.
        """
        ch = self._channel("jesse@example.com")
        with patch("backend.notifications.smtplib.SMTP", _RecordingSMTP):
            sent = asyncio.run(ch.send(_notification()))
        assert sent is True
        assert len(_RecordingSMTP.calls) == 1
        call = _RecordingSMTP.calls[0]
        assert call["from"] == "scanhound@example.com"
        assert call["rcpt"] == ["jesse@example.com"]
        assert "To: jesse@example.com" in call["raw"]
        assert "[ScanHound] Scan Complete" in call["raw"]

    def test_positive_control_non_tls_path_still_sends(self):
        # The SMTP_SSL branch takes the same to_addrs; prove the fix reaches
        # both sendmail call sites.
        ch = EmailChannel("smtp.example.com", 465, "u", "p",
                          "scanhound@example.com", "jesse@example.com",
                          use_tls=False)
        with patch("backend.notifications.smtplib.SMTP_SSL", _RecordingSMTP):
            sent = asyncio.run(ch.send(_notification()))
        assert sent is True
        assert _RecordingSMTP.calls[0]["rcpt"] == ["jesse@example.com"]


class TestNormalizeAddrsHelper:

    @pytest.mark.parametrize("value,expected", [
        ("a@x.com", ["a@x.com"]),
        ("a@x.com,b@x.com", ["a@x.com", "b@x.com"]),
        ("a@x.com; b@x.com", ["a@x.com", "b@x.com"]),
        (["a@x.com"], ["a@x.com"]),
        ([], []),
        ("", []),
        ("   ", []),
        (None, []),
    ])
    def test_normalize(self, value, expected):
        assert _normalize_addrs(value) == expected


class TestEmailChannelViaConfigureFromDict:
    """The production path: config dict -> configure_from_dict -> channel."""

    def test_str_email_to_from_config_reaches_channel_as_list(self):
        mgr = NotificationManager()
        mgr.configure_from_dict({
            "email_enabled": True,
            "smtp_host": "smtp.example.com",
            "email_from": "scanhound@example.com",
            # config.py declares this as a plain str — this is the real shape
            "email_to": "jesse@example.com, alerts@example.com",
        })
        channel = [c for c in mgr._channels if c.name == "email"][0]
        assert channel.to_addrs == ["jesse@example.com", "alerts@example.com"]


# ===================================================================
# Finding #24 — GET webhook payload must be a query string, not a body
# ===================================================================

class TestWebhookMethodPayloadPlacement:

    def _send(self, channel, notification=None, status=200):
        Session, calls = _make_recording_session(status)
        with patch("backend.notifications.aiohttp.ClientSession", Session):
            result = asyncio.run(channel.send(notification or _notification()))
        return result, calls

    # -- bodyless verbs -----------------------------------------------------

    def test_get_uses_query_string_not_json_body(self):
        ch = GenericWebhookChannel("https://example.com/hook", "GET")
        ok, calls = self._send(ch)
        assert ok is True
        kwargs = calls[0]["kwargs"]
        assert "params" in kwargs
        # The whole point of the finding: no body on a GET.
        assert "json" not in kwargs
        assert "data" not in kwargs

    def test_get_query_string_carries_the_payload(self):
        ch = GenericWebhookChannel("https://example.com/hook", "GET")
        _, calls = self._send(ch, _notification(title="Scan Complete",
                                                message="Found 3 items"))
        params = calls[0]["kwargs"]["params"]
        assert params["title"] == "Scan Complete"
        assert params["message"] == "Found 3 items"
        assert params["type"] == "scan_complete"

    def test_get_lowercase_method_is_normalized(self):
        ch = GenericWebhookChannel("https://example.com/hook", "get")
        _, calls = self._send(ch)
        assert "params" in calls[0]["kwargs"]

    @pytest.mark.parametrize("method", ["HEAD", "DELETE"])
    def test_other_bodyless_verbs_use_query_string(self, method):
        ch = GenericWebhookChannel("https://example.com/hook", method)
        _, calls = self._send(ch)
        assert "params" in calls[0]["kwargs"]
        assert "json" not in calls[0]["kwargs"]

    # -- body verbs must be UNCHANGED (disagreeing cases) -------------------

    def test_post_still_sends_a_json_body(self):
        # POSITIVE CONTROL for the default, overwhelmingly common config.
        ch = GenericWebhookChannel("https://example.com/hook", "POST")
        ok, calls = self._send(ch)
        assert ok is True
        kwargs = calls[0]["kwargs"]
        assert "json" in kwargs
        assert "params" not in kwargs
        assert kwargs["json"]["title"] == "Scan Complete"

    def test_put_still_sends_a_json_body(self):
        # Disagreeing case: an implementation that routed everything except
        # POST to the query string would pass every GET assertion above and
        # silently break PUT, which IS a body verb and is a selectable value
        # in the UI (config.py Literal["POST","GET","PUT"]).
        ch = GenericWebhookChannel("https://example.com/hook", "PUT")
        ok, calls = self._send(ch)
        assert ok is True
        assert "json" in calls[0]["kwargs"]
        assert "params" not in calls[0]["kwargs"]

    def test_default_method_is_post_with_body(self):
        ch = GenericWebhookChannel("https://example.com/hook")
        _, calls = self._send(ch)
        assert calls[0]["method"] == "POST"
        assert "json" in calls[0]["kwargs"]

    # -- params must be encodable by the REAL consumer ---------------------

    def test_get_params_contain_no_types_aiohttp_rejects(self):
        # Notification.to_dict()['data'] is a dict and 'priority' is an int;
        # a naive `kwargs["params"] = payload` passes a mocked session happily
        # but raises TypeError against real aiohttp. Assert the value types
        # directly so the mock cannot hide it.
        ch = GenericWebhookChannel("https://example.com/hook", "GET")
        _, calls = self._send(ch, _notification(
            data={"missing": 3, "items": ["A", "B"], "nested": {"k": "v"}},
            priority=NotificationPriority.HIGH,
        ))
        params = calls[0]["kwargs"]["params"]
        for key, value in params.items():
            assert not isinstance(value, bool), f"{key} is a bool"
            assert isinstance(value, (str, int, float)), \
                f"{key} is {type(value).__name__}, which aiohttp rejects"

    def test_get_params_are_accepted_by_yarl_the_real_encoder(self):
        # Verify the CONSUMER, not just the shape: aiohttp hands `params` to
        # yarl.URL.with_query, which is what actually raises on a dict/bool
        # value. Exercising it here proves the flattening is sufficient
        # without opening a socket.
        from yarl import URL

        ch = GenericWebhookChannel("https://example.com/hook", "GET")
        _, calls = self._send(ch, _notification(
            data={"missing": 3, "items": ["A", "B"], "flag": True},
        ))
        params = calls[0]["kwargs"]["params"]
        url = URL("https://example.com/hook").with_query(params)
        assert "title=Scan+Complete" in str(url) or "title=Scan%20Complete" in str(url)

    def test_get_nested_data_is_json_encoded_not_dropped(self):
        ch = GenericWebhookChannel("https://example.com/hook", "GET")
        _, calls = self._send(ch, _notification(data={"missing": 3}))
        params = calls[0]["kwargs"]["params"]
        assert json.loads(params["data"]) == {"missing": 3}

    # -- untouched sibling channels (positive controls) --------------------

    def test_positive_control_discord_still_posts_json(self):
        ch = DiscordWebhookChannel("https://discord.example.com/webhooks/1/a")
        Session, calls = _make_recording_session(204)
        with patch("backend.notifications.aiohttp.ClientSession", Session):
            ok = asyncio.run(ch.send(_notification()))
        assert ok is True
        assert calls[0]["method"] == "POST"
        assert "embeds" in calls[0]["kwargs"]["json"]

    def test_positive_control_pushover_still_posts_form_data(self):
        # use_data=True is a third branch; prove the method check did not
        # swallow it.
        ch = PushoverChannel("user", "token")
        Session, calls = _make_recording_session(200)
        with patch("backend.notifications.aiohttp.ClientSession", Session):
            ok = asyncio.run(ch.send(_notification()))
        assert ok is True
        assert "data" in calls[0]["kwargs"]
        assert "params" not in calls[0]["kwargs"]

    def test_non_2xx_still_fails(self):
        ch = GenericWebhookChannel("https://example.com/hook", "GET")
        ok, _ = self._send(ch, status=500)
        assert ok is False


# ===================================================================
# Finding #5 — settings changes must reach the running bridge
# ===================================================================

def _configured_bridge(config):
    """Build a bridge without spawning the background loop thread.

    The channel list — the thing the finding is about — lives on the manager,
    so the tests drive _send_notification directly and stay deterministic.
    """
    bridge = NotificationBridge()
    with patch.object(NotificationBridge, "_start_loop"):
        bridge.configure(config)
    return bridge


def _channel_names(bridge):
    return sorted(c.name for c in bridge._manager._channels)


class TestBridgeReconfigure:

    def test_baseline_config_mutation_alone_never_reaches_the_channels(self):
        # Pins the constraint the fix exists for: channels are built from a
        # SNAPSHOT, so mutating the dict the bridge was configured with does
        # nothing. If this ever starts failing, reconfigure() is redundant.
        config = {"discord_webhook": ""}
        bridge = _configured_bridge(config)
        assert _channel_names(bridge) == []
        config["discord_webhook"] = "https://discord.example.com/webhooks/1/a"
        assert _channel_names(bridge) == []

    def test_reconfigure_picks_up_a_newly_configured_channel(self):
        config = {"discord_webhook": ""}
        bridge = _configured_bridge(config)
        config["discord_webhook"] = "https://discord.example.com/webhooks/1/a"
        with patch.object(NotificationBridge, "_start_loop"):
            bridge.reconfigure(config)
        assert _channel_names(bridge) == ["discord"]

    def test_reconfigure_drops_a_cleared_channel(self):
        # Disagreeing case: an implementation that only ADDED channels
        # (no clear_channels) passes the test above and leaves a deleted
        # webhook live forever.
        config = {"discord_webhook": "https://discord.example.com/webhooks/1/a"}
        bridge = _configured_bridge(config)
        assert _channel_names(bridge) == ["discord"]
        config["discord_webhook"] = ""
        with patch.object(NotificationBridge, "_start_loop"):
            bridge.reconfigure(config)
        assert _channel_names(bridge) == []

    def test_reconfigure_does_not_duplicate_an_unchanged_channel(self):
        config = {"discord_webhook": "https://discord.example.com/webhooks/1/a"}
        bridge = _configured_bridge(config)
        with patch.object(NotificationBridge, "_start_loop"):
            bridge.reconfigure(config)
            bridge.reconfigure(config)
        assert _channel_names(bridge) == ["discord"]

    def test_reconfigure_applies_a_changed_url_not_just_a_changed_key(self):
        config = {"webhook_url": "https://example.com/old"}
        bridge = _configured_bridge(config)
        config["webhook_url"] = "https://example.com/new"
        with patch.object(NotificationBridge, "_start_loop"):
            bridge.reconfigure(config)
        channel = bridge._manager._channels[0]
        assert channel.webhook_url == "https://example.com/new"

    def test_reconfigure_keeps_the_same_manager(self):
        # Disagreeing case: `reconfigure = configure` would pass every
        # channel-list assertion above while silently replacing the manager,
        # discarding history/callbacks and orphaning a second event loop.
        bridge = _configured_bridge({})
        manager_before = bridge._manager
        with patch.object(NotificationBridge, "_start_loop"):
            bridge.reconfigure({"slack_webhook": "https://hooks.example.com/x"})
        assert bridge._manager is manager_before

    def test_reconfigure_preserves_history(self):
        bridge = _configured_bridge({})
        asyncio.run(bridge._manager._send_notification(_notification(title="Earlier")))
        assert len(bridge._manager.get_history()) == 1
        with patch.object(NotificationBridge, "_start_loop"):
            bridge.reconfigure({"slack_webhook": "https://hooks.example.com/x"})
        history = bridge._manager.get_history()
        assert len(history) == 1
        assert history[0]["title"] == "Earlier"

    def test_reconfigure_preserves_callbacks(self):
        bridge = _configured_bridge({})
        seen = []
        bridge._manager.add_callback(seen.append)
        with patch.object(NotificationBridge, "_start_loop"):
            bridge.reconfigure({"slack_webhook": "https://hooks.example.com/x"})
        asyncio.run(bridge._manager._send_notification(_notification()))
        assert len(seen) == 1

    def test_reconfigure_before_configure_initializes_the_bridge(self):
        bridge = NotificationBridge()
        assert bridge._manager is None
        with patch.object(NotificationBridge, "_start_loop"):
            bridge.reconfigure({"discord_webhook": "https://discord.example.com/w/1/a"})
        assert bridge._manager is not None
        assert _channel_names(bridge) == ["discord"]

    def test_reconfigure_survives_a_bad_channel_config(self):
        bridge = _configured_bridge({})
        with patch.object(NotificationBridge, "_start_loop"):
            with patch.object(bridge._manager, "configure_from_dict",
                              side_effect=ValueError("bad config")):
                # Must not raise — the settings save already landed.
                bridge.reconfigure({"discord_webhook": "https://x/y"})

    # -- POSITIVE CONTROL: the rebuilt channel actually delivers ------------

    def test_positive_control_reconfigured_channel_actually_sends(self):
        """Trace config -> reconfigure -> manager -> channel -> transport.

        A "fix" that populated _channels with something unusable would pass
        every name-based assertion above; this asserts the transport was
        really invoked with the notification.
        """
        config = {"discord_webhook": ""}
        bridge = _configured_bridge(config)
        config["discord_webhook"] = "https://discord.example.com/webhooks/1/a"
        with patch.object(NotificationBridge, "_start_loop"):
            bridge.reconfigure(config)

        Session, calls = _make_recording_session(204)
        with patch("backend.notifications.aiohttp.ClientSession", Session):
            asyncio.run(bridge._manager._send_notification(
                _notification(title="Scan Complete")))

        assert len(calls) == 1
        assert calls[0]["url"] == "https://discord.example.com/webhooks/1/a"
        assert calls[0]["kwargs"]["json"]["embeds"][0]["title"] == "Scan Complete"

    def test_positive_control_end_to_end_through_the_real_bridge_loop(self):
        """Same trace, but through the production sync API and its loop thread.

        bridge.send() is what every caller in the app actually uses; the tests
        above bypass the background loop for determinism, so exercise it once.
        """
        bridge = NotificationBridge()
        try:
            bridge.configure({})
            assert _channel_names(bridge) == []
            bridge.reconfigure({
                "discord_webhook": "https://discord.example.com/webhooks/1/a",
            })

            delivered = threading.Event()
            Session, calls = _make_recording_session(204)

            class _SignallingSession(Session):
                def request(self, method, url, **kwargs):
                    result = super().request(method, url, **kwargs)
                    delivered.set()
                    return result

            with patch("backend.notifications.aiohttp.ClientSession",
                       _SignallingSession):
                bridge.notify_scan_complete(total=3, missing=2, upgrades=1)
                assert delivered.wait(timeout=10), "notification never dispatched"

            assert calls[0]["url"] == "https://discord.example.com/webhooks/1/a"
        finally:
            bridge.shutdown()


# ===================================================================
# Finding #5 — the settings route must trigger the rebuild
# ===================================================================

class _StubBackend:
    def __init__(self, config):
        self.config = config
        self.save_calls = 0
        self._cleared_keys = set()

    def save_config(self):
        self.save_calls += 1


class _StubRegistry:
    """Minimal stand-in for ServiceRegistry (whose `notifications` is a
    read-only property, so a real instance cannot be pointed at a double)."""

    def __init__(self, config, bridge=None):
        self.config = config
        self.backend = _StubBackend(config)
        self.notifications = bridge


def _update(reg, **fields):
    from backend.api.routes.settings import SettingsUpdate, update_settings
    return update_settings(SettingsUpdate(**fields), reg)


class TestUpdateSettingsReloadsNotifications:

    def test_notification_key_triggers_reconfigure(self):
        bridge = MagicMock()
        reg = _StubRegistry({"discord_webhook": ""}, bridge)
        resp = _update(reg, discord_webhook="https://discord.example.com/w/1/a")
        assert resp["status"] == "ok"
        bridge.reconfigure.assert_called_once()

    def test_reconfigure_receives_the_updated_config(self):
        # Ordering matters: reconfigure must see the NEW value, so it has to
        # run after reg.config.update, not before.
        bridge = MagicMock()
        seen = {}
        bridge.reconfigure.side_effect = lambda cfg: seen.update(cfg)
        reg = _StubRegistry({"discord_webhook": ""}, bridge)
        _update(reg, discord_webhook="https://discord.example.com/w/1/a")
        assert seen["discord_webhook"] == "https://discord.example.com/w/1/a"

    def test_reconfigure_runs_after_save_config(self):
        # save_config restores sensitive keys from disk and can change the
        # effective value, so rebuilding before it would use the wrong one.
        order = []
        bridge = MagicMock()
        bridge.reconfigure.side_effect = lambda cfg: order.append("reconfigure")
        reg = _StubRegistry({"smtp_host": ""}, bridge)
        reg.backend.save_config = lambda: order.append("save_config")
        _update(reg, smtp_host="smtp.example.com")
        assert order == ["save_config", "reconfigure"]

    def test_non_notification_key_does_not_reconfigure(self):
        # Disagreeing case: reconfiguring unconditionally passes every
        # "was it called" assertion above. Rebuilding the channel list on
        # every unrelated settings save would drop in-flight state for no
        # reason, so the key filter has to be real.
        bridge = MagicMock()
        reg = _StubRegistry({"min_size_mb": 100}, bridge)
        _update(reg, min_size_mb=500)
        bridge.reconfigure.assert_not_called()

    def test_masked_sensitive_value_does_not_reconfigure(self):
        # The UI echoes back a bullet placeholder for unchanged secrets; those
        # are filtered out of real_updates, so nothing actually changed.
        bridge = MagicMock()
        reg = _StubRegistry({"discord_webhook": "https://real/hook"}, bridge)
        _update(reg, discord_webhook="•" * 8)
        bridge.reconfigure.assert_not_called()
        assert reg.config["discord_webhook"] == "https://real/hook"

    def test_cleared_notification_key_still_reconfigures(self):
        # Deleting a webhook must reach the running bridge too.
        bridge = MagicMock()
        reg = _StubRegistry({"discord_webhook": "https://real/hook"}, bridge)
        _update(reg, discord_webhook="")
        bridge.reconfigure.assert_called_once()

    @pytest.mark.parametrize("key,value", [
        ("desktop_notifications", True),
        ("discord_webhook", "https://discord.example.com/w/1/a"),
        ("discord_username", "Hound"),
        ("slack_webhook", "https://hooks.example.com/x"),
        ("email_enabled", True),
        ("smtp_host", "smtp.example.com"),
        ("smtp_port", 465),
        ("smtp_username", "u"),
        ("smtp_password", "p"),
        ("email_from", "a@example.com"),
        ("email_to", "b@example.com"),
        ("smtp_tls", False),
        ("pushover_user", "user"),
        ("pushover_token", "token"),
        ("webhook_url", "https://example.com/hook"),
        ("webhook_method", "GET"),
    ])
    def test_every_channel_key_triggers_reconfigure(self, key, value):
        # NOTIFICATION_KEYS must cover every key _build_notif_config reads;
        # a missing one is exactly the original bug, scoped to one field.
        bridge = MagicMock()
        reg = _StubRegistry({}, bridge)
        _update(reg, **{key: value})
        assert bridge.reconfigure.call_count == 1, f"{key} did not reconfigure"

    def test_notification_keys_match_the_bridge_mapping(self):
        # Structural guard: every key the bridge reads is declared here.
        from backend.api.routes.settings import NOTIFICATION_KEYS
        import inspect
        source = inspect.getsource(NotificationBridge._build_notif_config)
        for key in NOTIFICATION_KEYS:
            assert f'"{key}"' in source, f"{key} is not read by the bridge"

    def test_missing_bridge_does_not_break_the_save(self):
        reg = _StubRegistry({"discord_webhook": ""}, None)
        resp = _update(reg, discord_webhook="https://discord.example.com/w/1/a")
        assert resp["status"] == "ok"
        assert reg.config["discord_webhook"] == "https://discord.example.com/w/1/a"

    def test_reconfigure_failure_does_not_fail_the_save(self):
        # The settings DID persist; a 500 here would wrongly tell the operator
        # nothing was saved.
        bridge = MagicMock()
        bridge.reconfigure.side_effect = RuntimeError("boom")
        reg = _StubRegistry({"discord_webhook": ""}, bridge)
        resp = _update(reg, discord_webhook="https://discord.example.com/w/1/a")
        assert resp["status"] == "ok"
        assert reg.config["discord_webhook"] == "https://discord.example.com/w/1/a"

    def test_positive_control_settings_save_still_works_end_to_end(self):
        """A normal save must persist, report ok, and list the changed keys."""
        bridge = MagicMock()
        reg = _StubRegistry({"min_size_mb": 100, "discord_webhook": ""}, bridge)
        resp = _update(reg, min_size_mb=500,
                       discord_webhook="https://discord.example.com/w/1/a")
        assert resp["status"] == "ok"
        assert set(resp["updated_keys"]) == {"min_size_mb", "discord_webhook"}
        assert reg.config["min_size_mb"] == 500
        assert reg.backend.save_calls == 1
        bridge.reconfigure.assert_called_once()


class TestSettingsRouteToBridgeIntegration:
    """End to end with a REAL bridge behind the real route function."""

    def test_saving_a_webhook_makes_the_live_channel_deliver(self):
        config = {"discord_webhook": ""}
        bridge = _configured_bridge(config)
        reg = _StubRegistry(config, bridge)
        assert _channel_names(bridge) == []

        with patch.object(NotificationBridge, "_start_loop"):
            _update(reg, discord_webhook="https://discord.example.com/webhooks/1/a")

        assert _channel_names(bridge) == ["discord"]

        Session, calls = _make_recording_session(204)
        with patch("backend.notifications.aiohttp.ClientSession", Session):
            asyncio.run(bridge._manager._send_notification(_notification()))
        assert len(calls) == 1
        assert calls[0]["url"] == "https://discord.example.com/webhooks/1/a"

    def test_saving_a_get_webhook_delivers_via_query_string(self):
        """The two fixes meet: a GET webhook saved at runtime must both go
        live AND carry its payload where the receiver will read it."""
        config = {"webhook_url": "", "webhook_method": "POST"}
        bridge = _configured_bridge(config)
        reg = _StubRegistry(config, bridge)

        with patch.object(NotificationBridge, "_start_loop"):
            _update(reg, webhook_url="https://example.com/hook",
                    webhook_method="GET")

        Session, calls = _make_recording_session(200)
        with patch("backend.notifications.aiohttp.ClientSession", Session):
            asyncio.run(bridge._manager._send_notification(_notification()))
        assert calls[0]["method"] == "GET"
        assert "params" in calls[0]["kwargs"]
        assert "json" not in calls[0]["kwargs"]

    def test_saving_email_settings_normalizes_the_recipients(self):
        config = {}
        bridge = _configured_bridge(config)
        reg = _StubRegistry(config, bridge)

        with patch.object(NotificationBridge, "_start_loop"):
            _update(reg, email_enabled=True, smtp_host="smtp.example.com",
                    email_from="scanhound@example.com",
                    email_to="jesse@example.com, alerts@example.com")

        channel = [c for c in bridge._manager._channels if c.name == "email"][0]
        assert channel.to_addrs == ["jesse@example.com", "alerts@example.com"]
