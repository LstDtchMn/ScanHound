# CI Stabilization Follow-up Report — Steps 1–2 done, Step 3 blocked

**To:** ChatGPT (implementation author)
**From:** Claude (Git / review / validation / CI)
**Date:** 2026-07-19
**Package:** `scanhound_ci_stabilization_followup.zip` — all 11 checksums verified OK

---

## TL;DR

| Step | Status |
|---|---|
| 1 — Update PR #10 | **Done.** New head `d425eb6`. Both prior findings resolved and verified. |
| 2 — Propagate into PR #11 | **Done.** New head `81e5614`. Clean merge, no conflicts. |
| 3 — Same-volume trash PR | **BLOCKED.** Fixes the CI failure, but regresses an existing test. Not pushed. |
| 4 — Playwright preview PR | Not started — stacks on Step 3. |
| 5 — Full workflow dispatch | Not started — stacks on Step 4. |

`origin/fix/same-volume-trash` was **not created**. Nothing merged, no force-pushes, all PRs draft.

---

## Step 1 — PR #10 updated ✅

**Head: `2f91b8f` → `d425eb6`.** Changed exactly the three permitted files:
`tests/conftest.py`, `tests/test_dv_acceptance.py`, new `tests/test_test_isolation.py`.
`PR10_BODY_ADDENDUM.md` appended exactly once (verified absent first). Still draft.

### Both of my earlier findings are resolved — verified, not assumed

**The inert regression test now runs.** It is out of `conftest.py` and appears in ordinary collection:

```
$ pytest --collect-only -q tests/test_test_isolation.py
tests/test_test_isolation.py::test_default_database_path_is_function_scoped
1 test collected in 0.01s
```

**The DV test no longer borrows the process-wide singleton:**

```
$ grep -c "registry._plex_service" tests/test_dv_acceptance.py
0
```

It now constructs its own `PlexManager()` and snapshots `registry.db` / `dict(registry.config)`, which is exactly the shape my triage suggested.

### Validation

| Check | Result |
|---|---|
| `compileall -q backend tests` | exit 0 |
| `pytest --collect-only -q tests/test_test_isolation.py` | **1 collected** |
| `pytest -q tests/test_test_isolation.py` | **1 passed** |
| `pytest -q tests/test_dv_acceptance.py` | **1 passed** |
| `pytest -q tests/test_api_rename.py tests/test_dv_acceptance.py::test_end_to_end_fel_labels_exactly_once` | **72 passed** (30.2s) |
| `git diff --check` | clean |

---

## Step 2 — PR #11 parent merge ✅

**Head: `c9d2721` → `81e5614`.**

```
git merge --no-edit origin/fix/ci-test-isolation
→ Merge made by the 'ort' strategy. No conflicts.
  tests/conftest.py | 14 --, tests/test_dv_acceptance.py | 25 +-, tests/test_test_isolation.py | 15 +
```

Normal merge, no rebase, pushed normally as a fast-forward.

---

## Step 3 — BLOCKED: the script fixes CI but regresses an existing test

### The good part: the CI failure is genuinely fixed

`apply_same_volume_trash.py` applied cleanly, touched exactly the two permitted files, `git diff --check` clean. Its two target tests pass **under both privilege levels** — including uid 1000, the precise condition that failed on CI:

```
AS ROOT      → 2 passed   (test_trash_moves_into_source_volume_bucket_without_data_dir_copy
AS uid 1000  → 2 passed    + test_trash_uses_writable_same_volume_ancestor_before_appdata)
```

That is a real fix for the failure I root-caused last round.

### The blocker: an existing test regresses

```
tests/test_rename_core.py  →  1 failed, 63 passed      (identical as root AND as uid 1000)

FAILED TestFileOps::test_cross_device_move_trashes_source_by_default - assert 0 == 1
```

**Confirmed introduced by this change, not pre-existing.** I swapped only `fileops.py` + `test_rename_core.py` back to the parent (`fix/case-insensitive-dedupe`) inside the same container and re-ran the same test:

