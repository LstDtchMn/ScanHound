"""A verification hold a timer cannot release.

THE DEFECT THIS PINS, verified empirically on 2026-08-09 before any code changed:

    decide(ItemFacts(state="verification_required",
                     queue_reason="interactive_challenge",
                     cooldown_until=<expired>), shared, now)  ->  AUTHORISED

`verification_required` is in DEFERRED_STATES and `interactive_challenge` is in
RECOGNISED_REASONS, so the row passed ownership and fell through to the cooldown
comparison — meaning a TIMER alone released a hold whose entire meaning is "an
interactive challenge our automated browser cannot complete is in the way". The
measured cause of the 2026-08 stall is a Cloudflare Turnstile challenge failing
with a 600*-family error in ScanHound's Chromium while a human browser passes it
in under a second; reclassifying Turnstile as INTERACTIVE_CHALLENGE without this
hold would therefore have fed every item straight back into the same failing
challenge on a schedule.

Three layers are covered, because each has its own way to regress:

  * the PURE POLICY — VERIFICATION_HOLD exists, a timer never crosses it, and
    its operator action never promises that retrying completes verification;
  * DETECTION — active Turnstile evidence is a conjunction with the not-ready
    reveal state, and dormant references are never evidence (the previous
    detector matched "show" inside a "TV Shows" navigation link);
  * the CONSUMER — the queue engine parks the episode, promotes nothing on
    timer expiry, allows a single explicit probe, and releases the siblings
    only when a probe genuinely delivers (the load-bearing negative control).
"""
from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.database import DatabaseManager
from backend.download_outcome import (
    TURNSTILE_CAUSE_CODE,
    is_source_wide_denial,
    is_turnstile_console_failure,
    turnstile_challenge_evidence,
)
from backend.download_queue import DownloadQueueError, DownloadQueueService
from backend.queue_recovery_policy import (
    ACTION_ADVICE, ACTION_ATTENTION_REQUIRED, AUTHORISED, NEEDS_HUMAN,
    SAFETY_HOLD, UNOWNED_REASON, VERIFICATION_HOLD, ItemFacts, SharedFacts,
    action_for, decide,
)
from backend.scrape_outcome import ScrapeCode

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
PAST = NOW - timedelta(days=1)
FUTURE = NOW + timedelta(days=1)
PAST_S = PAST.isoformat()
FUTURE_S = FUTURE.isoformat()


@pytest.fixture(autouse=True)
def _pin_production_clock():
    """Same pin as test_queue_recovery_policy; see its docstring for the bomb."""
    with patch("backend.download_queue._utcnow", return_value=NOW):
        yield


def _shared(cooldown=None, *, enabled=True, used=0, delivered=0, mark=0, cap=3,
            hold=False):
    return SharedFacts(cooldown_until=cooldown, auto_resume_enabled=enabled,
                       attempts_used=used, source_delivery_count=delivered,
                       progress_mark=mark, max_attempts=cap,
                       verification_hold=hold)


def _item(cooldown=None, *, reason="interactive_challenge", last="",
          state="verification_required"):
    return ItemFacts(state=state, cooldown_until=cooldown, queue_reason=reason,
                     last_reason_code=last)


# ─────────────────────────────────────────────────────────────────────────────
# The pure policy
# ─────────────────────────────────────────────────────────────────────────────

def test_the_blocking_defect_is_closed():
    """The EXACT facts measured on 2026-08-09, which returned AUTHORISED."""
    got = decide(_item(PAST), _shared(PAST), NOW)
    assert got == VERIFICATION_HOLD, (
        f"an expired cooldown released a human-verification hold: {got}")


@pytest.mark.parametrize("own", [PAST, FUTURE, None])
@pytest.mark.parametrize("shared_cd", [PAST, FUTURE, None])
def test_no_temporal_combination_releases_a_challenge_row(own, shared_cd):
    """The full time matrix: queue_reason=interactive_challenge never resolves
    to a time-based decision, let alone AUTHORISED."""
    got = decide(_item(own), _shared(shared_cd), NOW)
    assert got == VERIFICATION_HOLD, (
        f"(own={own}, shared={shared_cd}) -> {got}; a timer must have no say")


@pytest.mark.parametrize("own", [PAST, FUTURE, None])
def test_the_shared_hold_parks_source_deferred_siblings(own):
    """The siblings are ordinary source_deferred rows; the batch's hold is what
    must stop them being promoted one by one into the failing challenge."""
    item = _item(own, reason="source_deferred", state="waiting_source")
    assert decide(item, _shared(PAST, hold=True), NOW) == VERIFICATION_HOLD


def test_without_the_hold_source_deferred_behaviour_is_unchanged():
    """The discrimination: an ordinary throttle pause must keep auto-resuming."""
    item = _item(PAST, reason="source_deferred", state="waiting_source")
    assert decide(item, _shared(PAST, hold=False), NOW) == AUTHORISED


def test_the_hold_outranks_configuration_and_budget():
    """DISABLED says "turn auto-resume on; the items are fine" and BUDGET_SPENT
    says "safe to resume explicitly" — both are wrong advice for a challenge
    row, so the hold must be decided before either."""
    assert decide(_item(PAST), _shared(PAST, enabled=False), NOW) == VERIFICATION_HOLD
    assert decide(_item(PAST), _shared(PAST, used=3, cap=3), NOW) == VERIFICATION_HOLD


def test_safety_still_outranks_the_hold():
    """An unknown outcome must be adjudicated FIRST, whatever else is true."""
    got = decide(_item(PAST, last="operation_timeout_unknown"), _shared(PAST), NOW)
    assert got == SAFETY_HOLD


def test_ownership_still_outranks_the_shared_hold():
    """An unowned reason must keep saying it is unowned; the hold does not
    extend automatic recovery's ownership to rows it never owned."""
    item = _item(PAST, reason="user_batch", state="waiting_source")
    assert decide(item, _shared(PAST, hold=True), NOW) == UNOWNED_REASON


def test_the_hold_needs_a_human_and_its_advice_promises_nothing():
    assert VERIFICATION_HOLD in NEEDS_HUMAN
    assert action_for(VERIFICATION_HOLD) == ACTION_ATTENTION_REQUIRED
    advice = ACTION_ADVICE[ACTION_ATTENTION_REQUIRED].lower()
    assert "manual attention" in advice
    assert "cannot" in advice, "the advice must say the probe cannot complete it"
    assert "safe to resume" not in advice, (
        "a verification hold must never be called safe to resume: " + advice)


# ─────────────────────────────────────────────────────────────────────────────
# Detection: active Turnstile evidence, and everything that must NOT be evidence
# ─────────────────────────────────────────────────────────────────────────────

