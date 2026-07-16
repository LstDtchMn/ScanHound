# Audit Remediation Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 10 findings verified real at HEAD from the external audit + Codex-share (data-integrity, security, correctness/UX scope).

**Architecture:** Seven independent, surgical fixes across auth/WS, rename apply(), fileops trash discovery, Plex snapshot completeness, pipeline package verification, frontend correctness, and a test sync. Each is minimal — no opportunistic refactoring. Root causes were empirically established this session (executed repros); this plan turns each verified fix sketch into TDD steps.

**Tech Stack:** Python 3.12 / FastAPI / sqlite3 backend; SvelteKit 5 runes + Tailwind frontend; pytest + vitest.

**Spec:** `docs/superpowers/specs/2026-07-14-audit-remediation-batch-design.md`

## Global Constraints

- TDD: failing test first, watch it fail, implement, watch it pass, commit. Backend pytest runs in a THROWAWAY `scanhound:latest` container: `docker run -d --name <uniq> --entrypoint sleep scanhound:latest infinity`; `docker cp backend/. <n>:/app/backend/` and `docker cp tests/. <n>:/app/tests/` (the `/.` contents form — a bare-dir copy nests `backend/backend`); `docker exec <n> pip install -q pytest pytest-timeout httpx`; `MSYS_NO_PATHCONV=1 docker exec -w /app <n> python -m pytest <files> -v --timeout=60`; `docker rm -f <n>` after. Never bind-mount for tests; never write the live `scanhound` container DB.
- Tests that read a frontend file (e.g. `tests/test_dv_settings.py::test_all_frontend_editable_settings_keys_are_in_model`, `tests/test_config.py`): `docker cp` the referenced frontend file into the container too, else FileNotFoundError (a harness artifact, not a real failure).
- Frontend (`npm run check`/`build`/`vitest`) runs on HOST in `frontend/`.
- Windows Git Bash: prefix `docker exec` with `MSYS_NO_PATHCONV=1` when container paths are arguments.
- NEVER curly/smart quotes in any source file — straight ASCII only.
- Never run the full unfiltered `tests/test_api_routes.py` (hangs on unmocked network tests) — run changed-module subsets.
- Read the actual current file at the cited lines before editing — line numbers shift between tasks.
- Deploy and push are OUT of scope for every task (user-gated).
- Preserve all behavior not named in a task. Minimal surgical changes only.

---

### Task 1: Rename destructive-op recovery (SH-H09 + SH-H08 + process_package confinement)

**Files:**
- Modify: `backend/rename/service.py` — `apply()` overwrite path (existing-dest trash ~1384-1389, place_file ~1462-1466, failure branch ~1467-1478, success DB write ~1482-1487); `process_package` (~line 576)
- Test: `tests/test_rename_service.py`

**Interfaces:**
- Consumes: `_fileops._trash(path) -> str` (returns the destination path inside the trash bucket), `_fileops.restore_trash_entry(bucket, name, roots) -> dict`, `_fileops.all_trash_roots()`, `_fileops.undo_place(...)` (the existing exception-path rollback), `db.update_rename_job(...) -> bool` (returns False on failure, never raises), `self._translate_path(path)`, and `_within`-style confinement (see `backend/api/routes/rename.py:_require_within_roots`/`_within` at `backend/api/main.py:426-437`).
- Produces: no new public signatures; behavior changes only.

**Read first:** the full `apply()` method and `process_package` in `backend/rename/service.py`, plus `_trash`/`restore_trash_entry`/`all_trash_roots` in `backend/rename/fileops.py`, to get exact current line numbers and the trash-bucket return contract.

#### Sub-fix A — SH-H09: overwrite restores the trashed original on placement failure

