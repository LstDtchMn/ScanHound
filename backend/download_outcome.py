"""Public-safe download outcome helpers."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic


_FAILURE_TITLES = {
    ScrapeCode.SOURCE_DISABLED.value: "HDEncode is disabled",
    ScrapeCode.SOURCE_TEMPORARILY_BLOCKED.value: "Download deferred",
    ScrapeCode.INTERACTIVE_CHALLENGE.value: "HDEncode verification required",
    ScrapeCode.BROWSER_LAUNCH_FAILED.value: "Browser could not start",
    ScrapeCode.BROWSER_NETWORK_ERROR.value: "HDEncode could not be reached",
    ScrapeCode.BROWSER_NAVIGATION_FAILED.value: "Page navigation failed",
    ScrapeCode.LAYOUT_CHANGED.value: "HDEncode page changed",
    ScrapeCode.REQUESTED_HOST_MISSING.value: "Requested host unavailable",
    ScrapeCode.NO_FILE_HOST_LINKS.value: "No supported links found",
    ScrapeCode.SCRAPE_EXCEPTION.value: "Link retrieval failed",
}

_SOURCE_WIDE_REASONS = {
    ScrapeCode.SOURCE_DISABLED.value,
    ScrapeCode.SOURCE_TEMPORARILY_BLOCKED.value,
    ScrapeCode.INTERACTIVE_CHALLENGE.value,
}


# Active interactive-challenge evidence. A source-wide challenge must be proven
# by a RENDERED challenge — a challenge iframe, a challenge page title, or
# visible challenge body text — never by a dormant reference to challenge
# infrastructure that appears only inside a <script>, preload URL, JavaScript
# configuration object, comment, or other non-active raw HTML.
CHALLENGE_IFRAME_MARKERS = (
    "turnstile",
    "challenges.cloudflare",
    "recaptcha",
    "hcaptcha",
    "captcha",
)
_CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "access denied",
)
_CHALLENGE_VISIBLE_MARKERS = (
    "checking your browser",
    "verify you are human",
)


def challenge_iframe_srcs(html: str) -> tuple[str, ...]:
    """Return iframe ``src`` values that identify active challenge infrastructure.

    Only a rendered ``<iframe>`` counts. A challenge marker that appears solely
    inside a ``<script>``, preload URL, JavaScript config object, comment, or any
    other non-iframe raw-HTML reference is dormant and is never returned.
    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return ()
    hits = []
    for frame in soup.find_all("iframe"):
        src = frame.get("src") or ""
        if any(marker in src.lower() for marker in CHALLENGE_IFRAME_MARKERS):
            hits.append(src)
    return tuple(hits)


def strong_challenge_markers(html: str, title: str = "") -> tuple[str, ...]:
    """Return active interactive-challenge evidence markers, or ``()`` for none.

    A source-wide interactive challenge requires ACTIVE evidence:

    1. a rendered challenge iframe whose ``src`` identifies Turnstile,
       Cloudflare Challenges, reCAPTCHA, hCaptcha, or captcha infrastructure;
    2. a challenge-specific page ``<title>`` (or supplied title) such as
       "Just a moment", "Attention required", "Checking your browser",
       "Verify you are human", or "Access denied"; or
    3. visible challenge body text ("checking your browser",
       "verify you are human").

    Dormant Turnstile/Cloudflare/reCAPTCHA references that appear only inside a
    ``<script>``, preload URL, JavaScript config, comment, or other non-active
    raw HTML are NOT evidence and never yield a challenge classification.
    """
    title_low = (title or "").lower()
    iframe_srcs: tuple[str, ...] = ()
    doc_title = ""
    visible = ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "html.parser")
        iframe_srcs = tuple(
            frame.get("src") or "" for frame in soup.find_all("iframe")
        )
        if soup.title is not None:
            doc_title = (soup.title.get_text() or "").lower()
        # Visible text only: drop <script>/<style>/<template>/<noscript> so a JS
        # string literal (e.g. a Turnstile config containing "verify you are
        # human") cannot be mistaken for rendered challenge copy.
        for tag in soup(["script", "style", "template", "noscript"]):
            tag.decompose()
        visible = " ".join((soup.get_text(" ") or "").split()).lower()
    except Exception:
        pass

    markers: list[str] = []
    for src in iframe_srcs:
        low = src.lower()
        matched = next(
            (marker for marker in CHALLENGE_IFRAME_MARKERS if marker in low),
            None,
        )
        if matched:
            markers.append(f"iframe:{matched}")
    title_haystack = f"{title_low} {doc_title}".strip()
    markers.extend(
        marker for marker in _CHALLENGE_TITLE_MARKERS if marker in title_haystack
    )
    markers.extend(
        marker for marker in _CHALLENGE_VISIBLE_MARKERS if marker in visible
    )
    return tuple(dict.fromkeys(markers))


