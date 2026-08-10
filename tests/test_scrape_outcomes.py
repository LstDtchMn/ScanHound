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
    # A real Cloudflare interstitial REPLACES the page: the challenge phrase is
    # in the <title>, which is authoritative regardless of reveal state. (Before
    # the round-2 review this test relied on a bare iframe classifying on its
    # own; that path is now gated on a not-ready reveal, so the test uses the
    # interstitial evidence a genuine challenge page actually carries.)
    driver.title = "Just a moment..."
    driver.page_source = """
        <html><head><title>Just a moment...</title></head>
        <body><h1>Just a moment</h1>
        <iframe src="https://challenges.cloudflare.com/turnstile"></iframe>
        </body></html>
    """

    diagnostic = service._log_page_diagnostics(driver, stage="access_control")

    assert diagnostic.code is ScrapeCode.INTERACTIVE_CHALLENGE
    assert diagnostic.retryable is False
    assert diagnostic.affects_source_health is True


def test_dormant_assets_do_not_invoke_coordinator_or_cooldown(monkeypatch):
    # 6. A page that only preloads Cloudflare/Turnstile assets must not call the
    #    HDEncode coordinator or start a source-wide cooldown.
    service = _service()
    coordinator = MagicMock()
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator
    )
    driver = MagicMock()
    driver.page_source = """
        <html><head>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
        </head><body>
        <article>A Movie (2024) 2160p - release description with no links yet.</article>
        </body></html>
    """

    diagnostic = service._log_page_diagnostics(
        driver, stage="access_control", source_kind="hdencode"
    )

    assert diagnostic.code is not ScrapeCode.INTERACTIVE_CHALLENGE
    coordinator.observe_challenge.assert_not_called()


def test_dormant_cloudflare_assets_stay_item_level(monkeypatch):
    # 7. Dormant Cloudflare assets alongside real file-host links classify as an
    #    ITEM-level outcome, never a source-wide challenge — so link retrieval is
    #    not blocked. (Scope note: this asserts the classification via
    #    _log_page_diagnostics; it does not itself drive scrape_links().)
    service = _service()
    coordinator = MagicMock()
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator
    )
    driver = MagicMock()
    driver.page_source = """
        <html><head>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
        </head><body>
        <a href="https://rapidgator.net/file/abc">Rapidgator</a>
        </body></html>
    """

    diagnostic = service._log_page_diagnostics(
        driver, keyword="rapidgator", stage="requested_host", source_kind="hdencode"
    )

    assert diagnostic.code is not ScrapeCode.INTERACTIVE_CHALLENGE
    assert diagnostic.affects_source_health is False
    coordinator.observe_challenge.assert_not_called()


def test_embedded_challenge_iframe_on_a_stuck_reveal_is_source_wide(monkeypatch):
    # 8. An embedded challenge iframe on a NOT-READY reveal produces the typed
    #    source-wide interactive-challenge outcome and invokes the coordinator.
    #    (Round-2 review, finding 4: the iframe no longer classifies on its own;
    #    it is the reveal-stuck conjunction that makes it a source-wide event.)
    service = _service()
    coordinator = MagicMock()
    coordinator.observe_challenge.return_value = SimpleNamespace(
        cooldown_until="2099-01-01T00:00:00+00:00"
    )
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator
    )
    driver = MagicMock()
    driver.page_source = """
        <html><body>
        <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/if"></iframe>
        </body></html>
    """

    diagnostic = service._log_page_diagnostics(
        driver, stage="access_control", source_kind="hdencode",
        reveal_tier="not-ready",
    )

    assert diagnostic.code is ScrapeCode.INTERACTIVE_CHALLENGE
    assert diagnostic.affects_source_health is True
    assert diagnostic.affected_scope == "source"
    assert diagnostic.cooldown_until == "2099-01-01T00:00:00+00:00"
    coordinator.observe_challenge.assert_called_once()


def test_embedded_challenge_iframe_on_a_working_reveal_is_not_a_challenge(monkeypatch):
    # Round-2 review, finding 4: the negative control. A rendered Turnstile
    # iframe on a page whose reveal is READY (or not evaluated) must NOT classify
    # as a source-wide challenge — otherwise a dormant/unrelated widget would
    # strand the source under a verification hold a timer cannot release.
    service = _service()
    coordinator = MagicMock()
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator
    )
    driver = MagicMock()
    driver.page_source = """
        <html><body>
        <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/if"></iframe>
        </body></html>
    """

    diagnostic = service._log_page_diagnostics(
        driver, stage="access_control", source_kind="hdencode",
        reveal_tier="links-control",
    )

    assert diagnostic.code is not ScrapeCode.INTERACTIVE_CHALLENGE
    coordinator.observe_challenge.assert_not_called()


