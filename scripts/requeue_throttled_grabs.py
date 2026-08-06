"""Requeue grabs that were marked terminal by the throttle misclassification.

RUN THIS ONLY AFTER DEPLOYING THE FIX. Before the fix, a stalled link-reveal was
recorded as LAYOUT_CHANGED with retryable=False, so requeueing against the old
code would simply burn the items again on the next throttle.

WHY THESE ITEMS ARE RECOVERABLE. HDEncode gates each reveal behind a client-side
countdown. Once it stops clearing, every remaining queue item meets the same
closed door. The releases were never missing; the source was rate-limiting. All
78 affected items carry automated_retry_count 0, because retryable=False meant no
automatic retry could ever fire.

WHY A BLANKET REQUEUE IS SAFE RATHER THAN RECKLESS. The reveal tier was not
persisted before this change, so it is NOT possible to prove retrospectively which
of the historical rows were throttle stalls and which were genuine layout
failures. That is fine, because the deployed fix now sorts them correctly on their
own merits:

  * a throttle stall  -> REVEAL_VERIFICATION_STALLED, cooldown, stays retryable
  * a real layout change -> LAYOUT_CHANGED, terminal, exactly as before

So a genuine layout failure that gets requeued simply fails terminal again, once,
and is classified correctly this time. Nothing is lost by trying.

WHAT IT DOES NOT TOUCH. Completed items, cancelled items, and anything currently
claimed. It only revives failed rows whose reason code is one the throttle can
produce.

    python scripts/requeue_throttled_grabs.py --db PATH [--apply]

Dry run by default: prints exactly what it would change and exits.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

# Reason codes a throttle can produce. layout_changed is the misclassification
# itself; no_file_host_links is the transition stage, where the control was
# clicked but the reveal returned nothing (observed 2026-08-06 13:50);
# browser_navigation_failed and operation_timeout_unknown are transient by
# nature. Deliberately NOT included: requested_host_missing (the page genuinely
# lacks the configured host) and source_disabled (an operator decision).
RECOVERABLE = (
    "layout_changed",
    "no_file_host_links",
    "browser_navigation_failed",
    "operation_timeout_unknown",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/dbvol/crawler.db")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Omit for a dry run.")
    args = ap.parse_args()

    mode = "" if args.apply else "?mode=ro"
    con = sqlite3.connect(f"file:{args.db}{mode}", uri=True)
    con.row_factory = sqlite3.Row

    placeholders = ",".join("?" for _ in RECOVERABLE)
    rows = list(con.execute(
        f"SELECT item_uuid, title, last_reason_code, last_attempt_at, "
        f"       attempt_count, automated_retry_count "
        f"FROM download_queue_items "
        f"WHERE state = 'failed' AND last_reason_code IN ({placeholders}) "
        f"ORDER BY last_attempt_at", RECOVERABLE))

    print(f"candidates: {len(rows)}")
    print("by reason code:")
    for reason, count in Counter(r["last_reason_code"] for r in rows).most_common():
        print(f"  {reason:28s} {count}")
    print("by day last attempted:")
    for day, count in sorted(Counter(
            str(r["last_attempt_at"])[:10] for r in rows).items()):
        print(f"  {day}: {count}")

    never_auto_retried = sum(1 for r in rows
                             if (r["automated_retry_count"] or 0) == 0)
    print(f"\nnever auto-retried: {never_auto_retried} of {len(rows)}"
          "   <- retryable=False is why")

    # Items left untouched, stated explicitly so the scope is auditable rather
    # than implied.
    skipped = list(con.execute(
        f"SELECT last_reason_code, COUNT(*) n FROM download_queue_items "
        f"WHERE state = 'failed' AND last_reason_code NOT IN ({placeholders}) "
        f"GROUP BY 1", RECOVERABLE))
    if skipped:
        print("\nleft alone (not throttle-producible):")
        for row in skipped:
            print(f"  {str(row['last_reason_code']):28s} {row['n']}")

    if not args.apply:
        print("\nDRY RUN. Nothing written. Re-run with --apply to requeue.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    with con:
        changed = con.execute(
            f"UPDATE download_queue_items "
            f"SET state = 'scheduled', "
            f"    scheduled_for = ?, "
            f"    cooldown_until = NULL, "
            f"    claimed_by = NULL, "
            f"    claim_expires_at = NULL, "
            f"    last_message = 'Requeued: the previous failure was a source "
            f"rate-limit recorded as a permanent error.', "
            f"    updated_at = ? "
            f"WHERE state = 'failed' AND last_reason_code IN ({placeholders})",
            (now, now, *RECOVERABLE)).rowcount
    print(f"\nrequeued {changed} item(s) to state='scheduled'.")
    print("The queue will work through them at its configured spacing. If the "
          "source throttles again, the batch now pauses and auto-resumes after "
          "the cooldown instead of burning the rest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