def diagnostic_from_traffic_denial(exc: BaseException) -> ScrapeDiagnostic:
    state = getattr(exc, "state", None)
    cause = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or state
    until = getattr(exc, "cooldown_until", None)
    if cause == ScrapeCode.SOURCE_DISABLED.value or state == "disabled":
        return ScrapeDiagnostic(
            ScrapeCode.SOURCE_DISABLED,
            retryable=False,
            stage="source_gate",
            cause_code=ScrapeCode.SOURCE_DISABLED.value,
            transport_attempted=False,
            affected_scope="source",
            retry_mode="configuration_change",
            action_code="open_settings",
            health_owner="coordinator",
        )
    return ScrapeDiagnostic(
        ScrapeCode.SOURCE_TEMPORARILY_BLOCKED,
        retryable=True,
        stage="source_gate",
        cause_code=str(cause or "cooldown"),
        cooldown_until=until,
        transport_attempted=False,
        affected_scope="source",
        retry_mode=(
            "manual_verification"
            if cause == ScrapeCode.INTERACTIVE_CHALLENGE.value
            else "after_time"
        ),
        action_code=(
            "verification_required"
            if cause == ScrapeCode.INTERACTIVE_CHALLENGE.value
            else "wait_until"
        ),
        deferred=True,
        health_owner="coordinator",
    )


def public_download_result(
    result: Optional[Mapping[str, Any]],
    *,
    title: str = "",
    url: str = "",
) -> dict:
    source = dict(result or {})
    signals = [str(value) for value in source.get("signals", []) if value is not None]
    return {
        "title": title,
        "url": url,
        "success": bool(source.get("success")),
        "deferred": bool(source.get("deferred")),
        "method": str(source.get("method") or ""),
        "link_count": int(source.get("link_count") or 0),
        "message": str(source.get("message") or ""),
        "reason_code": source.get("reason_code"),
        "cause_code": source.get("cause_code"),
        "stage": source.get("stage"),
        "retryable": bool(source.get("retryable")),
        "retry_mode": source.get("retry_mode"),
        "cooldown_until": source.get("cooldown_until"),
        "transport_attempted": source.get("transport_attempted"),
        "affected_scope": source.get("affected_scope") or "item",
        "action_code": source.get("action_code"),
        "signals": signals,
    }


def notification_for_result(result: Mapping[str, Any], *, title: str) -> dict:
    payload = public_download_result(result, title=title)
    reason = payload.get("reason_code")
    body = payload.get("message") or f"Download failed: {title}"
    until = payload.get("cooldown_until")
    if until and reason == ScrapeCode.SOURCE_TEMPORARILY_BLOCKED.value:
        body = f"{body} Retry after {until}."
    return {
        "title": _FAILURE_TITLES.get(reason, "Download Failed"),
        "body": body,
        "priority": "warning" if payload.get("deferred") else "high",
        **{
            key: payload.get(key)
            for key in (
                "reason_code",
                "cause_code",
                "stage",
                "retryable",
                "retry_mode",
                "cooldown_until",
                "transport_attempted",
                "affected_scope",
                "action_code",
                "deferred",
                "signals",
            )
        },
    }


def is_source_wide_denial(result: Mapping[str, Any]) -> bool:
    return (
        not bool(result.get("success"))
        and result.get("affected_scope") == "source"
        and result.get("reason_code") in _SOURCE_WIDE_REASONS
    )


def deferred_result(
    blocker: Mapping[str, Any],
    *,
    title: str,
    url: str,
) -> dict:
    cause = blocker.get("cause_code") or blocker.get("reason_code")
    blocker_reason = blocker.get("reason_code")
    until = blocker.get("cooldown_until")
    disabled = blocker_reason == ScrapeCode.SOURCE_DISABLED.value
    return {
        "title": title,
        "url": url,
        "success": False,
        "deferred": True,
        "method": "",
        "link_count": 0,
        "message": (
            "No request was made for this title because HDEncode is disabled."
            if disabled
            else "No request was made for this title because HDEncode is temporarily paused after a source-wide failure."
        ),
        "reason_code": (
            ScrapeCode.SOURCE_DISABLED.value
            if disabled
            else ScrapeCode.SOURCE_TEMPORARILY_BLOCKED.value
        ),
        "cause_code": cause,
        "stage": "source_gate",
        "retryable": not disabled,
        "retry_mode": (
            "configuration_change"
            if disabled
            else "after_time" if until else "manual_verification"
        ),
        "cooldown_until": until,
        "transport_attempted": False,
        "affected_scope": "source",
        "action_code": (
            "open_settings"
            if disabled
            else "wait_until" if until else "verification_required"
        ),
        "signals": [],
    }