def test_body_only_interstitial_no_title_still_classifies(monkeypatch):
    # FOLD regression guard (both branches' partitions had this shape): a genuine
    # page-replacing Cloudflare interstitial — no <title>, no captured
    # cf-mitigated header, a challenge iframe, and NO access/download/link
    # controls — must still be INTERACTIVE_CHALLENGE, not demoted to
    # LAYOUT_CHANGED. Recognised STRUCTURALLY (iframe + no controls = the
    # interstitial shape), not by the body phrase — see the peer-review finding
    # that keying on the phrase or the iframe alone false-positives on working
    # release pages.
    service = _service()
    coordinator = MagicMock()
    coordinator.observe_challenge.return_value = SimpleNamespace(cooldown_until=None)
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator)
    driver = MagicMock()
    driver.title = ""                    # NO <title>
    # A bare interstitial: a spinner, its copy, the challenge iframe — and NO
    # download/access/link controls (that absence is the structural signal).
    driver.page_source = """
        <html><body>
        <h1>Just a moment…</h1>
        <p>Please wait while your request is reviewed.</p>
        <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/if"></iframe>
        </body></html>
    """
    # No reveal_tier passed — a genuine interstitial has no reveal control.
    diagnostic = service._log_page_diagnostics(
        driver, stage="access_control", source_kind="hdencode")
    assert diagnostic.code is ScrapeCode.INTERACTIVE_CHALLENGE
    assert diagnostic.affected_scope == "source"


def test_challenge_iframe_on_a_working_page_with_controls_is_not_a_challenge(monkeypatch):
    # PEER-REVIEW COUNTEREXAMPLE (agent/turnstile-classification). The invisible
    # Turnstile widget renders a TRANSIENT iframe (~11s build/teardown) on
    # otherwise-working pages, and a release page carries phrases like "Access
    # Denied" as related-release NAMES. A working release page — reveal control
    # READY, access/download/link controls present, an "Access Denied (2021)"
    # related title in the body, and a transient challenge iframe — must NOT be
    # a source-wide interstitial. The structural guard (controls present) is what
    # prevents arming a hold on a healthy source; keying on the iframe or the
    # body phrase would not.
    service = _service()
    coordinator = MagicMock()
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator)
    driver = MagicMock()
    driver.title = "Some.Release.2026.2160p.WEB-DL"
    driver.page_source = """
        <html><body>
        <form action="/some-release/#unlocked">
          <input type="submit" value="View links">
        </form>
        <a href="https://rapidgator.net/file/abc">Rapidgator download</a>
        <aside>Related: <a href="/access-denied-2021/">Access Denied (2021)</a></aside>
        <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/if"></iframe>
        </body></html>
    """
    diagnostic = service._log_page_diagnostics(
        driver, stage="access_control", source_kind="hdencode",
        reveal_tier="links-control")
    assert diagnostic.code is not ScrapeCode.INTERACTIVE_CHALLENGE, (
        "a working page with controls must not be held on a transient iframe")
    coordinator.observe_challenge.assert_not_called()


def test_a_control_less_page_with_a_host_link_is_not_an_interstitial(monkeypatch):
    # FOLD review counterexample (ChatGPT): a post-click page for a DIFFERENT
    # requested host exposes a real file-host link whose visible label is just
    # "Rapidgator" — no lexical access/download/link keyword, so `candidates` is
    # empty — while the invisible-Turnstile iframe is transiently present and the
    # reveal tier is None. `host_links` (the actual host URL) is the stronger
    # signal that the page is working; without requiring its absence, the
    # structural guard would arm a source-wide hold on a healthy page.
    service = _service()
    coordinator = MagicMock()
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator)
    driver = MagicMock()
    driver.title = "Some.Release.2026.2160p.WEB-DL"
    driver.page_source = """
        <html><body>
        <a href="https://rapidgator.net/file/abc">Rapidgator</a>
        <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/if"></iframe>
        </body></html>
    """
    diagnostic = service._log_page_diagnostics(
        driver, keyword="nitroflare", stage="requested_host",
        source_kind="hdencode")
    assert diagnostic.code is not ScrapeCode.INTERACTIVE_CHALLENGE, (
        "a page exposing a real host link must not be held on a transient iframe")
    coordinator.observe_challenge.assert_not_called()


def test_body_title_phrase_without_an_iframe_is_not_a_challenge(monkeypatch):
    # The broader ambiguous title phrases ("just a moment", "access denied") in
    # a release body, with NO challenge iframe, must NOT classify — they are not
    # standalone evidence. (The narrow phrases "checking your browser" / "verify
    # you are human" remain standalone, pre-existing base behaviour.)
    service = _service()
    coordinator = MagicMock()
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator)
    driver = MagicMock()
    driver.title = "Some.Release.2026.2160p.WEB-DL"
    driver.page_source = """
        <html><body>
        <article>Just a moment of your time — a great 2160p release. No
        access denied here, just links.</article>
        </body></html>
    """
    diagnostic = service._log_page_diagnostics(
        driver, stage="access_control", source_kind="hdencode")
    assert diagnostic.code is not ScrapeCode.INTERACTIVE_CHALLENGE
    coordinator.observe_challenge.assert_not_called()


