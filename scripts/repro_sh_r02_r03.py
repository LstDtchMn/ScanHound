#!/usr/bin/env python3
"""Re-run the 2026-07-19 SH-R02 / SH-R03 data-loss reproductions.

Plan item 0.2 (R3). These two defects are why auto-rename has been frozen since
19 July. Both were reproduced then. Fixes have since landed -- SH-R02 in
`70dca70`, SH-R03 in `44ea7ba` + `4d678bd` -- but the reproductions were never
re-run against the fixed code. Rev 2.1 §1.5 withdrew the standing claim for
exactly that reason.

THIS SCRIPT ONLY MEANS SOMETHING WHEN RUN TWICE.

    Against the fixed code   -> every scenario must report SAFE.
    Against the pre-fix code -> the SH-R02 scenarios must report DATA LOSS.

A run that reports SAFE on both is not good news: it means the reproduction no
longer reproduces anything and cannot distinguish a fixed system from a broken
one. That is the failure mode this project has hit before, so the second run is
not optional and the exit status encodes which mode was expected.

Usage:
    python repro_sh_r02_r03.py --expect safe     # against current code
    python repro_sh_r02_r03.py --expect loss     # against pre-fix code

Exit 0 when observations match --expect, 1 otherwise.

Nothing here touches real media. Every scenario runs entirely inside a
temporary directory created by this process.
"""

import argparse
import contextlib
import errno
import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.rename import fileops  # noqa: E402

try:  # the writer guard postdates 70dca70, so it is absent on the old tree
    from backend.runtime_lock import _unlocked_fileops_for_tests as _bypass
except Exception:  # pragma: no cover - old tree
    @contextlib.contextmanager
    def _bypass():
        yield


VICTIM = b"COMPETING WRITER'S BYTES - MUST SURVIVE" * 64
OURS = b"the bytes ScanHound was asked to place" * 64


