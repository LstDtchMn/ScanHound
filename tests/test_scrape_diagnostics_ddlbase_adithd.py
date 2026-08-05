"""Audit pass-2 finding #22 — DDLBase / Adit-HD scrape diagnostics.

Both non-HDEncode scrapers used to `return []` on EVERY failure, so a
transient shortlink timeout, a Turnstile wall, or a dead browser was
laundered into the confident, permanent, non-retryable message
"No download links found on the source page." with reason_code NULL and
transport_attempted 0 — a durable record asserting no request was made
after a full Selenium session had run.

The suite is built around the pair that a lazy fix would collapse:

* a GENUINE empty page must stay permanent (retryable False), and
* a TRANSIENT failure on the same code path must be retryable,

so an implementation that stamps one blanket diagnostic on every empty
return fails here, and one that simply stops returning links fails the
positive controls.
"""

import itertools
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from backend.download_service import DownloadService
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic, ScrapedLinks


DDLBASE_URL = "https://ddlbase.com/movie-xyz"
ADITHD_URL = "https://adit-hd.com/thread/123"

CHALLENGE_PAGE = """
<html><head><title>Just a moment...</title></head><body>
    <p>Checking your browser before accessing the site.</p>
</body></html>
"""


def _make_service(config=None):
    db = MagicMock()
    db.is_downloaded.return_value = False
    return DownloadService(config=config or {}, db=db)


def _fake_driver(page_source, title="DDLBase", current_url=DDLBASE_URL):
    """A Selenium stand-in that reads as a NORMAL page, not an error page.

    ``find_elements`` must return a real empty list: a bare MagicMock is
    truthy, which makes ``_browser_error_code`` classify every page as
    Chrome's own neterror page and short-circuits the scrape before the
    code under test runs. ``get_log`` raises so the cf-mitigated header
    probe deterministically reports "no signal" rather than parsing a mock.
    """
    driver = MagicMock()
    driver.page_source = page_source
    driver.title = title
    driver.current_url = current_url
    driver.find_elements.return_value = []
    driver.get_log.side_effect = RuntimeError("performance log unavailable")
    return driver


def _diag(result):
    assert isinstance(result, ScrapedLinks), (
        f"scraper returned {type(result).__name__}; a bare list cannot carry "
        "a diagnostic and is exactly the defect"
    )
    return result.diagnostic


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("backend.download_service.time.sleep"):
        yield


@pytest.fixture(autouse=True)
def _selenium_stub():
    with patch("backend.download_service._ensure_selenium"):
        yield


@pytest.fixture
def _expired_clock():
    """Make _wait_past_cloudflare's 20s budget expire on its second reading."""
    ticks = itertools.count(0, 1000)
    with patch("backend.download_service.time.monotonic", side_effect=lambda: next(ticks)):
        yield


# ======================================================================
# DDLBase — positive controls (a fix that breaks these broke the feature)
# ======================================================================

class TestDDLBaseHealthyPaths:
    def test_direct_file_host_links_are_still_returned(self):
        """POSITIVE CONTROL: a working page still delivers its links."""
        svc = _make_service()
        svc.cached_driver = _fake_driver("""
        <html><body>
            <a href="https://1fichier.com/?abc123">Download</a>
            <a href="https://1fichier.com/?def456">Download 2</a>
        </body></html>
        """)

        result = svc._scrape_ddlbase_links(DDLBASE_URL)

        assert list(result) == [
            "https://1fichier.com/?abc123",
            "https://1fichier.com/?def456",
        ]
        assert _diag(result) is None, "a successful scrape must carry no diagnostic"

    def test_resolved_shortlink_is_still_returned(self):
        """POSITIVE CONTROL: the shortlink flow still delivers on success."""
        svc = _make_service()
        svc.cached_driver = _fake_driver("""
        <html><body><a href="https://cuty.io/abc123">Mirror 1</a></body></html>
        """)
        svc._resolve_cuttlinks_shortlink = MagicMock(
            return_value="https://1fichier.com/?resolved123"
        )

        result = svc._scrape_ddlbase_links(DDLBASE_URL)

        assert list(result) == ["https://1fichier.com/?resolved123"]
        assert _diag(result) is None


