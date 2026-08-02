# B2 — the rename reproductions, re-run against the current commit

**Date:** 2026-08-01 · **Plan item:** 0.2 (R3) · **Script:** `scripts/repro_sh_r02_r03.py`

Auto-rename has been frozen since 2026-07-19 because two file-operation defects
were reproduced. Fixes landed afterwards. **Until today nobody had re-run the
reproductions against the fixed code** — rev 2.1 §1.5 withdrew the standing
claim for exactly that reason, and B2 was left open.

This closes B2.

---

## Result

Same container, same script, same scenarios. The only variable changed between
runs was `backend/rename/fileops.py`.

| Scenario | pre-fix `555e26b` | current `main` |
|---|---|---|
| **SH-R02 copy** — the path a cross-volume placement falls back to | **DATA LOSS** — competing writer's file overwritten by ours | **SAFE** — raises `FileExistsError`, victim's bytes intact |
| **SH-R02 move** — same-volume publication | **DATA LOSS** — competing writer's file overwritten by ours | **SAFE** — raises `FileExistsError`, victim's bytes intact |
| **SH-R02 hardlink** — control, never vulnerable | SAFE | SAFE |
| **SH-R03** — two concurrent disposals inside one bucket-second | **DATA LOSS** — only 1 of 2 files reached the trash; the other was destroyed | **SAFE** — both moved, both hold restore records (`duplicate-name.mkv`, `duplicate-name (1).mkv`) |

Run order was **A → B → A** (current, pre-fix, current) inside one container, so
the pre-fix result cannot be explained by container state.

**Verdict: both defects are fixed, and the reproductions prove it rather than
asserting it.**

---

## Why the control matters more than the pass

The first attempt reported SAFE on *both* trees. That looked like good news and
was not: `docker cp` had silently failed — an MSYS `/tmp` source path is not
valid for `docker cp` on Windows, and I had suppressed stderr — so **both runs
executed the current code.** Verified afterwards: the container's `fileops.py`
was 1770 lines with 10 `renameat2` references, i.e. the new file.

Had the script only ever been run in `--expect safe` mode, it would have printed
PASS, and the conclusion "the defects are fixed" would have rested on a
reproduction that reproduces nothing. The script therefore refuses to be run in
one direction only: `--expect loss` fails loudly if the pre-fix tree does *not*
lose data, because that means the scenarios cannot discriminate.

This is the same failure shape as the two already recorded this week — a
security test that asserted a proxy and could never pass for the right reason,
and a misconfigured pytest harness that produced three wrong failure counts.
The lesson is not "be careful with `docker cp`"; it is that **a green result
from an unvalidated instrument is indistinguishable from a green result from a
working system.**

---

## What the fix actually is

The `os.path.lexists(dst)` precheck in `place_file` is still there, and the
check-to-publish window is still open — the script exploits it by patching
`os.makedirs`, which `place_file` calls immediately after the check, so the
competing file genuinely appears after the destination was found absent.

The window no longer matters, because **every publication primitive is now
no-replace**:

* hardlink → `os.link` (always raised `EEXIST`; this is why it never lost data)
* symlink → `os.symlink`
* copy → `_copy_verify_atomic` → `_move_no_replace`
* move → `_move_no_replace_durable` → `renameat2(RENAME_NOREPLACE)`, falling
  back to atomic `os.link` + unlink where the kernel or filesystem lacks it

The pre-fix tree reached `os.replace(part, dst)` and `os.rename(path, dst)`,
both of which replace silently on POSIX. The precheck is now an early-exit
convenience; the safety is in the primitive.

For SH-R03, the trash bucket name still has one-second precision, so two
disposals in the same second still share a bucket. That was never the defect.
The defect was what happened next: the pre-fix tree computed both destinations
before either moved, and both `os.rename` calls targeted the same path, so the
second destroyed the first. The current tree reserves the name durably under a
process-wide lock *before* the source can move, and `_choose_reserved_trash_name`
resolves the collision to `duplicate-name (1).mkv`.

---

