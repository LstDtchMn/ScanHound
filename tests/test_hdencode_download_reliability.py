"""End-to-end regressions for HDEncode download reliability."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import BackgroundTasks

from backend.database import DatabaseManager
from backend.download_outcome import (
    cf_mitigated_from_perf_log,
    challenge_iframe_srcs,
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


def test_dormant_challenge_assets_are_not_evidence():
    # 1. A normal page that preloads a Turnstile / Cloudflare script (no rendered
    #    challenge) must NOT be classified as an active challenge.
    turnstile_preload = (
        "<html><head>"
        "<link rel='preload' as='script' "
        "href='https://challenges.cloudflare.com/turnstile/v0/api.js'>"
        "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>"
        "</head><body><h1>A Movie (2024) 2160p</h1>"
        "<a href='https://rapidgator.net/file/abc'>Rapidgator</a>"
        "</body></html>"
    )
    assert strong_challenge_markers(turnstile_preload, "A Movie (2024)") == ()
    assert challenge_iframe_srcs(turnstile_preload) == ()

    # 2. JavaScript that only references cf-chl / recaptcha / turnstile, with no
    #    rendered challenge iframe, title, or visible message, is not a challenge.
    dormant_js = (
        "<html><head><script>"
        "window.__cfg = {provider: 'turnstile', "
        "path: '/cdn-cgi/challenge-platform/cf-chl', recaptcha_site_key: 'abc'};"
        "</script></head><body><article>Release notes</article></body></html>"
    )
    assert strong_challenge_markers(dormant_js, "Release (2024)") == ()
    assert challenge_iframe_srcs(dormant_js) == ()


def test_active_challenge_evidence_is_detected():
    # 3. A rendered challenge iframe is strong evidence.
    turnstile_iframe = (
        "<html><body><iframe src='https://challenges.cloudflare.com/cdn-cgi/"
        "challenge-platform/turnstile/if/ov2/av0/12345'></iframe></body></html>"
    )
    assert challenge_iframe_srcs(turnstile_iframe)
    assert strong_challenge_markers(turnstile_iframe)

    recaptcha_iframe = (
        "<html><body><iframe title='reCAPTCHA' "
        "src='https://www.google.com/recaptcha/api2/anchor?k=xyz'></iframe></body></html>"
    )
    assert challenge_iframe_srcs(recaptcha_iframe)
    assert strong_challenge_markers(recaptcha_iframe)

    # 4. A challenge-specific page title is strong evidence — both when supplied
    #    by the caller and when present only in the document <title>.
    assert strong_challenge_markers("<html><body>...</body></html>", "Just a moment...")
    assert strong_challenge_markers(
        "<html><head><title>Attention Required! | Cloudflare</title></head>"
        "<body>...</body></html>"
    )

    # 5. Visible challenge body text is strong evidence.
    assert strong_challenge_markers(
        "<html><body><h1>Checking your browser before accessing the site</h1></body></html>"
    )
    assert strong_challenge_markers(
        "<html><body><p>Verify you are human by completing the action below.</p></body></html>"
    )


def test_release_titles_containing_challenge_phrases_are_not_challenges():
    """A real release can be *named* "Access Denied" or "Just a Moment".

    Matching those as a challenge starts a bogus one-hour source-wide cooldown.
    A Cloudflare interstitial replaces the page, so its title never carries
    release metadata — that metadata is what distinguishes the two.
    """
    access_denied = (
        "<html><head><title>Access Denied 2009 1080p BluRay x264 - 8.4 GB</title>"
        "</head><body><a href='https://rapidgator.net/file/abc'>RG</a></body></html>"
    )
    assert strong_challenge_markers(
        access_denied, "Access Denied 2009 1080p BluRay x264 - 8.4 GB"
    ) == ()

    just_a_moment = (
        "<html><head><title>Just a Moment 2021 2160p WEB-DL HEVC - 12.0 GB</title>"
        "</head><body>release</body></html>"
    )
    assert strong_challenge_markers(
        just_a_moment, "Just a Moment 2021 2160p WEB-DL HEVC - 12.0 GB"
    ) == ()

    # The guard must not blind the classifier to genuine challenge pages, whose
    # titles carry no release metadata.
    assert strong_challenge_markers("<html><body>...</body></html>", "Just a moment...")
    assert strong_challenge_markers(
        "<html><head><title>Attention Required! | Cloudflare</title></head>"
        "<body>...</body></html>"
    )
    assert strong_challenge_markers(
        "<html><head><title>Access denied | hdencode.org used Cloudflare to "
        "restrict access</title></head><body>...</body></html>"
    )


def test_stale_release_title_cannot_suppress_a_live_challenge_title():
    """driver.title and page_source are separate reads.

    During navigation or dynamic replacement they can reflect different moments,
    so the two title sources are evaluated independently — a release title in
    one must never discard genuine challenge evidence in the other.
    """
    # Live challenge title supplied, stale release <title> still in page_source.
    assert strong_challenge_markers(
        "<html><head><title>Access Denied 2009 1080p BluRay x264 - 8.4 GB</title>"
        "</head><body>...</body></html>",
        "Just a moment...",
    )
    # The reverse: stale release title supplied, live challenge <title> parsed.
    assert strong_challenge_markers(
        "<html><head><title>Just a moment...</title></head><body>...</body></html>",
        "Access Denied 2009 1080p BluRay x264 - 8.4 GB",
    )


def _perf(url, headers, rtype="Document"):
    import json
    return {
        "message": json.dumps(
            {
                "message": {
                    "method": "Network.responseReceived",
                    "params": {
                        "type": rtype,
                        "response": {"url": url, "headers": headers},
                    },
                }
            }
        )
    }


def test_cf_mitigated_ignores_an_iframe_document_response():
    """An IFRAME navigation is also a ``type=Document`` load.

    Verified against a real browser: an outer page with no header and an
    embedded frame carrying ``cf-mitigated: challenge`` produced TWO
    ``type=Document`` responses. Attributing the frame's header to the page
    would turn an embedded widget into a source-wide interstitial.
    """
    page = "https://hdencode.org/a-release/"
    entries = [
        _perf(page, {"content-type": "text/html"}),
        _perf(
            "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/if",
            {"cf-mitigated": "challenge"},
        ),
    ]
    assert cf_mitigated_from_perf_log(entries, page_url=page) is None


def test_cf_mitigated_matches_despite_a_url_fragment():
    """``driver.current_url`` can carry ``#unlocked``; response URLs never do."""
    page = "https://hdencode.org/a-release/"
    entries = [_perf(page, {"cf-mitigated": "challenge"})]
    assert (
        cf_mitigated_from_perf_log(entries, page_url=f"{page}#unlocked") == "challenge"
    )


