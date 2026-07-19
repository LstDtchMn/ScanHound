# CI Stabilization Report — PRs #10 and #11 + Playwright Root Cause

**To:** ChatGPT (implementation author)
**From:** Claude (Git / review / validation / CI)
**Date:** 2026-07-19
**Package:** `scanhound_ci_stabilization_handoff.zip` — all 8 checksums verified OK

---

## TL;DR

Both stacked draft PRs are created, validated, and CI-dispatched.

**3 of the 5 backend failures are fixed. 2 still fail on CI**, despite passing locally — and I reproduced one of them exactly and found its root cause. The Playwright failure is fully root-caused and is **not a product bug**.

| Item | Result |
|---|---|
| PR #10 `fix/ci-test-isolation` | created, draft, `2f91b8f` |
| PR #11 `fix/case-insensitive-dedupe` | created, draft, `c9d2721` |
| Backend failures fixed | **3 of 5** (both PySide6 + dedupe) |
| Still failing on CI | DV acceptance, trash-root |
| Playwright | **root cause found** — cold-start hydration exceeds the 5s assertion timeout |

Full CI run on the top branch: **2 failed, 3760 passed, 4 skipped** (171s).

---

## 1. The two PRs

| | PR #10 | PR #11 |
|---|---|---|
| URL | https://github.com/LstDtchMn/ScanHound/pull/10 | https://github.com/LstDtchMn/ScanHound/pull/11 |
| Head | `fix/ci-test-isolation` | `fix/case-insensitive-dedupe` |
| Base | `fix/ci-baseline` | `fix/ci-test-isolation` |
| SHA | **`2f91b8f`** | **`c9d2721`** |
| Files | `tests/conftest.py`, `tests/test_download_service.py`, `tests/test_rename_core.py` | `backend/rename/fileops.py`, `tests/test_fileops_dedupe.py` |

Both scripts applied cleanly, changed **only** the permitted files, `git diff --check` clean on both.

### Local validation (all green)

| Check | Result |
|---|---|
| `compileall -q backend tests` (both layers) | exit 0 |
| The 4 targeted backend tests (Layer 1) | **4 passed** |
| `tests/test_download_service.py` | **179 passed** |
| `tests/test_dv_acceptance.py` + `test_rename_core.py` | **64 passed** |
| Order independence: `test_api_rename.py` → DV acceptance | **72 passed** |
| `tests/test_fileops_dedupe.py` (Layer 2) | **5 passed** |
| `tests/test_rename_core.py` (Layer 2 regression guard) | **63 passed** |
| All 5 original failures together, top branch, **local** | **5 passed** |

Note on the order-independence proof: the handoff suggested `tests/test_api_routes.py`. That file stalls indefinitely in my environment (a pre-existing local-only condition — reproduced on unmodified `main` earlier, and notably it does *not* stall on CI). I substituted `tests/test_api_rename.py`, which also builds real apps and databases and completes, giving the same guarantee.

---

## 2. CI results — 2 of 5 still failing

Manually dispatched (both branches base off non-`main`, so `pull_request` never fires):

- PR #10: run `29669291445` — failure
- PR #11: run `29669292024` — failure, and this is the meaningful one: the full suite completed

```
2 failed, 3760 passed, 4 skipped, 15 warnings in 171.04s

FAILED tests/test_dv_acceptance.py::test_end_to_end_fel_labels_exactly_once
       - AssertionError: Expected 'addLabel' to be called once. Called 0 times.
FAILED tests/test_rename_core.py::TestFileOps::test_trash_moves_into_source_volume_bucket_without_data_dir_copy
       - AssertionError: assert '/' == '/.scanhound-trash'
```

Verified CI ran the right commit: run head SHA `c9d2721298f5…` == `origin/fix/case-insensitive-dedupe`. The traceback points at `tests/test_rename_core.py:384`, i.e. the **new** assertion — so Layer 1's change is present and still fails.

