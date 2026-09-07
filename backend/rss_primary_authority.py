"""Who is allowed to make RSS the primary discovery path, when, and for how long.

One module, asked two different questions, because they are different questions.

**Activation** -- may the owner turn primary ON right now? -- is asked only by
``POST /rss/mode``, against a PROPOSED promotion record it has not yet written.
It demands the full shadow qualification, a completed canary qualification, and
a safety contract it can pin.

**Runtime** -- is primary still in effect this cycle? -- is asked by the
background scanner, by the RSS poll, by the transient listing fallback and by
``GET /rss/status``. It deliberately does NOT re-ask for shadow readiness.

That split is the correction from the 2026-09-05 design review (RHC-1). Raw
readiness blocks on ``not_yet_assessable`` rows because, before the hybrid,
promoting STOPPED the comparison that resolves them -- the gate would open on
evidence its own promoted mode destroys. The canary removes that premise by
continuing the comparison after promotion, so a pending row is once again just
a row that is not yet decidable. Keeping readiness as a runtime conjunct would
mean one ordinary canary sighting RSS has not carried yet could bounce the
system back to shadow before the next canary could resolve it.

SUSPENSION IS NOT REVOCATION (design review 2026-09-06, R2-6). Two ways to stop
being primary, and conflating them opens a hole:

* a **suspension** is temporary and keeps the promotion record. The effective
  mode drops to ``rss_shadow`` immediately, and when the condition clears,
  primary resumes without the owner doing anything. ``database_unavailable`` is
  the archetype: we cannot evaluate, so we do not run as primary, but nothing
  durable has been decided.
* a **revocation** is a durable safety finding. The mode is persisted back to
  ``rss_shadow``, the promotion record is deleted, and only a fresh explicit
  owner promotion can undo it.

Treating a transient database failure as a revocation looked safe -- the
trigger keeps the mode shadow anyway -- but only within one process: if the
revoking write failed and the process restarted with a healthy database, the
surviving on-disk record would authorize primary again. Every revocation
trigger here is derived from evidence or configuration rather than process
memory, so each one re-derives after a restart and a failed persist is simply
retried on the next cycle.

THE CONTRACT. A promotion is bound to ``canary_contract_hash``, over exactly
the settings that make the protection mean what it says: which listing sources
are watched and which feed each maps to, how deep and how often the canary
crawls, how stale it may get, how long membership evidence is kept, and the
versions of the policy and estimator that interpret all of it. Change any of
them while primary is live and the promotion no longer describes the running
system, so it is revoked and must be made again. Nothing measured goes into the
hash, and neither does unrelated configuration.

WHAT THIS FILE DOES NOW. The paragraph here used to say ``CANARY_IMPLEMENTED``
was False and there was no path to primary. Both stopped being true when the
canary was built on 2026-09-07 -- the scheduled listing crawl, the membership
replay, the four coverage states, overlap and churn all exist and this module
reads their evidence. A promotion is reachable, and only through the whole
gate: shadow qualification, a continuous epoch, a replay showing the chosen
cadence would have missed nothing, retention above the evidence horizon, a
fresh canary, armed auto-demotion, and a record pinned to the contract.

THE EVIDENCE COMES FROM THE DATABASE, NOT FROM THE CALLER. Timestamps arrive as
stored strings and must go through ``parse_utc`` before they meet anything the
policy layer compares, and source names arrive in the contract's spelling
("4k") while the crawler stores its own ("hdencode:4k"), so every read goes
through ``canary_source_key``. Both rules exist because both were broken, and
neither break was visible to a test whose double produced the value it then
consumed.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The coverage canary exists as of 2026-09-07: it crawls on its own schedule
#: after promotion, records per-source membership, and the checks below read
#: that evidence. Primary is therefore reachable -- but only through the full
#: gate: shadow qualification, a continuous epoch, a replay showing the chosen
#: cadence would have missed nothing, retention above the evidence horizon, a
#: fresh canary, armed auto-demotion, and a promotion record pinned to the
#: contract. Flipping this alone authorizes nothing.
CANARY_IMPLEMENTED = True

#: Bumped when the meaning of the canary's protection changes. It is part of
#: the contract hash, so an existing promotion does not survive a redefinition.
CANARY_VERSION = 1

#: Bumped when the demotion policy itself changes shape.
DEMOTION_POLICY_VERSION = 1

#: Bumped when the visibility estimator's rule changes.
ESTIMATOR_VERSION = 1

RSS_MODES = ("listing", "rss_shadow", "rss_primary")

# ── blockers ────────────────────────────────────────────────────────────────
# Activation-only.
BLOCKER_NOT_READY = "shadow_readiness_not_met"
BLOCKER_EPOCH_INCOMPLETE = "qualification_window_incomplete"
BLOCKER_WINDOW_UNKNOWN = "visibility_window_unknown"
BLOCKER_INTERVAL_UNSAFE = "interval_unsafe"
BLOCKER_CANARY_NOT_RECENT = "canary_not_recent"
BLOCKER_CONTRACT_MISMATCH = "contract_hash_mismatch"
BLOCKER_RETENTION_TOO_SHORT = "retention_below_evidence_horizon"
#: The measured request reduction is below the floor the design fixed. The
#: shadow readiness gate only asks for "better than zero"
#: (``request_reduction_not_proven``), which a 1% saving satisfies; the
#: promotion contract asks for the floor.
BLOCKER_REDUCTION_BELOW_FLOOR = "request_reduction_below_floor"

#: The qualification epoch, exactly as the accepted design fixes it (rev 2 §7):
#: 14 consecutive observed clean days of eligible comparison cycles with no gap
#: longer than six hours between consecutive ones. A longer gap resets the
#: clock to the first eligible cycle after it.
QUALIFICATION_DAYS = 14
QUALIFICATION_MAX_GAP_HOURS = 6
#: RHC-13's cost thresholds as percentages. Published as fractions (0.50/0.70)
#: on the status surface because that is how the design writes them; compared
#: here against ``request_reduction_pct``, which the summary reports as a
#: percentage. Safety cadence outranks both: these can only ever ADD a blocker.
REDUCTION_FLOOR_PCT = 50.0
REDUCTION_TARGET_PCT = 70.0

# Runtime-only.
BLOCKER_NO_RECORD = "promotion_record_missing"
BLOCKER_CONTRACT_CHANGED = "promotion_contract_changed"
BLOCKER_CANARY_STALE = "canary_stale"
BLOCKER_MARGIN_LOST = "visibility_margin_lost"
BLOCKER_OVERLAP_LOST = "overlap_lost_twice"
BLOCKER_GAP_PROVEN = "gap_proven"
BLOCKER_COVERAGE_UNASSESSABLE = "coverage_unassessable"
BLOCKER_SYSTEMATIC_GAP = "systematic_gap"

# Both.
BLOCKER_NO_CANARY = "coverage_canary_not_implemented"
#: The canary's evidence is readable but too thin to judge -- no membership
#: recorded for a source yet, or fewer canaries than the check needs. That is a
#: PAUSE, not a finding: "we cannot tell yet" and "we have shown a gap" are
#: different claims, and treating the first as the second would demote a
#: healthy system the moment it was promoted, before its first canary ran.
BLOCKER_EVIDENCE_INSUFFICIENT = "canary_evidence_insufficient"
BLOCKER_NO_DEMOTION = "auto_demotion_not_armed"
BLOCKER_NO_DB = "database_unavailable"
#: The canary's own evidence cannot be read because the canary does not exist
#: yet. Distinct from a passing check on purpose: absence of evidence is not
#: evidence of coverage.
#: REMOVED 2026-09-07. Until the canary existed, every evaluation carried
#: ``canary_evidence_unavailable`` to say plainly that the checks reading its
#: evidence could not be made. The canary exists now, so that evidence either
#: reads -- and its verdicts speak for themselves -- or it does not, which is
#: ``database_unavailable`` and suspends. Keeping a third answer would have
#: invited "unavailable" to be read as a permanent state of the world rather
#: than a fault to fix.

#: Temporary: the effective mode drops to shadow, the promotion record STAYS.
#: ``coverage_canary_not_implemented`` belongs here rather than nowhere: while
#: the canary does not exist, primary cannot run, but nothing durable has been
#: decided about a promotion, and building the canary is not a safety finding
#: against one. Leaving it unclassified let it fall through the state logic and
#: pick its own severity, which is the implicit behaviour the two sets exist to
#: prevent (PR #116 review, PR1-R5).
SUSPENSION_BLOCKERS = frozenset({
    BLOCKER_NO_DB, BLOCKER_NO_CANARY, BLOCKER_EVIDENCE_INSUFFICIENT,
})

#: Durable: persist shadow, delete the promotion record, record the reason.
REVOCATION_BLOCKERS = frozenset({
    BLOCKER_GAP_PROVEN, BLOCKER_COVERAGE_UNASSESSABLE, BLOCKER_SYSTEMATIC_GAP,
    BLOCKER_CANARY_STALE, BLOCKER_OVERLAP_LOST, BLOCKER_MARGIN_LOST,
    BLOCKER_CONTRACT_CHANGED, BLOCKER_NO_DEMOTION, BLOCKER_NO_RECORD,
})

#: Every blocker ``evaluate_runtime`` can produce, and therefore every blocker
#: whose severity is decided rather than assumed. A blocker outside this set is
#: a programming error, and is treated as a revocation so the mistake fails
#: closed instead of quietly running as a pause.
RUNTIME_BLOCKERS = SUSPENSION_BLOCKERS | REVOCATION_BLOCKERS

STATE_AUTHORIZED = "authorized"
STATE_SUSPENDED = "runtime_suspended"
STATE_REVOKED = "runtime_revoked"
STATE_NOT_REQUESTED = "not_requested"

# ── the safety contract ─────────────────────────────────────────────────────
#: Config keys the contract hash covers, with the default used when a key is
#: absent. Order is irrelevant: the hash is taken over a sorted mapping.
CONTRACT_KEYS: Dict[str, Any] = {
    "hdencode_listing_canary_sources": ["4k", "remux", "tv"],
    "hdencode_listing_canary_feed_map": {
        "4k": "movies_2160p", "remux": "movies_remux", "tv": "tv_all"},
    "hdencode_listing_canary_pages": 3,
    "hdencode_listing_canary_minutes": 360,
    "hdencode_listing_canary_max_age_minutes": 720,
    "hdencode_listing_membership_retention_days": 90,
    "hdencode_rss_auto_demotion_enabled": True,
}

#: How much membership history the evidence needs to survive: the qualification
#: epoch plus a replay window plus margin. Retention below this is refused at
#: activation, and lowering it later changes the hash, which revokes.
MIN_RETENTION_DAYS = 30

PROMOTION_KEY = "hdencode_rss_primary_promotion"
LAST_DEMOTION_KEY = "hdencode_rss_last_demotion"
EPOCH_KEY = "hdencode_rss_qualification_epoch_started_at"


def contract_inputs(config) -> Dict[str, Any]:
    """The settings the promotion is pinned to, with defaults applied."""
    cfg = config or {}
    return {key: cfg.get(key, default) for key, default in CONTRACT_KEYS.items()}


def canary_contract_hash(config) -> str:
    """A stable digest of the safety-critical settings plus the code versions.

    Measurements are deliberately excluded: the hash answers "is the promotion
    still describing this system", not "is the system healthy".
    """
    payload = {
        "contract": contract_inputs(config),
        "canary_version": CANARY_VERSION,
        "demotion_policy_version": DEMOTION_POLICY_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_promotion_record(config, *, at, by="operator") -> Dict[str, Any]:
    """The record a promotion would write. Prospective until it is persisted."""
    return {
        "at": at,
        "by": by,
        "canary_version": CANARY_VERSION,
        "canary_contract_hash": canary_contract_hash(config),
    }


def _readiness(config, db) -> Dict[str, Any]:
    cfg = config or {}
    return db.get_hdencode_rss_readiness(
        min_cycles=cfg.get("hdencode_rss_shadow_min_cycles", 20),
        min_days=cfg.get("hdencode_rss_shadow_min_days", 7),
    )


#: The listing source the canary watches when a configured canary source is
#: named by category alone. See ``canary_source_key``.
CANARY_DEFAULT_SOURCE = "hdencode"


def canary_source_key(name) -> str:
    """The membership key for a configured canary source.

    TWO KEY SPACES MET HERE AND DID NOT MATCH. The contract names canary
    sources by listing category -- "4k", "remux", "tv" -- because that is what
    an operator configures and what the feed map is keyed by. The crawler
    names each listing arm it traverses "<source>:<category>", so what actually
    reaches hdencode_listing_membership and hdencode_canary_state is
    "hdencode:4k". Every consumer here looked up the configured name verbatim,
    so every lookup missed: the canary read as never having run however well it
    was running, its schedule was never found so it was due on every cycle, and
    its membership was never found so its evidence stayed permanently thin.

    The whole suite passed because each test used one spelling consistently on
    both sides of the boundary, which is exactly what a boundary bug survives.

    An unqualified name is resolved against the HDEncode listing because the
    canary is an HDEncode mechanism; a name that already carries a source
    (anything containing ":") is passed through, so the config can name another
    listing explicitly if one is ever added.
    """
    key = str(name)
    return key if ":" in key else "%s:%s" % (CANARY_DEFAULT_SOURCE, key)


#: How many example URLs each coverage finding lists. The COUNT is always
#: exact; only the examples are capped, because this block is published on an
#: endpoint the UI polls.
EVIDENCE_EXAMPLES = 20


def _note_example(detail, key, value) -> None:
    """Count one finding, and keep the first few as examples."""
    detail["%s_count" % key] = int(detail.get("%s_count" % key) or 0) + 1
    examples = detail.setdefault(key, [])
    if len(examples) < EVIDENCE_EXAMPLES:
        examples.append(value)


def parse_utc(value) -> Optional[datetime.datetime]:
    """A stored timestamp as an aware UTC datetime, or None if it is not one.

    THE POLICY LAYER COMPARES DATETIMES; THE DATABASE RETURNS STRINGS. Passing
    a raw stored value into ``classify_coverage`` raised
    ``TypeError: '<=' not supported between 'str' and 'datetime.datetime'`` the
    moment the authority had real evidence to judge -- an outright break of the
    "never raises" contract, and invisible to every test because the doubles
    handed in datetimes the database never produces.

    Comparing the strings instead would have been worse than the crash: ISO
    strings only sort chronologically while they all carry the same offset
    shape, so a "+09:00" row sorts after a later "+00:00" one and the newest
    canary silently becomes the wrong cycle.
    """
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        try:
            parsed = datetime.datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def qualification_continuity(config, db) -> Dict[str, Any]:
    """Has the epoch actually run clean for long enough?

    The accepted design (rev 2 §7) asks for 14 consecutive observed clean days
    of eligible comparison cycles with no gap longer than six hours between
    consecutive ones, and says a longer gap RESETS the clock to the first
    eligible cycle after it. The implementation asked
    ``if not cfg.get(EPOCH_KEY)``, which is a truthiness test on a setting: the
    literal string "not-a-timestamp" passed it, and four cycles spanning three
    minutes passed it, so ``qualification_window_incomplete`` was a blocker
    that never checked the window.

    Returns what it measured as well as its verdict, because an owner refused a
    promotion needs to know whether they are two days short or were reset by an
    outage last night.
    """
    out: Dict[str, Any] = {
        "complete": False, "epoch_started_at": (config or {}).get(EPOCH_KEY),
        "required_days": QUALIFICATION_DAYS,
        "max_gap_hours_allowed": QUALIFICATION_MAX_GAP_HOURS,
        "consecutive_clean_days": None, "max_gap_hours": None,
        "eligible_cycles": None, "reasons": [],
    }
    epoch = parse_utc(out["epoch_started_at"]) if out["epoch_started_at"] else None
    if epoch is None:
        # Absent AND unparseable both land here, and they are different
        # problems, so they are reported as different reasons.
        out["reasons"].append(
            "epoch_not_started" if not out["epoch_started_at"]
            else "epoch_timestamp_unreadable")
        return out

    if db is None or not hasattr(db, "get_shadow_cycle_url_sets"):
        out["reasons"].append("evidence_unavailable")
        return out
    try:
        read = db.get_shadow_cycle_url_sets(since=epoch.isoformat())
    except Exception as exc:  # noqa: BLE001 -- unreadable is a reason, not a raise
        logger.warning("qualification continuity unavailable: %s", exc)
        out["reasons"].append("evidence_unavailable")
        return out
    if read is None:
        out["reasons"].append("evidence_unavailable")
        return out

    eligible = []
    for cycle in read.get("cycles") or []:
        # Eligible means the cycle can be trusted as an observation: the normal
        # feeds completed, and the listing arm did not explicitly fail. A cycle
        # that did not observe cleanly cannot extend a CLEAN day count.
        if cycle.get("normal_feeds_complete") is not True:
            continue
        if cycle.get("listing_complete") is False:
            continue
        at = parse_utc(cycle.get("at"))
        if at is None or at < epoch:
            continue
        eligible.append(at)
    eligible.sort()
    out["eligible_cycles"] = len(eligible)
    if not eligible:
        out["reasons"].append("no_eligible_cycles_in_epoch")
        return out

    # Walk forward, restarting the run whenever the gap is too long. The run
    # that survives to the end is the one the owner is currently accruing.
    run_start = eligible[0]
    max_gap = 0.0
    run_max_gap = 0.0
    for previous, current in zip(eligible, eligible[1:]):
        gap_hours = (current - previous).total_seconds() / 3600.0
        max_gap = max(max_gap, gap_hours)
        if gap_hours > QUALIFICATION_MAX_GAP_HOURS:
            run_start = current
            run_max_gap = 0.0
            continue
        run_max_gap = max(run_max_gap, gap_hours)
    clean_days = (eligible[-1] - run_start).total_seconds() / 86400.0
    out["consecutive_clean_days"] = round(clean_days, 2)
    out["max_gap_hours"] = round(max_gap, 2)
    out["current_run_max_gap_hours"] = round(run_max_gap, 2)
    out["run_started_at"] = run_start.isoformat()

    if clean_days < QUALIFICATION_DAYS:
        out["reasons"].append("fewer_than_%d_clean_days" % QUALIFICATION_DAYS)
    if max_gap > QUALIFICATION_MAX_GAP_HOURS and run_start != eligible[0]:
        # Surfaced with its cause, per the design: the clock was reset, and the
        # owner should know an outage is why they are short.
        out["reasons"].append("clock_reset_by_gap")
    out["complete"] = not out["reasons"]
    return out


def _retention_days(config) -> Optional[int]:
    value = (config or {}).get("hdencode_listing_membership_retention_days",
                               CONTRACT_KEYS["hdencode_listing_membership_retention_days"])
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _auto_demotion_armed(config) -> bool:
    return (config or {}).get("hdencode_rss_auto_demotion_enabled",
                              CONTRACT_KEYS["hdencode_rss_auto_demotion_enabled"]) is True


def _age_seconds(value, now) -> Optional[float]:
    try:
        at = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.timezone.utc)
    return (now - at.astimezone(datetime.timezone.utc)).total_seconds()


def canary_evidence(config, db) -> Dict[str, Any]:
    """Read the canary's own evidence and say what it implies.

    Returns ``{"blockers": [...], "detail": {...}}``. Every reader here is
    tri-state: ``None`` means the evidence could not be read, and that is
    reported as ``database_unavailable`` -- a SUSPENSION -- never as "no gaps
    found". A reader that folded an outage into an empty result would make
    the whole safety claim unfalsifiable, which is why the readers were built
    tri-state in the first place.

    An unparseable stored cycle is treated differently from an outage on
    purpose: it does not heal on its own, so it is reported as
    ``coverage_unassessable``, which revokes. "We can no longer tell" is a
    durable finding, even though it is not a proven gap.
    """
    from backend import rss_canary_policy as policy

    now = datetime.datetime.now(datetime.timezone.utc)
    contract = contract_inputs(config)
    sources = [str(s) for s in (contract.get(
        "hdencode_listing_canary_sources") or [])]
    depth = int(contract.get("hdencode_listing_canary_pages") or 3)
    max_age = max(1, int(contract.get(
        "hdencode_listing_canary_max_age_minutes") or 720)) * 60
    blockers: List[str] = []
    detail: Dict[str, Any] = {"sources": {}, "max_age_seconds": max_age}

    if db is None:
        return {"blockers": [BLOCKER_NO_DB], "detail": detail}

    def _read(name, *args, **kwargs):
        fn = getattr(db, name, None)
        if fn is None:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- unreadable is a blocker, not a raise
            logger.warning("canary evidence: %s unavailable: %s", name, exc)
            return None

    states = _read("list_canary_states")
    # SCOPED TO THE PROTECTED WINDOW. Reading every stored cycle unioned RSS
    # carriage from BEFORE the promotion into the set used to judge coverage
    # after it, so a URL that RSS carried last month could excuse a listing-only
    # sighting today -- the authority reported "authorized" on evidence that
    # said the opposite. The promotion instant is where protection begins, so
    # it is where the evidence window begins.
    promotion_at = parse_utc(((config or {}).get(PROMOTION_KEY) or {}).get("at")
                             if isinstance((config or {}).get(PROMOTION_KEY), dict)
                             else None)
    cycles = _read("get_shadow_cycle_url_sets",
                   since=promotion_at.isoformat() if promotion_at else None)
    if states is None or cycles is None:
        return {"blockers": [BLOCKER_NO_DB], "detail": detail}
    if cycles.get("evidence_problems"):
        blockers.append(BLOCKER_COVERAGE_UNASSESSABLE)
        detail["evidence_problems"] = list(cycles["evidence_problems"])

    by_key = {str(s.get("source_key")): s for s in states}
    for source in sources or [""]:
        state = by_key.get(canary_source_key(source)) or {}
        age = _age_seconds(state.get("last_success_at"), now)
        entry = {"source_key": canary_source_key(source),
                 "age_seconds": age,
                 "overlap_losses": int(state.get("consecutive_overlap_losses") or 0)}
        # A source that has never succeeded is not "young", it is unprotected.
        if age is None or age > max_age:
            if BLOCKER_CANARY_STALE not in blockers:
                blockers.append(BLOCKER_CANARY_STALE)
            entry["stale"] = True
        if entry["overlap_losses"] >= 2 and BLOCKER_OVERLAP_LOST not in blockers:
            blockers.append(BLOCKER_OVERLAP_LOST)
        # The canary records a churn breach as its outcome's REASON rather
        # than as a success, because a crawl that watched a window turning
        # over faster than it can sample protected nothing.
        if (str(state.get("last_reason") or "") == "visibility_margin_lost"
                and BLOCKER_MARGIN_LOST not in blockers):
            blockers.append(BLOCKER_MARGIN_LOST)
            entry["margin_lost"] = True
        detail["sources"][source] = entry

    record = (config or {}).get(PROMOTION_KEY) or {}
    promoted_at = record.get("at")
    parsed_promoted = None
    if promoted_at:
        try:
            parsed_promoted = datetime.datetime.fromisoformat(str(promoted_at))
            if parsed_promoted.tzinfo is None:
                parsed_promoted = parsed_promoted.replace(
                    tzinfo=datetime.timezone.utc)
        except (TypeError, ValueError):
            parsed_promoted = None
    if parsed_promoted is None:
        # Without a promotion time there is no protected window to judge, and
        # the missing record is already its own blocker.
        return {"blockers": blockers, "detail": detail}

    rss_carried = set()
    latest_canary_at = None
    unreadable_cycles = []
    for cycle in cycles.get("cycles") or []:
        rss_carried |= set(cycle.get("feed_only") or ())
        rss_carried |= set(cycle.get("duplicate_urls") or ())
        if cycle.get("mode") != "rss_primary_canary" or not cycle.get("at"):
            continue
        # PARSED, not compared as a string. See parse_utc: the raw value from
        # the database is a str, and handing it to the policy raised TypeError
        # on the first piece of real evidence the authority ever saw.
        at = parse_utc(cycle["at"])
        if at is None:
            unreadable_cycles.append(str(cycle.get("cycle_uuid") or "?"))
            continue
        if latest_canary_at is None or at > latest_canary_at:
            latest_canary_at = at
    if unreadable_cycles:
        # A canary whose own timestamp cannot be read cannot establish that it
        # ran after anything. That is "we can no longer tell", which is a
        # durable finding, not a pause -- the same treatment
        # get_shadow_cycle_url_sets gives an unparseable stored cycle.
        if BLOCKER_COVERAGE_UNASSESSABLE not in blockers:
            blockers.append(BLOCKER_COVERAGE_UNASSESSABLE)
        detail.setdefault("unreadable_cycle_timestamps", unreadable_cycles[:20])

    for source in sources or [""]:
        rows = _read("list_listing_membership",
                     source_key=canary_source_key(source))
        if rows is None:
            return {"blockers": [BLOCKER_NO_DB], "detail": detail}
        observations: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            url = row.get("canonical_url")
            at = row.get("observed_at")
            if not url or not at:
                continue
            try:
                seen = datetime.datetime.fromisoformat(str(at))
            except (TypeError, ValueError):
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=datetime.timezone.utc)
            observations.setdefault(url, []).append(
                {"at": seen, "page_index": row.get("page_index")})
        # COUNT EVERY URL, LIST A FEW. The blocker is recorded once, but the
        # evidence is not: an earlier version appended the url inside the
        # "blocker not already present" guard, so the detail could only ever
        # hold ONE example however many releases were missed, and the surface
        # read the same for one gap as for fifty. The count is what an owner
        # needs to judge severity; the examples are what they need to go and
        # look. The list is capped because this is published on an endpoint
        # the UI polls.
        for url, seen_rows in observations.items():
            state = policy.classify_coverage(
                url, seen_rows, rss_carried,
                promotion_at=parsed_promoted,
                latest_complete_canary_at=latest_canary_at,
                depth=depth)
            if state == "gap_proven":
                if BLOCKER_GAP_PROVEN not in blockers:
                    blockers.append(BLOCKER_GAP_PROVEN)
                _note_example(detail, "gap_proven", url)
            elif state == "coverage_unassessable":
                if BLOCKER_COVERAGE_UNASSESSABLE not in blockers:
                    blockers.append(BLOCKER_COVERAGE_UNASSESSABLE)
                _note_example(detail, "unassessable", url)

        systematic = policy.systematic_gap(rows, rss_carried, canaries=4)
        if systematic is True and BLOCKER_SYSTEMATIC_GAP not in blockers:
            blockers.append(BLOCKER_SYSTEMATIC_GAP)
            detail.setdefault("systematic", []).append(source)
        elif systematic is None and BLOCKER_EVIDENCE_INSUFFICIENT not in blockers:
            # Not enough evidence to say. Fails closed -- primary does not run
            # on it -- but as a PAUSE, not a proven gap. An earlier version
            # revoked here, which demoted a freshly promoted system before its
            # first canary had recorded anything, and would have made promotion
            # impossible in practice.
            blockers.append(BLOCKER_EVIDENCE_INSUFFICIENT)
            detail.setdefault("insufficient", []).append(source)

    return {"blockers": blockers, "detail": detail}


def canary_activation_evidence(config, db) -> Dict[str, Any]:
    """Would the proposed cadence actually have caught everything?

    This is the load-bearing activation question, and it is answered by
    REPLAY rather than by an estimate: take the dense membership the
    qualification epoch recorded, work out which of those cycles a canary at
    the contract's interval would actually have sampled, and check whether
    every URL the listing showed within the protected depth appears in one of
    those samples. A URL in the population that no sample recorded is a
    release this cadence would have missed, and the interval is unsafe.

    An arithmetic estimate (depth times posts-per-page over the arrival rate)
    is kept only as a cross-check elsewhere. It cannot see a burst, and it
    cannot see a population that pages off between two samples, which is
    exactly the case the canary exists for.
    """
    from backend import rss_canary_policy as policy

    contract = contract_inputs(config)
    sources = [str(s) for s in (contract.get(
        "hdencode_listing_canary_sources") or [])]
    depth = int(contract.get("hdencode_listing_canary_pages") or 3)
    interval = max(1, int(contract.get(
        "hdencode_listing_canary_minutes") or 360)) * 60
    blockers: List[str] = []
    detail: Dict[str, Any] = {"per_source": {}}

    if db is None or not hasattr(db, "list_listing_membership"):
        return {"blockers": [BLOCKER_NO_DB], "detail": detail}

    # SCOPED TO THE EPOCH. The replay is a claim about what this cadence would
    # have missed DURING QUALIFICATION; reading all retained membership let
    # evidence from before the epoch -- including from a previous, abandoned
    # qualification -- decide whether the current one is safe.
    epoch = parse_utc(cfg_epoch) if (cfg_epoch := (config or {}).get(EPOCH_KEY)) else None
    detail["epoch_started_at"] = epoch.isoformat() if epoch else None

    for source in sources or [""]:
        try:
            # RESOLVED, like every other consumer. This one was missed when the
            # boundary was fixed elsewhere, so with the contract's default
            # category names the replay read zero rows and answered
            # visibility_window_unknown forever -- a promotion that could never
            # be granted, for a reason that was not true.
            rows = db.list_listing_membership(
                source_key=canary_source_key(source),
                since=epoch.isoformat() if epoch else None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("canary activation: membership unavailable: %s", exc)
            return {"blockers": [BLOCKER_NO_DB], "detail": detail}
        if rows is None:
            return {"blockers": [BLOCKER_NO_DB], "detail": detail}

        cycles = {}
        for row in rows:
            at = row.get("observed_at")
            uuid_ = row.get("cycle_uuid")
            if not at or not uuid_:
                continue
            try:
                seen = datetime.datetime.fromisoformat(str(at))
            except (TypeError, ValueError):
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=datetime.timezone.utc)
            cycles.setdefault(uuid_, seen)
        ordered = sorted(({"cycle_uuid": k, "at": v} for k, v in cycles.items()),
                         key=lambda c: c["at"])
        if len(ordered) < 2:
            # One cycle cannot show what a cadence would have missed between
            # two of them. Unknown is refused, never assumed safe.
            blockers.append(BLOCKER_WINDOW_UNKNOWN)
            detail["per_source"][source] = {"cycles": len(ordered)}
            continue

        sampled = policy.select_sampled_cycles(ordered, interval)
        sampled_ids = {c["cycle_uuid"] for c in sampled}
        population = {r.get("canonical_url") for r in rows
                      if r.get("canonical_url")
                      and isinstance(r.get("page_index"), int)
                      and r["page_index"] <= depth}
        sampled_rows = [r for r in rows if r.get("cycle_uuid") in sampled_ids]
        result = policy.replay(population, sampled_rows, depth)
        detail["per_source"][source] = {
            "cycles": len(ordered), "sampled": len(sampled),
            "population": len(population), "missed": len(result["missed"]),
        }
        if result["missed"] and BLOCKER_INTERVAL_UNSAFE not in blockers:
            blockers.append(BLOCKER_INTERVAL_UNSAFE)

    return {"blockers": blockers, "detail": detail}


def evaluate_activation(config, db, proposed_record=None) -> Dict[str, Any]:
    """May the owner turn primary on right now, given a PROPOSED record?

    The route calls this BEFORE persisting anything, which is what stops the
    obvious circularity: if the check demanded an existing promotion record,
    the first legitimate promotion could never be made (design review R2-2 of
    2026-09-05). Never raises.
    """
    cfg = config or {}
    blockers: List[str] = []
    readiness: Optional[Dict[str, Any]] = None

    if db is None:
        blockers.append(BLOCKER_NO_DB)
    else:
        try:
            readiness = _readiness(cfg, db)
        except Exception as exc:  # noqa: BLE001 -- unknown is a blocker, not an exception
            logger.warning("rss primary activation: readiness unavailable: %s", exc)
            blockers.append(BLOCKER_NO_DB)
        else:
            if not (readiness or {}).get("ready"):
                blockers.append(BLOCKER_NOT_READY)

    # MEASURED, not merely configured. This used to be `if not cfg.get(...)`,
    # a truthiness test that "not-a-timestamp" satisfied.
    continuity = qualification_continuity(cfg, db)
    if not continuity["complete"]:
        blockers.append(BLOCKER_EPOCH_INCOMPLETE)

    # RHC-13's floor. Shadow readiness only asks for a reduction better than
    # zero, so a 1% saving satisfied it; the promotion contract fixes 0.50.
    if readiness is not None:
        measured = (readiness or {}).get("request_reduction_pct")
        if measured is None or float(measured) < REDUCTION_FLOOR_PCT:
            blockers.append(BLOCKER_REDUCTION_BELOW_FLOOR)

    retention = _retention_days(cfg)
    if retention is None or retention < MIN_RETENTION_DAYS:
        blockers.append(BLOCKER_RETENTION_TOO_SHORT)

    if not _auto_demotion_armed(cfg):
        blockers.append(BLOCKER_NO_DEMOTION)

    if proposed_record is not None:
        if proposed_record.get("canary_contract_hash") != canary_contract_hash(cfg):
            blockers.append(BLOCKER_CONTRACT_MISMATCH)

    # Would this cadence actually have caught everything the listing showed?
    # Answered by replaying the qualification epoch's own dense membership,
    # not by an arithmetic estimate that cannot see a burst.
    for blocker in canary_activation_evidence(cfg, db)["blockers"]:
        if blocker not in blockers:
            blockers.append(blocker)

    # Freshness is an activation condition too: promoting onto a canary that
    # has not run recently would start the protected state already stale.
    for blocker in canary_evidence(cfg, db)["blockers"]:
        if blocker == BLOCKER_CANARY_STALE:
            if BLOCKER_CANARY_NOT_RECENT not in blockers:
                blockers.append(BLOCKER_CANARY_NOT_RECENT)
        elif blocker == BLOCKER_NO_DB and BLOCKER_NO_DB not in blockers:
            blockers.append(BLOCKER_NO_DB)

    if not CANARY_IMPLEMENTED:
        blockers.append(BLOCKER_NO_CANARY)

    return {
        "eligible": not blockers,
        "blockers": blockers,
        # What the epoch check MEASURED, not just its verdict: an owner refused
        # a promotion needs to know whether they are two days short or were
        # reset by last night's outage.
        "qualification": continuity,
        "readiness": readiness,
        "contract_hash": canary_contract_hash(cfg),
        "retention_days": retention,
        "epoch_started_at": cfg.get(EPOCH_KEY),
    }


def evaluate_runtime(config, db) -> Dict[str, Any]:
    """Is primary in effect this cycle, and if not, is that temporary or final?

    Shadow readiness is deliberately absent: see the module docstring. Never
    raises -- a database that cannot answer is a suspension, not an exception.
    """
    cfg = config or {}
    requested = cfg.get("hdencode_discovery_mode", "listing")
    blockers: List[str] = []
    record = cfg.get(PROMOTION_KEY) or None

    if requested != "rss_primary":
        return {
            "state": STATE_NOT_REQUESTED,
            "authorized": False,
            "blockers": [],
            "suspensions": [],
            "revocations": [],
            "record": record,
            "contract_hash": canary_contract_hash(cfg),
        }

    if db is None:
        blockers.append(BLOCKER_NO_DB)

    if not isinstance(record, dict) or not record.get("at"):
        # A mode persisted before the hybrid existed, or hand-written. It is
        # refused, and no migration rewrites it: the missing record IS the
        # refusal, and /rss/status shows requested and effective separately.
        blockers.append(BLOCKER_NO_RECORD)
    elif record.get("canary_contract_hash") != canary_contract_hash(cfg):
        blockers.append(BLOCKER_CONTRACT_CHANGED)

    if not _auto_demotion_armed(cfg):
        blockers.append(BLOCKER_NO_DEMOTION)

    # Canary freshness, the coverage states, overlap and the systematic-gap
    # check, read from the canary's own evidence. Anything unreadable comes
    # back as database_unavailable, which suspends; a durable finding revokes.
    evidence = canary_evidence(cfg, db)
    for blocker in evidence["blockers"]:
        if blocker not in blockers:
            blockers.append(blocker)

    if not CANARY_IMPLEMENTED:
        blockers.append(BLOCKER_NO_CANARY)

    suspensions = [b for b in blockers if b in SUSPENSION_BLOCKERS]
    revocations = [b for b in blockers if b in REVOCATION_BLOCKERS]
    unclassified = [b for b in blockers if b not in RUNTIME_BLOCKERS]
    if unclassified:
        # Nothing decides its own severity by omission. An unclassified
        # blocker is a bug, and the safe reading of a bug in this module is
        # the durable one.
        logger.error("rss primary runtime: unclassified blocker(s) %s; "
                     "treating as a revocation", ", ".join(unclassified))
        revocations = revocations + unclassified
    if revocations:
        state = STATE_REVOKED
    elif suspensions:
        state = STATE_SUSPENDED
    else:
        state = STATE_AUTHORIZED
    return {
        "state": state,
        "authorized": state == STATE_AUTHORIZED,
        "blockers": blockers,
        "suspensions": suspensions,
        "revocations": revocations,
        "record": record,
        "contract_hash": canary_contract_hash(cfg),
    }


def evaluate_rss_primary_authority(config, db) -> Dict[str, Any]:
    """The ACTIVATION question, in the response shape #108's callers consume.

    This is the "may primary be turned on at all" question, so it still
    includes shadow readiness. It is deliberately NOT what the runtime asks:
    see ``evaluate_runtime`` and the module docstring for why readiness must
    not gate an already-promoted system.

    NO PRODUCTION CALLER REMAINS. The docstring said "it is what POST /rss/mode
    asks" and "#108's route and tests use it"; PR 1 moved the route onto
    ``evaluate_activation`` and left this sentence behind, which is the stale
    kind of comment HDE-5 spent a round removing. It is kept, not deleted,
    because it is #108's published response shape and this branch is stacked on
    #108 -- deleting it here would resolve a question that belongs to that PR.
    The canary block it returns is filled for real (see ``_canary_summary``)
    rather than left as the placeholder, so if a caller does come back it is
    not answered with a permanent "the canary has never succeeded".
    """
    activation = evaluate_activation(config, db, None)
    blockers = list(activation["blockers"])
    return {
        "authorized": activation["eligible"],
        "blockers": blockers,
        "state": STATE_AUTHORIZED if activation["eligible"] else STATE_REVOKED,
        "provisional": activation["eligible"],
        "readiness": activation["readiness"],
        # Same three placeholders as status_fields, filled the same way and
        # for the same reason: a permanent "the canary has never succeeded" is
        # a fact nobody measured, and this shape is published (see the
        # docstring above on who does and does not call this).
        "canary": dict(_canary_summary(config, db),
                       contract_hash=activation["contract_hash"]),
        "auto_demotion_armed": _auto_demotion_armed(config),
    }


def reconcile_requested_primary(config, db, backend) -> Dict[str, Any]:
    """Demote a REQUESTED primary whose runtime verdict is a durable finding.

    Asked whenever the requested mode is ``rss_primary`` -- not only when
    primary is in effect. That distinction is the whole point: if the runtime
    has already dropped to shadow for its own reasons, a demotion that only
    ran "while primary" would never run at all, and the promotion record would
    survive to authorize primary again the moment the blocker cleared.

    A SUSPENSION changes nothing durable. Only a revocation persists shadow,
    deletes the promotion record and writes what caused it. There is no
    automatic re-promotion: coming back requires a fresh, explicit one.

    The write is fail-closed. If it cannot be verified on disk, nothing is
    committed to memory -- and that is safe, because every revocation trigger
    is derived from evidence or configuration rather than process memory, so
    the next cycle in this process or the next reaches the same verdict and
    tries again.
    """
    cfg = config or {}
    if cfg.get("hdencode_discovery_mode") != "rss_primary":
        return {"acted": False, "reason": "primary_not_requested"}

    runtime = evaluate_runtime(cfg, db)
    if not runtime["revocations"]:
        return {"acted": False, "state": runtime["state"],
                "suspensions": list(runtime["suspensions"])}

    if backend is None or not hasattr(backend, "persist_config_snapshot"):
        logger.error("rss primary revoked (%s) but no writer is available to "
                     "persist it; the runtime still refuses primary",
                     ", ".join(runtime["revocations"]))
        return {"acted": False, "persist_failed": True,
                "reason": list(runtime["revocations"])}

    demotion = {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reason": list(runtime["revocations"]),
        "evidence": canary_evidence(cfg, db).get("detail", {}),
    }
    candidate = dict(cfg)
    candidate["hdencode_discovery_mode"] = "rss_shadow"
    candidate.pop(PROMOTION_KEY, None)
    candidate[LAST_DEMOTION_KEY] = demotion
    must_contain = {"hdencode_discovery_mode": "rss_shadow",
                    LAST_DEMOTION_KEY: demotion}
    try:
        verified = backend.persist_config_snapshot(
            candidate, must_contain=must_contain)
    except Exception as exc:  # noqa: BLE001 -- retried next cycle, never half-applied
        logger.error("rss primary revoked (%s) but the demotion could not be "
                     "persisted: %s; it will be retried",
                     ", ".join(runtime["revocations"]), exc)
        return {"acted": False, "persist_failed": True,
                "reason": list(runtime["revocations"])}

    if verified.get(PROMOTION_KEY):
        logger.error("the persisted config still carries a promotion record "
                     "after a demotion; refusing to call it demoted")
        return {"acted": False, "persist_failed": True,
                "reason": list(runtime["revocations"])}

    backend.commit_config_in_place(verified)
    logger.warning("RSS primary REVOKED and demoted to rss_shadow: %s. "
                   "Returning to primary requires a fresh explicit promotion.",
                   ", ".join(runtime["revocations"]))
    return {"acted": True, "reason": list(runtime["revocations"]),
            "demotion": demotion}


def effective_discovery_mode(config, db) -> Tuple[str, Optional[Dict[str, Any]]]:
    """The mode the runtime actually runs, and the authority that decided it.

    A requested ``rss_primary`` runs as ``rss_shadow`` unless authorized --
    shadow keeps every observation flowing and acquires nothing, which is the
    safe side of the decision record.
    """
    requested = (config or {}).get("hdencode_discovery_mode", "listing")
    if requested != "rss_primary":
        return requested, None
    authority = evaluate_runtime(config, db)
    if authority.get("authorized"):
        return "rss_primary", authority
    logger.warning(
        "hdencode_discovery_mode is rss_primary but primary is %s (%s); "
        "running as rss_shadow", authority.get("state", "unauthorized"),
        ", ".join(authority.get("blockers") or ()))
    return "rss_shadow", authority


def _canary_health(config, db, evidence) -> Dict[str, Any]:
    """Per-source canary health for the status surface.

    ``available`` is False when the scheduling state could not be READ, which
    is not the same as a canary that has never run: the first would be a fault
    to fix, the second a system that has not started yet, and a surface that
    showed them identically would let an outage look like a quiet week.
    """
    cfg = config or {}
    contract = contract_inputs(cfg)
    interval = max(1, int(contract.get(
        "hdencode_listing_canary_minutes") or 360)) * 60
    max_age = max(1, int(contract.get(
        "hdencode_listing_canary_max_age_minutes") or 720)) * 60
    health: Dict[str, Any] = {
        "implemented": CANARY_IMPLEMENTED,
        "interval_seconds": interval,
        "max_age_seconds": max_age,
        "available": False,
        "sources": {},
    }
    states = None
    if db is not None and hasattr(db, "list_canary_states"):
        try:
            states = db.list_canary_states()
        except Exception as exc:  # noqa: BLE001 -- unreadable is reported, not raised
            logger.warning("canary health unavailable: %s", exc)
            states = None
    if states is None:
        return health

    health["available"] = True
    now = datetime.datetime.now(datetime.timezone.utc)
    by_key = {str(s.get("source_key")): s for s in states}
    per_source = (evidence or {}).get("detail", {}).get("sources", {})
    for source in [str(s) for s in (contract.get(
            "hdencode_listing_canary_sources") or [])]:
        state = by_key.get(canary_source_key(source)) or {}
        age = _age_seconds(state.get("last_success_at"), now)
        health["sources"][source] = {
            # Published so an operator can match what they configured to the
            # row the crawler actually writes; the two are not the same string.
            "source_key": canary_source_key(source),
            "last_attempt_at": state.get("last_attempt_at"),
            "next_attempt_at": state.get("next_attempt_at"),
            "last_success_at": state.get("last_success_at"),
            "age_seconds": age,
            "last_outcome": state.get("last_outcome"),
            "last_reason": state.get("last_reason"),
            "consecutive_failures": state.get("consecutive_failures"),
            "consecutive_overlap_losses": state.get("consecutive_overlap_losses"),
            "stale": bool(per_source.get(source, {}).get("stale"))
                     or age is None or age > max_age,
            "has_run": state.get("last_success_at") is not None,
        }
    return health


def _request_cost(db, config=None, *, days=7) -> Dict[str, Any]:
    """What the hybrid actually spent, over wall-clock.

    Reports the counts and nothing derived from them. A reduction percentage
    needs a baseline for what listing-only would have cost, which this cannot
    observe after promotion -- that projection belongs to the replay, and
    printing a number here that looked like a measurement would be worse than
    printing none. ``available`` False means the ledger could not be read.

    THE WINDOW IS THE HONEST PART. The ledger records the mode each batch was
    spent in, but sum_requests aggregates by kind across every mode, so a flat
    trailing window on a system promoted two days ago would add five days of
    shadow spending to a figure labelled as the hybrid's. It starts at the
    promotion instead whenever the promotion is inside the window, and the
    block says which of the two it did: these are two different regimes, and
    silently averaging them is how a cost claim stops meaning anything.
    """
    cfg = config or {}
    now = datetime.datetime.now(datetime.timezone.utc)
    trailing = now - datetime.timedelta(days=days)
    since, scope = trailing, "trailing_window"

    record = cfg.get(PROMOTION_KEY) or {}
    promoted_at = record.get("at") if isinstance(record, dict) else None
    if promoted_at:
        try:
            parsed = datetime.datetime.fromisoformat(str(promoted_at))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            if parsed > trailing:
                since, scope = parsed, "since_promotion"
        except (TypeError, ValueError):
            # An unparseable promotion time is not a reason to mislabel the
            # window; a broken record is already its own blocker.
            pass

    out: Dict[str, Any] = {"available": False, "window_days": days,
                           "since": since.isoformat(), "scope": scope,
                           "floor": 0.50, "target": 0.70}
    if db is None or not hasattr(db, "sum_requests"):
        return out
    try:
        totals = db.sum_requests(since.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.warning("request cost unavailable: %s", exc)
        return out
    if totals is None:
        return out
    out["available"] = True
    out["totals"] = dict(totals)
    return out


def _worst_canary_source(health) -> Optional[Dict[str, Any]]:
    """The configured canary source whose last success is OLDEST.

    One source succeeding does not make the listing observed; a scalar has to
    describe the weakest link or it overstates the protection. None when the
    health is unreadable, when no source is configured, or when any configured
    source has never succeeded -- all of which are "we cannot say this is
    protected", which is the safe way for a scalar to be wrong.

    Both published scalars come from this ONE entry rather than being reduced
    separately, so the timestamp and the age can never describe different
    sources. The comparison is on the parsed age, never on the timestamp
    STRING: ISO strings only sort chronologically while every one of them
    carries the same offset shape, and this module has no way to promise that
    about a value that has been through the database.
    """
    sources = (health or {}).get("sources") or {}
    if not health.get("available") or not sources:
        return None
    entries = list(sources.values())
    if any(e.get("age_seconds") is None or e.get("last_success_at") is None
           for e in entries):
        return None
    return max(entries, key=lambda e: float(e["age_seconds"]))


def _worst_canary_success(health) -> Optional[str]:
    """When the weakest configured source last succeeded."""
    worst = _worst_canary_source(health)
    return None if worst is None else worst["last_success_at"]


def _worst_canary_age(health) -> Optional[int]:
    """Seconds since the weakest configured source last succeeded."""
    worst = _worst_canary_source(health)
    return None if worst is None else int(worst["age_seconds"])


def _canary_summary(config, db) -> Dict[str, Any]:
    """The scalar canary block both surfaces publish."""
    health = _canary_health(config, db, canary_evidence(config or {}, db))
    return {
        "implemented": CANARY_IMPLEMENTED,
        "available": health["available"],
        "last_success": _worst_canary_success(health),
        "age_seconds": _worst_canary_age(health),
        "interval_seconds": (health["interval_seconds"]
                             if CANARY_IMPLEMENTED else None),
    }


def status_fields(config, db) -> Dict[str, Any]:
    """The promotion-authority block published on /rss/status.

    Both questions are published: ``activation`` is evaluated against a
    prospective record built from the live contract, so the owner can see what
    a promotion would face before making one, and ``runtime`` says what the
    running system is doing about the mode already requested.
    """
    cfg = config or {}
    requested = cfg.get("hdencode_discovery_mode", "listing")
    effective, _ = effective_discovery_mode(cfg, db)
    runtime = evaluate_runtime(cfg, db)
    prospective = build_promotion_record(cfg, at=None)
    activation = evaluate_activation(cfg, db, prospective)
    evidence = canary_evidence(cfg, db)
    canary = _canary_health(cfg, db, evidence)
    return {
        # PER-SOURCE CANARY HEALTH. A stale or blocked canary means the
        # protection is degraded, and the design record is explicit that a
        # system must not run for long labelled canary-protected while its
        # canary has not successfully observed the listing. Publishing it is
        # how that stops being an internal detail.
        "canary": canary,
        "canary_evidence": evidence.get("detail", {}),
        "request_cost": _request_cost(db, cfg),
        "requested_mode": requested,
        "effective_mode": effective,
        "state": runtime["state"],
        "primary_authorized": runtime["authorized"],
        "provisional": runtime["authorized"],
        "promotion_blockers": list(runtime["blockers"]),
        "suspensions": list(runtime["suspensions"]),
        "revocations": list(runtime["revocations"]),
        "activation": {
            "eligible": activation["eligible"],
            "blockers": list(activation["blockers"]),
        },
        # Published per the design's status contract: how many clean days the
        # epoch has actually accrued, the worst gap in it, and whether an
        # outage reset the clock.
        "qualification": activation["qualification"],
        "contract_hash": runtime["contract_hash"],
        "contract": contract_inputs(cfg),
        "canary_implemented": CANARY_IMPLEMENTED,
        "canary_version": CANARY_VERSION,
        # Published since #108, when they were placeholders because no canary
        # existed to fill them. It does now, so leaving them None would no
        # longer mean "not built yet", it would assert the canary has never
        # succeeded -- to any consumer, indistinguishable from a dead one.
        # They summarise the WORST configured source, since protection needs
        # every one of them observed, and read None whenever that cannot be
        # established. Per-source truth is in "canary".
        "canary_last_success": _worst_canary_success(canary),
        "canary_age_seconds": _worst_canary_age(canary),
        "canary_interval_seconds": (canary["interval_seconds"]
                                    if CANARY_IMPLEMENTED else None),
        "retention_days": activation["retention_days"],
        "epoch_started_at": cfg.get(EPOCH_KEY),
        "promotion_record": runtime["record"],
        "last_demotion": cfg.get(LAST_DEMOTION_KEY),
        "auto_demotion_armed": _auto_demotion_armed(cfg),
    }