- [ ] **Step 1: Failing test** — in `tests/test_rename_service.py`, add a test that on the OVERWRITE path, if `place_file` raises after the existing destination was trashed, the original is restored to `dst`. Use the file's real `_service(db, ...)` factory and `db` fixture. Set up: a real temp library with an existing destination file and an incoming source file; a `matched` job whose apply uses overwrite; monkeypatch `backend.rename.service._fileops.place_file` to raise `OSError("disk full")`. Assert after apply: `os.path.isfile(dst)` is True (original restored, same bytes), the job status is `failed`, and the result is `{"ok": False, ...}`.

- [ ] **Step 2: Run, verify FAIL** (original NOT restored → `os.path.isfile(dst)` False). `MSYS_NO_PATHCONV=1 docker exec -w /app <n> python -m pytest tests/test_rename_service.py -k overwrite_restores -v --timeout=60`

- [ ] **Step 3: Implement** — in `apply()`'s overwrite path: capture the trash destination returned by `_fileops._trash(dst)` (bind it, e.g. `trashed_to = _fileops._trash(dst)`; derive `bucket = os.path.basename(os.path.dirname(trashed_to))`, `name = os.path.basename(trashed_to)`). In the placement-failure `except` branch (before marking `failed`), if a trash happened this call, attempt `_fileops.restore_trash_entry(bucket, name, _fileops.all_trash_roots())` to put the original back at `dst`. If restore succeeds, proceed to mark failed as today. If restore ITSELF fails, set a loud `error_message`/warning naming the now-empty library slot and the exact trash path (mirror the `restore_warning` pattern used in `undo()`), then mark failed. Do not swallow — the user must see that the slot is empty.

- [ ] **Step 4: Run, verify PASS.** Add a second test where `restore_trash_entry` is also monkeypatched to fail: assert the job's `error_message` contains the stranded trash path and the word "moved"/"empty" (whatever the loud message uses), and status is `failed`.

- [ ] **Step 5: Commit** — `git add backend/rename/service.py tests/test_rename_service.py && git commit -m "fix(rename): restore the trashed original when an overwrite placement fails (SH-H09)"`

#### Sub-fix B — SH-H08: check the post-placement DB write

- [ ] **Step 6: Failing test** — a test that if the final `db.update_rename_job(job_id, status="applied", ...)` returns False (DB write silently failed), `apply()` does NOT return `{"ok": True}` and the placed file is rolled back / job left consistent-recoverable (not stuck `applying`). Use a fake/db-wrapper whose `update_rename_job` returns False on the `status="applied"` write while a real `place_file` copies the file. Assert: result is not `{"ok": True}`, and the file was rolled back (or the job ends `failed`, matching the exception-path behavior).

- [ ] **Step 7: Run, verify FAIL** (currently returns `{"ok": True}`, job stuck `applying`).

- [ ] **Step 8: Implement** — treat a False return from the final `status="applied"` `update_rename_job` as failure: run the same `_fileops.undo_place(...)` rollback + failed-status handling already coded for the exception case (factor a small local helper or raise an internal exception caught by the existing except, whichever is cleaner in the real code). `apply()` must never report success when the applied-status write didn't persist.

- [ ] **Step 9: Run, verify PASS**, then run the whole `TestApplyUndo`/overwrite/conflict test classes to confirm no regression: `MSYS_NO_PATHCONV=1 docker exec -w /app <n> python -m pytest tests/test_rename_service.py -k "Apply or Conflict or Overwrite or overwrite_restores" -v --timeout=90`

- [ ] **Step 10: Commit** — `git commit -am "fix(rename): treat a failed post-placement DB write as failure, not success (SH-H08)"`

#### Sub-fix C — process_package path confinement

- [ ] **Step 11: Failing test** — a test that `process_package(name, save_to)` where the translated `save_to` resolves OUTSIDE all configured library/download roots is SKIPPED (no jobs created) with a logged warning, while an in-root `save_to` is processed normally. (Configure roots via the `_service(db, ..., auto_rename_movie_library=..., auto_rename_tv_library=...)` factory kwargs and a temp dir.)

- [ ] **Step 12: Run, verify FAIL** (currently processes any path).

