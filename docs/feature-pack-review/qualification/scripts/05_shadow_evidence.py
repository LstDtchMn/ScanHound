#!/usr/bin/env python3
"""Read-only RSS shadow-mode evidence collector.

Computes shadow-validation readiness directly from the authoritative
``hdencode_shadow_cycles`` / ``hdencode_shadow_misses`` / ``hdencode_feed_state``
schema (backend/database.py), mirroring ``Database.get_hdencode_shadow_summary``
and ``get_hdencode_rss_readiness`` exactly, so this independent collector and the
running app agree on what "ready" means.

Repaired 2026-07-21 (Claude, git+validation lane). The prior revision guessed at
column names (``complete``/``is_complete``, ``recovery_observed``/``is_recovery``)
that do not exist in the real schema, so it (a) counted EVERY introspected row --
including ``hdencode_shadow_misses`` rows -- as a completed cycle, and (b) always
reported zero recovery. This revision uses the real columns:

  * a completed comparison cycle = a ``hdencode_shadow_cycles`` row with
    ``outcome IN ('success','relevant_miss')`` and ``normal_feeds_complete=1``;
  * relevant misses = ``SUM(relevant_miss_count)`` over those cycles
    (cross-checked against the ``hdencode_shadow_misses`` row count);
  * recovery = cycles with ``restart_recovery=1 OR catchup_used=1``;
  * request reduction = ``100*(listing_requests-rss_requests)/listing_requests``;
  * normal feeds healthy = ``movies_all`` and ``tv_all`` in ``hdencode_feed_state``
    with ``last_status IN (200,304)``, zero consecutive failures, and a
    ``last_checked_at`` within ``--max-stale-minutes``.

A relevant miss is a mandatory stop condition, so mis-reading these columns is
safety-relevant -- hence the exact-schema rewrite.

If ``--base-url`` is supplied, the app's own ``GET /rss/status`` readiness is also
captured and reconciled against this collector's independent computation.
"""
from __future__ import annotations
import argparse, datetime as dt, json, sqlite3, urllib.error, urllib.request
from pathlib import Path

CYCLE_OUTCOMES = ("success", "relevant_miss")


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _feeds_healthy(con, now, max_stale_minutes):
    if not _table_exists(con, "hdencode_feed_state"):
        return False, {}
    rows = {
        r["feed_key"]: dict(r)
        for r in con.execute(
            "SELECT feed_key,last_status,consecutive_failures,last_checked_at "
            "FROM hdencode_feed_state WHERE feed_key IN ('movies_all','tv_all')"
        )
    }
    detail = {}
    healthy = True
    stale_cutoff = max(15, int(max_stale_minutes)) * 60
    for key in ("movies_all", "tv_all"):
        row = rows.get(key)
        checked = _parse_iso(row.get("last_checked_at")) if row else None
        fresh = bool(checked) and (now - checked).total_seconds() <= stale_cutoff
        ok = (
            bool(row)
            and row.get("last_status") in (200, 304)
            and int(row.get("consecutive_failures") or 0) == 0
            and fresh
        )
        detail[key] = {
            "present": bool(row),
            "last_status": row.get("last_status") if row else None,
            "consecutive_failures": row.get("consecutive_failures") if row else None,
            "last_checked_at": row.get("last_checked_at") if row else None,
            "fresh": fresh,
            "healthy": ok,
        }
        healthy = healthy and ok
    return healthy, detail