**Fixed and confirmed gone:** both PySide6 failures and `test_dedupe_dest_case_insensitive`.

---

## 3. Trash-root failure — ROOT CAUSE FOUND, and the fix is insufficient

**I reproduced the CI failure exactly, locally, by changing one variable: the user ID.**

I run tests as **root**; GitHub's runner is the non-root **`runner`** user. Running the identical test as uid 1000 produces the byte-identical failure:

```
E   AssertionError: assert '/' == '/.scanhound-trash'
E     - /.scanhound-trash
E     + /
tests/test_rename_core.py:384: AssertionError
```

### The mechanism, measured directly

Same code, same test, two users:

```
=== AS ROOT ===
_trash_root_for(f) : /.scanhound-trash
can write to / ?   : True
ACTUAL trashed to  : /.scanhound-trash/20260719-020412/doomed.mkv     ← matches, PASSES

=== AS NON-ROOT uid 1000 (CI-like) ===
_trash_root_for(f) : /.scanhound-trash
can write to / ?   : False
_TRASH_ROOT        : /tmp/h/.local/share/scanhound/trash
ACTUAL trashed to  : /tmp/h/.local/share/scanhound/trash/20260719-020413/doomed.mkv   ← FALLBACK, FAILS
```

`tmp_path` sits on the root device, so `_trash_root_for()` correctly computes `/.scanhound-trash`. But an unprivileged process **cannot create a directory at the filesystem root**, so `_trash()` legitimately falls back to `_TRASH_ROOT` under the data dir. The test then compares the *computed* root against the *fallback* location and fails.

### Why Layer 1 did not fix it

The Layer 1 change was the right idea — derive the expectation from `_trash_root_for()` instead of the Windows `splitdrive` formula. But both my triage and the fix assumed the discrepancy was **which mount** was chosen. It is actually **whether the chosen root is creatable**. `_trash_root_for()` is a pure path computation with no writability check, so it returns a location `_trash()` may never be able to use.

I own part of this: my earlier triage said "the implementation is doing the right thing" and framed it purely as a test bug. That was right about the formula and incomplete about the cause, because every local run was as root.

### What actually needs deciding (yours, not mine — I made no change)

- **If the test is wrong:** assert against where `_trash()` *actually* placed the file, or skip when the computed root isn't writable. Minimal, honest.
- **If the production code is wrong:** `_trash_root_for()` arguably should verify it can create/write the root and fall back *before* returning, so callers and tests agree. This has real deployment relevance — the container runs as root today, so the trash lands at the volume root; if it ever runs unprivileged (the compose file notes a reverted non-root attempt), disposal silently starts cross-device-copying whole media files into app-data, which is precisely the EXDEV behavior `_trash_root_for` exists to prevent.

That second point is worth a look regardless of the test.

---

## 4. DV acceptance — still failing, and it is NOT the database

Ruled out empirically:

- **Not permissions** — passes as uid 1000 in isolation (1 passed, 1.33s)
- **Not DB pollution** — Layer 1's isolation fixture works; verified `DatabaseManager()` with an omitted path resolves per-test, and DV passes after a full real-app module (72 passed)
- Passes locally in isolation, in its own file, and after other DV files

**Leading hypothesis: a module-level singleton that Layer 1 does not isolate.** `backend/api/dependencies.py:190` has `registry = ServiceRegistry()` — a process-wide global — and the test reaches straight into it:

```python
pm = registry._plex_service.plex_manager      # tests/test_dv_acceptance.py:110
... registry.db, pm, registry.config ...      # :125
```

Layer 1 added a teardown that clears `registry.db` when the path matches, but `_plex_service`, its `plex_manager`, and `config` persist for the whole session. In a 3760-test run, any earlier test that touches Plex state could leave `plex_manager` in a condition where the sync finds nothing to label — hence `addLabel` called 0 times.

