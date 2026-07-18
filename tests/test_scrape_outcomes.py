"""Structured scrape outcome tests for PR 1c."""
from unittest.mock import MagicMock

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
