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

WHAT THIS FILE DOES NOT DO YET. ``CANARY_IMPLEMENTED`` is False and every
evaluation therefore carries ``coverage_canary_not_implemented``. The canary
itself -- the scheduled listing crawl, the membership replay, the four coverage
states, overlap and churn -- is the next change. Until it exists the evidence
those checks read does not exist either, and this module says so with
``canary_evidence_unavailable`` rather than quietly passing a check it cannot
make. There is no path to primary in this file.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Flip when the coverage-canary hybrid exists AND its tests exist. Until then
#: every evaluation carries the ``coverage_canary_not_implemented`` blocker.
CANARY_IMPLEMENTED = False

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
BLOCKER_NO_DEMOTION = "auto_demotion_not_armed"
BLOCKER_NO_DB = "database_unavailable"
#: The canary's own evidence cannot be read because the canary does not exist
#: yet. Distinct from a passing check on purpose: absence of evidence is not
#: evidence of coverage.
BLOCKER_NO_CANARY_EVIDENCE = "canary_evidence_unavailable"

#: Temporary: the effective mode drops to shadow, the promotion record STAYS.
#: ``coverage_canary_not_implemented`` belongs here rather than nowhere: while
#: the canary does not exist, primary cannot run, but nothing durable has been
#: decided about a promotion, and building the canary is not a safety finding
#: against one. Leaving it unclassified let it fall through the state logic and
#: pick its own severity, which is the implicit behaviour the two sets exist to
#: prevent (PR #116 review, PR1-R5).
SUSPENSION_BLOCKERS = frozenset({
    BLOCKER_NO_DB, BLOCKER_NO_CANARY_EVIDENCE, BLOCKER_NO_CANARY,
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

    if not cfg.get(EPOCH_KEY):
        blockers.append(BLOCKER_EPOCH_INCOMPLETE)

    retention = _retention_days(cfg)
    if retention is None or retention < MIN_RETENTION_DAYS:
        blockers.append(BLOCKER_RETENTION_TOO_SHORT)

    if not _auto_demotion_armed(cfg):
        blockers.append(BLOCKER_NO_DEMOTION)

    if proposed_record is not None:
        if proposed_record.get("canary_contract_hash") != canary_contract_hash(cfg):
            blockers.append(BLOCKER_CONTRACT_MISMATCH)

    # The canary's own qualification -- the replay result per source, the
    # interval-versus-window rule and canary freshness -- reads evidence that
    # only exists once the canary does. Saying so is not the same as passing.
    blockers.append(BLOCKER_NO_CANARY_EVIDENCE)

    if not CANARY_IMPLEMENTED:
        blockers.append(BLOCKER_NO_CANARY)

    return {
        "eligible": not blockers,
        "blockers": blockers,
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

    # Canary freshness, coverage state, overlap, churn and the systematic-gap
    # check all read canary evidence, which does not exist yet.
    blockers.append(BLOCKER_NO_CANARY_EVIDENCE)

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
    includes shadow readiness, and it is what ``POST /rss/mode`` asks. It is
    deliberately NOT what the runtime asks: see ``evaluate_runtime`` and the
    module docstring for why readiness must not gate an already-promoted
    system. The name is kept because #108's route and tests use it.
    """
    activation = evaluate_activation(config, db, None)
    blockers = list(activation["blockers"])
    return {
        "authorized": activation["eligible"],
        "blockers": blockers,
        "state": STATE_AUTHORIZED if activation["eligible"] else STATE_REVOKED,
        "provisional": activation["eligible"],
        "readiness": activation["readiness"],
        "canary": {
            "implemented": CANARY_IMPLEMENTED,
            "contract_hash": activation["contract_hash"],
            "last_success": None,
            "age_seconds": None,
            "interval_seconds": None,
        },
        "auto_demotion_armed": _auto_demotion_armed(config),
    }


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
    return {
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
        "contract_hash": runtime["contract_hash"],
        "contract": contract_inputs(cfg),
        "canary_implemented": CANARY_IMPLEMENTED,
        "canary_version": CANARY_VERSION,
        # Published since #108 and kept: the canary's own health, which stays
        # empty until the canary exists.
        "canary_last_success": None,
        "canary_age_seconds": None,
        "canary_interval_seconds": None,
        "retention_days": activation["retention_days"],
        "epoch_started_at": cfg.get(EPOCH_KEY),
        "promotion_record": runtime["record"],
        "last_demotion": cfg.get(LAST_DEMOTION_KEY),
        "auto_demotion_armed": _auto_demotion_armed(cfg),
    }
