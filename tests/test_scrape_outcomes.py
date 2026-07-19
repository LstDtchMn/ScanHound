"""Structured scrape outcome tests for PR 1c."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import BackgroundTasks

from backend.download_service import DownloadService
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic, ScrapedLinks


def _service():
    db = MagicMock()
    db.is_downloaded.return_value = False
    db.get_downloaded_title_quality.return_value = []
    return DownloadService(config={}, db=db, server_mode=True)


def test_scraped_links_preserves_bare_list_compatibility():
    links = ScrapedLinks(["https://rapidgator.net/file/abc"])
    assert isinstance(links, list)
    assert links == ["https://rapidgator.net/file/abc"]
    assert len(links) == 1


def test_download_item_surfaces_structured_failure_message():
    service = _service()
    diagnostic = ScrapeDiagnostic(
        ScrapeCode.BROWSER_NETWORK_ERROR,
        retryable=True,
        signals=("ERR_NAME_NOT_RESOLVED",),
        detail="driver failed at C:/Users/example/private-profile",
    )
    service.scrape_links = MagicMock(return_value=ScrapedLinks(diagnostic=diagnostic))
    service._is_supported_download_link = MagicMock(return_value=False)

    result = service.download_item(
        "https://hdencode.org/release/", "Example", None, "4K", "20 GB"
    )

    assert result["success"] is False
    assert result["reason_code"] == "browser_network_error"
    assert result["retryable"] is True
    assert result["signals"] == ["ERR_NAME_NOT_RESOLVED"]
    assert "network" in result["message"].lower() or "reach" in result["message"].lower()
    assert "C:/Users/example/private-profile" not in result["message"]


def test_page_diagnostics_classifies_interactive_challenge():
    service = _service()
    driver = MagicMock()
    driver.page_source = """
        <html><body><h1>Just a moment</h1>
        <iframe src="https://challenges.cloudflare.com/turnstile"></iframe>
        </body></html>
    """

    diagnostic = service._log_page_diagnostics(driver, stage="access_control")

    assert diagnostic.code is ScrapeCode.INTERACTIVE_CHALLENGE
    assert diagnostic.retryable is False
    assert diagnostic.affects_source_health is True


def test_page_diagnostics_distinguishes_requested_host_missing():
    service = _service()
    driver = MagicMock()
    driver.page_source = """
        <html><body>
        <button>View links</button>
        <a href="https://nitroflare.com/view/abc">NF</a>
        </body></html>
    """

    diagnostic = service._log_page_diagnostics(
        driver, keyword="rapidgator", stage="requested_host"
    )

    assert diagnostic.code is ScrapeCode.REQUESTED_HOST_MISSING
    assert diagnostic.affects_source_health is False


def test_page_diagnostics_distinguishes_layout_change():
    service = _service()
    driver = MagicMock()
    driver.page_source = "<html><body><article>Release text only</article></body></html>"

    diagnostic = service._log_page_diagnostics(driver, stage="access_control")

    assert diagnostic.code is ScrapeCode.LAYOUT_CHANGED
    assert diagnostic.affects_source_health is True


def test_query_text_cannot_spoof_hdencode_off_switch(monkeypatch):
    service = _service()
    service.config["hdencode_enabled"] = False
    service._scrape_ddlbase_links = MagicMock(
        side_effect=AssertionError("query text must not route to DDLBase")
    )

    links = service.scrape_links(
        "https://hdencode.org/release/?next=https://ddlbase.com/post/example",
        "Rapidgator",
    )

    assert links == []
    assert links.diagnostic.code is ScrapeCode.SOURCE_DISABLED
    service._scrape_ddlbase_links.assert_not_called()


def test_exact_ddlbase_hostname_bypasses_hdencode_switch(monkeypatch):
    service = _service()
    service.config["hdencode_enabled"] = False
    monkeypatch.setattr("backend.download_service._ensure_selenium", lambda: None)
    service._scrape_ddlbase_links = MagicMock(
        return_value=["https://1fichier.com/?abc"]
    )

    links = service.scrape_links(
        "https://www.ddlbase.com/post/example",
        "1fichier",
    )

    assert links == ["https://1fichier.com/?abc"]
    assert links.diagnostic is None
    service._scrape_ddlbase_links.assert_called_once()


def test_batch_exception_is_reported_as_structured_failure(monkeypatch):
    from backend.api.routes import downloads as download_routes

    dl = MagicMock()
    dl.scrape_links.side_effect = RuntimeError("browser exploded")
    dl.copy_to_clipboard.return_value = False
    reg = SimpleNamespace(download=dl, db=None)
    background = BackgroundTasks()
    events = []
    monkeypatch.setattr(
        download_routes.ws_manager,
        "broadcast_sync",
        events.append,
    )

    response = download_routes.copy_links_batch(
        download_routes.ScrapeBatchRequest(items=[
            download_routes.ScrapeRequest(
                url="https://hdencode.org/release/example",
                service_type="Rapidgator",
            )
        ]),
        background,
        reg,
    )

    assert response == {"status": "started", "count": 1}
    assert len(background.tasks) == 1
    task = background.tasks[0]
    task.func(*task.args, **task.kwargs)

    notification = next(
        event for event in events
        if event.get("type") == "notification"
    )
    assert notification["data"]["reason_codes"] == ["scrape_exception"]
    assert "scrape_exception" in notification["data"]["body"]


def test_release_text_named_captcha_is_not_a_challenge():
    service = _service()
    driver = MagicMock()
    driver.title = "Captcha (2024)"
    driver.page_source = """
        <html><body>
        <article>A documentary titled Captcha with ordinary release text.</article>
        </body></html>
    """

    diagnostic = service._log_page_diagnostics(
        driver,
        stage="access_control",
    )

    assert diagnostic.code is ScrapeCode.LAYOUT_CHANGED
    assert diagnostic.affects_source_health is True



def test_serialized_diagnostic_never_exposes_internal_detail():
    secret = "C:/Users/example/private-profile/chromedriver"
    diagnostic = ScrapeDiagnostic(
        ScrapeCode.BROWSER_LAUNCH_FAILED,
        detail=secret,
        signals=("SessionNotCreatedException",),
    )

    # Internal logs may use the detailed message.
    assert secret in diagnostic.message

    # API and WebSocket serialization must remain stable and sanitized.
    payload = diagnostic.to_dict()
    assert payload["message"] == diagnostic.public_message
    assert secret not in payload["message"]
    assert payload["signals"] == ["SessionNotCreatedException"]



def test_challenge_iframe_signal_drops_path_query_and_fragment():
    service = _service()
    driver = MagicMock()
    secret = "sensitive-site-key"
    driver.title = "Just a moment"
    driver.page_source = f"""
        <html><body>
        <iframe src="https://challenges.cloudflare.com/turnstile/v0/api.js?sitekey={secret}#state"></iframe>
        </body></html>
    """

    diagnostic = service._log_page_diagnostics(
        driver,
        stage="access_control",
    )
    payload = diagnostic.to_dict()

    assert diagnostic.code is ScrapeCode.INTERACTIVE_CHALLENGE
    assert any(
        signal.startswith("iframe:turnstile@challenges.cloudflare.com")
        for signal in payload["signals"]
    )
    serialized = repr(payload)
    assert secret not in serialized
    assert "api.js" not in serialized
    assert "#state" not in serialized



def test_challenge_iframe_signal_rejects_arbitrary_non_url_text():
    from backend.download_service import _challenge_iframe_signal

    signal = _challenge_iframe_signal("not a url at all captcha SECRET")

    assert signal == "iframe:captcha@unknown"
    assert "SECRET" not in signal
    assert "not a url" not in signal


def test_challenge_iframe_signal_parses_protocol_relative_host():
    from backend.download_service import _challenge_iframe_signal

    signal = _challenge_iframe_signal(
        "//challenges.cloudflare.com/turnstile?sitekey=SECRET"
    )

    assert signal == "iframe:turnstile@challenges.cloudflare.com"
    assert "SECRET" not in signal
