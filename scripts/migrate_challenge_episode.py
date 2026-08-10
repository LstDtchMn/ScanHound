#!/usr/bin/env python3
"""Move already-parked rows onto challenge-episode semantics, atomically.

WHY A MIGRATION AT ALL. The rows parked before this change carry
``last_reason_code = 'reveal_verification_stalled'`` (the item that met the
challenge) and ``'source_temporarily_blocked'`` (its siblings). Nothing about
them says "a human challenge stopped this", so the new hold in
``queue_recovery_policy.decide()`` does not see them and they resume on their
old timers exactly as before.

WHY IT IS ONE TRANSACTION. Relabelling ``last_reason_code`` on its own is the
trap this script exists to avoid. The label is not what authorises a retry --
``queue_reason``, the batch's ``challenge_episode_id``, the item cooldown and
the auto-resume budget are. Rewriting one of those and not the others produces a
row that LOOKS held and is not, which is worse than one that plainly is not
held, because it stops anyone looking further. So every fact that decides
whether these rows may run moves together or not at all.

WHAT IT DELIBERATELY DOES NOT TOUCH: the cooldowns. It is tempting to null them
"for safety", and it would be a mistake -- a deferred row with no cooldown of
its own is NO_AUTHORISATION forever (see the round-14 note in the policy), so
clearing them would also block the legitimate release after a probe succeeds.
The episode is the hold; the cooldowns are what let the siblings restart
politely, with spacing, once it is answered.

DRY RUN BY DEFAULT. Pass --apply to write.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid

DEFAULT_DB = "/dbvol/crawler.db"
DEFERRED = ("verification_required", "waiting_source")

# The item that actually met the challenge, as opposed to the siblings parked
# behind it. transport_attempted is the discriminator the queue already uses:
# the trigger opened the page, the siblings never left the gate.
TRIGGER_CODES = ("reveal_verification_stalled", "interactive_challenge")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--source", default="hdencode")
    parser.add_argument("--apply", action="store_true",
                        help="write the change (default is a dry run)")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    columns = {r["name"] for r in conn.execute(
        "PRAGMA table_info(download_queue_batches)")}
    if "challenge_episode_id" not in columns:
        print("ERROR: download_queue_batches.challenge_episode_id is missing. "
              "Start the app once so the schema migration runs, then re-run.",
              file=sys.stderr)
        return 2

    placeholders = ",".join("?" for _ in DEFERRED)
    parked = conn.execute(
        f"""
        SELECT item_uuid, batch_uuid, state, queue_reason, cooldown_until,
               COALESCE(last_reason_code, '') AS last_reason_code,
               COALESCE(transport_attempted, 0) AS transport_attempted,
               title
        FROM download_queue_items
        WHERE source = ? AND state IN ({placeholders})
        ORDER BY batch_uuid, sequence_number
        """,
        (args.source, *DEFERRED),
    ).fetchall()

    if not parked:
        print(f"No parked {args.source} rows. Nothing to migrate.")
        return 0

    triggers = [r for r in parked
                if r["last_reason_code"] in TRIGGER_CODES
                and int(r["transport_attempted"] or 0) == 1]
    batches = sorted({r["batch_uuid"] for r in parked})

    print(f"parked rows          : {len(parked)}")
    print(f"batches to hold      : {len(batches)}")
    print(f"challenge triggers   : {len(triggers)}")
    for row in triggers:
        print(f"   trigger {row['item_uuid']}  {row['title'][:60]}")
    print(f"siblings held        : {len(parked) - len(triggers)}")

    if not triggers:
        # Refusing rather than inventing one. Opening an episode with no
        # triggering row would hold every sibling behind a challenge nobody can
        # point at, and the probe -- which targets the trigger -- would have no
        # target. A parked set with no trigger means something other than a
        # challenge parked it.
        print("\nNo row carries challenge-trigger evidence; refusing to open an "
              "episode. These rows were not parked by a challenge.",
              file=sys.stderr)
        return 1

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to write.")
        return 0

    episode_id = str(uuid.uuid4())
    try:
        with conn:                       # one transaction; rolls back on error
            conn.execute("BEGIN IMMEDIATE")
            for row in triggers:
                conn.execute(
                    """
                    UPDATE download_queue_items
                    SET state = 'verification_required',
                        queue_reason = 'interactive_challenge',
                        last_reason_code = 'interactive_challenge',
                        last_cause_code = 'turnstile_challenge_failed'
                    WHERE item_uuid = ?
                    """,
                    (row["item_uuid"],),
                )
            conn.executemany(
                "UPDATE download_queue_batches SET challenge_episode_id = ? "
                "WHERE batch_uuid = ?",
                [(episode_id, b) for b in batches],
            )
    except sqlite3.Error as exc:
        print(f"ERROR: migration rolled back: {exc}", file=sys.stderr)
        return 3

    held = conn.execute(
        "SELECT COUNT(*) AS n FROM download_queue_batches "
        "WHERE challenge_episode_id = ?", (episode_id,)
    ).fetchone()["n"]
    print(f"\nApplied. episode {episode_id}: {len(triggers)} trigger(s), "
          f"{held} batch(es) held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