def test_cf_mitigated_ignores_an_unrelated_earlier_document():
    """A previous page's challenge must not leak into this navigation."""
    page = "https://hdencode.org/a-release/"
    entries = [
        _perf("https://hdencode.org/another-release/", {"cf-mitigated": "challenge"}),
        _perf(page, {"content-type": "text/html"}),
    ]
    assert cf_mitigated_from_perf_log(entries, page_url=page) is None


def test_cf_mitigated_resolves_a_redirect_chain_to_the_displayed_url():
    """Only the finally-displayed URL counts; intermediate hops do not."""
    page = "https://hdencode.org/a-release/"
    entries = [
        _perf("https://hdencode.org/old-path/", {"cf-mitigated": "challenge"}),
        _perf(page, {"cf-mitigated": "challenge"}),
    ]
    assert cf_mitigated_from_perf_log(entries, page_url=page) == "challenge"

    # The displayed URL is clean even though a hop was challenged.
    entries = [
        _perf("https://hdencode.org/old-path/", {"cf-mitigated": "challenge"}),
        _perf(page, {"content-type": "text/html"}),
    ]
    assert cf_mitigated_from_perf_log(entries, page_url=page) is None


def test_cf_mitigated_requires_a_known_displayed_url():
    """Without a displayed URL, ownership cannot be proven — fail to no signal."""
    entries = [_perf("https://hdencode.org/a-release/", {"cf-mitigated": "challenge"})]
    assert cf_mitigated_from_perf_log(entries) is None


def test_cf_mitigated_reads_only_the_main_document_response():
    page = "https://hdencode.org/a-release/"

    # Document response carrying the header.
    assert (
        cf_mitigated_from_perf_log(
            [_perf(page, {"cf-mitigated": "challenge"})], page_url=page
        )
        == "challenge"
    )
    # Header casing and padding are normalised.
    assert (
        cf_mitigated_from_perf_log(
            [_perf(page, {"CF-Mitigated": " Challenge "})], page_url=page
        )
        == "challenge"
    )
    # A sub-resource (an embedded Turnstile widget's own request) is NOT the
    # interstitial and must never be read as one.
    assert (
        cf_mitigated_from_perf_log(
            [
                _perf(page, {"content-type": "text/html"}),
                _perf(
                    "https://challenges.cloudflare.com/turnstile/v0/api.js",
                    {"cf-mitigated": "challenge"},
                    rtype="Script",
                ),
            ],
            page_url=page,
        )
        is None
    )
    # No document response, malformed records, and an empty log are all "no
    # signal" rather than errors.
    assert cf_mitigated_from_perf_log([]) is None
    assert cf_mitigated_from_perf_log([{"message": "not json"}]) is None
    assert cf_mitigated_from_perf_log(None) is None


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


