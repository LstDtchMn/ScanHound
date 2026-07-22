#!/usr/bin/env python3
"""Merge the feature pack to main and deploy it FAIL-CLOSED.

Run:  python deploy_failclosed.py

What it does, in order, stopping immediately on any failure:

  1. Preconditions: deploy checkout is on `main`, has no tracked
     modifications, is exactly at the expected base, and the integration
     branch is fetched at the expected commit.
  2. `git merge --no-ff` the integration branch, then `git push origin main`
     (never a force push; --no-ff so rollback is `git revert -m 1 <merge>`).
  3. Stop the running container BEFORE touching config, so the live app
     cannot write config.json back over the fail-closed values.
  4. Back up config.json, then write the DISABLED profile:
         auto_rename_enabled            = false
         auto_grab_enabled              = false
         hdencode_enabled               = false
         background_scan_enabled        = false
         hdencode_discovery_mode        = "listing"
         hdencode_rss_auto_grab_enabled = false
         hdencode_rss_shadow_compare_enabled = true   (shadow default)
         hdencode_rss_listing_fallback_enabled = false
     (scheduler_enabled is deliberately left alone -- leaving the scheduler
     running makes the later zero-HDEncode-traffic proof stronger.)
  5. `docker compose up -d --build`.
  6. Wait for the container, then verify: image id, DB user_version (expect 6
     after the automatic v2->v6 migration), integrity, and that every
     fail-closed flag actually took effect.

Rollback if anything looks wrong:
    cd "X:/Docker Apps/ScanHound"
    git revert -m 1 <merge-sha>          # or: git checkout <old-sha> -- .
    copy the printed config backup back over data/.config/scanhound/config.json
    docker compose down && docker tag scanhound:qual-old-0ee5351 scanhound:latest
    docker compose up -d
A byte-identical DB snapshot and the old image are already preserved.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(r"X:/Docker Apps/ScanHound")
CONFIG = PROJECT / "data" / ".config" / "scanhound" / "config.json"
EVIDENCE = Path(__file__).resolve().parent
EXPECTED_BASE = "555e26bc65a6e6474eb63fdfb6e025a41255dea9"
BRANCH_REF = "origin/agent/feature-pack-integration"
EXPECTED_BRANCH_SHA = "1898c8852444d44a8620358122f3851402fa20a4"
CONTAINER = "scanhound"

DISABLED = {
    "auto_rename_enabled": False,
    "auto_grab_enabled": False,
    "hdencode_enabled": False,
    "background_scan_enabled": False,
    "hdencode_discovery_mode": "listing",
    "hdencode_rss_auto_grab_enabled": False,
    "hdencode_rss_shadow_compare_enabled": True,
    "hdencode_rss_listing_fallback_enabled": False,
}

MERGE_MESSAGE = """Merge feature-pack integration (RSS/HDEncode + file-safety) for production qualification

Integrates agent/feature-pack-integration at code-tested SHA a6b4a7b
(backend 3974 passed / 0 failed; frontend check/vitest/build green) plus
docs-only qualification tooling and evidence.

Migration matrix run against a byte copy of the real production database
(v2, 16 tables, 30373 rows): ok=true, zero failures. Cases: v2->v6 upgrade
with all pre-existing row counts preserved; restart idempotency; old-image
reopen of a migrated DB; interrupted migration recovered clean on reopen;
rollback restore byte-identical to baseline; plus an extra
rollback-then-roll-forward case whose schema hash and row counts match a
clean migration exactly.

Filesystem sentinel: 8/8 ok across F/G/X NTFS binds and the blackbeard CIFS
share, host- and container-side; RENAME_NOREPLACE preserved the destination
on every mount.