- [ ] **Step 13: Implement** — in `process_package`, after `resolved = self._translate_path(save_to)`, confine `resolved` to the configured roots using the `_within` containment rule (real-prefix check, `path == base or path.startswith(base + os.sep)`, after `os.path.normpath`). Gather roots from the configured library/download settings the service already reads. If `resolved` is outside all roots, `logger.warning(...)` and return early (skip) rather than walking it. This is a background path — log-and-skip, never raise HTTP.

- [ ] **Step 14: Run, verify PASS**; run the whole `TestProcessPackage`/`TestProcessFolder` classes for regression.

- [ ] **Step 15: Commit** — `git commit -am "fix(rename): confine process_package's translated save_to to configured roots (Codex path caveat)"`

---

### Task 2: Trash discoverability (SH-H05 trash, Codex Critical)

**Files:**
- Modify: `backend/rename/fileops.py` — `all_trash_roots()` (POSIX branch, ~line 424-425)
- Test: `tests/test_rename_core.py` (where `TestFileOps` trash tests live) or `tests/test_fileops_dedupe.py` — check which file holds the existing trash tests and follow it.

**Interfaces:**
- Consumes: `_trash_root_for(path)`, `_TRASH_ROOT`, `list_trash_entries(roots)`, `restore_trash_entry(bucket, name, roots)`.
- Produces: `all_trash_roots()` returns, on POSIX, `_TRASH_ROOT` + a `<mountpoint>/.scanhound-trash` for every mount in `/proc/self/mountinfo` (still `sorted(set(...))`). Windows branch unchanged.

**Read first:** `all_trash_roots()`, `_trash_root_for()`, and `list_trash_entries()` in `backend/rename/fileops.py`. Current POSIX branch is just `roots.add(_trash_root_for("/"))` — that's the bug.

- [ ] **Step 1: Failing test** — with a monkeypatched mount source (see Step 3 for the seam), assert `all_trash_roots()` includes `<mp>/.scanhound-trash` for several mountpoints (e.g. `/`, `/library/movies`, `/library/tv`), plus `_TRASH_ROOT`. A second test: a malformed/empty mount source still returns at least `_TRASH_ROOT` and the `/` candidate and never raises. An integration test (throwaway container, real fileops): make two dirs on the same device simulating separate roots — since a container is single-device, instead assert the WIRING: monkeypatch the mountinfo reader to report a temp dir as a "mount", `_trash()` a file whose `_trash_root_for` lands under that temp dir, then assert `list_trash_entries(all_trash_roots())` finds it (previously `[]`).

- [ ] **Step 2: Run, verify FAIL** (POSIX branch only returns `/` + app-data; the extra mountpoints are absent).

- [ ] **Step 3: Implement** — add a small module-level helper `_posix_mount_points() -> list[str]` that reads `/proc/self/mountinfo` (field 5 is the mount point; fall back to `/proc/mounts` field 2; return `["/"]` on any error/empty), and have the POSIX branch of `all_trash_roots()` do: for each mount point, `roots.add(os.path.join(mp, ".scanhound-trash"))`, then always also `roots.add(_trash_root_for("/"))` and keep `_TRASH_ROOT`. Make `_posix_mount_points` the single monkeypatch seam the tests use. Downstream `list_trash_entries`/`sweep` already `isdir()`-filter, so non-existent candidates are harmless.

- [ ] **Step 4: Run, verify PASS**; run the whole trash test class for regression.

- [ ] **Step 5: Commit** — `git add backend/rename/fileops.py tests/ && git commit -m "fix(trash): enumerate all POSIX mount points so library-volume trash is discoverable (SH-H05 trash)"`

---

### Task 3: Plex item-level prune guard (SH-H07)

**Files:**
- Modify: `backend/plex_service.py` — movie loop (`_extract_movie_data` ~440-516, caller loop ~249-277) and TV loop (`tv_errors`/`tv_extract_fail` ~307-368); the `movies_load_incomplete`/`tv_load_incomplete` flags (~230-231) and the cache-save gate (~396-423)
- Test: `tests/test_plex_service.py`