_RELEASE_PAGE = """
<html><head><title>Some.Release.2026.2160p.WEB-DL – 9.0 GB</title></head>
<body>
  <a href="/tv-shows/">TV Shows</a>
  <form action="#unlocked">
    <input type="submit" value="Verifying… Please wait">{extra}
  </form>
</body></html>
"""


def test_the_response_field_is_evidence():
    html = _RELEASE_PAGE.format(
        extra='<input type="hidden" name="cf-turnstile-response" value="">')
    assert "turnstile:response-field" in turnstile_challenge_evidence(html)


def test_the_container_is_evidence():
    html = _RELEASE_PAGE.format(extra='<div class="cf-turnstile"></div>')
    assert "turnstile:container" in turnstile_challenge_evidence(html)


def test_a_challenges_cloudflare_iframe_is_evidence():
    html = _RELEASE_PAGE.format(
        extra='<iframe src="https://challenges.cloudflare.com/cdn-cgi/'
              'challenge-platform/turnstile/if/ov2/"></iframe>')
    assert "turnstile:iframe" in turnstile_challenge_evidence(html)


@pytest.mark.parametrize("code", ["600010", "600321"])
def test_a_console_600_family_error_is_evidence(code):
    """600* is the FAMILY; 600010 is an observation, not a contract."""
    line = (f'https://challenges.cloudflare.com/turnstile/v0/api.js 0:17636 '
            f'"[Cloudflare Turnstile] Error: {code}."')
    assert is_turnstile_console_failure(line)
    html = _RELEASE_PAGE.format(extra="")
    assert "turnstile:console-600" in turnstile_challenge_evidence(html, [line])


def test_non_turnstile_console_lines_are_not_evidence():
    for line in (
        # 600-family number without the Turnstile origin
        'https://hdencode.org/app.js 1:1 "Error: 600010."',
        # Turnstile origin without a 600-family error
        'https://challenges.cloudflare.com/turnstile/v0/api.js 0 '
        '"Failed to execute \'postMessage\' on \'DOMWindow\'"',
        # an unrelated number that merely contains 600
        'https://hdencode.org/app.js 1:1 "Error: 12600010 things."',
    ):
        assert not is_turnstile_console_failure(line), line


def test_a_dormant_script_reference_is_not_evidence():
    html = _RELEASE_PAGE.format(
        extra='<script src="https://challenges.cloudflare.com/turnstile/v0/'
              'api.js"></script>')
    assert turnstile_challenge_evidence(html) == ()


def test_navigation_text_and_raw_cloudflare_words_are_not_evidence():
    """The prior detector matched "show" inside "TV Shows"; this one must not
    key on navigation text, control labels, or the word cloudflare in prose."""
    html = _RELEASE_PAGE.format(
        extra='<p>Mirrors are behind cloudflare. Click show to reveal.</p>')
    assert turnstile_challenge_evidence(html) == ()


# ── response-field discrimination (fold review: ChatGPT + b087aa20) ──────────
# A cf-turnstile-response field is reveal evidence ONLY when it is UNSOLVED and
# belongs to a form that posts the reveal's unlock endpoint.

def _unlock_only(target):
    """Test unlock_target: only a destination ending in #unlocked is the reveal."""
    return (target or "").endswith("#unlocked")


def test_a_solved_response_token_is_not_evidence():
    html = """<html><body><form action="#unlocked">
        <input type="submit" value="View links">
        <input type="hidden" name="cf-turnstile-response" value="SOLVED-TOKEN">
        </form></body></html>"""
    assert turnstile_challenge_evidence(html, unlock_target=_unlock_only) == (), (
        "a populated token is a challenge that SUCCEEDED, not failure evidence")


def test_a_response_field_on_the_comment_form_is_not_reveal_evidence():
    html = """<html><body>
        <form action="#unlocked"><input type="submit" value="View links"></form>
        <form action="/comment/post">
          <input type="hidden" name="cf-turnstile-response" value="">
          <input type="submit" value="Post Comment">
        </form>
        </body></html>"""
    assert turnstile_challenge_evidence(html, unlock_target=_unlock_only) == (), (
        "a Turnstile widget on the comment form is not evidence about the reveal")


def test_an_unsolved_response_field_on_the_unlock_form_is_evidence():
    html = """<html><body><form action="#unlocked">
        <input type="submit" value="Verifying… Please wait">
        <input type="hidden" name="cf-turnstile-response" value="">
        </form></body></html>"""
    assert "turnstile:response-field" in turnstile_challenge_evidence(
        html, unlock_target=_unlock_only)


def test_form_id_ownership_is_honoured():
    # Associated to the unlock form by form="id", not by nesting.
    html = """<html><body>
        <form id="unlock" action="#unlocked">
          <input type="submit" value="Verifying… Please wait">
        </form>
        <input type="hidden" name="cf-turnstile-response" value="" form="unlock">
        </body></html>"""
    assert "turnstile:response-field" in turnstile_challenge_evidence(
        html, unlock_target=_unlock_only)


def test_formaction_override_decides_ownership():
    # The form action posts #unlocked, but the submit's formaction posts /report,
    # so the field's effective form does NOT post the reveal endpoint.
    html = """<html><body><form action="#unlocked">
        <input type="submit" formaction="/report" value="Report">
        <input type="hidden" name="cf-turnstile-response" value="">
        </form></body></html>"""
    assert turnstile_challenge_evidence(html, unlock_target=_unlock_only) == ()


def test_a_non_submit_button_formaction_is_not_a_post_target():
    # Re-review: a button[type=button] CANNOT submit, so its formaction must not
    # make a non-unlock form look like it posts the unlock endpoint (false pos).
    html = """<html><body><form action="/comment">
        <button type="button" formaction="#unlocked">Preview</button>
        <input type="hidden" name="cf-turnstile-response" value="">
        </form></body></html>"""
    assert turnstile_challenge_evidence(html, unlock_target=_unlock_only) == ()


def test_a_non_submit_button_does_not_suppress_the_action_fallback():
    # And the mirror: a non-submit button's formaction must not REPLACE the
    # form's own action, or a genuine unlock form would be missed (false neg).
    html = """<html><body><form action="#unlocked">
        <button type="button" formaction="/report">Report</button>
        <input type="hidden" name="cf-turnstile-response" value="">
        </form></body></html>"""
    assert "turnstile:response-field" in turnstile_challenge_evidence(
        html, unlock_target=_unlock_only)


# ─────────────────────────────────────────────────────────────────────────────
# The classification boundary: not-ready reveal + evidence -> challenge
# (harness pattern shared with test_reveal_verification_throttle)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeDecision:
    cooldown_until = "2026-08-08T20:00:00+00:00"