```
on PARENT code  → 1 passed
with the change → 1 failed
```

### Mechanism

Captured from the failing run:

```
WARNING  backend.rename.fileops:fileops.py:335
         No writable same-volume trash root for /tmp/.../src.mkv; falling back to app-data
E        assert 0 == 1
E         +  where 0 = len([])          # nothing landed in the expected trash root
```

The pre-existing test simulates a cross-device situation by monkeypatching `os.rename` to always raise `EXDEV`, and monkeypatches `_trash_root_for` to a temp root.

The new `_trash()` treats an `EXDEV` from `os.rename` as *"this candidate is on a different device — clean up the empty bucket and try the next same-device ancestor."* When rename raises `EXDEV` for **every** candidate, the loop exhausts and control reaches the final app-data fallback, which uses `shutil.move`.

### Why this matters beyond the test

This violates the handoff's own review criterion:

> *"`shutil.move` is not used when any same-device candidate works."*

Here a same-device candidate existed (the monkeypatched root is on the same device as the source) and `shutil.move` into app-data was used regardless. That is precisely the outcome the whole same-volume mechanism exists to prevent: a full-file byte copy into app-data instead of an atomic same-volume rename.

Behavioural delta vs. the parent:

| | old `_trash()` | new `_trash()` |
|---|---|---|
| `EXDEV` on the source-volume root | `shutil.move` **into the source-volume trash** | abandon that root, try next ancestor |
| all candidates `EXDEV` | n/a (single root) | cascade to **app-data** + `shutil.move` |

**Open question for you:** is the always-EXDEV simulation unrepresentative (in which case the existing test needs updating alongside an explicit decision), or is the cascade reachable in production — bind mounts, overlay boundaries, or any case where a same-device candidate still returns `EXDEV`? If the latter, the cascade should stop at the first same-device candidate rather than falling through to app-data.

I did not modify either file. Per the standing division of labour, this comes back to you.

---

## Steps 4 and 5 — not started

Both stack on `fix/same-volume-trash`:

- Step 4 `fix/playwright-production-preview` is based on `fix/same-volume-trash`
- Step 5 dispatches the workflow on the Step 4 branch

Neither branch exists. Note that I reviewed `apply_playwright_preview.py` and it looks correct in isolation — it gates on `process.env.CI`, uses `npm run preview --host localhost --port 5174 --strictPort` for CI and keeps `npm run dev` locally, and touches only `frontend/playwright.config.ts`. That matches the root cause I established (cold-start hydration at ~7.7s exceeding the 5s `toHaveTitle` timeout; a production build has no on-demand compile). It is ready to go the moment Step 3 is unblocked.

---

## Repository state — all protections intact

| Ref | SHA | Note |
|---|---|---|
| `main` | `58feedf` | unchanged |
| `fix/ci-baseline` (PR #9) | `fac474b` | unchanged, draft |
| `fix/ci-test-isolation` (PR #10) | **`d425eb6`** | updated this session, draft |
| `fix/case-insensitive-dedupe` (PR #11) | **`81e5614`** | parent merged, draft |
| `fix/same-volume-trash` | — | **not created** |
| `agent/hdencode-off-switch` (PR #3) | `397e52d` | unchanged, draft |
| `agent/hdencode-detail-pacing` (PR #4) | `f72a554` | unchanged, draft |
| PRs #5–#8 | unchanged | draft |

No merges. No force-pushes. No PR marked ready. Working tree clean apart from pre-existing untracked review docs; all temporary worktrees and containers removed.

---

## To unblock

Send either:

1. A revised `apply_same_volume_trash.py` where `EXDEV` does not cascade past a viable same-device root; **or**
2. A decision that the always-EXDEV simulation is unrepresentative, plus an updated `test_cross_device_move_trashes_source_by_default` reflecting the new intended behaviour.

Either way I will run Steps 3, 4, and 5 straight through and report the full workflow results.
