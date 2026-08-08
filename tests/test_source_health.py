"""Minimal persistent source-health tests for PR 2."""
from backend.database import DatabaseManager
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic, ScrapedLinks
from backend.source_health import record_scrape_outcome


def test_source_health_success_and_failure_transitions(tmp_path):
    db = DatabaseManager(str(tmp_path / "health.db"))
    try:
        assert db.get_source_health("hdencode") is None

        assert db.record_source_failure(
            "hdencode", "blocked", "interactive_challenge"
        )
        blocked = db.get_source_health("hdencode")
        assert blocked["state"] == "blocked"
        assert blocked["reason_code"] == "interactive_challenge"
        assert blocked["consecutive_failures"] == 1
        assert blocked["last_failure_at"]

        assert db.record_source_success("hdencode")
        healthy = db.get_source_health("hdencode")
        assert healthy["state"] == "healthy"
        assert healthy["reason_code"] is None
        assert healthy["consecutive_failures"] == 0
        assert healthy["last_success_at"]
    finally:
        db.close()


def test_429_cooldown_survives_restart(tmp_path):
    path = str(tmp_path / "cooldown.db")
    db = DatabaseManager(path)
    assert db.record_source_failure(
        "hdencode", "cooldown", "http_429", cooldown_seconds=900
    )
    db.close()

    reopened = DatabaseManager(path)
    try:
        row = reopened.get_source_health("hdencode")
        assert row["state"] == "cooldown"
        assert row["cooldown_until"]
    finally:
        reopened.close()


def test_local_non_health_affecting_failure_does_not_change_source_state(
        tmp_path):
    """A local browser/network failure neither degrades nor clears the source."""
    db = DatabaseManager(str(tmp_path / "operation.db"))
    try:
        db.record_source_failure(
            "hdencode",
            "blocked",
            "interactive_challenge",
        )
        links = ScrapedLinks(
            diagnostic=ScrapeDiagnostic(
                ScrapeCode.BROWSER_NETWORK_ERROR,
                retryable=True,
                affects_source_health=False,
            )
        )

        record_scrape_outcome(db, "hdencode", links)

        row = db.get_source_health("hdencode")
        assert row["state"] == "blocked"
        assert row["reason_code"] == "interactive_challenge"
        assert row["consecutive_failures"] == 1
    finally:
        db.close()


def test_successful_scraped_links_mark_source_healthy(tmp_path):
    db = DatabaseManager(str(tmp_path / "success.db"))
    try:
        record_scrape_outcome(
            db,
            "hdencode",
            ScrapedLinks(["https://rapidgator.net/file/abc"]),
        )
        assert db.get_source_health("hdencode")["state"] == "healthy"
    finally:
        db.close()



def test_reachable_empty_page_clears_stale_blocked_state(tmp_path):
    db = DatabaseManager(str(tmp_path / "reachable.db"))
    try:
        db.record_source_failure(
            "hdencode",
            "blocked",
            "interactive_challenge",
        )
        record_scrape_outcome(
            db,
            "hdencode",
            ScrapedLinks(
                diagnostic=ScrapeDiagnostic(
                    ScrapeCode.REQUESTED_HOST_MISSING,
                    affects_source_health=False,
                )
            ),
        )

        row = db.get_source_health("hdencode")
        assert row["state"] == "healthy"
        assert row["consecutive_failures"] == 0
    finally:
        db.close()


def test_expired_cooldown_is_exposed_as_degraded():
    from datetime import datetime, timedelta, timezone
    from backend.source_health import effective_health_state

    expired = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    active = (
        datetime.now(timezone.utc) + timedelta(seconds=60)
    ).isoformat()

    assert effective_health_state({
        "state": "cooldown",
        "cooldown_until": expired,
    }) == "degraded"
    assert effective_health_state({
        "state": "cooldown",
        "cooldown_until": active,
    }) == "cooldown"


def test_health_routing_uses_parsed_hostname(monkeypatch):
    """Path/query text must never decide source health ownership.

    UPDATED 2026-08-07. The route used to classify with its own module-level
    `_source_page_kind(url)`, which ignored the operator's configured HDEncode host;
    peer review round 8 found that a configured mirror's health was therefore not
    attributed to HDEncode. The route now asks the service.

    So `dl` here is a REAL DownloadService with only `scrape_links` stubbed, not a
    bare MagicMock. A MagicMock's `owns_source_health()` returns a truthy Mock, which
    made the route record health for EVERY url and broke the negative assertion below
    -- and had that assertion been positive-only, it would instead have passed
    vacuously forever. Using the real object keeps this test exercising the same
    classifier production uses, which is stronger than what it tested before.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from backend.api.routes import downloads as download_routes
    from backend.download_service import DownloadService

    links = ScrapedLinks(
        diagnostic=ScrapeDiagnostic(
            ScrapeCode.INTERACTIVE_CHALLENGE,
            affects_source_health=True,
        )
    )
    dl = DownloadService.__new__(DownloadService)
    dl.config = {"base_url": "https://hdencode.org"}
    dl.scrape_links = MagicMock(return_value=links)
    db = MagicMock()
    reg = SimpleNamespace(download=dl, db=db)
    recorder = MagicMock()
    monkeypatch.setattr(download_routes, "record_scrape_outcome", recorder)

    download_routes.scrape_links(
        download_routes.ScrapeRequest(
            url=(
                "https://hdencode.org/release/"
                "?next=https://ddlbase.com/post/example"
            )
        ),
        reg,
    )
    recorder.assert_called_once_with(db, "hdencode", links)

    recorder.reset_mock()
    download_routes.scrape_links(
        download_routes.ScrapeRequest(
            url="https://www.ddlbase.com/post/example"
        ),
        reg,
    )
    recorder.assert_not_called()


def test_sources_endpoint_fails_open_when_health_db_is_unavailable(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from backend.api.routes import sources as source_routes

    source_registry = MagicMock()
    source_registry.list_sources.return_value = [{
        "name": "hdencode",
        "display_name": "HDEncode",
        "enabled": False,
    }]
    monkeypatch.setattr(
        source_routes,
        "get_source_registry",
        lambda: source_registry,
    )

    db = MagicMock()
    db.get_source_health.side_effect = RuntimeError("database locked")
    reg = SimpleNamespace(config={}, db=db)

    result = source_routes.list_sources(reg)

    assert result[0]["health_state"] == "unknown"
    assert result[0]["health_reason_code"] is None
