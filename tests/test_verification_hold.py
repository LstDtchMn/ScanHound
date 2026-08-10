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

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.database import DatabaseManager
from backend.download_outcome import (
    TURNSTILE_CAUSE_CODE,
    is_source_wide_denial,
    is_turnstile_console_failure,
    turnstile_challenge_evidence,
)
from backend.download_queue import DownloadQueueService
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
  <form action="/some-release/#unlocked">
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
        assert svc._claim_due() is None, (
            "an expired timer made a held item claimable")
        assert download.download_item.call_count == 1, (
            "an automatic retry re-entered the failing challenge")
        assert _state_counts(db, batch) == counts, (
            "timer expiry changed the episode's state")
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


# ─────────────────────────────────────────────────────────────────────────────
# The migration: the episode that predates the column
# ─────────────────────────────────────────────────────────────────────────────

def _insert_batch(conn, batch_uuid, *, state, reason, items, source="hdencode",
                  item_state="waiting_source"):
    conn.execute(
        "INSERT INTO download_queue_batches (batch_uuid, mode, "
        " interval_seconds, state, source, total_items, created_at, "
        " updated_at, cooldown_until, last_reason_code, "
        " auto_resume_after_cooldown) "
        "VALUES (?, 'staggered', 0, ?, ?, ?, ?, ?, ?, ?, 1)",
        (batch_uuid, state, source, items, PAST_S, PAST_S, PAST_S, reason))
    for i in range(items):
        conn.execute(
            "INSERT INTO download_queue_items (item_uuid, batch_uuid, "
            " sequence_number, source, canonical_url, title, service_type, "
            " queue_reason, state, cooldown_until, attempt_count, "
            " last_reason_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'Rapidgator', 'source_deferred', ?, "
            " ?, 1, ?, ?, ?)",
            (f"{batch_uuid}-i{i}", batch_uuid, i, source,
             f"https://hdencode.org/{batch_uuid}-{i}/", f"T{i}", item_state,
             PAST_S, reason, PAST_S, PAST_S))


def test_the_migration_holds_only_the_challenge_episode(tmp_path):
    db = DatabaseManager(str(tmp_path / "migrate.db"))
    try:
        with db.transaction() as conn:
            _insert_batch(conn, "stalled", state="paused_source",
                          reason="reveal_verification_stalled", items=2)
            _insert_batch(conn, "challenged", state="paused_source",
                          reason="interactive_challenge", items=2)
            _insert_batch(conn, "throttled", state="paused_source",
                          reason="source_temporarily_blocked", items=2)
            _insert_batch(conn, "drained", state="paused_source",
                          reason="reveal_verification_stalled", items=1,
                          item_state="completed")
            DatabaseManager._mark_existing_challenge_pauses_held(conn)
            holds = {r["batch_uuid"]: r["verification_hold_source"]
                     for r in conn.execute(
                         "SELECT batch_uuid, verification_hold_source "
                         "FROM download_queue_batches")}
        assert holds["stalled"] == "hdencode", (
            "the measured episode is recorded as reveal_verification_stalled "
            "and must come under the hold")
        assert holds["challenged"] == "hdencode"
        assert holds["throttled"] is None, (
            "an ordinary throttle pause must keep auto-resuming")
        assert holds["drained"] is None, (
            "a batch with nothing deferred has nothing to hold")
    finally:
        db.close()


def test_migrated_rows_are_held_without_touching_item_history(tmp_path):
    """The chip's warning made concrete: the migration moves the EPISODE (the
    batch-level hold) and leaves last_reason_code as the true record of what
    each attempt observed at the time — yet the old cooldowns can no longer
    reschedule anything."""
    db = DatabaseManager(str(tmp_path / "migrate2.db"))
    try:
        with db.transaction() as conn:
            _insert_batch(conn, "ep", state="paused_source",
                          reason="reveal_verification_stalled", items=3)
            DatabaseManager._mark_existing_challenge_pauses_held(conn)

        svc = DownloadQueueService({}, db, MagicMock())
        svc._coordinator_snapshot = MagicMock(return_value={"blocked": False})
        _expire_every_cooldown(db)
        for _ in range(3):
            svc._maybe_auto_resume()

        rows = db._query_dicts(
            "SELECT state, last_reason_code FROM download_queue_items "
            "WHERE batch_uuid='ep'", default=[])
        assert all(r["state"] == "waiting_source" for r in rows), rows
        assert all(r["last_reason_code"] == "reveal_verification_stalled"
                   for r in rows), "history must not be rewritten"
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# The operator tools see the same hold (the adapter path)
# ─────────────────────────────────────────────────────────────────────────────

def _joined_row(**over):
    row = {"item_uuid": "i", "batch_uuid": "b", "title": "T",
           "state": "verification_required", "cooldown_until": PAST_S,
           "queue_reason": "interactive_challenge", "last_reason_code": "",
           "item_source": "hdencode",
           "batch_state": "paused_source", "batch_cooldown": PAST_S,
           "auto_resume_after_cooldown": 1, "auto_resume_used": 0,
           "source_delivery_count": 0, "auto_resume_progress_mark": 0,
           "verification_hold_source": "hdencode"}
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


def test_the_hold_is_source_matched_not_batch_global():
    """A DDLBase row in a batch whose hold names hdencode is NOT held — the
    same source-ownership rule the budget refund follows."""
    row = _joined_row(state="waiting_source", queue_reason="source_deferred",
                      item_source="ddlbase")
    assert _classify(row) == AUTHORISED