@contextlib.contextmanager
def race_at_makedirs(dst, payload):
    """Drop a competing file at `dst` inside place_file's check-to-publish gap.

    place_file checks `os.path.lexists(dst)` and only then calls
    `os.makedirs`. Both the fixed and the pre-fix trees have that ordering, so
    patching makedirs opens the real window rather than simulating one -- the
    racing file genuinely appears after the destination has been found absent.
    """
    real = fileops.os.makedirs
    done = threading.Event()

    def racing_makedirs(*args, **kwargs):
        result = real(*args, **kwargs)
        if not done.is_set():
            done.set()
            with open(dst, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        return result

    fileops.os.makedirs = racing_makedirs
    try:
        yield
    finally:
        fileops.os.makedirs = real


def _verdict(dst, raised):
    """SAFE only when the competing writer's bytes are still on disk."""
    if not os.path.lexists(dst):
        return "DATA LOSS", "destination no longer exists at all"
    with open(dst, "rb") as handle:
        found = handle.read()
    if found == VICTIM:
        return "SAFE", "competing writer's bytes intact; placement raised %s" % (
            type(raised).__name__ if raised else "nothing")
    if found == OURS:
        return "DATA LOSS", "competing writer's file was overwritten by ours"
    return "DATA LOSS", "destination holds neither payload (%d bytes)" % len(found)


def scenario_r02(method, workdir):
    """SH-R02 -- publication must never replace a racing destination."""
    root = tempfile.mkdtemp(dir=workdir)
    src = os.path.join(root, "source.mkv")
    dst = os.path.join(root, "library", "placed.mkv")
    with open(src, "wb") as handle:
        handle.write(OURS)

    raised = None
    with _bypass(), race_at_makedirs(dst, VICTIM):
        try:
            fileops.place_file(src, dst, method=method)
        except Exception as exc:  # the safe outcome is a refusal
            raised = exc

    state, detail = _verdict(dst, raised)
    if state == "SAFE" and method == "move" and not os.path.lexists(src):
        return "DATA LOSS", "refused to publish but consumed the source anyway"
    return state, detail


def scenario_r03(workdir):
    """SH-R03 -- concurrent disposals in one second must both stay restorable.

    The bucket name still has one-second precision, so two disposals in the
    same second share a bucket. That is not itself the defect; the defect was
    the unlocked read-modify-write of the manifest afterwards, which silently
    dropped a record and left `restore_trash_entry` hard-refusing the file.
    """
    root = tempfile.mkdtemp(dir=workdir)
    trash_root = os.path.join(root, ".scanhound-trash")

    sources = []
    for i in range(2):
        holder = os.path.join(root, "vol%d" % i)
        os.makedirs(holder)
        path = os.path.join(holder, "duplicate-name.mkv")
        with open(path, "wb") as handle:
            handle.write(b"payload %d " % i * 512)
        sources.append(path)

    real_roots = fileops._same_volume_trash_roots
    original_trash_root = fileops._TRASH_ROOT
    fileops._same_volume_trash_roots = lambda path: [trash_root]
    fileops._TRASH_ROOT = trash_root

    start = threading.Barrier(len(sources))
    errors = []

    def dispose(path):
        try:
            start.wait(timeout=10)
            with _bypass():
                fileops._trash(path)
        except Exception as exc:
            errors.append(exc)

    try:
        threads = [threading.Thread(target=dispose, args=(p,)) for p in sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    finally:
        fileops._same_volume_trash_roots = real_roots
        fileops._TRASH_ROOT = original_trash_root

    if errors:
        return "DATA LOSS", "disposal raised: %r" % (errors[0],)

    moved, recorded = [], []
    for bucket_name in sorted(os.listdir(trash_root)):
        bucket = os.path.join(trash_root, bucket_name)
        if not os.path.isdir(bucket):
            continue
        moved += [f for f in os.listdir(bucket) if f != "manifest.json"]
        manifest = os.path.join(bucket, "manifest.json")
        if os.path.lexists(manifest):
            with open(manifest, "r", encoding="utf-8") as handle:
                recorded += [
                    r.get("trashed_name") for r in json.load(handle)
                    if r.get("trashed_name")
                ]

    if len(moved) != 2:
        return "DATA LOSS", "%d of 2 files reached the trash" % len(moved)
    orphans = sorted(set(moved) - set(recorded))
    if orphans:
        return "DATA LOSS", (
            "%d file(s) on disk with no restore record -> restore_trash_entry "
            "hard-refuses them: %s" % (len(orphans), ", ".join(orphans)))
    return "SAFE", "both files moved and both restorable (%s)" % ", ".join(
        sorted(moved))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=("safe", "loss"), required=True)
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="sh-repro-")
    try:
        results = [
            ("SH-R02 copy   (the cross-volume path)", scenario_r02("copy", workdir)),
            ("SH-R02 move   (same-volume publication)", scenario_r02("move", workdir)),
            ("SH-R02 hardlink (control, never vulnerable)",
             scenario_r02("hardlink", workdir)),
            ("SH-R03 concurrent trash disposal", scenario_r03(workdir)),
        ]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    width = max(len(name) for name, _ in results)
    print("\n%-*s  %-10s  %s" % (width, "SCENARIO", "RESULT", "OBSERVATION"))
    print("-" * (width + 60))
    for name, (state, detail) in results:
        print("%-*s  %-10s  %s" % (width, name, state, detail))

    # The hardlink control is excluded: os.link has always raised EEXIST, so it
    # was never the vulnerable path and cannot discriminate between the trees.
    discriminating = [r for name, r in results if "control" not in name]
    lost = [state for state, _ in discriminating if state == "DATA LOSS"]

    print()
    if args.expect == "safe":
        if lost:
            print("FAIL: expected no data loss, but %d scenario(s) lost data."
                  % len(lost))
            return 1
        print("PASS: no data loss in any discriminating scenario.")
        print("      This is only meaningful alongside a --expect loss run on")
        print("      the pre-fix tree. Without it, the scenarios are unproven.")
        return 0

    if not lost:
        print("FAIL: expected the pre-fix tree to lose data and it did not.")
        print("      The reproduction is not exercising the defect, so it also")
        print("      cannot attest that the current tree is fixed.")
        return 1
    print("PASS: %d scenario(s) reproduced data loss, as expected on the "
          "pre-fix tree." % len(lost))
    print("      The scenarios discriminate. A --expect safe run on the fixed")
    print("      tree is therefore meaningful.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
