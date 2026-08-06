#!/usr/bin/env python3
"""Take a DIAGNOSTIC-ONLY database snapshot before a deploy (contract R-10).

WHAT THIS IS FOR, and the distinction the contract insists on:

A snapshot taken here is a *diagnostic aid*. If a deploy goes wrong you can
open it and see what the database looked like beforehand. That is all.

It is explicitly NOT ADMISSIBLE as evidence when grading a promotion window
or any other measured outcome, and every artifact it writes says so in its
own filename and sidecar. The reason is not bureaucratic: this file is a
point-in-time copy taken by an operator at a moment of their choosing, with
no attestation of what code produced it, no proof it was not edited
afterwards, and no link to the window's pinned digest. Grading anything
against it would be measuring a convenient artifact rather than the system.
Restoring from it is likewise an operator decision with consequences, not a
step this script will take for you -- it only ever READS.

WAL SAFETY, which is the whole reason this is not a `cp`:

The live database runs in WAL mode, so committed data lives in
crawler.db-wal until a checkpoint folds it back. `cp crawler.db` copies only
the main file and reports success.

MEASURED, not assumed. Comparing a raw copy against the backup API on the
live database, 3 of 34 tables differed:

    dismissed_items          1041  vs  1057   (16 rows absent)
    download_queue_batches     21  vs    22   ( 1 row  absent)
    download_queue_items      234  vs   261   (27 rows absent)

So a raw copy silently loses 27 queued downloads and 16 dismissals -- real
committed rows, with no error and a plausible-looking file. Note the first
comparison I ran used hdencode_candidates, where both agreed (3284): a
single-table check would have "confirmed" the raw copy was fine.

The copy therefore goes through sqlite3's own backup API against a READ-ONLY
connection, which walks a consistent view including the WAL.

    python scripts/rollback_snapshot.py take --note "before R-11 deploy"
    python scripts/rollback_snapshot.py verify <snapshot.db>

Exit: 0 ok | 1 the snapshot is unusable | 2 usage/environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

CONTAINER = "scanhound"
DB_IN_CONTAINER = "/dbvol/crawler.db"
#: Deliberately OUTSIDE the repo and off the X: mirror (which has a failing
#: disk). NOT_ADMISSIBLE is in the directory name so the artifact cannot be
#: quoted as evidence without the reader seeing what it is.
DEST_DIR = r"C:\DockerData\infra-ops\rollback-snapshots-NOT_ADMISSIBLE"

BANNER = (
    "DIAGNOSTIC ONLY - NOT ADMISSIBLE AS EVIDENCE. A point-in-time operator "
    "copy with no attestation of the code that produced it and no binding to "
    "any pinned build. Use it to LOOK at prior state. Do not grade a "
    "promotion window, or any measured outcome, against it."
)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(args: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(args, capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def cmd_take(args) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"crawler-{stamp}.NOT_ADMISSIBLE.db"
    tmp_in_container = f"/tmp/{name}"

    # The backup API against a read-only connection: a consistent view that
    # INCLUDES the WAL. Doing this inside the container avoids copying a live
    # file across the bind-mount boundary mid-write.
    code, out, err = _run([
        "docker", "exec", CONTAINER, "python", "-c",
        "import sqlite3,sys;"
        f"src=sqlite3.connect('file:{DB_IN_CONTAINER}?mode=ro',uri=True);"
        f"dst=sqlite3.connect('{tmp_in_container}');"
        "src.backup(dst);dst.close();src.close();print('ok')",
    ])
    if code != 0 or "ok" not in out:
        print("Snapshot FAILED inside the container:", err or out, file=sys.stderr)
        return 1

    os.makedirs(DEST_DIR, exist_ok=True)
    dest = os.path.join(DEST_DIR, name)
    code, _out, err = _run(["docker", "cp", f"{CONTAINER}:{tmp_in_container}", dest])
    _run(["docker", "exec", CONTAINER, "rm", "-f", tmp_in_container])
    if code != 0 or not os.path.exists(dest):
        print("Copy out of the container FAILED:", err, file=sys.stderr)
        return 1

    digest = _sha256(dest)
    size = os.path.getsize(dest)

    # Integrity check the COPY, not the source: a snapshot that cannot be
    # opened is worse than none, because it is discovered only when needed.
    ok, detail = _integrity(dest)

    sidecar = dest + ".json"
    with open(sidecar, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({
            "ADMISSIBILITY": BANNER,
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "note": args.note,
            "source": f"{CONTAINER}:{DB_IN_CONTAINER}",
            "method": "sqlite3 backup API over a read-only connection "
                      "(includes WAL; a raw file copy would not)",
            "file": os.path.basename(dest),
            "sha256": digest,
            "bytes": size,
            "integrity_check": detail,
            "running_image": _running_image(),
        }, fh, indent=2)

    print(BANNER)
    print()
    print(f"  file      : {dest}")
    print(f"  bytes     : {size:,}")
    print(f"  sha256    : {digest}")
    print(f"  integrity : {detail}")
    print(f"  sidecar   : {os.path.basename(sidecar)}")
    if not ok:
        print("\nSNAPSHOT UNUSABLE - integrity check did not pass.")
        return 1
    return 0


def _integrity(path: str) -> tuple[bool, str]:
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()
        conn.close()
        verdict = (row or ["?"])[0]
        return verdict == "ok", f"{verdict} ({(tables or [0])[0]} tables)"
    except Exception as e:  # noqa: BLE001 - the reason matters more than the type
        return False, f"unreadable: {e}"


def _running_image() -> str | None:
    code, out, _ = _run(["docker", "inspect", CONTAINER, "--format", "{{.Image}}"])
    return out if code == 0 and out else None


def cmd_verify(args) -> int:
    path = args.path
    if not os.path.exists(path):
        print(f"No such snapshot: {path}", file=sys.stderr)
        return 2
    ok, detail = _integrity(path)
    digest = _sha256(path)
    print(BANNER)
    print()
    print(f"  file      : {path}")
    print(f"  sha256    : {digest}")
    print(f"  integrity : {detail}")
    sidecar = path + ".json"
    if os.path.exists(sidecar):
        with open(sidecar, encoding="utf-8") as fh:
            meta = json.load(fh)
        match = meta.get("sha256") == digest
        print(f"  recorded  : {meta.get('sha256')}")
        print(f"  matches   : {match}")
        if not match:
            print("\nTHE FILE HAS CHANGED since it was taken. It is not the "
                  "snapshot the sidecar describes.")
            return 1
    else:
        print("  sidecar   : MISSING - provenance unknown")
        return 1
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("take", help="take a diagnostic snapshot")
    t.add_argument("--note", default="", help="why this snapshot was taken")
    v = sub.add_parser("verify", help="re-check a snapshot's integrity + digest")
    v.add_argument("path")
    args = ap.parse_args(argv)
    return {"take": cmd_take, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