# ======================================================================
# DDLBase — the permanent/transient split
# ======================================================================

class TestDDLBasePermanentVsTransient:
    def test_genuinely_empty_post_stays_permanent(self):
        """A real "this post has no links" page must NOT become retryable."""
        svc = _make_service()
        svc.cached_driver = _fake_driver("""
        <html><body><div class="entry-content"><p>No downloads available</p></div></body></html>
        """)

        result = svc._scrape_ddlbase_links(DDLBASE_URL)
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None, "the empty page must still be classified"
        assert diagnostic.code is ScrapeCode.NO_FILE_HOST_LINKS
        assert diagnostic.retryable is False
        assert diagnostic.retry_mode == "none"

    def test_unresolved_automatable_shortlinks_are_transient(self):
        """The headline case: cuty.io timed out / hit Turnstile.

        The post DID advertise links, so "no download links found on the
        source page" is a false statement about the page, and the next
        attempt often succeeds. Contrast with the genuinely-empty test
        above: a fix that returns one blanket diagnostic for every empty
        result makes these two identical and is wrong.
        """
        svc = _make_service()
        svc.cached_driver = _fake_driver("""
        <html><body><a href="https://cuty.io/abc123">Mirror 1</a></body></html>
        """)
        svc._resolve_cuttlinks_shortlink = MagicMock(return_value=None)

        result = svc._scrape_ddlbase_links(DDLBASE_URL)
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None
        assert diagnostic.retryable is True
        assert diagnostic.retry_mode == "immediate"
        assert diagnostic.stage == "shortlink_resolution"
        # A full browser session ran; the durable record must not claim
        # that no request was made.
        assert diagnostic.transport_attempted is True

    def test_unautomatable_mirrors_stay_permanent(self):
        """Disagreeing case for the rule above.

        exe.io is a mirror this build cannot automate at all, so retrying
        produces the identical outcome forever. An implementation that
        marks "shortlinks present but nothing delivered" retryable passes
        the cuty.io test above and fails here.
        """
        svc = _make_service()
        svc.cached_driver = _fake_driver("""
        <html><body><a href="https://exe.io/4WwsnT4">Mirror 2</a></body></html>
        """)
        svc._resolve_cuttlinks_shortlink = MagicMock(
            side_effect=AssertionError("exe.io must never be auto-resolved")
        )

        result = svc._scrape_ddlbase_links(DDLBASE_URL)
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None
        assert diagnostic.retryable is False
        assert diagnostic.stage == "shortlink_resolution"

    def test_turnstile_wall_reports_interactive_challenge(self, _expired_clock):
        svc = _make_service()
        svc.cached_driver = _fake_driver(CHALLENGE_PAGE, title="Just a moment...")

        result = svc._scrape_ddlbase_links(DDLBASE_URL)
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None
        # This exact value is what routes/downloads.py enqueues a retry on
        # ({"interactive_challenge", "source_temporarily_blocked"}), so the
        # string matters as much as the classification.
        assert diagnostic.code.value == "interactive_challenge"
        assert diagnostic.action_code == "verification_required"
        assert diagnostic.affected_scope == "source"
        assert diagnostic.transport_attempted is True
        # DDLBase must never be routed through the HDEncode coordinator's
        # source-health bookkeeping.
        assert diagnostic.health_owner == "outcome_recorder"

    def test_navigation_failure_is_transient(self):
        svc = _make_service()
        driver = _fake_driver("<html></html>")
        driver.get.side_effect = RuntimeError("session died")
        svc.cached_driver = driver
        svc._recycle_driver = MagicMock()

        result = svc._scrape_ddlbase_links(DDLBASE_URL)
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None
        assert diagnostic.code is ScrapeCode.BROWSER_NAVIGATION_FAILED
        assert diagnostic.retryable is True

    def test_thrown_scrape_is_transient(self):
        svc = _make_service()
        driver = _fake_driver("<html></html>")
        # page_source blows up the way a dead session does, AFTER navigation
        # succeeded — the `except Exception: return []` branch.
        type(driver).page_source = PropertyMock(side_effect=RuntimeError("session gone"))
        svc.cached_driver = driver

        result = svc._scrape_ddlbase_links(DDLBASE_URL)
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None
        assert diagnostic.code is ScrapeCode.SCRAPE_EXCEPTION
        assert diagnostic.retryable is True
        assert diagnostic.transport_attempted is True
        # public_message must stay generic — detail may carry driver internals.
        assert "session gone" not in diagnostic.public_message