class _FakeCoordinator:
    def __init__(self):
        self.observed = []

    def observe_challenge(self, reason_code="interactive_challenge"):
        self.observed.append(("challenge", reason_code))
        return _FakeDecision()

    def observe_reveal_stall(self, reason_code="reveal_verification_stalled",
                             **kwargs):
        self.observed.append(("stall", reason_code))
        return _FakeDecision()


@pytest.fixture
def service(monkeypatch):
    from backend import download_service as ds

    coordinator = _FakeCoordinator()
    monkeypatch.setattr(ds, "get_hdencode_coordinator", lambda: coordinator)
    svc = object.__new__(ds.DownloadService)
    svc._log = lambda *a, **k: None
    svc._last_cf_mitigated = None
    return svc, coordinator


def _driver(html, console_lines=()):
    lines = [{"level": "WARNING", "message": line} for line in console_lines]

    class Driver:
        page_source = html
        title = "Some.Release.2026.2160p.WEB-DL – 9.0 GB"
        current_url = "https://hdencode.org/some-release-2026-2160p-9-0-gb/"

        def execute_script(self, *a, **k):
            return None

        def get_log(self, kind):
            if kind == "browser":
                drained, lines[:] = list(lines), []
                return drained
            return []

    return Driver()


def _diagnose(svc, driver):
    return svc._log_page_diagnostics(
        driver, stage="access_control", source_kind="hdencode",
        reveal_tier="not-ready")


def test_not_ready_plus_response_field_is_a_challenge(service):
    svc, coordinator = service
    html = _RELEASE_PAGE.format(
        extra='<input type="hidden" name="cf-turnstile-response" value="">')
    d = _diagnose(svc, _driver(html))
    assert d.code == ScrapeCode.INTERACTIVE_CHALLENGE
    assert d.cause_code == TURNSTILE_CAUSE_CODE
    assert d.affected_scope == "source", "containment must be preserved"
    assert d.retry_mode == "manual_verification"
    assert d.action_code == "verification_required"
    assert coordinator.observed == [("challenge", "interactive_challenge")], (
        "a challenge must be reported under challenge semantics, not stall")
    assert is_source_wide_denial(d.to_dict()), (
        "the queue would route this to _fail and burn the siblings")


def test_not_ready_plus_console_error_is_a_challenge(service):
    svc, _ = service
    line = ('https://challenges.cloudflare.com/turnstile/v0/api.js 0:17636 '
            '"[Cloudflare Turnstile] Error: 600010."')
    d = _diagnose(svc, _driver(_RELEASE_PAGE.format(extra=""), [line]))
    assert d.code == ScrapeCode.INTERACTIVE_CHALLENGE
    assert d.cause_code == TURNSTILE_CAUSE_CODE
    assert "turnstile:console-600" in d.signals


def test_the_evidence_survives_into_the_persisted_message(service):
    """last_message is one of only three persisted fields; the evidence must be
    in it or the next investigation starts from nothing again."""
    svc, _ = service
    html = _RELEASE_PAGE.format(
        extra='<input type="hidden" name="cf-turnstile-response" value="">')
    persisted = _diagnose(svc, _driver(html)).to_dict()["message"]
    assert "[signals:" in persisted and "turnstile:response-field" in persisted


def test_not_ready_with_only_a_dormant_script_stays_a_reveal_stall(service):
    """The fallback (and the false-positive guard): no ACTIVE evidence, no
    challenge — REVEAL_VERIFICATION_STALLED keeps its retry semantics."""
    svc, coordinator = service
    html = _RELEASE_PAGE.format(
        extra='<script src="https://challenges.cloudflare.com/turnstile/v0/'
              'api.js"></script>')
    d = _diagnose(svc, _driver(html))
    assert d.code == ScrapeCode.REVEAL_VERIFICATION_STALLED
    assert d.retryable is True
    assert coordinator.observed == [("stall", "reveal_verification_stalled")]


def test_a_previous_pages_console_error_does_not_classify_this_one(service):
    """NAVIGATION SCOPING. The console log is drained at each navigation
    boundary (_drain_browser_console), so an old page's Turnstile error must
    never classify the next page."""
    svc, _ = service
    line = ('https://challenges.cloudflare.com/turnstile/v0/api.js 0:17636 '
            '"[Cloudflare Turnstile] Error: 600010."')
    driver = _driver(_RELEASE_PAGE.format(extra=""), [line])
    svc._drain_browser_console(driver)          # the navigation boundary
    d = _diagnose(svc, driver)
    assert d.code == ScrapeCode.REVEAL_VERIFICATION_STALLED, (
        "a drained (previous-navigation) console error still classified")


