#!/usr/bin/env python3
"""Prove what is ACTUALLY running, before and after a deploy.

Contract rows R-9 and R-11 require a recorded image digest and an equality
check between the deployed digest and the running container's. Neither had
ever been scripted, so every deploy so far ended with "it restarted, looks
fine" -- and this session found the concrete cost of that: main carried the
category-switch fix for hours while the running container, built days
earlier, still had the bug the fix was for. Nobody could tell by looking.

`docker compose up -d --build` reuses the tag `scanhound:latest`, so the TAG
proves nothing. Only the image ID and the code inside the container do.

Usage
-----
    python scripts/verify-deploy.py before       # snapshot, then deploy
    python scripts/verify-deploy.py after        # compare + verify markers

    # assert a specific commit's code is really running:
    python scripts/verify-deploy.py after --expect-marker CATEGORY_NONE_SENTINEL

Exit codes: 0 verified | 1 MISMATCH (the deploy did not take) | 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

CONTAINER = "scanhound"
IMAGE = "scanhound:latest"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     ".deploy-state.json")


def _run(args: list[str]) -> tuple[int, str]:
    """Run a command, returning (exit_code, stripped_stdout).

    stderr is NOT merged into stdout: docker writes progress there and PS 5.1
    reports a nonzero exit for it, which has produced false failures in this
    repo before.
    """
    p = subprocess.run(args, capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip()


def _image_id() -> str | None:
    code, out = _run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"])
    return out if code == 0 and out else None


def _running() -> dict:
    code, out = _run(["docker", "inspect", CONTAINER, "--format",
                      "{{.Image}}|{{.State.Status}}|{{.State.StartedAt}}"])
    if code != 0 or not out:
        return {}
    image_id, status, started = (out.split("|") + ["", "", ""])[:3]
    return {"image_id": image_id, "status": status, "started_at": started}


def _git_head() -> str:
    code, out = _run(["git", "rev-parse", "HEAD"])
    return out if code == 0 else "unknown"


def _marker_in_container(marker: str) -> int:
    """How many times `marker` appears in the RUNNING container's backend.

    This is the check that actually matters. An image can be rebuilt and the
    container restarted while still not containing the code you think you
    deployed -- a stale build context, a cached layer, the wrong branch
    checked out. Reading the string out of the live container is the only
    answer that cannot be inferred wrongly.
    """
    code, out = _run(["docker", "exec", CONTAINER, "sh", "-c",
                      f"grep -rl '{marker}' /app/backend/ 2>/dev/null | wc -l"])
    if code != 0:
        return -1
    try:
        return int(out.splitlines()[-1].strip())
    except (ValueError, IndexError):
        return -1


def _snapshot(label: str) -> dict:
    return {
        "label": label,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_head(),
        "image_id": _image_id(),
        "container": _running(),
    }


def cmd_before(_args) -> int:
    snap = _snapshot("before")
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2)
    print("PRE-DEPLOY STATE")
    print(f"  git HEAD        : {snap['git_head']}")
    print(f"  image id        : {snap['image_id'] or 'MISSING'}")
    c = snap["container"]
    print(f"  container image : {c.get('image_id') or 'NOT RUNNING'}")
    print(f"  container status: {c.get('status') or '-'}")
    if snap["image_id"] and c.get("image_id") and snap["image_id"] != c["image_id"]:
        print("  NOTE: the running container is ALREADY not on the current "
              "image -- it was started from an older build.")
    print(f"\nSaved to {STATE}. Now deploy, then run: verify-deploy.py after")
    return 0


def cmd_after(args) -> int:
    if not os.path.exists(STATE):
        print("No pre-deploy snapshot. Run 'before' first.", file=sys.stderr)
        return 2
    with open(STATE, encoding="utf-8") as fh:
        before = json.load(fh)
    after = _snapshot("after")

    print("POST-DEPLOY VERIFICATION")
    print(f"  git HEAD        : {before['git_head']}  ->  {after['git_head']}")
    print(f"  image id        : {before['image_id']}  ->  {after['image_id']}")
    bc, ac = before["container"], after["container"]
    print(f"  container image : {bc.get('image_id')}  ->  {ac.get('image_id')}")
    print(f"  container status: {ac.get('status')}  (started {ac.get('started_at')})")

    failures: list[str] = []

    if not ac.get("image_id"):
        failures.append("container is not running")
    elif after["image_id"] and ac["image_id"] != after["image_id"]:
        failures.append(
            "RUNNING CONTAINER IS NOT ON THE CURRENT IMAGE -- it was rebuilt "
            "but the container was never recreated from it")
    if ac.get("status") and ac["status"] != "running":
        failures.append(f"container status is {ac['status']}, not running")
    if bc.get("image_id") and ac.get("image_id") == bc.get("image_id"):
        failures.append(
            "the container is on the SAME image as before -- nothing was "
            "actually deployed (a no-op rebuild leaves the tag unchanged)")

    for marker in (args.expect_marker or []):
        n = _marker_in_container(marker)
        if n > 0:
            print(f"  marker {marker!r}: present in {n} file(s) -- code verified")
        elif n == 0:
            failures.append(
                f"marker {marker!r} is NOT in the running container: the code "
                f"you expected to deploy is not what is running")
        else:
            failures.append(f"could not check marker {marker!r} in the container")

    print()
    if failures:
        print("RESULT: NOT VERIFIED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: VERIFIED -- the running container is on the current image, "
          "and every expected marker is present in it.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("before", help="snapshot state prior to deploying")
    a = sub.add_parser("after", help="compare against the snapshot and verify")
    a.add_argument("--expect-marker", action="append", metavar="STRING",
                   help="a string that MUST appear in the running container's "
                        "backend (repeatable). Use a symbol unique to the "
                        "commit you are deploying.")
    args = ap.parse_args(argv)
    return {"before": cmd_before, "after": cmd_after}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