def _fetch_status_readiness(base_url, token):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        base_url.rstrip("/") + "/rss/status", headers=headers, method="GET"
    )
    with urllib.request.urlopen(request, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--evidence-dir", required=True)
    ap.add_argument("--base-url")
    ap.add_argument("--token", default="")
    ap.add_argument("--min-cycles", type=int, default=20)
    ap.add_argument("--min-days", type=int, default=7)
    ap.add_argument("--max-stale-minutes", type=int, default=180)
    a = ap.parse_args()
    db = Path(a.db).resolve()
    ev = Path(a.evidence_dir).resolve()
    ev.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        user_version = con.execute("PRAGMA user_version").fetchone()[0]
        has_cycles = _table_exists(con, "hdencode_shadow_cycles")
        totals = None
        cycle_rows = []
        miss_rows = 0
        if has_cycles:
            placeholders = ",".join("?" * len(CYCLE_OUTCOMES))
            totals = con.execute(
                f"""SELECT COUNT(*) AS complete_cycles,
                           MIN(completed_at) AS first_completed_at,
                           MAX(completed_at) AS last_completed_at,
                           SUM(rss_requests) AS rss_requests,
                           SUM(listing_requests) AS listing_requests,
                           SUM(CASE WHEN restart_recovery=1 OR catchup_used=1 THEN 1 ELSE 0 END) AS recovery_cycles
                    FROM hdencode_shadow_cycles
                    WHERE outcome IN ({placeholders})
                      AND normal_feeds_complete=1
                      AND rss_requests>0
                      AND listing_requests>0""",
                CYCLE_OUTCOMES,
            ).fetchone()
            all_misses = con.execute(
                "SELECT COALESCE(SUM(relevant_miss_count),0) "
                "FROM hdencode_shadow_cycles"
            ).fetchone()[0]
            cycle_rows = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM hdencode_shadow_cycles "
                    "ORDER BY completed_at DESC LIMIT 500"
                )
            ]
            if _table_exists(con, "hdencode_shadow_misses"):
                miss_rows = con.execute(
                    "SELECT COUNT(*) FROM hdencode_shadow_misses"
                ).fetchone()[0]
        feeds_healthy, feed_detail = _feeds_healthy(con, now, a.max_stale_minutes)
        auto_action_rows = (
            con.execute(
                "SELECT COUNT(*) FROM hdencode_actions "
                "WHERE requested_by='auto'"
            ).fetchone()[0]
            if _table_exists(con, "hdencode_actions")
            else 0
        )
        active_action_rows = (
            con.execute(
                "SELECT COUNT(*) FROM hdencode_actions "
                "WHERE state IN ('queued','retrieving_links','links_ready','submitting')"
            ).fetchone()[0]
            if _table_exists(con, "hdencode_actions")
            else 0
        )
        miss_count_mismatches = (
            con.execute(
                """SELECT COUNT(*) FROM (
                       SELECT c.cycle_uuid
                       FROM hdencode_shadow_cycles c
                       LEFT JOIN hdencode_shadow_misses m
                         ON m.cycle_uuid=c.cycle_uuid
                       GROUP BY c.cycle_uuid,c.relevant_miss_count
                       HAVING c.relevant_miss_count != COUNT(m.canonical_url)
                   )"""
            ).fetchone()[0]
            if has_cycles and _table_exists(con, "hdencode_shadow_misses")
            else 0
        )
    finally:
        con.close()

    def _int(row, key):
        return int(row[key]) if row is not None and row[key] is not None else 0

    complete_cycles = _int(totals, "complete_cycles")
    relevant_misses = int(all_misses or 0) if has_cycles else 0
    recovery_cycles = _int(totals, "recovery_cycles")
    rss_requests = _int(totals, "rss_requests")
    listing_requests = _int(totals, "listing_requests")
    first_at = totals["first_completed_at"] if totals else None
    last_at = totals["last_completed_at"] if totals else None
    first_dt = _parse_iso(first_at)
    last_dt = _parse_iso(last_at)
    observed_days = (
        (last_dt - first_dt).total_seconds() / 86400.0
        if first_dt and last_dt
        else 0.0
    )
    reduction = (
        100.0 * (listing_requests - rss_requests) / listing_requests
        if listing_requests > 0
        else 0.0
    )

    required_cycles = max(1, int(a.min_cycles))
    required_days = max(1, int(a.min_days))
    reasons = []
    if complete_cycles < required_cycles:
        reasons.append("insufficient_comparison_cycles")
    if observed_days < required_days:
        reasons.append("insufficient_observation_days")
    if relevant_misses > 0:
        reasons.append("relevant_misses_detected")
    if reduction <= 0:
        reasons.append("request_reduction_not_proven")
    if recovery_cycles < 1:
        reasons.append("restart_or_catchup_recovery_not_proven")
    if not feeds_healthy:
        reasons.append("normal_feeds_unhealthy_or_stale")
    if integrity != "ok":
        reasons.append(f"integrity_check={integrity}")
    if user_version != 6:
        reasons.append(f"unexpected_schema_version={user_version}")
    if miss_count_mismatches:
        reasons.append("shadow_miss_count_mismatch")
    if auto_action_rows:
        reasons.append("automatic_action_activity_detected")
    if active_action_rows:
        reasons.append("active_action_activity_detected")

    readiness = {
        "ready": not reasons,
        "required_cycles": required_cycles,
        "successful_cycles": complete_cycles,
        "required_days": required_days,
        "observed_days": observed_days,
        "relevant_misses": relevant_misses,
        "shadow_miss_rows": miss_rows,
        "request_reduction_pct": round(reduction, 2),
        "recovery_cycles": recovery_cycles,
        "normal_feeds_healthy": feeds_healthy,
        "first_completed_at": first_at,
        "last_completed_at": last_at,
        "reasons": reasons,
    }

    app_readiness = None
    reconciliation = None
    if a.base_url:
        try:
            status = _fetch_status_readiness(a.base_url, a.token)
            app_readiness = status.get("readiness")
            mode = status.get("mode")
            safe_defaults = status.get("safe_defaults") or {}
            if mode != "rss_shadow":
                reasons.append(f"unsafe_rss_mode={mode}")
            if safe_defaults.get("rss_auto_grab") is not False:
                reasons.append("rss_auto_grab_not_disabled")
            if safe_defaults.get("listing_fallback") is not False:
                reasons.append("listing_fallback_not_disabled")
            # Mode/default safety checks occur after the initial readiness dict
            # is assembled, so recompute the boolean before reconciliation.
            readiness["ready"] = not reasons
            if isinstance(app_readiness, dict):
                reconciliation = {
                    "ready_matches": app_readiness.get("ready") == readiness["ready"],
                    "successful_cycles_delta": (app_readiness.get("successful_cycles") or 0)
                    - complete_cycles,
                    "relevant_misses_delta": (app_readiness.get("relevant_misses") or 0)
                    - relevant_misses,
                }
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as e:
            app_readiness = {"error": str(e)}

    summary = {
        "integrity_check": integrity,
        "db_user_version": user_version,
        "has_shadow_cycles_table": has_cycles,
        "readiness": readiness,
        "app_readiness": app_readiness,
        "reconciliation": reconciliation,
        "feed_detail": feed_detail,
        "safety": {
            "auto_action_rows": int(auto_action_rows or 0),
            "active_action_rows": int(active_action_rows or 0),
            "miss_count_mismatches": int(miss_count_mismatches or 0),
            "schema_version_expected": 6,
            "violations": [
                reason for reason in reasons
                if reason.startswith((
                    "unexpected_schema_version",
                    "shadow_miss_count_mismatch",
                    "automatic_action_activity",
                    "active_action_activity",
                    "unsafe_rss_mode",
                    "rss_auto_grab",
                    "listing_fallback",
                    "integrity_check",
                ))
            ],
        },
        "gate_passes": not reasons,
    }
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    report = {
        "collected_at": now.isoformat(),
        "db": str(db),
        "summary": summary,
        "recent_cycles": cycle_rows,
        "note": "Independent DB-derived readiness; when --base-url is given it is "
        "reconciled against the app's own GET /rss/status readiness.",
    }
    (ev / f"05_shadow_evidence_{stamp}.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n"
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0 if integrity == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
