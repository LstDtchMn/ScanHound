"""End-to-end regressions for HDEncode download reliability."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import BackgroundTasks

from backend.database import DatabaseManager
from backend.download_outcome import (
    deferred_result,
    diagnostic_from_traffic_denial,
    is_source_wide_denial,
    notification_for_result,
    strong_challenge_markers,
)
from backend.download_service import _extract_requested_host_links
from backend.hdencode_coordinator import (
    HDEncodeDecision,
    HDEncodeTrafficCoordinator,
    HDEncodeTrafficDenied,
)
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic, ScrapedLinks
from backend.source_health import record_scrape_outcome


def test_decision_denial_carries_state_cause_and_expiry():
    until = datetime.now(timezone.utc).isoformat()
    exc = HDEncodeTrafficDenied.from_decision(
        HDEncodeDecision(True, "cooldown", "interactive_challenge", until)
    )
    assert exc.code == "interactive_challenge"
    assert exc.state == "cooldown"
    assert exc.reason_code == "interactive_challenge"
    assert exc.cooldown_until == until


def test_disabled_and_temporary_denials_have_distinct_diagnostics():
    disabled = diagnostic_from_traffic_denial(
        HDEncodeTrafficDenied(
            "source_disabled",
            "disabled",
            state="disabled",
            reason_code="source_disabled",
        )
    )
    assert disabled.code is ScrapeCode.SOURCE_DISABLED
    assert disabled.retryable is False
    assert disabled.transport_attempted is False

    temporary = diagnostic_from_traffic_denial(
        HDEncodeTrafficDenied(
            "interactive_challenge",
            "cooldown",
            state="cooldown",
            reason_code="interactive_challenge",
            cooldown_until="2099-01-01T00:00:00+00:00",
        )
    )
    assert temporary.code is ScrapeCode.SOURCE_TEMPORARILY_BLOCKED
    assert temporary.cause_code == "interactive_challenge"
    assert temporary.deferred is True
    assert temporary.retryable is True
    assert temporary.transport_attempted is False


def test_local_cooldown_preserves_original_cause():
    coordinator = HDEncodeTrafficCoordinator()
    coordinator._HEALTH_CACHE_SECONDS = 0
    coordinator.configure({"hdencode_enabled": True}, None)
    decision = coordinator.observe_challenge()
    snapshot = coordinator.snapshot()
    assert decision.reason_code == "interactive_challenge"
    assert snapshot["reason_code"] == "interactive_challenge"
    assert snapshot["cooldown_until"]


def test_one_challenge_is_persisted_once(tmp_path):
    db = DatabaseManager(str(tmp_path / "health.db"))
    coordinator = HDEncodeTrafficCoordinator()
    coordinator._HEALTH_CACHE_SECONDS = 0
    coordinator.configure({"hdencode_enabled": True}, db)
    try:
        decision = coordinator.observe_challenge()
        links = ScrapedLinks(
            diagnostic=ScrapeDiagnostic(
                ScrapeCode.INTERACTIVE_CHALLENGE,
                affects_source_health=True,
                cause_code="interactive_challenge",
                cooldown_until=decision.cooldown_until,
                health_owner="coordinator",
            )
        )
        record_scrape_outcome(db, "hdencode", links)
        row = db.get_source_health("hdencode")
        assert row["state"] == "cooldown"
        assert row["reason_code"] == "interactive_challenge"
        assert row["consecutive_failures"] == 1
        assert row["cooldown_until"]
    finally:
        db.close()


def test_failure_without_expiry_cannot_null_active_cooldown(tmp_path):
    db = DatabaseManager(str(tmp_path / "preserve.db"))
    try:
        assert db.record_source_failure(
            "hdencode", "cooldown", "http_429", cooldown_seconds=900
        )
        before = db.get_source_health("hdencode")["cooldown_until"]
        assert db.record_source_failure(
            "hdencode", "degraded", "browser_network_error"
        )
        after = db.get_source_health("hdencode")["cooldown_until"]
        assert after == before
    finally:
        db.close()


def test_visible_links_are_usable_without_reveal_control():
    html = """
    <html><body>
      <a href="https://rapidgator.net/file/abc">RG</a>
      <a href="https://nitroflare.com/view/other">NF</a>
    </body></html>
    """
    assert _extract_requested_host_links(html, "rapidgator") == [
        "https://rapidgator.net/file/abc"
    ]


def test_challenge_marker_detection_requires_strong_evidence():
    assert strong_challenge_markers(
        "<html><iframe src='https://challenges.cloudflare.com/turnstile'></iframe></html>",
        "Just a moment",
    )
    assert not strong_challenge_markers(
        "<html><article>A documentary named Captcha</article></html>",
        "Captcha (2024)",
    )
    assert not strong_challenge_markers(
        "<html><script>const text = 'verify you are human';</script><article>Release</article></html>",
        "Release (2024)",
    )


def test_reason_specific_notification_retains_typed_fields():
    result = {
        "success": False,
        "deferred": True,
        "message": "HDEncode is temporarily paused.",
        "reason_code": "source_temporarily_blocked",
        "cause_code": "interactive_challenge",
        "stage": "source_gate",
        "retryable": True,
        "retry_mode": "after_time",
        "cooldown_until": "2099-01-01T00:00:00+00:00",
        "transport_attempted": False,
        "affected_scope": "source",
        "action_code": "wait_until",
        "signals": [],
    }
    notification = notification_for_result(result, title="Example")
    assert notification["title"] == "Download deferred"
    assert notification["reason_code"] == "source_temporarily_blocked"
    assert notification["cause_code"] == "interactive_challenge"
    assert notification["transport_attempted"] is False


def test_deferred_result_is_source_wide_and_never_attempted():
    blocker = {
        "reason_code": "interactive_challenge",
        "cause_code": "interactive_challenge",
        "affected_scope": "source",
        "cooldown_until": "2099-01-01T00:00:00+00:00",
    }
    result = deferred_result(blocker, title="Later", url="https://example")
    assert result["deferred"] is True
    assert result["transport_attempted"] is False
    assert is_source_wide_denial(result)


def test_batch_stops_transport_after_first_source_wide_failure(monkeypatch):
    from backend.api.routes import downloads as download_routes

    challenge = {
        "success": False,
        "method": "",
        "link_count": 0,
        "message": "Verification required.",
        "reason_code": "interactive_challenge",
        "cause_code": "interactive_challenge",
        "stage": "verification",
        "retryable": False,
        "retry_mode": "manual_verification",
        "cooldown_until": "2099-01-01T00:00:00+00:00",
        "transport_attempted": True,
        "affected_scope": "source",
        "action_code": "verification_required",
        "signals": [],
    }
    dl = MagicMock()
    dl.download_item.return_value = challenge
    reg = SimpleNamespace(download=dl, db=None, scanner=None)
    tasks = BackgroundTasks()
    events = []
    monkeypatch.setattr(download_routes.ws_manager, "broadcast_sync", events.append)

    request = download_routes.BatchDownloadRequest(items=[
        download_routes.DownloadRequest(
            url=f"https://hdencode.org/release/{index}",
            title=f"Title {index}",
        )
        for index in range(3)
    ])
    response = download_routes.download_batch(request, tasks, reg)
    assert response == {"status": "started", "count": 3}
    task = tasks.tasks[0]
    task.func(*task.args, **task.kwargs)

    assert dl.download_item.call_count == 1
    batch_result = next(
        event for event in events if event.get("type") == "download:batch_result"
    )
    assert batch_result["data"]["failed"] == 1
    assert batch_result["data"]["deferred"] == 2
    assert all(
        outcome["transport_attempted"] is False
        for outcome in batch_result["data"]["outcomes"][1:]
    )
