#!/usr/bin/env python3
"""Open a verification hold over an EXPLICITLY IDENTIFIED set of parked rows.

FOLD port of `agent/turnstile-classification`'s migrate_challenge_episode.py,
adapted to this branch's `verification_hold_source` column. The two branches'
inline schema migrations are automatic and keyed on a genuine
`verification_required` + `interactive_challenge` trigger row; on the current
live database that finds nothing, because the parked rows predate the classifier
(their `last_reason_code` is `reveal_verification_stalled`). This is the
auditable escape hatch for the OTHER case: an operator who has VERIFIED that a
specific historical row met a Turnstile challenge and wants to hold its source.

WHY THIS TAKES ITEM IDS INSTEAD OF FINDING THEM. `reveal_verification_stalled`
is exactly the code the runtime classifier emits when a reveal stalled and there
was NO active Turnstile evidence. Treating it as proof of Turnstile invents the
very evidence the classifier requires, and would assign a hold to every parked
batch for the source — an unrelated parked batch joining an incident it had
nothing to do with. Historical rows cannot be re-classified, because the
evidence that would decide it (the page, its console) is gone. So the operator
names the incident and this script does exactly what it is told, refusing
anything it cannot verify.

WHAT IT DELIBERATELY DOES NOT TOUCH: the cooldowns. Nulling them "for safety"
would make the siblings NO_AUTHORISATION forever (see the round-14 note in
queue_recovery_policy) and block the legitimate release after a probe succeeds.
The hold is what parks them; the cooldowns are what let them restart politely
afterwards. Release is still the ordinary path: an affirmative source reveal, or
the operator's `clear_verification_hold`.

DRY RUN BY DEFAULT. Pass --apply to write.

    migrate_challenge_episode.py --trigger <item_uuid> [--trigger <item_uuid>]
                                [--hold-batch <batch_uuid>] [--apply]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

DEFAULT_DB = "/dbvol/crawler.db"
DEFERRED = ("verification_required", "waiting_source")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--source", default="hdencode")
    parser.add_argument(
        "--trigger", action="append", default=[], metavar="ITEM_UUID",
        help="an item VERIFIED to have met the challenge; repeatable. "
             "Required -- this script does not guess which rows those are.")
    parser.add_argument(
        "--hold-batch", action="append", default=[], metavar="BATCH_UUID",
        help="an additional batch to hold behind the same source. Default is "
             "only the batches containing the named triggers.")
    parser.add_argument("--apply", action="store_true",
                        help="write the change (default is a dry run)")
    args = parser.parse_args(argv)

    if not args.trigger:
        print("ERROR: --trigger is required. Historical rows carry no evidence "
              "of which challenge they met, and inferring it from "
              "reveal_verification_stalled would fabricate exactly the evidence "
              "the classifier requires. Name the item(s) you have verified.",
              file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    columns = {r["name"] for r in conn.execute(
        "PRAGMA table_info(download_queue_batches)")}
    if "verification_hold_source" not in columns:
        print("ERROR: download_queue_batches.verification_hold_source is "
              "missing. Start the app once so the schema migration runs, then "
              "re-run.", file=sys.stderr)
        return 2

    placeholders = ",".join("?" for _ in DEFERRED)
    triggers = []
    for item_uuid in args.trigger:
        row = conn.execute(
            f"""
            SELECT item_uuid, batch_uuid, state, source, title
            FROM download_queue_items
            WHERE item_uuid = ? AND source = ? AND state IN ({placeholders})
            """,
            (item_uuid, args.source, *DEFERRED),
        ).fetchone()
        if row is None:
            # REFUSING rather than skipping. A typo'd or already-resumed id
            # would otherwise silently shrink the incident, and the run would
            # still report success.
            print(f"ERROR: {item_uuid} is not a parked {args.source} row. "
                  "Nothing was written.", file=sys.stderr)
            return 1
        triggers.append(row)

    batches = sorted({row["batch_uuid"] for row in triggers}
                     | set(args.hold_batch))
    # A --hold-batch must actually contain a deferred row for this source. Fold
    # review: stamping verification_hold_source='hdencode' on a DDLBase-only
    # batch would create a durable HDEncode hold with no HDEncode row behind it.
    # (This also subsumes the existence check — a missing batch has no rows.)
    for batch_uuid in args.hold_batch:
        if conn.execute(
            f"SELECT 1 FROM download_queue_items WHERE batch_uuid = ? "
            f"AND source = ? AND state IN ({placeholders}) LIMIT 1",
            (batch_uuid, args.source, *DEFERRED),
        ).fetchone() is None:
            print(f"ERROR: batch {batch_uuid} has no deferred {args.source} row; "
                  f"refusing to stamp a {args.source} hold on it. Nothing was "
                  "written.", file=sys.stderr)
            return 1

    held = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM download_queue_items
        WHERE source = ? AND state IN ({placeholders})
          AND batch_uuid IN ({",".join("?" for _ in batches)})
        """,
        (args.source, *DEFERRED, *batches),
    ).fetchone()["n"]

    print(f"source               : {args.source}")
    print(f"challenge triggers   : {len(triggers)}")
    for row in triggers:
        print(f"   trigger {row['item_uuid']}  {str(row['title'])[:60]}")
    print(f"batches to hold      : {len(batches)}")
    print(f"deferred rows held   : {held} "
          f"({held - len(triggers)} sibling(s))")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to write.")
        return 0

    try:
        with conn:                       # one transaction; rolls back on error
            conn.execute("BEGIN IMMEDIATE")
            for row in triggers:
                # RE-CHECK the predicate INSIDE the write, and require exactly one
                # row. Fold review: validation ran before BEGIN IMMEDIATE, so a
                # live queue could move a validated row (resume, cancel, claim)
                # before this write — and the bare `WHERE item_uuid = ?` would
                # then force an already-progressed row back to
                # verification_required. Repeating state+source and asserting the
                # rowcount makes the apply atomic with its own validation.
                updated = conn.execute(
                    f"""
                    UPDATE download_queue_items
                    SET state = 'verification_required',
                        queue_reason = 'interactive_challenge',
                        last_reason_code = 'interactive_challenge',
                        last_cause_code = 'operator_identified_challenge'
                    WHERE item_uuid = ? AND source = ?
                      AND state IN ({placeholders})
                    """,
                    (row["item_uuid"], args.source, *DEFERRED),
                ).rowcount
                if updated != 1:
                    raise sqlite3.Error(
                        f"trigger {row['item_uuid']} is no longer a parked "
                        f"{args.source} row (it moved before the write); nothing "
                        "applied")
            conn.executemany(
                "UPDATE download_queue_batches "
                "SET verification_hold_source = ? WHERE batch_uuid = ?",
                [(args.source, b) for b in batches],
            )
    except sqlite3.Error as exc:
        print(f"ERROR: migration rolled back: {exc}", file=sys.stderr)
        return 3

    # Report only what THIS invocation held, not every batch already carrying the
    # source hold. Fold review: the global count conflated pre-existing holds
    # with this run's effect.
    print(f"\nApplied. {len(triggers)} trigger(s); {args.source} held on "
          f"{len(batches)} batch(es) this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
