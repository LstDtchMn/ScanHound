"""Public-safe download outcome helpers."""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional, Sequence

from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic


_FAILURE_TITLES = {
    ScrapeCode.SOURCE_DISABLED.value: "HDEncode is disabled",
    ScrapeCode.SOURCE_TEMPORARILY_BLOCKED.value: "Download deferred",
    # "HDEncode verification required" until 2026-08-09. It read as an
    # instruction -- go and verify something -- and there is nothing the reader
    # can go and verify. The title now names the state, and the message says
    # what did not happen.
    ScrapeCode.INTERACTIVE_CHALLENGE.value: "Manual attention required",
    ScrapeCode.BROWSER_LAUNCH_FAILED.value: "Browser could not start",
    ScrapeCode.BROWSER_NETWORK_ERROR.value: "HDEncode could not be reached",
    ScrapeCode.BROWSER_NAVIGATION_FAILED.value: "Page navigation failed",
    ScrapeCode.LAYOUT_CHANGED.value: "HDEncode page changed",
    ScrapeCode.REQUESTED_HOST_MISSING.value: "Requested host unavailable",
    ScrapeCode.NO_FILE_HOST_LINKS.value: "No supported links found",
    ScrapeCode.SCRAPE_EXCEPTION.value: "Link retrieval failed",
    # FOUND BY THE NEW EXHAUSTIVENESS TEST, not by review. I added
    # REVEAL_VERIFICATION_STALLED earlier this session precisely so a source
    # throttle would stop being reported as a broken scraper -- and then left it
    # out of this map, so `.get(reason, "Download Failed")` rendered it as
    # "Download Failed". The item's own message said "nothing is wrong with this
    # release" underneath a title that said the opposite, on the exact code behind
    # the 45 items currently parked in cooldown. The fix I shipped was undone in
    # the UI by the omission.
    # NO CAUSAL CLAIM. This said "HDEncode is throttling" until 2026-08-09,
    # when the throttle attribution was refuted twice over: the user opened the
    # exact stalled URL on a phone browser and the links appeared with almost no
    # wait, and six later loads from ScanHound's own browser found the reveal
    # control ready and enabled. The reason CODE was always neutral; only the
    # rendered title asserted a cause, and it is the title the reader believes.
    # What is observed is that the control did not clear inside our window --
    # not why.
    ScrapeCode.REVEAL_VERIFICATION_STALLED.value: (
        "HDEncode links did not unlock in time"
    ),
    # Reached only when the link IS a direct file host we identify but cannot hand
    # off; the ordinary direct-host path clears this diagnostic before it is ever
    # rendered. Titled without "HDEncode" on purpose -- these two codes are for
    # URLs that are not HDEncode, which is the whole reason they exist.
    ScrapeCode.DIRECT_LINK_NO_SOURCE_PAGE.value: "Direct link not supported",
    ScrapeCode.UNSUPPORTED_SOURCE.value: "Website not supported",
}