## What this does NOT establish

**It does not open the rename gate.** Everything above ran inside a container on
a single filesystem. It exercises the *logic*, not the storage.

The topology that made SH-R02 reachable on this system in the first place —
downloads and library on different volumes, so `hardlink` always hits `EXDEV`
and falls through to copy-and-publish — was **not** exercised here. Neither were
the real 9p bind mounts, the X: mirror, or V:. That is precisely B5's job
(copy-only rehearsal on the real storage surfaces, hashes verified), and it
remains outstanding.

So the honest statement of the freeze is unchanged from the plan: **no known
live data-loss defect; the hold is want of real-storage evidence.**

One narrower gap worth naming: `_move_no_replace` falls back to `os.link` when
`renameat2` is unavailable. Which branch the real volumes take has not been
measured. B5 should record it per volume rather than assume `renameat2`.

> **Correction (2026-08-02, ChatGPT second pass).** An earlier version of this
> paragraph said `_move_no_replace` raises `UnsupportedFilesystemSafetyError`
> when neither primitive is atomic. **That is wrong.** Verified at `7cc5275`:
> when `os.link` fails for any reason other than `EXDEV`, `_move_no_replace`
> raises a **plain `OSError`** carrying the original errno and a fail-closed
> message, source intact (`fileops.py:304-312`).
> `UnsupportedFilesystemSafetyError` is raised only by
> `_require_directory_durability`, the directory-fsync preflight
> (`fileops.py:141-152`).
>
> No data-loss consequence — the source survives either way — but the error
> mattered for the instruction it produced: **B5 must not detect unsupported
> no-replace publication by catching that custom class.** It will never be
> raised on that path. Detect it from the errno instead (`ENOTSUP`,
> `EOPNOTSUPP`, `ENOSYS`), which the ledger's classifier already handles, and
> treat observed disk state as authoritative.
>
> Related: `filesystem_safety_status()` returns the literal string
> `"renameat2_or_hardlink"` (`fileops.py:215/221`) — it reports that one of the
> two is expected, not which one actually works. **B5 needs an operative scratch
> probe per volume, not that diagnostic.** Capability discovery should be B5's
> first stop condition, before the rehearsal counts as started, and it can be
> done entirely with scratch files — no source-consuming operation on real media
> is needed merely to learn the capability.

### Two scope corrections to the claim above

* This is a **file-level differential reproduction using `fileops.py` from
  `555e26b`**, not an execution of that historical commit in full. The rest of
  the tree stayed current. That is still a valid — arguably cleaner — causal
  isolation, but it is a different and narrower claim than "the old build lost
  data," and it should be stated as such.
* The reproduction recipe below ends after current → pre-fix. The actual run was
  **current → pre-fix → current**, and the third step is what rules out
  container drift. The recipe should include it, and should pin each installed
  `fileops.py` by hash rather than by a `grep -c renameat2` sanity check.

---

## Reproducing

```bash
docker run -d --rm --name sh-repro-r3 --entrypoint sleep scanhound:latest infinity
docker cp backend sh-repro-r3:/work/backend
docker cp scripts sh-repro-r3:/work/scripts
docker exec -e PYTHONPATH=/work -e HOME=/tmp -w /work sh-repro-r3 \
  python /work/scripts/repro_sh_r02_r03.py --expect safe

# the control — swap in the pre-fix file and VERIFY the swap landed
git show 70dca70^:backend/rename/fileops.py > /tmp/old.py   # use a native path on Windows
docker cp /tmp/old.py sh-repro-r3:/work/backend/rename/fileops.py
docker exec sh-repro-r3 sh -c 'grep -c renameat2 /work/backend/rename/fileops.py'  # must be 0
docker exec sh-repro-r3 rm -rf /work/backend/rename/__pycache__
docker exec -e PYTHONPATH=/work -e HOME=/tmp -w /work sh-repro-r3 \
  python /work/scripts/repro_sh_r02_r03.py --expect loss
```

Nothing touches real media; every scenario runs in a temporary directory the
script creates and deletes.
