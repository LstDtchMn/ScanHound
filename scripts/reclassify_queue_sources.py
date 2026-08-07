"""Reclassify durable queue rows under the affirmative source classifier.

WHY THIS EXISTS. `download_queue._source()` used to default every URL that was not
DDLBase or Adit-HD to ``"hdencode"``, so a direct file-host row was stored as an
HDEncode row. Round 4 fixed new rows; the queue is durable, so pre-upgrade rows keep
the old classification and can still take part in HDEncode pause / resume / UI status
and the retry-budget refund. Peer review round 5 raised that as a HIGH.

WHY IT IS A SCRIPT AND NOT A STARTUP MIGRATION. Reclassifying changes
`download_queue_items.source`, which the ACTIVE UNIQUE INDEX
``(source, canonical_url, service_type)`` is built on. An old active
``("hdencode", RapidgatorURL, svc)`` and a reclassified
``("filehost", sameURL, svc)`` are distinct keys, so a naive UPDATE can create two
active logical jobs for one release. That needs a decision per collision, not a
silent unattended write on the next deploy.

MEASURED ON THE LIVE DATABASE, 2026-08-07: 280 rows, every one genuinely
``hdencode.org``, 0 reclassifications, 0 collisions. On that deployment this is a
no-op -- which is worth knowing, but is not a reason to skip the logic, because it is
the data that happens to be clean rather than the code.

    python scripts/reclassify_queue_sources.py --db PATH [--apply]

Dry run by default. Nothing is ever deleted.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, "/app")
try:
    from backend.source_identity import source_kind
except ImportError:  # running outside the container
    sys.path.insert(0, ".")
    from backend.source_identity import source_kind

#: The queue stores `filehost` where the shared classifier says `direct_file`,
#: because that value is already in the column and the unique index is built on it.
KIND_TO_SOURCE = {"hdencode": "hdencode", "ddlbase": "ddlbase",
                  "adithd": "adithd", "direct_file": "filehost",
                  "other": "other"}

#: States in which a row occupies the active unique key. A collision only matters
#: between two rows that are BOTH active.
ACTIVE_STATES = ("ready", "scheduled", "claimed", "waiting_source",
                 "verification_required")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/dbvol/crawler.db")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Omit for a dry run.")
    ap.add_argument("--hdencode-host", default=None,
                    help="override the configured HDEncode host. Defaults to the "
                         "app's own base_url so identity follows configuration.")
    args = ap.parse_args()

    host = args.hdencode_host
    if host is None:
        try:
            import json
            from backend.config import CONFIG_FILE
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                host = json.load(fh).get("base_url") or "https://hdencode.org"
        except Exception:
            host = "https://hdencode.org"
    print(f"HDEncode host in use: {host}")

    con = sqlite3.connect(f"file:{args.db}" + ("" if args.apply else "?mode=ro"),
                          uri=True)
    con.row_factory = sqlite3.Row

    rows = list(con.execute(
        "SELECT item_uuid, batch_uuid, source, canonical_url, service_type, state "
        "FROM download_queue_items"))
    print(f"queue rows: {len(rows)}")

    changes = []
    for row in rows:
        want = KIND_TO_SOURCE.get(source_kind(row["canonical_url"], host), "other")
        if want != row["source"]:
            changes.append((row, want))

    print(f"rows whose stored source is wrong: {len(changes)}")
    if changes:
        print("  by transition: " + ", ".join(
            f"{k}={v}" for k, v in Counter(
                f"{r['source']}->{w}" for r, w in changes).items()))

    # COLLISIONS. After reclassification, would two ACTIVE rows share the unique key?
    projected = Counter()
    for row in rows:
        if row["state"] not in ACTIVE_STATES:
            continue
        want = KIND_TO_SOURCE.get(source_kind(row["canonical_url"], host), "other")
        projected[(want, row["canonical_url"], row["service_type"])] += 1
    collisions = {k: n for k, n in projected.items() if n > 1}
    print(f"active unique-key collisions the change would create: {len(collisions)}")
    for key, n in list(collisions.items())[:10]:
        print(f"  {n}x  source={key[0]} service={key[2]}  {str(key[1])[-52:]}")

    if collisions:
        print("")
        print("REFUSING TO APPLY while collisions exist.")
        print("Each collision means two ACTIVE rows would claim one logical job.")
        print("Resolve deliberately -- cancel the older duplicate, or finish it --")
        print("then re-run. This script will not choose a survivor for you: that is")
        print("a decision about real downloads, not a data-cleanup detail.")
        return 2

    # Batch source must be recomputed: a batch is labelled by its items' sources.
    batch_ids = sorted({r["batch_uuid"] for r in rows if r["batch_uuid"]})
    print(f"batches whose source label will be recomputed: {len(batch_ids)}")

    if not args.apply:
        print("\nDRY RUN. Nothing written. Re-run with --apply.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    with con:
        for row, want in changes:
            updated += con.execute(
                "UPDATE download_queue_items SET source = ?, updated_at = ? "
                "WHERE item_uuid = ? AND source = ?",
                (want, now, row["item_uuid"], row["source"])).rowcount
        for batch_uuid in batch_ids:
            kinds = {r["source"] for r in con.execute(
                "SELECT source FROM download_queue_items WHERE batch_uuid = ?",
                (batch_uuid,))}
            label = kinds.pop() if len(kinds) == 1 else "mixed"
            con.execute(
                "UPDATE download_queue_batches SET source = ?, updated_at = ? "
                "WHERE batch_uuid = ?", (label, now, batch_uuid))
    print(f"\nreclassified {updated} row(s); recomputed {len(batch_ids)} batch label(s).")

    # VERIFY by re-deriving from the URLs, not by re-reading what was just written.
    wrong = [r["item_uuid"] for r in con.execute(
        "SELECT item_uuid, source, canonical_url FROM download_queue_items")
        if KIND_TO_SOURCE.get(source_kind(r["canonical_url"], host), "other")
        != r["source"]]
    print(f"verification: {len(wrong)} row(s) still disagree with the classifier")
    if wrong:
        print("  " + ", ".join(wrong[:5]))
        return 1
    print("Every stored source now matches what the classifier derives from its URL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