def _perf_entry(url, headers, *, rtype="Document"):
    import json
    return {
        "message": json.dumps(
            {
                "message": {
                    "method": "Network.responseReceived",
                    "params": {
                        "type": rtype,
                        "response": {"url": url, "status": 403, "headers": headers},
                    },
                }
            }
        )
    }


PAGE = "https://hdencode.org/a-release/"


def test_cf_mitigated_header_detects_localized_challenge(monkeypatch):
    """A custom/localized Challenge Page carries none of the English phrases.

    The header is authoritative, so it is recognised anyway.
    """
    service = _service()
    coordinator = MagicMock()
    coordinator.observe_challenge.return_value = SimpleNamespace(cooldown_until=None)
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator
    )
    driver = MagicMock()
    driver.current_url = PAGE
    driver.title = "Un momento"
    # No challenge iframe, no English title/body phrase anywhere.
    driver.page_source = (
        "<html><head><title>Un momento…</title></head>"
        "<body><h1>Comprobando su navegador</h1></body></html>"
    )
    driver.get_log.return_value = [_perf_entry(PAGE, {"cf-mitigated": "challenge"})]

    assert service._capture_cf_mitigated(driver) == "challenge"
    diagnostic = service._log_page_diagnostics(
        driver, stage="access_control", source_kind="hdencode"
    )

    assert diagnostic.code is ScrapeCode.INTERACTIVE_CHALLENGE
    assert diagnostic.affected_scope == "source"
    assert "cf-mitigated:challenge" in diagnostic.signals
    coordinator.observe_challenge.assert_called_once()


def test_cf_mitigated_absent_leaves_page_evidence_in_charge(monkeypatch):
    """Absence is 'no signal', never 'no challenge' — and never a false positive."""
    service = _service()
    coordinator = MagicMock()
    monkeypatch.setattr(
        "backend.download_service.get_hdencode_coordinator", lambda: coordinator
    )
    driver = MagicMock()
    driver.current_url = PAGE
    driver.page_source = (
        "<html><head>"
        "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>"
        "</head><body><a href='https://rapidgator.net/file/abc'>RG</a></body></html>"
    )
    driver.get_log.return_value = [_perf_entry(PAGE, {"content-type": "text/html"})]

    assert service._capture_cf_mitigated(driver) is None
    diagnostic = service._log_page_diagnostics(
        driver, keyword="rapidgator", stage="requested_host", source_kind="hdencode"
    )

    assert diagnostic.code is not ScrapeCode.INTERACTIVE_CHALLENGE
    coordinator.observe_challenge.assert_not_called()


def test_consecutive_navigations_do_not_inherit_a_stale_result():
    """The browser session is persistent, so state must be per-navigation.

    A challenge on one page must not linger and cool the source down on the
    next, unrelated grab.
    """
    service = _service()
    driver = MagicMock()

    first = "https://hdencode.org/first-release/"
    driver.current_url = first
    driver.get_log.return_value = [_perf_entry(first, {"cf-mitigated": "challenge"})]
    assert service._capture_cf_mitigated(driver) == "challenge"

    second = "https://hdencode.org/second-release/"
    driver.current_url = second
    driver.get_log.return_value = [_perf_entry(second, {"content-type": "text/html"})]
    assert service._capture_cf_mitigated(driver) is None
    assert service._last_cf_mitigated is None


def test_unattributable_challenge_header_is_reported_not_silent():
    """The one case that would leave the signal permanently inert.

    A challenge header on a document that is NOT the displayed page cannot be
    attributed to the page (that was the iframe blocker), but it must not
    vanish silently either.
    """
    service = _service()
    logged = []
    service._log = lambda message, level="info": logged.append(message)
    driver = MagicMock()
    driver.current_url = PAGE
    driver.get_log.return_value = [
        _perf_entry("https://hdencode.org/somewhere-else/", {"cf-mitigated": "challenge"})
    ]

    assert service._capture_cf_mitigated(driver) is None
    assert any("challenge header on" in m and "non-displayed" in m for m in logged)


def test_ordinary_unmatched_document_does_not_warn():
    """An embedded document with no challenge header is unremarkable.

    Warning on *any* unmatched document would fire on ordinary grabs — most
    pages load iframes — and the noise would bury the case above. Only an
    unattributable challenge header is worth surfacing.
    """
    service = _service()
    logged = []
    service._log = lambda message, level="info": logged.append(message)
    driver = MagicMock()
    driver.current_url = f"{PAGE}#unlocked"
    driver.get_log.return_value = [
        _perf_entry(PAGE, {"content-type": "text/html"}),
        _perf_entry("https://ads.example/frame", {"content-type": "text/html"}),
    ]

    assert service._capture_cf_mitigated(driver) is None
    assert logged == []


def test_unavailable_performance_log_is_not_a_challenge():
    """An adapter without performance logging must fail open to page evidence."""
    service = _service()
    driver = MagicMock()
    driver.get_log.side_effect = Exception("log type 'performance' not found")
    assert service._capture_cf_mitigated(driver) is None


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