def test_the_console_is_drained_before_navigation_not_after(monkeypatch):
    """Round-2 review, finding 5: the drain must be a PRE-navigation boundary.

    Draining at the top of _wait_past_cloudflare (the old placement) ran AFTER
    driver.get, discarding the current page's own 600* error. Model the console
    as a queue that get_log drains and that navigation appends to: after
    _navigate_with_diagnostic, the PREVIOUS page's error must be gone and the
    CURRENT navigation's error must survive to be read.
    """
    from backend import download_service as ds
    svc = object.__new__(ds.DownloadService)
    svc._log = lambda *a, **k: None
    svc._source_kind_of = lambda u: "ddlbase"      # avoid the HDEncode coordinator
    console = {"lines": [
        "OLD https://challenges.cloudflare.com/turnstile Error: 600010."]}

    driver = MagicMock()

    def _get_log(kind):
        if kind == "browser":
            out, console["lines"] = console["lines"], []
            return [{"message": m} for m in out]
        return []

    def _get(url):
        # The navigation itself emits THIS page's Turnstile failure.
        console["lines"].append(
            "NEW https://challenges.cloudflare.com/turnstile Error: 600321.")

    driver.get_log.side_effect = _get_log
    driver.get.side_effect = _get
    monkeypatch.setattr(svc, "get_driver", lambda **k: driver, raising=False)
    monkeypatch.setattr(svc, "_browser_error_code",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(svc, "_recycle_driver",
                        lambda *a, **k: None, raising=False)

    got, diag = svc._navigate_with_diagnostic(
        "https://ddlbase.com/x/", tag="DDL", attempts=1)
    assert got is driver and diag is None
    lines = svc._browser_console_lines(driver)
    assert any("600321" in line for line in lines), (
        "the current navigation's 600 error was drained away")
    assert not any("600010" in line for line in lines), (
        "a previous page's 600 error survived into this navigation")


# ─────────────────────────────────────────────────────────────────────────────
# The consumer: the queue engine, end to end
# ─────────────────────────────────────────────────────────────────────────────

def _challenge_outcome():
    """What download_item returns when the reveal hits an active Turnstile."""
    return {
        "success": False,
        "deferred": True,
        "method": "",
        "link_count": 0,
        "message": ("The source presented an interactive verification "
                    "challenge that did not clear. [signals: "
                    "turnstile:response-field, reveal-tier:not-ready]"),
        "reason_code": "interactive_challenge",
        "cause_code": TURNSTILE_CAUSE_CODE,
        "stage": "verification",
        "retryable": False,
        "retry_mode": "manual_verification",
        "cooldown_until": (NOW + timedelta(hours=1)).isoformat(),
        "transport_attempted": True,
        "affected_scope": "source",
        "action_code": "verification_required",
        "signals": ["turnstile:response-field", "reveal-tier:not-ready"],
    }


def _success_outcome():
    return {
        "success": True,
        "method": "jdownloader",
        "message": "Sent to JDownloader",
        "link_count": 2,
        "source_progress": True,
        "source_reveal_succeeded": True,
    }


def _reveal_ok_delivery_failed_outcome():
    """HDEncode served the links; the JDownloader hand-off then failed."""
    return {
        "success": False,
        "method": "",
        "message": "Links found but the JDownloader hand-off failed.",
        "link_count": 2,
        "reason_code": "download_failed",
        "stage": "download",
        "retryable": True,
        "transport_attempted": True,
        "affected_scope": "item",
        "source_progress": False,
        "source_reveal_succeeded": True,
    }


def _rig(db, count, interval_minutes=0):
    download = MagicMock()
    download.download_item.return_value = _challenge_outcome()
    svc = DownloadQueueService({}, db, download)
    svc._coordinator_snapshot = MagicMock(return_value={"blocked": False})
    batch = svc.schedule_batch(
        [{"url": f"https://hdencode.org/r-{i}-2160p/", "title": f"R{i}",
          "media_type": "movie"} for i in range(count)],
        interval_minutes=interval_minutes, mode="immediate",
        auto_resume_after_cooldown=True)
    return svc, download, batch["batch_uuid"]


def _drive_to_quiescence(svc, limit=64):
    """Claim and execute every due item until none remain.

    FOLD, from agent/turnstile-classification's stricter harness: a held sibling
    that is wrongly promoted is only caught if something actually TRIES to run
    it. Asserting call_count against a queue nobody drove would pass on an
    implementation that promotes a held row but is never exercised. This runs
    the real claim→execute path, so a promotion becomes a transport attempt.
    """
    for _ in range(limit):
        item = svc._claim_due()
        if item is None:
            return
        svc._execute(item)


def _state_counts(db, batch_uuid):
    rows = db._query_dicts(
        "SELECT state, COUNT(*) n FROM download_queue_items "
        "WHERE batch_uuid=? GROUP BY 1", (batch_uuid,), default=[])
    return {r["state"]: r["n"] for r in rows}


def _hold(db, batch_uuid):
    rows = db._query_dicts(
        "SELECT verification_hold_source FROM download_queue_batches "
        "WHERE batch_uuid=?", (batch_uuid,), default=[])
    return rows[0]["verification_hold_source"] if rows else None


def _expire_every_cooldown(db):
    with db.transaction() as conn:
        conn.execute("UPDATE download_queue_items SET cooldown_until=? "
                     "WHERE cooldown_until IS NOT NULL", (PAST_S,))
        conn.execute("UPDATE download_queue_batches SET cooldown_until=? "
                     "WHERE cooldown_until IS NOT NULL", (PAST_S,))


def test_the_load_bearing_negative_control(tmp_path):
    """22 scheduled items; the first hits an active Turnstile.

    Required: 0 subsequent sibling transport attempts, 0 sibling permanent
    failures, 1 verification-required trigger, 21 source-held siblings — and
    after EVERY cooldown expires, STILL zero automatic challenge retries
    absent an explicit operator action. On the pre-hold code the expiry half
    fails: the timer promoted the whole episode back into the challenge.
    """
    db = DatabaseManager(str(tmp_path / "control.db"))
    try:
        svc, download, batch = _rig(db, count=22)
        item = svc._claim_due()
        assert item is not None, "the first item must be claimable"
        svc._execute(item)

        assert download.download_item.call_count == 1
        counts = _state_counts(db, batch)
        assert counts.get("verification_required") == 1
        assert counts.get("waiting_source") == 21
        assert not counts.get("failed"), "siblings must not become terminal"
        assert _hold(db, batch) == "hdencode", "the episode must carry the hold"

        _expire_every_cooldown(db)
        for _ in range(4):
            svc._maybe_auto_resume()
        # DRIVE the queue: if any held sibling was wrongly promoted, this runs
        # it, turning the bug into a real transport attempt rather than a soft
        # state check that a never-executed queue would pass.
        _drive_to_quiescence(svc)
        assert svc._claim_due() is None, (
            "an expired timer made a held item claimable")
        assert download.download_item.call_count == 1, (
            "an automatic retry re-entered the failing challenge")
        assert _state_counts(db, batch) == counts, (
            "timer expiry changed the episode's state")
    finally:
        db.close()


def test_the_rig_can_promote_when_nothing_is_held(tmp_path):
    """POSITIVE CONTROL for the negative control above.

    FOLD, from agent/turnstile-classification: without this, the 22-item
    negative control passes on a rig that never promotes ANYTHING. Same shape —
    22 items, an ordinary source pause (NO verification hold) — must, after the
    cooldowns expire, promote every deferred row. If this fails, the negative
    control's "nothing ran" proves nothing.
    """
    db = DatabaseManager(str(tmp_path / "positive.db"))
    try:
        download = MagicMock()
        download.download_item.return_value = _success_outcome()
        svc = DownloadQueueService({}, db, download)
        svc._coordinator_snapshot = MagicMock(return_value={"blocked": False})
        batch = svc.schedule_batch(
            [{"url": f"https://hdencode.org/p-{i}-2160p/", "title": f"P{i}",
              "media_type": "movie"} for i in range(22)],
            interval_minutes=0, mode="immediate",
            auto_resume_after_cooldown=True)["batch_uuid"]
        # Park as an ORDINARY throttle: source_deferred, NO hold.
        with db.transaction() as conn:
            conn.execute(
                "UPDATE download_queue_items SET state='waiting_source', "
                "queue_reason='source_deferred', cooldown_until=?, "
                "last_reason_code='source_temporarily_blocked' WHERE batch_uuid=?",
                (PAST_S, batch))
            conn.execute(
                "UPDATE download_queue_batches SET state='paused_source', "
                "cooldown_until=? WHERE batch_uuid=?", (PAST_S, batch))
        assert _hold(db, batch) is None, "this control must have NO hold"
        assert _state_counts(db, batch).get("waiting_source") == 22

        _expire_every_cooldown(db)
        for _ in range(4):
            svc._maybe_auto_resume()
        promoted = _state_counts(db, batch)
        assert promoted.get("ready") == 22, (
            f"an unheld batch must fully promote — the rig is inert otherwise: "
            f"{promoted}")
    finally:
        db.close()


def test_an_explicit_probe_promotes_exactly_one_item(tmp_path):
    db = DatabaseManager(str(tmp_path / "probe.db"))
    try:
        svc, download, batch = _rig(db, count=5)
        svc._execute(svc._claim_due())
        trigger = db._query_dicts(
            "SELECT item_uuid FROM download_queue_items "
            "WHERE batch_uuid=? AND state='verification_required'",
            (batch,), default=[])[0]["item_uuid"]

        svc.retry_item(trigger)

        counts = _state_counts(db, batch)
        assert counts.get("ready") == 1, "the probe is ONE item"
        assert counts.get("waiting_source") == 4, "siblings stay held"
        assert _hold(db, batch) == "hdencode", (
            "asking for a probe must not clear the hold — only its success may")
    finally:
        db.close()


def test_a_failing_probe_reparks_the_episode_without_burning_siblings(tmp_path):
    db = DatabaseManager(str(tmp_path / "probefail.db"))
    try:
        svc, download, batch = _rig(db, count=5)
        svc._execute(svc._claim_due())
        trigger = db._query_dicts(
            "SELECT item_uuid FROM download_queue_items "
            "WHERE batch_uuid=? AND state='verification_required'",
            (batch,), default=[])[0]["item_uuid"]
        svc.retry_item(trigger)

        svc._execute(svc._claim_due())          # the probe fails the challenge

        assert download.download_item.call_count == 2, "exactly the probe ran"
        counts = _state_counts(db, batch)
        assert counts.get("verification_required") == 1
        assert counts.get("waiting_source") == 4
        assert not counts.get("failed")
        assert _hold(db, batch) == "hdencode"
    finally:
        db.close()


def test_a_delivering_probe_releases_the_siblings_with_spacing(tmp_path):
    """Release is an AFFIRMATIVE ScanHound-side success, nothing else: the
    probe delivers, the hold clears, and the ordinary auto-resume pass promotes
    the siblings with the batch's spacing."""
    db = DatabaseManager(str(tmp_path / "release.db"))
    try:
        svc, download, batch = _rig(db, count=5, interval_minutes=5)
        svc._execute(svc._claim_due())
        trigger = db._query_dicts(
            "SELECT item_uuid FROM download_queue_items "
            "WHERE batch_uuid=? AND state='verification_required'",
            (batch,), default=[])[0]["item_uuid"]
        svc.retry_item(trigger)

        download.download_item.return_value = _success_outcome()
        svc._execute(svc._claim_due())          # the probe DELIVERS

        assert _hold(db, batch) is None, (
            "a real source delivery must release the hold")

        _expire_every_cooldown(db)
        svc._maybe_auto_resume()

        rows = db._query_dicts(
            "SELECT state, scheduled_for FROM download_queue_items "
            "WHERE batch_uuid=? AND state != 'completed' "
            "ORDER BY scheduled_for", (batch,), default=[])
        assert len(rows) == 4
        assert all(r["state"] == "ready" for r in rows), (
            f"released siblings must be promoted: {rows}")
        stamps = [r["scheduled_for"] for r in rows]
        assert stamps == sorted(stamps) and len(set(stamps)) == 4, (
            f"siblings must be spaced, not fired at once: {stamps}")
    finally:
        db.close()


def test_a_duplicate_dedup_success_does_not_release_the_hold(tmp_path):
    """The discrimination on release: a pre-scrape dedup 'success' never
    contacted the source, so it proves nothing about the challenge."""
    db = DatabaseManager(str(tmp_path / "dedup.db"))
    try:
        svc, download, batch = _rig(db, count=3)
        svc._execute(svc._claim_due())
        trigger = db._query_dicts(
            "SELECT item_uuid FROM download_queue_items "
            "WHERE batch_uuid=? AND state='verification_required'",
            (batch,), default=[])[0]["item_uuid"]
        svc.retry_item(trigger)

        download.download_item.return_value = {
            "success": True, "method": "duplicate",
            "message": "Already grabbed", "link_count": 0,
        }
        svc._execute(svc._claim_due())

        assert _hold(db, batch) == "hdencode", (
            "a success that never crossed the source boundary released the hold")
    finally:
        db.close()


def test_a_held_source_holds_a_second_batch_until_a_probe_succeeds(tmp_path):
    """Round-2 review, finding 1: the hold is SOURCE-scoped, not batch-scoped.

    A second HDEncode batch, parked as an ordinary source pause while the
    coordinator was blocked, must NOT auto-probe the challenge once its own
    cooldown expires — and one successful probe releases BOTH batches.
    """
    db = DatabaseManager(str(tmp_path / "crossbatch.db"))
    try:
        svc, download, batch_a = _rig(db, count=3)
        svc._execute(svc._claim_due())          # batch A hits Turnstile → held
        assert _hold(db, batch_a) == "hdencode"

        # Batch B: a SEPARATE HDEncode batch parked as an ordinary throttle
        # (source_deferred, no hold of its own), cooldown already expired.
        batch_b = svc.schedule_batch(
            [{"url": f"https://hdencode.org/b-{i}-2160p/", "title": f"B{i}",
              "media_type": "movie"} for i in range(2)],
            interval_minutes=0, mode="immediate",
            auto_resume_after_cooldown=True)
        b_uuid = batch_b["batch_uuid"]
        with db.transaction() as conn:
            conn.execute(
                "UPDATE download_queue_items SET state='waiting_source', "
                "queue_reason='source_deferred', cooldown_until=?, "
                "last_reason_code='source_temporarily_blocked' "
                "WHERE batch_uuid=?", (PAST_S, b_uuid))
            conn.execute(
                "UPDATE download_queue_batches SET state='paused_source', "
                "cooldown_until=? WHERE batch_uuid=?", (PAST_S, b_uuid))
        assert _hold(db, b_uuid) is None, "batch B records no hold of its own"

        _expire_every_cooldown(db)
        for _ in range(3):
            svc._maybe_auto_resume()
        assert _state_counts(db, b_uuid).get("waiting_source") == 2, (
            "a second batch auto-probed a source under a verification hold")

        # One successful probe on A releases the source; B becomes eligible.
        a_trigger = db._query_dicts(
            "SELECT item_uuid FROM download_queue_items WHERE batch_uuid=? "
            "AND state='verification_required'",
            (batch_a,), default=[])[0]["item_uuid"]
        svc.retry_item(a_trigger)
        download.download_item.return_value = _success_outcome()
        svc._execute(svc._claim_due())          # the probe delivers
        assert _hold(db, batch_a) is None and _hold(db, b_uuid) is None, (
            "an affirmative probe must clear the hold for EVERY batch of the source")

        _expire_every_cooldown(db)
        for _ in range(3):
            svc._maybe_auto_resume()
        assert _state_counts(db, b_uuid).get("ready") == 2, (
            "released siblings in the second batch must become eligible")
    finally:
        db.close()


def test_retry_ready_excludes_verification_held_rows(tmp_path):
    """Round-2 review, finding 2: the bulk 'Retry all ready' path must not
    schedule verification-held rows — that would fan the source into the
    challenge, one transport per batch, while the UI says 'a single probe'."""
    db = DatabaseManager(str(tmp_path / "retryready.db"))
    try:
        svc, download, batch = _rig(db, count=4)
        svc._execute(svc._claim_due())          # → held
        result = svc.retry_ready(interval_minutes=0)
        assert result["scheduled"] == 0, "held rows must not be bulk-scheduled"
        assert result["held"] >= 1, "the skipped held rows must be reported"
        assert not _state_counts(db, batch).get("ready"), (
            "no held row may have been promoted to ready")
    finally:
        db.close()


def test_a_reveal_success_with_a_failed_delivery_still_releases_the_hold(tmp_path):
    """Round-2 review, finding 6: the hold owns SOURCE accessibility, not the
    downstream hand-off. If HDEncode serves the reveal links but JDownloader
    then fails, the challenge has cleared for our session and the hold releases;
    the delivery failure is recorded separately as a failed item."""
    db = DatabaseManager(str(tmp_path / "revealok.db"))
    try:
        svc, download, batch = _rig(db, count=3)
        svc._execute(svc._claim_due())          # → held
        trigger = db._query_dicts(
            "SELECT item_uuid FROM download_queue_items WHERE batch_uuid=? "
            "AND state='verification_required'",
            (batch,), default=[])[0]["item_uuid"]
        svc.retry_item(trigger)
        download.download_item.return_value = _reveal_ok_delivery_failed_outcome()
        svc._execute(svc._claim_due())          # reveal ok, JDownloader fails
        assert _hold(db, batch) is None, (
            "the reveal served links, so the hold must release even though "
            "delivery failed")
        assert _state_counts(db, batch).get("failed") == 1, (
            "the delivery failure is still recorded as a failed item")
    finally:
        db.close()


def test_resume_batch_is_refused_while_a_hold_is_open(tmp_path):
    """FOLD (agent/turnstile-classification): the manual resume_batch path must
    not fan a held batch into the challenge. The base guarded retry_ready but
    left resume_batch promoting every deferred row without calling decide()."""
    db = DatabaseManager(str(tmp_path / "resumeguard.db"))
    try:
        svc, download, batch = _rig(db, count=3)
        svc._execute(svc._claim_due())          # → held
        with pytest.raises(DownloadQueueError) as exc:
            svc.resume_batch(batch, 0)
        assert "verification challenge" in str(exc.value).lower()
        assert not _state_counts(db, batch).get("ready"), (
            "no held row may have been promoted")
        assert _hold(db, batch) == "hdencode"
    finally:
        db.close()


def test_resume_batch_is_hold_safe_against_a_check_use_race(tmp_path):
    """FOLD review (ChatGPT + b087aa20): the AUTHORITATIVE hold check lives inside
    _resume_batch's promotion transaction, not only in resume_batch's outer fast
    check. Calling _resume_batch directly simulates a worker arming the hold
    AFTER the outer check committed — it must still refuse and promote nothing."""
    db = DatabaseManager(str(tmp_path / "race.db"))
    try:
        svc, download, batch = _rig(db, count=3)
        svc._execute(svc._claim_due())          # → held
        with pytest.raises(DownloadQueueError):
            svc._resume_batch(batch, interval_minutes=0, automated=False)
        assert not _state_counts(db, batch).get("ready"), (
            "the in-transaction check must promote nothing")
        assert _hold(db, batch) == "hdencode"
    finally:
        db.close()


def test_clear_verification_hold_releases_the_siblings(tmp_path):
    """FOLD: the operator escape hatch. A permanently-challenged source would
    otherwise deadlock — the only automatic clear is a reveal success the hold
    blocks. An explicit operator clear releases the source hold; the
    source_deferred siblings then auto-resume, while the challenge trigger stays
    held by its own reason and needs a single probe."""
    db = DatabaseManager(str(tmp_path / "clearhold.db"))
    try:
        svc, download, batch = _rig(db, count=4)
        svc._execute(svc._claim_due())          # → held (1 trigger + 3 siblings)
        assert _hold(db, batch) == "hdencode"

        result = svc.clear_verification_hold("hdencode")
        assert result["cleared"] >= 1
        assert _hold(db, batch) is None, "the operator clear must release the hold"

        _expire_every_cooldown(db)
        for _ in range(3):
            svc._maybe_auto_resume()
        counts = _state_counts(db, batch)
        assert counts.get("ready") == 3, (
            f"the source_deferred siblings must recover after the clear: {counts}")
        assert counts.get("verification_required") == 1, (
            "the trigger is held by its own reason and still needs a probe")
    finally:
        db.close()


def test_clear_verification_hold_is_source_matched(tmp_path):
    """Clearing one source must not release a hold on another."""
    db = DatabaseManager(str(tmp_path / "clearsrc.db"))
    try:
        svc, download, batch = _rig(db, count=2)
        svc._execute(svc._claim_due())          # → hdencode held
        svc.clear_verification_hold("ddlbase")  # a different source
        assert _hold(db, batch) == "hdencode", (
            "clearing a different source must not touch the hdencode hold")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# The migration: the episode that predates the column
# ─────────────────────────────────────────────────────────────────────────────

def _insert_batch(conn, batch_uuid, *, state, batch_reason, items):
    """items: list of (source, item_state, queue_reason) triples, in sequence."""
    conn.execute(
        "INSERT INTO download_queue_batches (batch_uuid, mode, "
        " interval_seconds, state, source, total_items, created_at, "
        " updated_at, cooldown_until, last_reason_code, "
        " auto_resume_after_cooldown) "
        "VALUES (?, 'staggered', 0, ?, 'hdencode', ?, ?, ?, ?, ?, 1)",
        (batch_uuid, state, len(items), PAST_S, PAST_S, PAST_S, batch_reason))
    for i, (src, istate, qreason) in enumerate(items):
        conn.execute(
            "INSERT INTO download_queue_items (item_uuid, batch_uuid, "
            " sequence_number, source, canonical_url, title, service_type, "
            " queue_reason, state, cooldown_until, attempt_count, "
            " last_reason_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'Rapidgator', ?, ?, ?, 1, ?, ?, ?)",
            (f"{batch_uuid}-i{i}", batch_uuid, i, src,
             f"https://hdencode.org/{batch_uuid}-{i}/", f"T{i}", qreason, istate,
             PAST_S, batch_reason, PAST_S, PAST_S))


_TRIGGER = ("hdencode", "verification_required", "interactive_challenge")
_SIBLING = ("hdencode", "waiting_source", "source_deferred")


def test_the_migration_holds_only_a_real_challenge_trigger(tmp_path):
    """Round-2 review, finding 3: the migration must key on a genuine challenge
    TRIGGER row, not a batch reason code — and must NOT retro-label a
    reveal_verification_stalled batch (which the runtime classifier defines as
    NOT-a-challenge) as a human challenge."""
    db = DatabaseManager(str(tmp_path / "migrate.db"))
    try:
        with db.transaction() as conn:
            _insert_batch(conn, "challenged", state="paused_source",
                          batch_reason="interactive_challenge",
                          items=[_TRIGGER, _SIBLING])
            _insert_batch(conn, "stalled", state="paused_source",
                          batch_reason="reveal_verification_stalled",
                          items=[_SIBLING, _SIBLING])
            _insert_batch(conn, "throttled", state="paused_source",
                          batch_reason="source_temporarily_blocked",
                          items=[_SIBLING])
            DatabaseManager._mark_existing_challenge_pauses_held(conn)
            holds = {r["batch_uuid"]: r["verification_hold_source"]
                     for r in conn.execute(
                         "SELECT batch_uuid, verification_hold_source "
                         "FROM download_queue_batches")}
        assert holds["challenged"] == "hdencode"
        assert holds["stalled"] is None, (
            "reveal_verification_stalled is NOT Turnstile evidence and must not "
            "be retro-labelled a human challenge")
        assert holds["throttled"] is None, (
            "an ordinary throttle pause must keep auto-resuming")
    finally:
        db.close()


def test_the_migration_source_is_the_trigger_not_the_first_child(tmp_path):
    """Round-2 review, finding 3, Problem B: a mixed-source batch must hold the
    source that produced the challenge, not whichever source is first in
    sequence."""
    db = DatabaseManager(str(tmp_path / "migrate_mixed.db"))
    try:
        with db.transaction() as conn:
            _insert_batch(
                conn, "mixed", state="paused_source",
                batch_reason="interactive_challenge",
                items=[("ddlbase", "waiting_source", "source_deferred"),
                       _TRIGGER])          # HDEncode is the challenge trigger
            DatabaseManager._mark_existing_challenge_pauses_held(conn)
            hold = conn.execute(
                "SELECT verification_hold_source FROM download_queue_batches "
                "WHERE batch_uuid='mixed'").fetchone()[0]
        assert hold == "hdencode", (
            "the held source must be the challenge trigger's (hdencode), not "
            "the first deferred child's (ddlbase)")
    finally:
        db.close()


def test_migrated_rows_are_held_without_touching_item_history(tmp_path):
    """The migration moves the EPISODE (the batch-level hold) and leaves
    last_reason_code as the true record of each attempt — yet the old cooldowns
    can no longer reschedule anything."""
    db = DatabaseManager(str(tmp_path / "migrate2.db"))
    try:
        with db.transaction() as conn:
            _insert_batch(conn, "ep", state="paused_source",
                          batch_reason="interactive_challenge",
                          items=[_TRIGGER, _SIBLING, _SIBLING])
            DatabaseManager._mark_existing_challenge_pauses_held(conn)

        svc = DownloadQueueService({}, db, MagicMock())
        svc._coordinator_snapshot = MagicMock(return_value={"blocked": False})
        _expire_every_cooldown(db)
        for _ in range(3):
            svc._maybe_auto_resume()

        rows = db._query_dicts(
            "SELECT state, last_reason_code FROM download_queue_items "
            "WHERE batch_uuid='ep'", default=[])
        assert all(r["state"] in ("waiting_source", "verification_required")
                   for r in rows), rows
        assert all(r["last_reason_code"] == "interactive_challenge"
                   for r in rows), "history must not be rewritten"
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# The operator tools see the same hold (the adapter path)
# ─────────────────────────────────────────────────────────────────────────────

def _joined_row(**over):
    # source_held is the precomputed SOURCE-scoped flag the SQL supplies (1 =
    # some batch holds this row's source). Defaults to held for these rows.
    row = {"item_uuid": "i", "batch_uuid": "b", "title": "T",
           "state": "verification_required", "cooldown_until": PAST_S,
           "queue_reason": "interactive_challenge", "last_reason_code": "",
           "item_source": "hdencode",
           "batch_state": "paused_source", "batch_cooldown": PAST_S,
           "auto_resume_after_cooldown": 1, "auto_resume_used": 0,
           "source_delivery_count": 0, "auto_resume_progress_mark": 0,
           "source_held": 1}
    row.update(over)
    return row


def _classify(row):
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts"))
    from queue_recovery_state import classify_rows
    return list(classify_rows([row], now=NOW))[0]


def test_the_tools_classify_a_challenge_row_as_held():
    assert _classify(_joined_row()) == VERIFICATION_HOLD


def test_the_tools_classify_a_held_sibling_as_held():
    row = _joined_row(state="waiting_source", queue_reason="source_deferred")
    assert _classify(row) == VERIFICATION_HOLD


def test_the_hold_is_source_matched_not_batch_global(tmp_path):
    """Round-2 review, finding 1: the operator tools' SQL holds a row only when
    ITS source is held. A DDLBase sibling in a batch alongside a held HDEncode
    trigger must NOT be held — exercised through the REAL JOINED_DEFERRED_SQL
    source_held subquery, not a hand-built flag."""
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts"))
    from queue_recovery_state import JOINED_DEFERRED_SQL, facts_from_row
    from backend.queue_recovery_policy import decide

    db = DatabaseManager(str(tmp_path / "toolsql.db"))
    try:
        with db.transaction() as conn:
            _insert_batch(
                conn, "held", state="paused_source",
                batch_reason="interactive_challenge",
                items=[_TRIGGER,
                       ("ddlbase", "waiting_source", "source_deferred")])
            DatabaseManager._mark_existing_challenge_pauses_held(conn)
        verdict = {}
        for r in db._query_dicts(JOINED_DEFERRED_SQL, default=[]):
            item, shared = facts_from_row(r)
            verdict[r["item_source"]] = decide(item, shared, NOW)
        assert verdict["hdencode"] == VERIFICATION_HOLD
        assert verdict["ddlbase"] == AUTHORISED, (
            "a DDLBase sibling must not be held by an HDEncode challenge")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# The clear-hold route + the standalone migration script (fold review)
# ─────────────────────────────────────────────────────────────────────────────

def test_clear_verification_hold_route():
    """FOLD review (finding 5): the escape-hatch route has a boundary test."""
    from fastapi import HTTPException
    from backend.api.routes.downloads import (
        ClearVerificationHoldRequest, clear_verification_hold as route)

    # queue unavailable -> 503
    with pytest.raises(HTTPException) as exc:
        route(ClearVerificationHoldRequest(source="hdencode"),
              reg=SimpleNamespace(download_queue=None))
    assert exc.value.status_code == 503

    # queue present -> delegates to the service and returns its result
    q = MagicMock()
    q.clear_verification_hold.return_value = {
        "source": "hdencode", "cleared": 2, "remaining_triggers": 1}
    result = route(ClearVerificationHoldRequest(source="hdencode"),
                   reg=SimpleNamespace(download_queue=q))
    assert result["cleared"] == 2
    q.clear_verification_hold.assert_called_once_with("hdencode")


def _migrate_mod():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts"))
    return importlib.import_module("migrate_challenge_episode")


def _migrate(argv):
    return _migrate_mod().main(argv)


def _one_deferred_hdencode(db):
    svc = DownloadQueueService({}, db, MagicMock())
    b = svc.schedule_batch(
        [{"url": "https://hdencode.org/x-2160p/", "title": "X",
          "media_type": "movie"}],
        interval_minutes=0, mode="immediate", auto_resume_after_cooldown=True)
    item_uuid = b["items"][0]["item_uuid"]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE download_queue_items SET state='waiting_source', "
            "queue_reason='source_deferred', cooldown_until=? WHERE item_uuid=?",
            (PAST_S, item_uuid))
        conn.execute("UPDATE download_queue_batches SET state='paused_source' "
                     "WHERE batch_uuid=?", (b["batch_uuid"],))
    return b["batch_uuid"], item_uuid


