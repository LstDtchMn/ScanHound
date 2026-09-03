"""Who is allowed to make RSS the primary discovery path, and when.

One executable predicate, consulted by every consumer that decides whether
``rss_primary`` is in effect: the ``POST /rss/mode`` route, the background
scanner's per-cycle mode selection, the RSS poll cycle, and ``GET /rss/status``.
A persisted or hand-written ``hdencode_discovery_mode: rss_primary`` therefore
never becomes blind primary by bypassing the route: the runtime asks the same
question and answers the same way.

Why (round-7 review, HDE-1; round-7b owner decision, 2026-09-03). The accepted
decision record -- docs/reviews/peer-rounds/2026-08-11-rss-readiness-gate-design.md,
merged as PR #61 -- says blind ``rss_primary`` is NO-GO. The supported state is
*canary-protected provisional primary*: a reduced-frequency listing scrape keeps
running the shadow comparison while RSS is primary, its health is surfaced, and
a proven ``never_acquired`` or a stale canary demotes automatically. None of that
is built yet. Until it is, the code allowed exactly what the design forbids, and
promoting stopped the shadow comparison that produced the readiness evidence the
gate itself reads -- using evidence to enter a state that stops generating it.

So, until the hybrid exists::

    authorized = False
    blockers  include  coverage_canary_not_implemented

even when shadow readiness is green. When the hybrid is built, this same
function grows the real prerequisites (canary freshness, interval < the
listing's visibility window, auto-demotion armed) rather than a second,
route-specific rule appearing somewhere else.

The status vocabulary below is the one ``GET /rss/status`` publishes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Flip when the coverage-canary hybrid exists AND its tests exist. Until then
#: every evaluation carries the ``coverage_canary_not_implemented`` blocker.
CANARY_IMPLEMENTED = False

BLOCKER_NO_CANARY = "coverage_canary_not_implemented"
BLOCKER_NOT_READY = "shadow_readiness_not_met"
BLOCKER_NO_DB = "database_unavailable"

RSS_MODES = ("listing", "rss_shadow", "rss_primary")


def _readiness(config, db) -> Dict[str, Any]:
    cfg = config or {}
    return db.get_hdencode_rss_readiness(
        min_cycles=cfg.get("hdencode_rss_shadow_min_cycles", 20),
        min_days=cfg.get("hdencode_rss_shadow_min_days", 7),
    )


def evaluate_rss_primary_authority(config, db) -> Dict[str, Any]:
    """Is ``rss_primary`` authorized right now, and if not, why not.

    Never raises: a database that cannot answer is itself a blocker, because
    "cannot establish readiness" is not "ready".
    """
    blockers = []
    readiness: Optional[Dict[str, Any]] = None
    if db is None:
        blockers.append(BLOCKER_NO_DB)
    else:
        try:
            readiness = _readiness(config, db)
        except Exception as exc:  # noqa: BLE001 -- unknown is a blocker, not an exception
            logger.warning("rss primary authority: readiness unavailable: %s", exc)
            blockers.append(BLOCKER_NO_DB)
        else:
            if not (readiness or {}).get("ready"):
                blockers.append(BLOCKER_NOT_READY)
    if not CANARY_IMPLEMENTED:
        blockers.append(BLOCKER_NO_CANARY)
    return {
        "authorized": not blockers,
        "blockers": blockers,
        "provisional": False,          # provisional primary exists only with a canary
        "readiness": readiness,
        "canary": {
            "implemented": CANARY_IMPLEMENTED,
            "last_success": None,
            "age_seconds": None,
            "interval_seconds": None,
        },
        "auto_demotion_armed": False,
    }


def effective_discovery_mode(config, db) -> Tuple[str, Optional[Dict[str, Any]]]:
    """The mode the runtime actually runs, and the authority that decided it.

    ``listing`` and ``rss_shadow`` are what they say. A requested
    ``rss_primary`` runs as ``rss_shadow`` unless authorized -- shadow keeps
    every observation flowing and acquires nothing, which is the safe side of
    the decision record. The demotion is logged so a persisted primary that is
    being refused is visible in the log as well as on /rss/status.
    """
    requested = (config or {}).get("hdencode_discovery_mode", "listing")
    if requested != "rss_primary":
        return requested, None
    authority = evaluate_rss_primary_authority(config, db)
    if authority["authorized"]:
        return "rss_primary", authority
    logger.warning(
        "hdencode_discovery_mode is rss_primary but primary is not authorized (%s); "
        "running as rss_shadow", ", ".join(authority["blockers"]))
    return "rss_shadow", authority


def status_fields(config, db) -> Dict[str, Any]:
    """The promotion-authority block published on /rss/status."""
    requested = (config or {}).get("hdencode_discovery_mode", "listing")
    effective, authority = effective_discovery_mode(config, db)
    if authority is None:
        authority = evaluate_rss_primary_authority(config, db)
    canary = authority["canary"]
    return {
        "requested_mode": requested,
        "effective_mode": effective,
        "primary_authorized": authority["authorized"],
        "promotion_blockers": list(authority["blockers"]),
        "provisional": authority["provisional"],
        "canary_implemented": canary["implemented"],
        "canary_last_success": canary["last_success"],
        "canary_age_seconds": canary["age_seconds"],
        "canary_interval_seconds": canary["interval_seconds"],
        "auto_demotion_armed": authority["auto_demotion_armed"],
    }