def test_batch_route_schedules_durable_queue_without_inline_transport():
    from backend.api.routes import downloads as download_routes

    dl = MagicMock()
    queue = MagicMock()
    queue.schedule_batch.return_value = {
        "batch_uuid": "batch-1",
        "total_items": 3,
        "mode": "staggered",
        "interval_seconds": 600,
        "items": [],
    }
    reg = SimpleNamespace(
        download=dl,
        download_queue=queue,
        config={
            "download_batch_interval_minutes": 10,
            "download_queue_auto_resume_after_cooldown": False,
        },
    )
    tasks = BackgroundTasks()
    request = download_routes.BatchDownloadRequest(items=[
        download_routes.DownloadRequest(
            url=f"https://hdencode.org/release/{index}",
            title=f"Title {index}",
        )
        for index in range(3)
    ])

    response = download_routes.download_batch(request, tasks, reg)

    assert response == {
        "status": "scheduled",
        "count": 3,
        "batch_uuid": "batch-1",
        "mode": "staggered",
        "interval_minutes": 10,
        "items": [],
    }
    assert tasks.tasks == []
    assert dl.download_item.call_count == 0
    queue.schedule_batch.assert_called_once()
    args, kwargs = queue.schedule_batch.call_args
    assert len(args[0]) == 3
    assert kwargs == {
        "interval_minutes": 10,
        "mode": "staggered",
        "auto_resume_after_cooldown": False,
    }


def test_cf_mitigated_observation_reports_matched_without_false_warning():
    """A clean displayed page must report matched, not "nothing matched".

    The parser normalizes fragments; a caller re-deriving the match by testing
    its own raw ``page_url`` (which carries ``#unlocked`` after the unlock form
    navigates) against the collected documents would wrongly conclude nothing
    matched on an ordinary grab.
    """
    page = "https://hdencode.org/a-release/"
    entries = [_perf(page, {"content-type": "text/html"})]
    obs: dict = {}
    assert (
        cf_mitigated_from_perf_log(
            entries, page_url=f"{page}#unlocked", observation=obs
        )
        is None
    )
    assert obs["matched"] is True
    assert obs["unmatched_challenges"] == 0
    assert obs["documents"] == [page]


def test_cf_mitigated_observation_counts_only_unmatched_challenge_headers():
    """An ordinary iframe document must not raise a warning; a challenged one must."""
    page = "https://hdencode.org/a-release/"

    quiet = [
        _perf(page, {"content-type": "text/html"}),
        _perf("https://ads.example/frame", {"content-type": "text/html"}),
    ]
    obs: dict = {}
    assert cf_mitigated_from_perf_log(quiet, page_url=page, observation=obs) is None
    assert obs["matched"] is True
    assert obs["unmatched_challenges"] == 0

    noisy = [
        _perf(page, {"content-type": "text/html"}),
        _perf(
            "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/if",
            {"cf-mitigated": "challenge"},
        ),
    ]
    obs = {}
    assert cf_mitigated_from_perf_log(noisy, page_url=page, observation=obs) is None
    assert obs["matched"] is True
    assert obs["unmatched_challenges"] == 1


def test_post_reveal_click_challenge_is_captured_not_reported_as_missing_links():
    """Cloudflare can challenge the unlock POST after a clean initial page load.

    The reveal click is a NEW top-level navigation. Without re-running the
    capture after it, ``_last_cf_mitigated`` keeps the FIRST navigation's value
    (None) and the run is misreported as "no links found" instead of a
    source-wide interactive challenge. The challenge page here carries only
    localized text, so the header is the ONLY evidence.
    """
    from unittest.mock import patch

    from backend.download_service import DownloadService

    page = "https://hdencode.org/a-release/"
    svc = DownloadService(config={}, db=MagicMock())

    driver = MagicMock()
    driver.current_url = f"{page}#unlocked"
    driver.title = "Nur einen Moment"          # localized: no English marker
    driver.page_source = "<html><body><p>Bitte warten</p></body></html>"
    driver.find_elements.return_value = []
    svc.cached_driver = driver

    # First drain: clean initial navigation. Second: the challenged unlock POST.
    driver.get_log.side_effect = [
        [_perf(page, {"content-type": "text/html"})],
        [_perf(page, {"cf-mitigated": "challenge"})],
    ]

    reveal = MagicMock()
    reveal.get_attribute.return_value = "View links"

    with patch("backend.download_service._ensure_selenium"), \
            patch.object(svc, "_find_reveal_control", return_value=reveal), \
            patch("backend.download_service._WebDriverWait"), \
            patch("backend.download_service._EC"), \
            patch("backend.download_service._By"):
        result = svc.scrape_links(page, "Rapidgator")

    assert result.diagnostic is not None
    assert result.diagnostic.code == ScrapeCode.INTERACTIVE_CHALLENGE
    assert svc._last_cf_mitigated == "challenge"
    # Both navigations were drained: the initial one and the post-click one.
    assert driver.get_log.call_count == 2