**Interfaces:**
- Consumes: existing `movies_load_incomplete`/`tv_load_incomplete` flags and the `save_plex_cache(..., full_replace=...)` gate that already skips full-replace when a flag is set.
- Produces: a `movie_extract_fail` counter; both content-type incomplete flags now also set on ANY per-item extraction failure.

**Read first:** `plex_service.py` lines ~230-423 — the movie caller loop, `_extract_movie_data`'s internal try/except, the TV loop's `tv_errors`/`tv_extract_fail`, the two incomplete flags, and the cache-save gate. Confirm exact current line numbers.

- [ ] **Step 1: Failing test** — mirror the existing `test_partial_movie_library_load_does_not_full_replace_cache`, but for a SINGLE item failing inside an otherwise-successful library load: mock 10 movies where movie #3's media access raises (caught inside `_extract_movie_data`, returns None). Assert `save_plex_cache` is called with `full_replace=False` (NOT True). Add the TV analogue: one show's `.seasons()` raises; assert TV save uses `full_replace=False`. Add a control: a fully-successful load still uses `full_replace=True`.

- [ ] **Step 2: Run, verify FAIL** (currently `full_replace=True` on a single-item failure — the item's cache row would be pruned).

- [ ] **Step 3: Implement** — add a `movie_extract_fail` counter incremented whenever `_extract_movie_data` returns None for an item that had media data (mirror the existing `tv_extract_fail`). Set `movies_load_incomplete = True` when `movie_extract_fail > 0`, and set `tv_load_incomplete = True` when `tv_errors`/`tv_extract_fail > 0` (fold the existing per-show counters into the flag, not just the whole-library exception). Leave the unconditional in-memory snapshot overwrite (~383-385) as-is (out of scope per spec).

- [ ] **Step 4: Run, verify PASS**; run the whole `tests/test_plex_service.py` for regression.

- [ ] **Step 5: Commit** — `git add backend/plex_service.py tests/test_plex_service.py && git commit -m "fix(plex): mark snapshot incomplete on item-level extraction failure so full-replace does not prune owned media (SH-H07)"`

---

### Task 4: Pipeline package verification (SH-H10)

**Files:**
- Modify: `backend/pipeline_service.py` — `_categorize_from_rename_rows` verified branch (~172-192)
- Test: `tests/test_pipeline_service.py`

**Interfaces:**
- Consumes: `find_plex_match(db, imdb_id, title, year, season, resolution)`; the category vocabulary (`verified`, `awaiting_plex_refresh`) established by the pipeline-redesign feature.
- Produces: `verified` only when ALL applied rows match in Plex; `awaiting_plex_refresh` (non-terminal) with an "N/M in Plex" detail when partial.

**Read first:** `backend/pipeline_service.py` `_categorize_from_rename_rows` — the `all(status == "applied")` branch that currently picks `latest = max(...)` and checks one identity.

- [ ] **Step 1: Failing test** — a package with 3 applied rename rows (distinct identities/seasons). Case A: all 3 present in Plex (stub `find_plex_match` to match all) → category `verified`. Case B: only 2 of 3 present → category `awaiting_plex_refresh` (NOT `verified`), with a `detail` conveying "2/3". Case C: single-row package, its one identity present → still `verified` (unchanged). Follow the file's existing categorize-test conventions (`_download_row()`/`_rename_row()` helpers, stubbed `find_plex_match`).

- [ ] **Step 2: Run, verify FAIL** (Case B currently returns `verified` off the latest row alone).

- [ ] **Step 3: Implement** — in the `all(status == "applied")` branch, run `find_plex_match` for EVERY row's identity (not just `latest`). If all match → `("verified", None, package_uuid, <latest rating_key>)`. If some match and some don't → `("awaiting_plex_refresh", f"{k}/{n} in Plex", package_uuid, None)` (non-terminal, stays reconcilable). Keep the grace-window / `processed_at`-missing guards exactly as they are before this all-applied check. Single-row packages naturally still return `verified` on their one match.

- [ ] **Step 4: Run, verify PASS**; run the whole `tests/test_pipeline_service.py` for regression.

- [ ] **Step 5: Commit** — `git add backend/pipeline_service.py tests/test_pipeline_service.py && git commit -m "fix(pipeline): verify a package only when every applied item is in Plex (SH-H10)"`

---

### Task 5: Auth/WS security (SH-H01 + bcrypt cost cap)

**Files:**
- Modify: `backend/api/ws.py` (guard ~93-96), `backend/api/routes/auth.py` (`/auth/status` ~80-92), `backend/auth_service.py` (`verify_password` ~42-49), frontend auth surface (`frontend/src/routes/+layout.svelte`, `frontend/src/routes/login/+page.svelte`, `frontend/src/lib/stores/*` auth store, `frontend/src/lib/api/client.ts`)
- Test: `tests/test_api_ws.py`, `tests/test_api_auth.py` (or wherever auth-status tests live), `tests/test_auth_service.py` (or wherever `verify_password` is tested); frontend vitest if a routing/store test fits.

**Interfaces:**
- Consumes: `backend.api.dependencies.auth_enabled()`, `token_authorized(token)`, `allow_open()` (all confirmed present at dependencies.py:197/232/219).
- Produces: WS fail-closed by default; `/auth/status` gains `setup_required: bool`; `verify_password` rejects non-canonical / over-cost hashes.

#### Sub-fix A — WS fail-closed

- [ ] **Step 1: Failing test** — in `tests/test_api_ws.py`: with an empty-credential DB and `SCANHOUND_ALLOW_OPEN` unset, a no-token WS connect is REJECTED (closed 1008). With `SCANHOUND_ALLOW_OPEN=1`, a no-token WS connect is ACCEPTED. NOTE: the existing test that codifies open no-credential WS access has its expectation INVERTED by this fix — update it (rename to reflect fail-closed) rather than leave it asserting the old behavior.

- [ ] **Step 2: Run, verify FAIL** (current guard accepts the no-credential socket).

- [ ] **Step 3: Implement** — `backend/api/ws.py`: import `allow_open` alongside `auth_enabled, token_authorized`, and replace the guard:
  ```python
  from backend.api.dependencies import auth_enabled, token_authorized, allow_open
  if not token_authorized(token):
      if auth_enabled() or not allow_open():
          await ws.close(code=1008, reason="Unauthorized")
          return
  ```
  This denies the empty-credential socket by default and opens both transports only under `SCANHOUND_ALLOW_OPEN=1`.

- [ ] **Step 4: Run, verify PASS.**

#### Sub-fix B — /auth/status setup_required + frontend redirect

- [ ] **Step 5: Failing test** — `/auth/status` returns `setup_required: true` when no password/nonce exists AND `allow_open()` is False; `setup_required: false` once a password is set (and when `allow_open()` is True). Add to the auth-status test module.

- [ ] **Step 6: Run, verify FAIL** (no such field).

- [ ] **Step 7: Implement backend** — `backend/api/routes/auth.py` `/auth/status`: add `setup_required = (not has_password and not nonce and not allow_open())` to the response. Keep `auth_required` as-is for back-compat.

- [ ] **Step 8: Implement frontend** — where the app reads `/auth/status` (`+layout.svelte`), when `setup_required` is true, route to the login/set-password surface (a one-time "set your password" prompt) instead of entering the app or bouncing to `/login`. In `login/+page.svelte`, when `setup_required` is true do NOT `goto('/')` (that's the loop) — show the set-password form. Reuse existing components; add no new bootstrap-token flow. Verify `npm run check` passes.

- [ ] **Step 9: Run frontend check** — `cd frontend && npm run check` (0 errors) and `npx vitest run` (all pass; add a store/routing unit test for the setup_required branch only if the existing test pattern supports it cleanly — otherwise a code trace + check suffices).

#### Sub-fix C — bcrypt cost cap

- [ ] **Step 10: Failing test** — `tests/test_auth_service.py`: `verify_password("pw", <cost-15 bcrypt hash>)` returns False WITHOUT the multi-second delay (assert it returns fast, e.g. under 0.5s, and returns False); `verify_password("pw", <non-$2 string>)` returns False; a normal cost-12 hash of "pw" still verifies True for "pw". (Generate the cost-15 hash once with `bcrypt.hashpw(b"pw", bcrypt.gensalt(15))` inside the test.)

- [ ] **Step 11: Run, verify FAIL** (the cost-15 case currently runs the expensive check / no cap).

- [ ] **Step 12: Implement** — `backend/auth_service.py` `verify_password`: before `bcrypt.checkpw`, `import re`; if the stored hash does not match `^\$2[aby]\$\d{2}\$` OR its parsed cost (the two digits after the 3rd `$`) is > 14, return False immediately (fail closed, no expensive check). Otherwise proceed as today.

- [ ] **Step 13: Run, verify PASS**; run the whole auth test module(s) for regression: `MSYS_NO_PATHCONV=1 docker exec -w /app <n> python -m pytest tests/test_api_ws.py tests/test_api_auth.py tests/test_auth_service.py -v --timeout=60` (adjust filenames to the real ones).

- [ ] **Step 14: Commit** — `git add backend/api/ws.py backend/api/routes/auth.py backend/auth_service.py frontend/ tests/ && git commit -m "fix(auth): WS fails closed with no credential + setup_required breaks the fresh-install redirect loop + bcrypt cost cap (SH-H01, Codex 1c)"`

---

### Task 6: Frontend correctness (SH-M04 + SH-M05 + SH-M06)

**Files:**
- Modify: `frontend/src/lib/grouping.ts` (~line 25), `backend/api/routes/results.py` (~558-562), `frontend/src/routes/settings/+page.svelte` (~16 `parseInt(...) || default` sites: audit cited 284, 326, 544, 768, 991, 1005, 1019, 1033, 1047, 1061, 1173, 1255, 1357, 1464, 1512, 1892 — verify each at HEAD), `frontend/src/lib/stores/settings.ts` (~16-47)
- Test: `frontend/src/lib/grouping.test.ts` (or create), settings-input vitest, `tests/test_results_routes.py` for the backend count if one exists

**Interfaces:** none new; behavior only.

**Read first:** `grouping.ts`, `results.py:113-120` (the canonical `group_key` builder) and `:558-562` (the bare-title count), `settings.ts`, and grep `settings/+page.svelte` for `parseInt(` to get the real current set of sites.

#### SH-M04 — group by group_key

- [ ] **Step 1: Failing test** — grouping vitest: two items with the same `title` but different `year` (Dune 1984 vs 2021), each carrying a distinct `group_key`, stay in SEPARATE groups; two genuine variants sharing a `group_key` still group together. Backend: if a results-count test module exists, a test that same-title/different-year rows count as 2, not 1.

- [ ] **Step 2: Run, verify FAIL** (bare-title grouping merges them).

- [ ] **Step 3: Implement** — `grouping.ts`: key on `item.group_key` (fallback to a composite `${title}|${year}|S${season}` for any legacy row lacking `group_key`). `results.py:558-562`: count by `group_key` with the same fallback.

- [ ] **Step 4: Run, verify PASS.**

#### SH-M05 — numeric zero inputs

- [ ] **Step 5: Failing test** — a vitest (or a focused DOM test if the pattern exists) asserting that typing `0` into a min-0 numeric setting persists `0`, not the default. If a direct component test is impractical, extract the parse into a tiny pure helper `numOrDefault(raw, fallback)` and unit-test THAT (`numOrDefault("0", 30) === 0`, `numOrDefault("", 30) === 30`, `numOrDefault("abc", 30) === 30`, `numOrDefault("45", 30) === 45`), then use it at every site.

- [ ] **Step 6: Run, verify FAIL.**

- [ ] **Step 7: Implement** — replace every remaining `parseInt(x) || default` on a min-0 field in `settings/+page.svelte` with the NaN-aware form (`const v = parseInt(x, 10); ... Number.isNaN(v) ? fallback : v`, or the `numOrDefault` helper). Leave the one already-fixed pipeline grace-margin site as-is. Do NOT touch fields whose true minimum is 1+ where 0 is genuinely invalid (verify each; the audit's list is min-0 fields).

- [ ] **Step 8: Run, verify PASS.**

#### SH-M06 — settings error propagation

- [ ] **Step 9: Failing test** — `settings.ts`: a `saveSettings` whose API call rejects surfaces failure to the caller (returns `false`/rejects) rather than resolving as success; same for `loadSettings`.

- [ ] **Step 10: Run, verify FAIL** (currently swallows, resolves normally).

- [ ] **Step 11: Implement** — `settings.ts` `loadSettings`/`saveSettings`: return a success/failure result (boolean) or rethrow, so callers can show a real error/retry state. Update callers in `settings/+page.svelte` to block "Test" actions / show an error when save fails (minimal — surface the failure; keep the toast).

- [ ] **Step 12: Run** — `cd frontend && npm run check && npm run build && npx vitest run` (all pass, 0 errors).

- [ ] **Step 13: Commit** — `git add frontend/ backend/api/routes/results.py tests/ && git commit -m "fix(frontend): group by canonical group_key, keep zero-valued numeric settings, propagate settings errors (SH-M04/M05/M06)"`

---

### Task 7: Test sync — test_config expected keys (SH-H11)

**Files:**
- Modify: `tests/test_config.py` (`TestDefaultConfig::test_default_config_has_no_unexpected_keys` expected-key set)
- Test: the same test.

**Read first:** `tests/test_config.py` — how `EXPECTED_DEFAULT_KEYS` (or equivalent) is built and compared against `_DEFAULT_CONFIG`.

- [ ] **Step 1: Reproduce the failure** — run `MSYS_NO_PATHCONV=1 docker exec -w /app <n> python -m pytest tests/test_config.py::TestDefaultConfig::test_default_config_has_no_unexpected_keys -v --timeout=60`. Expected: FAIL naming `plex_library_path_mappings`, `pipeline_verify_grace_margin_minutes`, `pipeline_reconcile_enabled`, `rename_detect_moved_files_enabled`.

- [ ] **Step 2: Implement** — add those 4 keys to the test's expected-key set. If the test's structure makes it feasible to derive the guard so a documented new key won't silently desync (e.g. compare `_DEFAULT_CONFIG` keys against a single authoritative list the config module exports), prefer that; otherwise add the 4 keys with a short comment tying them to their features.

- [ ] **Step 3: Run, verify PASS** — same command, now passes.

- [ ] **Step 4: Commit** — `git add tests/test_config.py && git commit -m "test: sync test_config expected keys with config keys added this session (SH-H11)"`

---

## Self-review notes

- **Spec coverage:** Fix 1→Task 5A/5B; Fix 2→Task 5C; Fix 3→Task 1A; Fix 4→Task 1B; Fix 5→Task 2; Fix 6→Task 3; Fix 7→Task 4; Fix 8→Task 1C; Fix 9→Task 6; Fix 10→Task 7. All 10 covered.
- **Sequencing:** Task 1 (rename apply/process_package) and no other task touch `service.py`, so ordering among tasks is flexible EXCEPT Task 1's internal 3→4→8 order (all edit `apply()`/`process_package`). Recommended run order: 1, 2, 3, 4, 5, 6, 7 (data-integrity first, frontend/test last).
- **Type consistency:** category strings (`verified`, `awaiting_plex_refresh`) match the pipeline-redesign feature; `find_plex_match` signature matches Task 4's use; `allow_open`/`auth_enabled`/`token_authorized` confirmed importable from `backend.api.dependencies`.