def _hold_of(path, batch):
    db = DatabaseManager(path)
    try:
        rows = db._query_dicts(
            "SELECT verification_hold_source h FROM download_queue_batches "
            "WHERE batch_uuid=?", (batch,), default=[])
        return rows[0]["h"] if rows else None
    finally:
        db.close()


def test_migration_requires_a_named_trigger(tmp_path):
    path = str(tmp_path / "m.db")
    DatabaseManager(path).close()
    assert _migrate(["--db", path]) == 2, "no --trigger must refuse (exit 2)"


def test_migration_dry_run_writes_nothing_then_apply_holds(tmp_path):
    path = str(tmp_path / "m.db")
    db = DatabaseManager(path)
    batch, item = _one_deferred_hdencode(db)
    db.close()
    assert _migrate(["--db", path, "--trigger", item]) == 0
    assert _hold_of(path, batch) is None, "dry run must not write"
    assert _migrate(["--db", path, "--trigger", item, "--apply"]) == 0
    assert _hold_of(path, batch) == "hdencode", "apply must hold the source"


def test_migration_refuses_a_typo_trigger(tmp_path):
    path = str(tmp_path / "m.db")
    db = DatabaseManager(path)
    batch, _item = _one_deferred_hdencode(db)
    db.close()
    assert _migrate(["--db", path, "--trigger", "not-a-real-id", "--apply"]) == 1
    assert _hold_of(path, batch) is None, "a typo'd trigger must write nothing"