_SOURCE_WIDE_REASONS = {
    ScrapeCode.SOURCE_DISABLED.value,
    ScrapeCode.SOURCE_TEMPORARILY_BLOCKED.value,
    ScrapeCode.INTERACTIVE_CHALLENGE.value,
    # A stalled link-reveal verification throttles the whole source, not one
    # item: once HDEncode stops clearing the countdown, every subsequent item in
    # the queue meets the same closed door. Without membership here,
    # is_source_wide_denial returns False, the outcome routes to _fail instead of
    # _pause_for_source, and the batch grinds on converting the rest of the queue
    # into permanent failures -- which is exactly how 78 items accumulated.
    ScrapeCode.REVEAL_VERIFICATION_STALLED.value,
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

# A release page title can legitimately contain a challenge phrase — there are
# real releases named "Access Denied" and "Just a Moment" — and treating those
# as a challenge starts a bogus one-hour source-wide cooldown. A Cloudflare
# interstitial REPLACES the page, so its title never carries release metadata
# (resolution, size, codec, source). When that metadata is present the title
# belongs to a release page and its challenge phrase is not evidence.
_RELEASE_TITLE_METADATA = re.compile(
    r"\b(?:\d{3,4}p|\d+(?:\.\d+)?\s*[GM]B|x26[45]|h\.?26[45]|hevc|avc|"
    r"blu-?ray|web-?dl|webrip|hdrip|bdrip|dvdrip|remux)\b",
    re.IGNORECASE,
)


# Cloudflare sets this response header on every Challenge Page type. It is
# language- and template-independent, so it recognises custom or localized
# challenge pages that carry none of the English phrases above — and, unlike a
# dormant Turnstile script, it is only present on an actual interstitial.
CF_MITIGATED_HEADER = "cf-mitigated"
CF_MITIGATED_CHALLENGE = "challenge"


def _without_fragment(value: str) -> str:
    """Return a URL without its fragment.

    ``driver.current_url`` can carry a fragment (the unlock form navigates to
    ``#unlocked``) while an HTTP response URL never does, so the two must be
    normalized before they can be compared.
    """
    from urllib.parse import urldefrag

    return urldefrag(value or "")[0]


def cf_mitigated_from_perf_log(
    entries, *, page_url: str = "", observation: Optional[dict] = None
) -> Optional[str]:
    """Return the DISPLAYED page's ``cf-mitigated`` value, or ``None``.

    ``entries`` are raw Chrome performance-log records.

    ``type == "Document"`` is a *resource* type, not proof of top-level
    ownership: an **iframe** navigation is also a document load, verified
    against a real browser — an embedded widget produced its own
    ``type=Document`` response carrying ``cf-mitigated: challenge`` while the
    top-level page had no such header. Attributing that to the page would turn
    an embedded widget into a source-wide interstitial, so the value is taken
    only from the response whose URL matches the displayed page, compared with
    fragments removed. There is deliberately **no fallback** to "the most
    recent document": an unmatched log yields ``None``.

    ``None`` means "no signal" — no matching document response, header absent,
    or the log was unavailable. It never means "no challenge", so callers must
    fall back to the other evidence rather than treating absence as safety.

    Pass ``observation`` (a dict) to receive telemetry about what was seen:

    ``documents``
        every document URL encountered, fragment-stripped.
    ``matched``
        whether any document response was the displayed page. The caller must
        NOT re-derive this by testing its own ``page_url`` against
        ``documents`` — those are normalized here and a raw ``page_url``
        carrying a fragment (the unlock form navigates to ``#unlocked``) would
        never compare equal, producing a false "nothing matched" report on a
        perfectly ordinary grab.
    ``unmatched_challenges``
        how many NON-displayed documents carried ``cf-mitigated: challenge``.
        Only this warrants a warning: an ordinary iframe document with no such
        header is unremarkable and must stay quiet.
    """
    import json

    if observation is not None:
        observation.setdefault("documents", [])
        observation.setdefault("matched", False)
        observation.setdefault("unmatched_challenges", 0)

    target = _without_fragment(page_url)
    if not target:
        # Without a displayed URL, ownership cannot be proven at all.
        return None

    matched: Optional[str] = None
    for entry in entries or ():
        try:
            message = json.loads(entry.get("message") or "{}").get("message") or {}
        except Exception:
            continue
        if message.get("method") != "Network.responseReceived":
            continue
        params = message.get("params") or {}
        if params.get("type") != "Document":
            continue
        response = params.get("response") or {}
        document_url = _without_fragment(response.get("url") or "")
        if document_url and observation is not None:
            # Lets the caller see that documents WERE captured but none was the
            # displayed page, so a silent None can be told apart from "the log
            # had nothing in it".
            observation["documents"].append(document_url)
        headers = {
            str(key).lower(): value
            for key, value in (response.get("headers") or {}).items()
        }
        value = headers.get(CF_MITIGATED_HEADER)
        if document_url != target:
            # A challenge header on a document that is NOT the displayed page
            # cannot be attributed to the page (that was the iframe blocker),
            # but it is worth counting: it is the only signal that would
            # otherwise vanish silently.
            if (
                observation is not None
                and value is not None
                and str(value).strip().lower() == CF_MITIGATED_CHALLENGE
            ):
                observation["unmatched_challenges"] += 1
            continue
        if observation is not None:
            observation["matched"] = True
        # Later responses for the same URL supersede earlier ones, so a redirect
        # chain resolves to whatever was finally displayed.
        matched = str(value).strip().lower() if value is not None else None
    return matched


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
    # Evaluate the supplied title and the document <title> INDEPENDENTLY. They
    # are separate reads and can reflect different moments during navigation or
    # dynamic replacement, so a stale release title must never suppress a live
    # challenge title (or vice versa). Fail closed: a challenge phrase in either
    # source counts, provided that source is not itself a release title.
    for candidate in dict.fromkeys(
        part for part in (title_low, doc_title) if part.strip()
    ):
        if _RELEASE_TITLE_METADATA.search(candidate):
            continue
        markers.extend(
            marker for marker in _CHALLENGE_TITLE_MARKERS if marker in candidate
        )
    markers.extend(
        marker for marker in _CHALLENGE_VISIBLE_MARKERS if marker in visible
    )
    return tuple(dict.fromkeys(markers))


# ── ACTIVE TURNSTILE EVIDENCE ───────────────────────────────────────────────
#
# MEASURED ON THE LIVE STALLED PAGE, 2026-08-09, before any of this was written.
# The measurements are what the rules below are for; without them this would be
# a list of plausible selectors, and two of the plausible ones are wrong.
#
#   * `input[name="cf-turnstile-response"]` EXISTS in the reveal form and its
#     value is empty. It is rendered by turnstile.render(), so it is present
#     only once a widget really exists -- unlike the api.js <script> tag.
#   * There is NO `.cf-turnstile` container and NO `data-sitekey` attribute:
#     hdencode renders the widget programmatically into `#turnstile-container-
#     <hash>`. A detector keyed on the documented Cloudflare markup finds
#     nothing here.
#   * There is NO reachable challenge <iframe> either. The widget runs in
#     INVISIBLE mode: it creates a frame, fails, and tears it down, retrying
#     about every 11 seconds. A DOM read lands between attempts more often than
#     not, so iframe presence is a race, not a signal.
#   * The console carries `[Cloudflare Turnstile] Error: 600010.` repeatedly.
#
# So the response field and the console error are the two signals that actually
# fire on the page this exists for. The container and iframe checks are kept
# because they are correct where they DO apply and cost nothing -- not because
# they were observed working here.
_TURNSTILE_RESPONSE_FIELD = "cf-turnstile-response"

# The 600 family is Cloudflare's GENERIC client-side challenge-execution
# failure. Matching the family rather than 600010 is deliberate: the specific
# code is an observation from one page on one day, not a contract, and pinning
# it would make the detector silently stop working when Cloudflare emits a
# sibling code for the same condition.
_TURNSTILE_CONSOLE_CODE = re.compile(r"\b(600\d{3})\b")


def _form_posts_unlock(form, unlock_target: Callable[[str], bool]) -> bool:
    """True when a form's EFFECTIVE destination is this page's unlock endpoint.

    Mirrors the reveal-control rule exactly: a submit may override its form's
    destination via ``formaction``, so the effective target is the submit's
    ``formaction`` when present and the form's ``action`` otherwise.

    Peer review caught the response-field check reusing the URL predicate but
    NOT this rule -- it read ``form.action`` alone. Both halves of that gap are
    wrong in a direction that matters: a form whose action looks safe while its
    submit posts the unlock endpoint would have been missed, and a form whose
    action is the unlock endpoint while its submit posts elsewhere would have
    counted. Two copies of "where does this actually post" is precisely the
    drift this codebase keeps paying for.
    """
    action = form.get("action") or ""
    submits = form.find_all(["input", "button"])
    targets = [
        (element.get("formaction") or action)
        for element in submits
        if (element.name == "button"
            or str(element.get("type") or "").lower() == "submit")
    ]
    if not targets:
        targets = [action]
    return any(unlock_target(target) for target in targets)


def turnstile_challenge_evidence(
    html: str,
    *,
    console_entries: Sequence[Mapping[str, Any]] = (),
    unlock_target: Optional[Callable[[str], bool]] = None,
) -> tuple[str, ...]:
    """Return markers proving an ACTIVE, UNSOLVED Turnstile challenge, or ``()``.

    ``console_entries`` MUST already be scoped to the current navigation. This
    function cannot tell a fresh error from one a previous page left in a
    persistent browser session, so the caller drains the log at navigation start
    and passes only what arrived afterwards. Getting that wrong would let one
    stalled page classify the next several.

    ``unlock_target`` is the caller's own "does this destination resolve to
    THIS page's unlock endpoint?" predicate, injected rather than reimplemented
    so there is exactly one copy of that rule. When supplied, the response field
    only counts if it sits in a form that posts the reveal endpoint -- a captcha
    belonging to the page's comment form is not evidence about the reveal.

    NOT evidence, each for a reason:

    ``<script src=".../turnstile/v0/api.js">``
        Dormant. hdencode ships it in ``<head>`` on every release page,
        including the ones that reveal links perfectly.
    the word "cloudflare" anywhere in the HTML
        The whole site is behind Cloudflare. It is true on every page.
    "Verifying… Please wait"
        The placeholder label. It is the SYMPTOM this code is trying to
        explain, and it is one site re-wording away from meaning nothing.
    a populated ``cf-turnstile-response`` value
        That is a challenge that SUCCEEDED. Treating it as failure evidence
        would misclassify the healthy case.
    """
    markers: list[str] = []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        soup = None

    if soup is not None:
        for field in soup.find_all(
            "input", attrs={"name": _TURNSTILE_RESPONSE_FIELD}
        ):
            if (field.get("value") or "").strip():
                # Solved. Not evidence of a failure, and deliberately not
                # recorded as a marker at all.
                continue
            if unlock_target is not None:
                # BOTH association forms. HTML lets an input belong to a form by
                # nesting OR by a `form="<id>"` attribute pointing at one
                # elsewhere in the document. The measured page nests it, but
                # checking only the parent would make a purely cosmetic markup
                # change silently disable the detector -- and the failure would
                # look like "no challenge", the most misleading of all the
                # possible wrong answers here.
                owner = field.get("form")
                form = None
                if owner:
                    form = soup.find("form", id=owner)
                if form is None:
                    form = field.find_parent("form")
                if form is None or not _form_posts_unlock(form, unlock_target):
                    continue
            markers.append("turnstile:unsolved-response-field")
            break

        if soup.select(".cf-turnstile"):
            markers.append("turnstile:widget-container")

        for frame in soup.find_all("iframe"):
            src = (frame.get("src") or "").lower()
            if "challenges.cloudflare.com" in src and "turnstile" in src:
                markers.append("turnstile:challenge-iframe")
                break

    for entry in console_entries or ():
        try:
            message = str(entry.get("message") or "")
        except Exception:
            continue
        if "turnstile" not in message.lower():
            continue
        found = _TURNSTILE_CONSOLE_CODE.search(message)
        if found:
            markers.append(f"turnstile:console-{found.group(1)}")
            break

    return tuple(dict.fromkeys(markers))


def interstitial_challenge_markers(html: str, title: str = "") -> tuple[str, ...]:
    """Markers proving the page ITSELF was replaced by a challenge interstitial.

    THE PARTITION THIS EXISTS FOR, found on peer review 2026-08-09.

    ``strong_challenge_markers`` returns two kinds of evidence that were being
    treated as one:

    * a challenge **page** -- a Cloudflare interstitial that REPLACED the
      release page. Nothing else is on it, so it is source-wide on its own.
    * a challenge **iframe** -- an embedded widget on a page that is otherwise
      perfectly normal. hdencode renders exactly that on release pages which go
      on to hand over links.

    Because the iframe kind classified by itself, a **ready** reveal control on
    a page carrying any turnstile/captcha frame anywhere -- the comments widget
    included -- became a source-wide manual hold. Verified: a page whose submit
    reads "View links" and which contains one Turnstile frame returns
    ``('iframe:turnstile',)`` and was classified INTERACTIVE_CHALLENGE.

    So only the interstitial kind is returned here. Embedded frames are handed
    to the reveal conjunction instead, where they must coincide with a
    not-ready control before they mean anything.
    """
    return tuple(
        marker
        for marker in strong_challenge_markers(html, title)
        if not marker.startswith("iframe:")
    )


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
        # Carried through so the queue can tell a real source delivery from a
        # pre-scrape duplicate. Dropping it here would make the producer's signal
        # invisible to its only consumer.
        "source_progress": bool(source.get("source_progress")),
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