# ======================================================================
# scrape_links wrapping — the literal line the finding points at
# ======================================================================

class TestScrapeLinksPreservesDiagnostic:
    def test_ddlbase_diagnostic_survives_the_wrapper(self):
        """`ScrapedLinks(ScrapedLinks([], diagnostic=d))` used to drop d."""
        svc = _make_service()
        marker = ScrapeDiagnostic(ScrapeCode.SCRAPE_EXCEPTION, retryable=True)
        svc._scrape_ddlbase_links = MagicMock(
            return_value=ScrapedLinks(diagnostic=marker)
        )

        result = svc.scrape_links(DDLBASE_URL, "Rapidgator")

        assert result.diagnostic is marker

    def test_adithd_diagnostic_survives_the_wrapper(self):
        svc = _make_service()
        marker = ScrapeDiagnostic(ScrapeCode.BROWSER_LAUNCH_FAILED, retryable=True)
        svc._scrape_adithd_links = MagicMock(
            return_value=ScrapedLinks(diagnostic=marker)
        )

        result = svc.scrape_links(ADITHD_URL, "Rapidgator")

        assert result.diagnostic is marker

    def test_plain_list_from_a_scraper_still_works(self):
        """Disagreeing case: not every return value is a ScrapedLinks.

        Mocks and any older caller may hand back a bare list. Returning it
        unwrapped would break `getattr(links, "diagnostic", None)` callers'
        type expectations, so it is still normalized — with no diagnostic
        invented for it.
        """
        svc = _make_service()
        svc._scrape_ddlbase_links = MagicMock(
            return_value=["https://1fichier.com/?abc"]
        )

        result = svc.scrape_links(DDLBASE_URL, "Rapidgator")

        assert isinstance(result, ScrapedLinks)
        assert list(result) == ["https://1fichier.com/?abc"]
        assert result.diagnostic is None


# ======================================================================
# End-to-end through download_item — what the operator actually sees
# ======================================================================

class TestDownloadItemReporting:
    NO_LINKS_MESSAGE = "No download links found on the source page."

    def _download(self, svc):
        return svc.download_item(
            url=DDLBASE_URL, title="Some Movie", season=None,
            resolution="1080p", size="10 GB", service_type="Rapidgator",
        )

    def test_transient_shortlink_failure_is_not_reported_as_no_links(self):
        svc = _make_service()
        svc.cached_driver = _fake_driver("""
        <html><body><a href="https://cuty.io/abc123">Mirror 1</a></body></html>
        """)
        svc._resolve_cuttlinks_shortlink = MagicMock(return_value=None)

        result = self._download(svc)

        assert result["success"] is False
        assert result["message"] != self.NO_LINKS_MESSAGE
        assert result["retryable"] is True
        assert result["reason_code"] == "scrape_exception"
        assert result["transport_attempted"] is True

    def test_genuinely_empty_post_is_still_reported_as_permanent(self):
        """POSITIVE CONTROL for the untouched half of the finding.

        A real no-links page must stay a confident, non-retryable failure.
        """
        svc = _make_service()
        svc.cached_driver = _fake_driver("""
        <html><body><div class="entry-content"><p>No downloads available</p></div></body></html>
        """)

        result = self._download(svc)

        assert result["success"] is False
        assert result["retryable"] is False
        assert result["retry_mode"] == "none"
        assert result["reason_code"] == "no_file_host_links"
        assert result["deferred"] is False