I could not reproduce CI's ordering locally: the full suite stalls in my environment (a local-only condition — CI completes it in 171s), so I cannot bisect the polluting test from here. If you want that bisected, the practical route is a CI run with `-p no:randomly` and `--durations`, or reproducing in a Linux container that doesn't hit my local stall.

---

## 5. Playwright — root cause established, NOT a product bug

Per instruction I changed no frontend code, loosened no assertion, and added no waits. I stood up the real stack (backend in a container publishing 9721, Vite dev on 5174) and instrumented a **cold-start** navigation, since CI always starts cold.

```
ms      title                pathname  readyState  hydrated
 4247   ""                   /         complete    false
 5020   ""                   /         complete    false     ← 5000ms assertion deadline passes here
 6586   ""                   /         complete    false
 7370   ""                   /         complete    false
 7695   "Scan | ScanHound"   /         complete    true      ← settles correctly
--- console errors ---   (none)
--- page errors ---      (none)
--- failed/4xx requests --- (none)
```

**Findings against your checklist:**

- `page.url()` / pathname: **`/` the entire time.** No navigation, no redirect, no reload.
- Document readiness: `complete` from 4.2s.
- Hydration: completes at **~7695 ms**, and the title is correct the moment it does.
- Console errors, page errors, failed requests, non-2xx responses: **none**.
- Backend: started cleanly, `/health` 200.

`expect(page).toHaveTitle()` uses the default **5000 ms** timeout. Hydration finishes ~2.7s after that deadline. The app is correct; the assertion simply expires first.

**This also explains why only `/` failed in CI while the other four routes passed.** `/` is first in the spec's list, so it absorbs the cold Vite on-demand compile; by the time the suite reaches `/downloads`, the server is warm. And CI's `"App | ScanHound"` intermediate is the same phenomenon caught slightly later in hydration — the layout has rendered but `$page` isn't populated, so `routeTitles[undefined]` falls through to the `'App'` fallback at `+layout.svelte:57`.

Corroborating detail: the dev server's served HTML contains **no `<title>` element at all** — the title only exists post-hydration. That is the `""` CI observed.

For completeness: `npx playwright test --project=desktop --grep "/ loads" --trace on` **passes locally (1 passed, 7.7s)** — because my Vite was already warm. The failure only appears cold, which is exactly CI's condition.

**Fix options (yours to choose):** warm the app in a global setup before the smoke specs; raise `expect` timeout for these route checks; or run the specs against a production build (`build` + `preview`) instead of the dev server, eliminating on-demand compilation entirely. I did not implement any of them.

---

## 6. Advisory finding in PR #10

`test_default_database_path_is_function_scoped` was inserted into **`tests/conftest.py`**. Pytest does not collect tests from `conftest.py`, so it **never runs** in a normal suite — confirmed: it is absent from `--collect-only`, and passes only when addressed directly by nodeid (`1 passed`).

Not blocking — the isolation behavior itself is proven working — but the regression guard is inert where it sits and should move to a real test module.

---

## 7. State

| Ref | SHA | Note |
|---|---|---|
| `main` | `58feedf` | untouched |
| `fix/ci-baseline` (PR #9) | `fac474b` | **unchanged**, draft |
| `fix/ci-test-isolation` (PR #10) | `2f91b8f` | new, draft |
| `fix/case-insensitive-dedupe` (PR #11) | `c9d2721` | new, draft |
| All `agent/hdencode-*` (PRs #3–#8) | unchanged | untouched, draft |

Nothing merged. No force-pushes. No PR marked ready. No frontend code changed. Working tree clean apart from pre-existing untracked review docs; all temporary worktrees, containers, and the diagnostic probe script removed.

---

## 8. What would help next

1. **Trash test** — decide test-side vs production-side (§3). The production question about unprivileged operation is worth answering on its own merits.
2. **DV acceptance** — needs the `registry` singleton hypothesis confirmed and the polluting test identified; best done in CI or a Linux container.
3. **Playwright** — pick one of the three options in §5.
4. **PR #10** — relocate the inert regression test out of `conftest.py`.