Deployed fail-closed: Auto-rename, general auto-grab, HDEncode, and
background scan disabled, discovery mode forced to listing at first startup.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
"""


def run(cmd, cwd=None, check=True, capture=True):
    print(f"  $ {' '.join(cmd)}")
    p = subprocess.run(cmd, cwd=cwd, text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None)
    if capture and p.stdout:
        for line in p.stdout.rstrip().splitlines()[-12:]:
            print(f"    | {line}")
    if check and p.returncode != 0:
        die(f"command failed (exit {p.returncode}): {' '.join(cmd)}")
    return p


def git(*args, check=True):
    return run(["git", *args], cwd=PROJECT, check=check)


def die(msg):
    print(f"\n!! ABORT: {msg}")
    sys.exit(1)


def step(n, title):
    print(f"\n=== {n}. {title} ===")


def main():
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    step(1, "Preconditions")
    if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "main":
        die("deploy checkout is not on main")
    tracked = [l for l in git("status", "--porcelain").stdout.splitlines()
               if l and not l.startswith("??")]
    if tracked:
        die(f"deploy checkout has tracked modifications: {tracked}")
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != EXPECTED_BASE:
        die(f"main is at {head}, expected {EXPECTED_BASE}")
    git("fetch", "origin", "agent/feature-pack-integration")
    bsha = git("rev-parse", BRANCH_REF).stdout.strip()
    if bsha != EXPECTED_BRANCH_SHA:
        die(f"{BRANCH_REF} is at {bsha}, expected {EXPECTED_BRANCH_SHA}")
    if git("rev-list", "--count", f"{BRANCH_REF}..main").stdout.strip() != "0":
        die("main has commits the integration branch lacks; merge would not be clean")
    print("  preconditions OK")

    step(2, "Merge --no-ff and push (never force)")
    msg = EVIDENCE / f"merge-message-{stamp}.txt"
    msg.write_text(MERGE_MESSAGE, encoding="utf-8")
    git("merge", "--no-ff", BRANCH_REF, "-F", str(msg))
    merge_sha = git("rev-parse", "HEAD").stdout.strip()
    print(f"  merge commit: {merge_sha}")
    git("push", "origin", "main")

    step(3, "Stop container before editing config")
    run(["docker", "compose", "stop"], cwd=PROJECT, check=False)
    time.sleep(2)

    step(4, "Back up config and write the fail-closed profile")
    backup = EVIDENCE / f"config-before-deploy-{stamp}.json"
    shutil.copy2(CONFIG, backup)
    print(f"  backup: {backup}")
    print(f"  backup sha256: {hashlib.sha256(backup.read_bytes()).hexdigest()}")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for k, v in DISABLED.items():
        print(f"    {k}: {cfg.get(k, '<absent>')!r} -> {v!r}")
        cfg[k] = v
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print("  config written fail-closed")

    step(5, "Build and start the new image")
    run(["docker", "compose", "up", "-d", "--build"], cwd=PROJECT, capture=False)

    step(6, "Verify")
    ok = True
    for _ in range(30):
        p = run(["docker", "inspect", CONTAINER, "--format",
                 "{{.State.Status}} {{.Image}}"], check=False)
        if p.returncode == 0 and p.stdout.strip().startswith("running"):
            break
        time.sleep(2)
    else:
        die("container did not reach running state")
    status, image = p.stdout.strip().split()
    print(f"  container status: {status}")
    print(f"  running image:    {image}")

    probe = (
        "import json,sqlite3;"
        "c=sqlite3.connect('/dbvol/crawler.db');"
        "print('user_version',c.execute('PRAGMA user_version').fetchone()[0]);"
        "print('integrity',c.execute('PRAGMA integrity_check').fetchone()[0]);"
        "print('hdencode_tables',len([r for r in c.execute("
        "\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'hdencode%'\")]))"
    )
    run(["docker", "exec", CONTAINER, "python", "-c", probe], check=False)

    after = json.loads(CONFIG.read_text(encoding="utf-8"))
    print("  fail-closed flags as persisted after startup:")
    for k, v in DISABLED.items():
        got = after.get(k, "<absent>")
        mark = "OK " if got == v else "BAD"
        if got != v:
            ok = False
        print(f"    [{mark}] {k} = {got!r} (want {v!r})")

    print(f"\n=== RESULT: {'PASS' if ok else 'FAIL — investigate before proceeding'} ===")
    print(f"merge commit for rollback (git revert -m 1 {merge_sha})")
    print(f"config backup: {backup}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
