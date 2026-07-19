# PR #17 MP-10 — defect REPRODUCED, fix validated, one test-harness regression

Stage-1 probe v2 (`scanhound-pr17-stage1-probe-v2`) + the previously delivered
Stage-2 fix (`02_apply_pr17_lifecycle_generation_fix.py`, checksum
`500532817ddb…4f31a8a`, re-verified). All package checksums OK. Nothing merged,
deployed, force-pushed, or marked ready. PR #17 branch **not advanced** — the fix
is NOT committed, because it regresses 15 existing tests (below).

## 1. The v2 probe is fixed

Both guard defects I flagged on the earlier probe are resolved: it now uses
`git rev-parse --is-inside-work-tree` (accepts linked worktrees) and
`git symbolic-ref` (detached-safe), plus a blob-hash gate confirming the target
test file matches PR #17 (`7fae8bd0…`). 121 lines, `__main__` guard present,
applies cleanly (only `tests/test_api_lifecycle.py`, +130). I verified the three
symbols the test uses (`ServiceRegistry`, `_teardown_services`,
`_prepare_registry_for_startup`) are already imported at the PR #17 head, so an
import/NameError could not masquerade as a reproduction.

## 2. MP-10 defect REPRODUCED — for the intended reason

Stage 1 on the unmodified PR #17 head (`bf07697`, fix NOT applied):

```
tests/test_api_lifecycle.py::test_late_background_worker_cannot_publish_into_next_lifespan
>   assert old_db.late_writes == []
E   AssertionError: assert ['upsert', 'purge', 'count'] == []
```

The worker, blocked past the two-second `BackgroundScanner.stop()` join and
released after `_prepare_registry_for_startup`, resumed and performed **three
writes through the captured, already-closed old database** (`upsert`, `purge`,
`count`). The worker itself raised no unrelated exception (it passed the
`"exception" not in worker_outcome` assertion and reached the intended one). This
is the genuine late-worker defect, not a harness artifact.

**MP-10 is therefore a confirmed real defect. The generation token is warranted.**

## 3. The Stage-2 generation fix works for the intended defect

Applied `02` (in a fresh local clone on branch `fix/lifecycle-registry-reset`,
because `02` still carries the old `.git.is_dir()`/detached-branch guards that
reject a worktree — see §5). Changes exactly the 4 expected files. Mechanism
matches the design:

- `ServiceRegistry` gains `_lifespan_generation` + lock, `begin_lifespan()`,
  `lifespan_generation`, `owns_lifespan(gen)` (`gen == current and not
  shutdown_requested`).
- `BackgroundScanner.__init__` captures `self._lifespan_generation =
  registry.lifespan_generation` (line 36); `_owns_lifespan()` rechecks it before
  the DB is captured and before every publish (lines 158/208/232/270).
- `_prepare_registry_for_startup` returns the new generation.

Validation with the fix applied:

| Check | Result |
|---|---|
| the reproduction test | **PASS** (flipped fail→pass) |
| full `tests/test_api_lifecycle.py` | **5 passed** |
| `tests/test_api_results.py` | **58 passed** |
| `test_api_routes.py::TestResults` (the flake family) | **16 passed** |
| generation increments exactly once per `_prepare_registry_for_startup` | **PASS** — 0→1→2→3, returns the new gen |
| old generation loses ownership; current retains it | `owns_lifespan(old)=False`, `owns_lifespan(current)=True` |

Review requirements 1–4 and 7 (for lifecycle/results) all satisfied.

## 4. BLOCKING — the fix regresses 15 tests in `test_background_scanner.py`

```
15 FAILED  tests/test_background_scanner.py
E  AttributeError: '_FakeRegistry' object has no attribute 'lifespan_generation'
   backend/background_scanner.py:36  ->  self._lifespan_generation = registry.lifespan_generation
```

Attribution: `TestConfigurableCategories` **passes (3 passed) on the unmodified
head** and fails only with the fix — a fix-induced regression. Cause: the fix
makes `BackgroundScanner.__init__` require `registry.lifespan_generation`, but
`test_background_scanner.py`'s `_FakeRegistry` test double does not implement it
(nor `owns_lifespan`). The `02` script updated `tests/test_api_lifecycle.py` but
**not** `tests/test_background_scanner.py`.

This is the same class as the SH-R02 EXDEV test repair: the production change is
correct (a real `ServiceRegistry` always has `lifespan_generation`), but a
minimal test double is now incompatible. It must be resolved before the fix can
land — a draft branch must not go red on 15 tests.

**Additional scope to check:** `tests/test_source_registry.py` also constructs a
`BackgroundScanner`/registry double and was not in this run; it may share the
same incompatibility.

I did not patch `_FakeRegistry` myself — authoring the test-double update is
yours, exactly as you authored the SH-R02 test repair. Two options, your call:

- (a) **Test-only:** add `lifespan_generation`/`begin_lifespan`/`owns_lifespan`
  to `_FakeRegistry` in `test_background_scanner.py` (and `test_source_registry.py`
  if affected). Minimal, matches how you fixed `test_api_lifecycle.py`.
- (b) **Defensive production:** `getattr(registry, "lifespan_generation", 0)` in
  `__init__` and a safe `owns_lifespan` fallback, so any registry-like object
  works. More robust, but lets an incomplete registry through — a mild smell.

I lean (a) for consistency; (b) is legitimate if you want `BackgroundScanner`
resilient to partial registries.

## 5. Secondary: the Stage-2 fix script has the old repo guards

`02_apply_pr17_lifecycle_generation_fix.py` still uses `Path(".git").is_dir()`
(rejects linked worktrees: "Not in the ScanHound repository root") and a
`git branch --show-current` equality check (rejects detached HEAD). The v2 probe
fixed both; the fix script did not. I worked around it with a full local clone on
the real branch name. When you resend the fix (with the §4 harness correction),
please port the probe's `git rev-parse --is-inside-work-tree` + `symbolic-ref`
guards so it runs in a worktree.

## Disposition

- MP-10 defect: **CONFIRMED real** (reproduced).
- Generation fix: **validated for the intended defect**, but **NOT landed** due
  to the §4 regression.
- Resend `02` with the `_FakeRegistry` update (and the §5 guard port). Then I
  re-run: reproduction passes, full lifecycle green, **`test_background_scanner.py`
  green**, `test_source_registry.py` green, and the broader suite — and only then
  commit to `fix/lifecycle-registry-reset`.

State: PR #15 `70dca70`, #16 `44ea7ba`, #17 `bf07697` — all draft, unchanged.
`main` `555e26b`. Nothing merged/deployed/force-pushed/ready. Auto-rename still
enabled in production per live verification.
