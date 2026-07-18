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


def test_non_health_affecting_diagnostic_does_not_change_state(tmp_path):
    db = DatabaseManager(str(tmp_path / "operation.db"))
    try:
        links = ScrapedLinks(
            diagnostic=ScrapeDiagnostic(
                ScrapeCode.REQUESTED_HOST_MISSING,
                retryable=False,
                affects_source_health=False,
            )
        )
        record_scrape_outcome(db, "hdencode", links)
        assert db.get_source_health("hdencode") is None
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