# ======================================================================
# Adit-HD
# ======================================================================

@pytest.fixture
def _empty_registry():
    """Force the fallback scrape path: no adithd plugin is registered."""
    registry = MagicMock()
    registry.get_source.return_value = None
    with patch("backend.sources.registry.get_registry", return_value=registry):
        yield


class TestAditHDDiagnostics:
    def test_fallback_scrape_still_returns_links(self, _empty_registry):
        """POSITIVE CONTROL: the working Adit-HD path is untouched."""
        svc = _make_service()
        svc.cached_driver = _fake_driver("""
        <html><body>
            <a href="https://rapidgator.net/file/abc123">RG</a>
            <a href="https://other.example/file">Other</a>
        </body></html>
        """, title="Adit-HD", current_url=ADITHD_URL)

        result = svc._scrape_adithd_links(ADITHD_URL, "Rapidgator")

        assert list(result) == ["https://rapidgator.net/file/abc123"]
        assert _diag(result) is None

    def test_thread_without_the_requested_host_stays_permanent(self, _empty_registry):
        """The page loaded and simply has no Rapidgator link — permanent."""
        svc = _make_service()
        svc.cached_driver = _fake_driver("""
        <html><body><a href="https://nitroflare.com/view/def456">NF</a></body></html>
        """, title="Adit-HD", current_url=ADITHD_URL)

        result = svc._scrape_adithd_links(ADITHD_URL, "Rapidgator")
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None
        assert diagnostic.retryable is False
        # The page DOES carry file-host links, just not the requested one —
        # reporting "no file-host links" here would be a false statement.
        assert diagnostic.code is ScrapeCode.REQUESTED_HOST_MISSING

    def test_verification_wall_is_not_reported_as_no_links(self, _empty_registry):
        svc = _make_service()
        svc.cached_driver = _fake_driver(
            CHALLENGE_PAGE, title="Just a moment...", current_url=ADITHD_URL
        )

        result = svc._scrape_adithd_links(ADITHD_URL, "Rapidgator")
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None
        assert diagnostic.code.value == "interactive_challenge"
        assert diagnostic.transport_attempted is True

    def test_thrown_scrape_is_transient(self, _empty_registry):
        svc = _make_service()
        driver = _fake_driver("<html></html>", current_url=ADITHD_URL)
        driver.get.side_effect = RuntimeError("nav fail")
        svc.cached_driver = driver

        result = svc._scrape_adithd_links(ADITHD_URL, "Rapidgator")
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None
        assert diagnostic.code is ScrapeCode.SCRAPE_EXCEPTION
        assert diagnostic.retryable is True
        assert diagnostic.transport_attempted is True

    def test_browser_launch_failure_is_transient_without_transport(self):
        """Disagreeing case for transport_attempted.

        The browser never started, so nothing was sent — the opposite of
        the thrown-scrape case above, which DID reach the source. An
        implementation that hardcodes transport_attempted=True on every
        diagnostic passes that test and fails this one.
        """
        svc = _make_service()
        svc.get_driver = MagicMock(side_effect=RuntimeError("chromedriver missing"))

        result = svc._scrape_adithd_links(ADITHD_URL, "Rapidgator")
        diagnostic = _diag(result)

        assert list(result) == []
        assert diagnostic is not None
        assert diagnostic.code is ScrapeCode.BROWSER_LAUNCH_FAILED
        assert diagnostic.retryable is True
        assert diagnostic.transport_attempted is False