def test_migration_refuses_a_hold_batch_without_a_source_row(tmp_path):
    path = str(tmp_path / "m.db")
    db = DatabaseManager(path)
    batch, item = _one_deferred_hdencode(db)
    # A second batch with only a DDLBase deferred row.
    svc = DownloadQueueService({}, db, MagicMock())
    ddl = svc.schedule_batch(
        [{"url": "https://ddlbase.com/y/", "title": "Y", "media_type": "movie"}],
        interval_minutes=0, mode="immediate", auto_resume_after_cooldown=True)
    ddl_batch = ddl["batch_uuid"]
    with db.transaction() as conn:
        conn.execute("UPDATE download_queue_items SET state='waiting_source', "
                     "queue_reason='source_deferred' WHERE batch_uuid=?",
                     (ddl_batch,))
    db.close()
    rc = _migrate(["--db", path, "--trigger", item,
                   "--hold-batch", ddl_batch, "--apply"])
    assert rc == 1, "a DDLBase-only hold-batch must be refused"
    assert _hold_of(path, batch) is None, "nothing may be written on refusal"


def test_apply_hold_rolls_back_a_hold_batch_that_lost_its_row(tmp_path):
    """Re-review M2: if a --hold-batch's only deferred source row is removed
    AFTER the dry-run precheck but before the write, the in-transaction
    re-validation rolls the WHOLE invocation back — no trigger set, no batch
    stamped. Exercised directly against _apply_hold, which is the atomic unit."""
    import sqlite3 as _sqlite
    path = str(tmp_path / "m.db")
    db = DatabaseManager(path)
    batch, item = _one_deferred_hdencode(db)          # the trigger's batch
    svc = DownloadQueueService({}, db, MagicMock())
    hb = svc.schedule_batch(
        [{"url": "https://hdencode.org/z-2160p/", "title": "Z",
          "media_type": "movie"}],
        interval_minutes=0, mode="immediate",
        auto_resume_after_cooldown=True)["batch_uuid"]
    hb_item = db._query_dicts(
        "SELECT item_uuid FROM download_queue_items WHERE batch_uuid=?",
        (hb,), default=[])[0]["item_uuid"]
    with db.transaction() as conn:
        conn.execute("UPDATE download_queue_items SET state='waiting_source', "
                     "queue_reason='source_deferred' WHERE batch_uuid=?", (hb,))
        # THE RACE: the hold-batch's only deferred row moves on before the write.
        conn.execute("UPDATE download_queue_items SET state='completed' "
                     "WHERE item_uuid=?", (hb_item,))
    db.close()

    m = _migrate_mod()
    conn = _sqlite.connect(path)
    conn.row_factory = _sqlite.Row
    try:
        with pytest.raises(_sqlite.Error):
            m._apply_hold(conn, [{"item_uuid": item}], [batch, hb], "hdencode")
    finally:
        conn.close()
    assert _hold_of(path, batch) is None and _hold_of(path, hb) is None, (
        "a rolled-back invocation must stamp NOTHING")
    # And the trigger must not have been left flipped either.
    db2 = DatabaseManager(path)
    try:
        row = db2._query_dicts(
            "SELECT state FROM download_queue_items WHERE item_uuid=?",
            (item,), default=[])[0]
        assert row["state"] == "waiting_source", "the trigger update rolled back"
    finally:
        db2.close()
