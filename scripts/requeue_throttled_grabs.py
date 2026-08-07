"""Requeue grabs that a throttle misclassification marked permanently failed.

RUN THIS ONLY AFTER DEPLOYING THE FIX. Before the fix, a stalled link-reveal was
recorded as LAYOUT_CHANGED with retryable=False, so requeueing against the old
code would simply burn the items again on the next throttle.

WHAT WENT WRONG. HDEncode gates each reveal behind a client-side countdown. Once
it stops clearing, every remaining queue item meets the same closed door. The
releases were never missing; the source was rate-limiting. The affected items
carry automated_retry_count 0, because retryable=False meant no automatic retry
could ever fire.

WHY THIS SCRIPT IS DELIBERATELY NARROW  (rewritten 2026-08-06 after peer review)

The first version swept every failed row carrying any of five reason codes, with
no constraint on source, time window, or batch, and justified that with: "a
genuine layout failure that gets requeued simply fails terminal again, once, and
is classified correctly this time, so nothing is lost by trying."

The review rejected that reasoning, correctly. It is risk management, not
evidence. It is not an argument that any particular row was throttled, and it
quietly re-runs ordinary no-link and layout outcomes that the new code
deliberately keeps terminal. Two concrete defects followed from it:

  1. operation_timeout_unknown was in the candidate set, but download_queue's
     claim query EXCLUDES that reason (with interrupted_unknown_outcome) so an
     unknown execution state is never retried blind -- a retry could
     double-submit a delivery that already happened. The script set those rows
     to 'scheduled' and reported them as requeued while the queue silently
     refused to ever claim them. It lied about its own effect.

  2. The success message claimed the batch would "auto-resume after the
     cooldown". Batch auto-resume is OPTIONAL and ONE-SHOT. On 2026-08-06 every
     affected batch had it disabled, so 69 recovered grabs sat inert until the
     flag was set by hand.

So this version requires you to say WHICH rows, by source plus an incident
window, a batch, or explicit ids. It still cannot prove retrospectively which
historical rows were throttle stalls -- the reveal tier was not persisted before
the fix -- and it does not pretend to. It makes you supply the bound instead.

WHAT IT NEVER TOUCHES. Completed, cancelled, and currently-claimed items; the
reason codes listed in NEVER_HERE; and, unless you override --source, anything
that is not an HDEncode row.

    python scripts/requeue_throttled_grabs.py --db PATH \\
        --since 2026-08-06T18:00:00+00:00 --until 2026-08-07T04:00:00+00:00 \\
        [--limit 5] [--apply]

Dry run by default: prints exactly what it would change, then exits.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from datetime import datetime, timezone

# Reason codes a reveal throttle can actually produce.
#
#   layout_changed           the misclassification itself: the reveal control
#                            never left its verifying state and the page shape
#                            read as an unfamiliar layout
#   no_file_host_links       the transition stage, where the control was clicked
#                            but the reveal returned nothing (observed
#                            2026-08-06 13:50)
#   browser_navigation_failed  transient by nature
RECOVERABLE = (
    "layout_changed",
    "no_file_host_links",
    "browser_navigation_failed",
)

# Excluded on purpose, each with its reason, so the scope is auditable rather
# than implied. Printed on every run.
NEVER_HERE = {
    "operation_timeout_unknown":
        "unknown execution state -- the queue's claim query excludes it, so "
        "requeueing it does nothing except misreport. Needs separate "
        "adjudication proving no delivery occurred.",
    "interrupted_unknown_outcome":
        "same class, same exclusion from claiming.",
    "requested_host_missing":
        "the page genuinely lacks the configured file host.",
    "source_disabled":
        "an operator decision, not a failure.",
}


def _build_filter(args, explicit):
    """The WHERE clause, plus a human-readable description of every bound."""
    where = ["state = 'failed'"]
    params: list = []
    described = []
    if explicit:
        where.append("item_uuid IN (" + ",".join("?" for _ in explicit) + ")")
        params.extend(explicit)
        described.append(f"explicit ids: {len(explicit)} "
                         "(every other filter ignored)")
        return " AND ".join(where), params, described

    where.append("last_reason_code IN ("
                 + ",".join("?" for _ in RECOVERABLE) + ")")
    params.extend(RECOVERABLE)
    described.append("reason codes: " + ", ".join(RECOVERABLE))
    if args.source:
        where.append("source = ?")
        params.append(args.source)
        described.append(f"source: {args.source}")
    else:
        described.append("source: ANY (--source '' was passed)")
    if args.since:
        where.append("last_attempt_at >= ?")
        params.append(args.since)
        described.append(f"since: {args.since}")
    if args.until:
        where.append("last_attempt_at <= ?")
        params.append(args.until)
        described.append(f"until: {args.until}")
    if args.batch:
        where.append("batch_uuid = ?")
        params.append(args.batch)
        described.append(f"batch: {args.batch}")
    return " AND ".join(where), params, described


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/dbvol/crawler.db")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Omit for a dry run.")
    ap.add_argument("--source", default="hdencode",
                    help="restrict to one source (default hdencode). A reveal "
                         "throttle is source-specific, so sweeping across "
                         "sources is never right. Pass '' to disable.")
    ap.add_argument("--since", default=None,
                    help="only rows whose last attempt is at or after this ISO "
                         "timestamp. Use the incident's start.")
    ap.add_argument("--until", default=None,
                    help="only rows whose last attempt is at or before this "
                         "ISO timestamp. Use the incident's end.")
    ap.add_argument("--batch", default=None, help="restrict to one batch_uuid.")
    ap.add_argument("--ids", default=None,
                    help="comma-separated item_uuids. The most precise option: "
                         "recovers exactly the rows you have evidence for and "
                         "ignores every other filter.")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap how many RELEASES are revived, oldest attempt "
                         "first. Use a small value (3-5) for a canary run "
                         "before recovering everything.")
    ap.add_argument("--allow-unbounded", action="store_true",
                    help="permit a run with no --since/--until/--batch/--ids. "
                         "Refused by default, because an unbounded sweep is the "
                         "exact behaviour the peer review rejected.")
    args = ap.parse_args()

    explicit = [x.strip() for x in (args.ids or "").split(",") if x.strip()]
    bounded = bool(explicit or args.since or args.until or args.batch)
    if not bounded and not args.allow_unbounded:
        print("REFUSING TO RUN UNBOUNDED.")
        print("")
        print("With no --since/--until, no --batch and no --ids, this selects "
              "EVERY failed row")
        print("for the source regardless of when it failed or why -- which is "
              "the overbroad")
        print("sweep the 2026-08-06 peer review rejected. Supply the incident "
              "window, a")
        print("batch, or explicit ids. Pass --allow-unbounded if you really "
              "mean all of them.")
        return 2

    mode = "" if args.apply else "?mode=ro"
    con = sqlite3.connect(f"file:{args.db}{mode}", uri=True)
    con.row_factory = sqlite3.Row

    clause, params, described = _build_filter(args, explicit)
    rows = list(con.execute(
        f"SELECT item_uuid, title, last_reason_code, last_attempt_at, "
        f"       attempt_count, automated_retry_count, source, "
        f"       canonical_url, service_type, batch_uuid "
        f"FROM download_queue_items "
        f"WHERE {clause} "
        f"ORDER BY last_attempt_at", params))

    print("filters in effect:")
    for line in described:
        print(f"  {line}")
    print(f"  limit: {args.limit if args.limit else '(none)'}")
    if not bounded:
        print("  *** UNBOUNDED RUN, explicitly allowed ***")

    # ONE SURVIVOR PER RELEASE. download_queue_items has a UNIQUE constraint on
    # (source, canonical_url, service_type) across live states, and the failed
    # set contains DUPLICATE rows for the same release -- two terminal attempts
    # on the same URL. A blanket UPDATE flips both to 'scheduled', creating two
    # live rows with the same key, and the whole transaction aborts with
    # IntegrityError. That is exactly what the first run did: it wrote nothing.
    #
    # Measured on the live data: 79 recoverable rows, 0 colliding with live
    # work, 7 duplicate keys within the failed set. So each release revives its
    # most recent attempt and CANCELS the older duplicates -- cancelled rather
    # than deleted, so the record of the attempt stays auditable.
    groups: dict = {}
    for row in rows:
        groups.setdefault(
            (row["source"], row["canonical_url"], row["service_type"]), []
        ).append(row)
    for members in groups.values():
        members.sort(key=lambda r: str(r["last_attempt_at"] or ""), reverse=True)

    # The limit is applied HERE, before anything is reported, so the dry run and
    # the --apply run describe the same set. An earlier version applied it after
    # the dry-run branch, meaning a canary's dry run printed a count the real
    # run would not honour -- the same class of misreporting as defect 1 above.
    ordered = sorted(groups.items(),
                     key=lambda kv: str(kv[1][0]["last_attempt_at"] or ""))
    dropped = 0
    if args.limit and args.limit < len(ordered):
        dropped = len(ordered) - args.limit
        ordered = ordered[:args.limit]

    survivors, superseded = [], []
    for _key, members in ordered:
        survivors.append(members[0]["item_uuid"])
        superseded.extend(m["item_uuid"] for m in members[1:])

    print(f"\nmatched rows: {len(rows)}   releases: {len(groups)}")
    print(f"selected releases: {len(ordered)}"
          + (f"   ({dropped} held back by --limit)" if dropped else ""))
    print(f"would revive: {len(survivors)}   "
          f"would cancel as duplicate: {len(superseded)}")

    print("\nby reason code (matched rows):")
    for reason, count in Counter(
            r["last_reason_code"] for r in rows).most_common():
        print(f"  {reason:28s} {count}")
    print("by day last attempted:")
    for day, count in sorted(Counter(
            str(r["last_attempt_at"])[:10] for r in rows).items()):
        print(f"  {day}: {count}")
    if rows:
        never_auto_retried = sum(1 for r in rows
                                 if (r["automated_retry_count"] or 0) == 0)
        print(f"\nnever auto-retried: {never_auto_retried} of {len(rows)}"
              "   <- retryable=False is why")

    # What is excluded and why. Counts come from the same population minus the
    # reason filter, so this reports rows this script will not act on even
    # though they are otherwise in scope.
    excl_where = ["state = 'failed'"]
    excl_params: list = []
    if args.source and not explicit:
        excl_where.append("source = ?")
        excl_params.append(args.source)
    if args.since and not explicit:
        excl_where.append("last_attempt_at >= ?")
        excl_params.append(args.since)
    if args.until and not explicit:
        excl_where.append("last_attempt_at <= ?")
        excl_params.append(args.until)
    excluded = dict(con.execute(
        f"SELECT last_reason_code, COUNT(*) FROM download_queue_items "
        f"WHERE {' AND '.join(excl_where)} "
        f"  AND last_reason_code NOT IN ("
        + ",".join("?" for _ in RECOVERABLE) + ") GROUP BY 1",
        excl_params + list(RECOVERABLE)))
    print("\nleft alone, in the same window, and why:")
    for reason, why in NEVER_HERE.items():
        print(f"  {reason:28s} {excluded.pop(reason, 0):4d}  {why}")
    for reason, count in sorted(excluded.items()):
        print(f"  {str(reason):28s} {count:4d}  not a throttle-producible code")

    # WILL THESE ACTUALLY RETRY UNATTENDED? Reported here, in the dry run too,
    # because the previous version simply ASSERTED that they would. Batch
    # auto-resume is optional and one-shot, and on 2026-08-06 every affected
    # batch had it disabled -- so 69 recovered grabs sat inert until the flag was
    # set by hand. Checking beats claiming.
    batches = sorted({str(r["batch_uuid"]) for _k, members in ordered
                      for r in members if r["batch_uuid"]})
    print("\nauto-resume state of the batches this run touches:")
    if not batches:
        print("  (none of the selected rows belong to a batch)")
    inert = []
    for b in batches:
        row = con.execute(
            "SELECT state, auto_resume_after_cooldown, auto_resume_used "
            "FROM download_queue_batches WHERE batch_uuid = ?", (b,)).fetchone()
        if row is None:
            print(f"  {b}  *** no such batch row ***")
            continue
        enabled = bool(row["auto_resume_after_cooldown"])
        used = bool(row["auto_resume_used"])
        verdict = ("will resume itself once" if enabled and not used
                   else "ENABLED BUT ALREADY SPENT (one-shot)" if enabled
                   else "WILL NOT RESUME -- auto_resume_after_cooldown = 0")
        if not (enabled and not used):
            inert.append(b)
        print(f"  {b}  state={row['state']:16s} {verdict}")
    if inert:
        print(f"\n  {len(inert)} batch(es) above will NOT retry on their own.")
        print("  Requeued items in them stay parked until the flag is set or a")
        print("  resume is triggered by hand. Decide that deliberately.")

    if not args.apply:
        print("\nDRY RUN. Nothing written. Re-run with --apply to requeue.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    changed = cancelled = 0
    with con:
        for uuid in survivors:
            changed += con.execute(
                "UPDATE download_queue_items "
                "SET state='scheduled', scheduled_for=?, cooldown_until=NULL, "
                "    claimed_by=NULL, claim_expires_at=NULL, "
                "    last_message='Requeued: the previous failure was a source "
                "rate-limit recorded as a permanent error.', updated_at=? "
                "WHERE item_uuid=? AND state='failed'",
                (now, now, uuid)).rowcount
        for uuid in superseded:
            cancelled += con.execute(
                "UPDATE download_queue_items "
                "SET state='cancelled', cancelled_at=?, "
                "    last_message='Superseded: a newer attempt on the same "
                "release was requeued.', updated_at=? "
                "WHERE item_uuid=? AND state='failed'",
                (now, now, uuid)).rowcount
    print(f"\nrequeued {changed} item(s); cancelled {cancelled} duplicate(s).")
    print("The queue will work through them at its configured spacing.")

    if inert:
        print("")
        print(f"REMINDER: {len(inert)} batch(es) listed above will NOT resume on")
        print("their own. The requeued items in them are scheduled but parked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
