# PR #17 late-worker package — Stage 1 BLOCKED: probe script truncated

MP-10 gate. Package `scanhound-pr17-late-worker`. ZIP + all 3 files verified
against `SHA256SUMS` — OK. Nothing merged, deployed, force-pushed, or applied.
PR #17 head unchanged at `bf07697`.

## Outcome

**The defect was NOT reproduced, because the Stage-1 probe cannot run.** Per the
accepted MP-10 protocol — "a generation token is prohibited as speculative scope
unless the defect reproduces" — the fix (`02_...`) was therefore **NOT applied**.
This is a correct stop, not a failure to complete: a broken probe is not a
reproduction, and applying the fix on faith would defeat the reproduce-first gate.

## The defect in `01_add_pr17_late_worker_probe.py`

The probe file is **truncated as delivered**:

```
lines            : 56
last line        : "    test = r"          <- assignment cut off mid-statement
__main__ guard   : ABSENT
test body        : ABSENT
write_text call  : ABSENT
```

Consequences:

1. There is no `if __name__ == "__main__": ...main()` entry point, so
   `python 01_add_pr17_late_worker_probe.py` only **defines** functions and
   **never calls `main()`** — it exits 0, prints nothing, and changes nothing.
   (Observed exactly: exit 0, empty stdout, empty stderr, no file change.)
2. The file ends at `test = r` — the raw-string test body (`test = r"""..."""`)
   and the subsequent `TEST_PATH.write_text(...)` are missing. The test
   `test_late_background_worker_cannot_publish_into_next_lifespan` that the
   README's Stage-1 pytest command targets is **never written into
   `tests/test_api_lifecycle.py`**. The name appears once in the file only as
   the `MARKER` constant (line 17), not as a test definition.
3. `python -m py_compile tests/test_api_lifecycle.py` "passes" because that file
   is unmodified — masking the no-op.

**This truncation is in the shipped artifact, not transit corruption:** the
recorded checksum for the probe (`1d30711f5d549039b63c3090154f1561838eed1786a2c26d8283075c476cc13b`)
matches the file exactly. The file was checksummed in its truncated state.

## Two secondary guard issues (relevant when you resend)

Even a complete probe would not run in a standard reviewer setup:

- `verify_repo` line 25: `if not Path(".git").is_dir(): raise`. In a **linked git
  worktree** (how I isolate every PR head) `.git` is a *file*, not a directory,
  so this always fails. `--skip-head-check` does not bypass it. Suggest
  `git rev-parse --git-dir` instead of `Path(".git").is_dir()`.
- Line 33-34 branch check compares `git branch --show-current` to the branch
  name; under a detached checkout of the exact SHA this is empty and fails.
  A HEAD-SHA check alone is sufficient and detached-safe.

When you resend a complete probe I will run it in a **full clone** checked out to
`fix/lifecycle-registry-reset` (so `.git` is a real directory and the branch
check passes), or, if you relax the guard, in a worktree at `bf07697`.

## The fix script (`02_...`) looks complete

For your reference (not applied): `02_apply_pr17_lifecycle_generation_fix.py`
is 337 lines, has a `__main__` guard, 4 `write_text` calls, and targets exactly
the four expected files (`backend/api/dependencies.py`, `backend/api/main.py`,
`backend/background_scanner.py`, `tests/test_api_lifecycle.py`). It was not
inspected in depth or executed, because Stage 1 must reproduce first.

## What I need

A complete `01_add_pr17_late_worker_probe.py` — with the test body and a
`__main__` entry point — re-checksummed. Then I run Stage 1; **only if the test
fails for the late-worker reason** do I apply `02` and validate. If it does not
reproduce, the generation token stays out per the disposition.

I did not reconstruct the missing test myself: authoring the reproduction is
yours, and a reviewer inventing the test would defeat the independent gate.
