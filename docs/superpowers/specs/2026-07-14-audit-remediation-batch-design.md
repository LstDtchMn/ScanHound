# Audit Remediation Batch — Design Spec

**Date:** 2026-07-14
**Status:** Design approved (3 key decisions confirmed by user); pending spec review
**Baseline:** current HEAD `a582838`

## Origin

An external comprehensive engineering audit (reviewed commit `a4090c3`) plus an
OpenAI-Codex chat-share produced ~40 findings. Each was empirically re-verified
against current HEAD via verification workflows this session (executed repros,
not opinion). Findings that were already-handled, not-applicable, or Codex-branch
inventions were discarded. This spec covers ONLY the findings confirmed genuinely
real at HEAD and in the user-approved "data-integrity + security + correctness/UX"
scope. Severities are recalibrated for the actual deployment: single-user,
Docker, behind an app-password login (Cloudflare Access removed 2026-06-27), not
the public multi-user release the audit assumed.

Explicitly OUT of scope (deferred, separate future effort): scraper root/no-sandbox
isolation (SH-H03), SSRF egress policy (SH-H04), token-in-WS-URL migration to
cookies/tickets (SH-H02), Tauri packaging (SH-H05 — Docker is the deployment),
server-scheduler audit (SH-H06), one-use bootstrap token, CI-lane overhaul,
dependency pinning, docs rewrite, hotspot refactors.

## Global constraints

- Every fix is TDD: failing test first, then the change. Backend tests run in a
  throwaway `scanhound:latest` container (docker cp code in; never bind-mount;
  `pip install pytest pytest-timeout httpx`); frontend on host. Never write to
  the live `scanhound` container DB.
- No curly/smart quotes in any source file — straight ASCII only.
- Preserve all existing behavior not named here. Each fix is minimal and
  surgical; no opportunistic refactoring.
- Full `tests/test_api_routes.py` hangs on unmocked network tests — run changed-
  module subsets, never the whole unfiltered file.

---

## Fix 1 — WebSocket fails open + fresh-install redirect loop (SH-H01)

**Root cause (verified):** `backend/api/ws.py:93-96` guards with
`if auth_enabled() and not token_authorized(token): close`. `auth_enabled()`
returns False whenever no password and no nonce exist (fresh install / wiped
`auth_credentials` — which happened in prod on 2026-06-29). The `and`
short-circuits, so an empty-credential socket is accepted with NO token check.
HTTP was hardened to fail *closed* in that same state
(`backend/api/main.py:400-423`) but the WS path was never updated. Empirically
confirmed: on an empty-cred DB, `GET /settings` → 401 while `/ws` → connected,
streaming download URLs / rename-job paths / notifications.

Separately, `/auth/status` (`backend/api/routes/auth.py:80-92`) still reports
`auth_required=false` in that state, so the frontend (`+layout.svelte`) proceeds,
its protected fetches 401, it `goto('/login')`, login re-checks status, sees
`auth_required=false`, `goto('/')` → infinite loop on a fresh install.

**Fix:**
1. `backend/api/ws.py`: replace the guard with fail-closed logic mirroring HTTP —
   reject unless the token is authorized, allowing the empty-credential socket
   only when `SCANHOUND_ALLOW_OPEN=1`. Import `allow_open` alongside the existing
   `auth_enabled`/`token_authorized`:
   ```python
   if not token_authorized(token):
       if auth_enabled() or not allow_open():
           await ws.close(code=1008, reason="Unauthorized")
           return
   ```
   This makes both transports deny by default in the no-credential state and both
   open identically under `SCANHOUND_ALLOW_OPEN=1`.
2. `backend/api/routes/auth.py` `/auth/status`: add a `setup_required: bool`
   field — true when no credential exists AND the fail-closed gate is active
   (i.e. not `allow_open()`). Keep `auth_required` semantics for back-compat.
3. Frontend: when `setup_required` is true, route to the existing login/set-password
   surface (a one-time "set your password" prompt) instead of the normal app,
   breaking the `/`↔`/login` loop. Reuse existing components; no new bootstrap-token
   flow (deferred).

**Out of scope here:** one-use bootstrap token, Origin validation, moving the
token out of the WS URL. The `/auth/set-password` first-run exemption stays as-is.

**Testing:** WS test asserting a no-credential socket is REJECTED by default and
ACCEPTED under `SCANHOUND_ALLOW_OPEN=1` (mirrors `tests/test_api_ws.py`, which
currently codifies the open behavior — that test's expectation is inverted by
this fix and must be updated). Auth-status test asserting `setup_required` true
on empty DB, false once a password is set. Frontend: a store/routing unit test
for the setup_required branch if the existing test pattern supports it, else
`npm run check` + manual trace.

---

## Fix 2 — bcrypt cost cap + canonical-hash guard (Codex 1c)

**Root cause (verified):** `backend/auth_service.py:42-49` `verify_password`
safely returns False on garbage hashes (caught `ValueError`), but there is NO cap
on bcrypt cost. A cost-31 hash in `auth_credentials` would hang a login for hours
(measured: cost-18 ≈ 12s per verify). Unreachable through the app's own surface
(no route accepts a raw hash), so this is defense-in-depth against a mangled
manual DB restore.

**Fix:** in `verify_password`, before `bcrypt.checkpw`, validate the stored hash
is canonical bcrypt (`^\$2[aby]\$\d{2}\$`) and reject (return False, fail closed)
any parsed cost above a ceiling (14). Treat a non-canonical / absurd-cost row the
same as "no valid credential."

**Testing:** unit — a cost-15 hash and a non-`$2` hash both return False without
attempting the expensive check; a normal cost-12 hash still verifies correctly.

---

## Fix 3 — Overwrite restores the trashed original on failure (SH-H09)

**Root cause (verified, fault-injection repro):** on an overwrite,
`backend/rename/service.py` trashes the existing destination via
`_fileops._trash(dst)` BEFORE `place_file(src, dst)`. If placement raises, the
failure branch marks the job `failed` but never restores the trashed original →
the library path is left with NO file (reproduced: `dst exists: False`, original
stranded in `.scanhound-trash/<bucket>/`). Restore logic
(`_fileops.restore_trash_entry`) exists but is only wired into `undo()`.

**Fix:** capture the bucket/destination returned by `_trash(dst)`. In the
placement-failure except branch, before marking failed, restore the original to
`dst` (reuse `restore_trash_entry`, the same primitive `undo()` uses). Only if
the restore itself fails, surface a loud, explicit error naming the empty library
slot and the trash path (mirror `undo()`'s `restore_warning` pattern). Order:
this fix lands before Fix 4 (both edit the same `apply()`).

**Testing:** fault-inject `place_file` to raise on the overwrite path; assert the
original is restored byte-for-byte at `dst` and the job ends failed with a clear
message; a second test where restore *also* fails asserts the loud error naming
the stranded path.

---

## Fix 4 — Post-placement DB write is checked, not ignored (SH-H08)

**Root cause (verified, fault-injection repro):** `DatabaseManager._mutate`
returns False on any DB failure and never raises; the success-path
`db.update_rename_job(job_id, status="applied", ...)` return is ignored, and the
rollback runs only on a raised exception. Reproduced: file physically placed, job
stuck `applying` forever, `apply()` returned `{ok: True}`.

**Fix:** treat a False return from the final `status="applied"` write as failure —
run the same rollback (`undo_place`) + failed-status path already coded for the
exception case, so `apply()` never returns `{ok: True}` when the write didn't
persist. (`reset_applying_rename_jobs()` at startup is a partial safety net, not a
substitute.)

**Testing:** fault-inject the final `update_rename_job` to return False; assert
the placed file is rolled back and the job ends in a consistent failed/recoverable
state, not `{ok: True}`/`applying`.

---

## Fix 5 — Trash is discoverable by list/restore/sweep (Codex trash, Critical)

**Root cause (verified, repro):** `_trash()` sites buckets on the trashed file's
own mount (`<mount>/.scanhound-trash`), but `all_trash_roots()`
(`backend/rename/fileops.py:403-426`) — used by `/rename/trash` list, restore, and
the retention sweep — only checks `/` and the app-data root on POSIX. It never
enumerates the separate library/download mounts, so library-volume trash is
invisible: list returns `[]`, restore says "not found," sweep never ages it out
(it accumulates forever). This is the orphaned MKVs the user found in
`F:\Downloads\.scanhound-trash` via Explorer.

**Fix (decision: `/proc/mounts`-based):** on POSIX, `all_trash_roots()` reads
mount points from `/proc/self/mountinfo` (fallback `/proc/mounts`), computes
`<mountpoint>/.scanhound-trash` for each, and unions with `_TRASH_ROOT` (and the
existing `/` candidate). Downstream (`list_trash_entries` at fileops.py:309-311)
already `isdir()`-filters every root, so non-existent candidates cost one stat
each and can never surface phantom entries. This is the faithful POSIX parallel to
the Windows "every drive letter" branch and catches every volume trash can land
on. The Windows branch is unchanged.

**Testing:** unit — with a mocked `/proc/self/mountinfo` listing several
mountpoints, `all_trash_roots()` includes each `<mp>/.scanhound-trash`; an
integration-style test (throwaway container, real fileops) trashing a file under a
non-`/` mount then asserting `list_trash_entries(all_trash_roots())` finds it and
`restore_trash_entry` recovers it. Guard against a malformed/empty
`/proc/self/mountinfo` (returns at least `_TRASH_ROOT` + `/` candidate, never
raises).

---

## Fix 6 — Item-level Plex extraction failures mark the snapshot incomplete (SH-H07)

**Root cause (verified, repro):** the existing `movies_load_incomplete`/
`tv_load_incomplete` flags are set ONLY on a whole-library exception
(`plex_service.py:277`/`:368`); the cache-save gate skips `full_replace` when set.
But `_extract_movie_data` (`:440-516`) swallows per-item exceptions and returns
None with NO counter, and the TV per-show failure counters
(`tv_errors`/`tv_extract_fail`) are used only for a log line, never to set
`tv_load_incomplete`. So a transient glitch on a few items silently drops them,
the load "looks complete," and `full_replace=True` deletes their still-owned
`plex_cache` rows → owned media reads as missing until a later scan happens to
succeed. The earlier "all TV shows Missing" fix (commit `69475de`) addressed the
whole-library case only; this item-level case is untested and open.

**Fix:** add a `movie_extract_fail` counter (mirroring `tv_extract_fail`)
incremented whenever `_extract_movie_data` returns None for an item that had media
data. Fold BOTH the movie counter and the existing `tv_errors`/`tv_extract_fail`
into `movies_load_incomplete`/`tv_load_incomplete` — any per-item failure marks
the content-type incomplete, so the existing full-replace gate then protects the
durable cache from pruning that cycle. (The unconditional in-memory overwrite at
`:383-385` is left as-is for this batch — pruning the durable cache is the
data-loss vector; the in-memory snapshot self-heals on the next successful load.
Noted, not fixed here, to keep the change surgical.)

**Testing:** mirror `test_partial_movie_library_load_does_not_full_replace_cache`
but for a SINGLE item failing inside an otherwise-successful library load: assert
`full_replace` is NOT used and the pre-existing cache row for the failed item
survives. Same for a single TV show failing. A control test: a fully-successful
load still `full_replace`s.

---

## Fix 7 — Multi-file packages verified only when every item is in Plex (SH-H10)

**Root cause (verified):** `_categorize_from_rename_rows`
(`backend/pipeline_service.py` verified-branch, ~:172-192) fetches all package rows
but checks only `latest`'s identity in Plex, then returns terminal `verified` for
the WHOLE package. `verified` is excluded from all future reconcile passes
(`database.py get_downloads_needing_reconcile`). So a season pack shows complete as
soon as one episode's identity matches, permanently, even if others never arrive.

**Fix (decision: minimal-state, no schema change):** when all rows are `applied`,
run `find_plex_match` for EVERY row's identity. Return `verified` only if all
match. If some match and some don't, return `awaiting_plex_refresh` (the existing
non-terminal category from the pipeline-redesign feature) so it stays reconcilable
and re-checks next pass; carry a `detail` noting how many of N are present. The
rename rows already enumerate the expected items, so no new persistence is needed.

**Testing:** categorize tests — a 3-row package with all 3 in Plex → `verified`;
2 of 3 in Plex → `awaiting_plex_refresh` with a "2/3" detail, NOT terminal; single-
row package unchanged (still `verified` on its one match). A reconcile test that a
partial package remains eligible for the next pass.

---

## Fix 8 — process_package path confinement (Codex path caveat, Minor)

**Root cause (verified):** `RenameService.process_package` (the JD poll-loop entry,
`service.py:576`) calls `_translate_path` on JD's reported `save_to` with no
`_require_within_roots` confinement, unlike the `/process-folder` API route. JD's
`save_to` is admin/downloader-controlled (not attacker HTTP), so this is minor —
but the asymmetry is real.

**Fix:** after translating `save_to`, confine the resolved path to the configured
library/download roots before walking it (reuse the `_within` containment rule; a
service-side equivalent of `_require_within_roots` that logs-and-skips rather than
raising HTTP 422, since this is a background path, not a request). If the resolved
path is outside all known roots, log a warning and skip the package rather than
walking an arbitrary location. Lands after Fix 3/4 (same file).

**Testing:** unit — a `save_to` translating to a path outside all roots is skipped
with a logged warning; an in-root path is processed normally.

---

## Fix 9 — Frontend correctness trio (SH-M04, SH-M05, SH-M06)

**SH-M04 — same-title merge:** `frontend/src/lib/grouping.ts:25` groups by bare
`item.title`; `backend/api/routes/results.py:558-562` counts by bare title —
despite a canonical `group_key` (`{normalized_title}|{year}|S{season}`,
results.py:113-120). Dune 1984 and 2021 merge. **Fix:** key both on `group_key`
(with a composite fallback for any legacy row lacking it).

**SH-M05 — numeric zero inputs:** `parseInt(...) || default` at ~16 sites in
`settings/+page.svelte` discards a legitimately-typed `0` on min-0 fields (one site
already fixed for the pipeline grace-margin). **Fix:** replace every remaining
occurrence with a NaN-aware read (`const v = parseInt(x, 10); ... isNaN(v) ?
fallback : v`).

**SH-M06 — swallowed settings errors:** `frontend/src/lib/stores/settings.ts`
`loadSettings`/`saveSettings` catch and swallow, showing only a toast, so a failed
save looks successful. **Fix:** propagate a success/failure result (return a
boolean or rethrow) so callers can show a real error/retry state and block "Test"
actions after a failed save.

**Testing:** vitest for grouping (Dune 1984 vs 2021 stay distinct; genuine
variants still group); a settings-input test that `0` persists as `0`; a
settings-store test that a failed save surfaces failure. `npm run check` + build.

---

## Fix 10 — Sync test_config expected-keys (SH-H11)

**Root cause (verified):** `tests/test_config.py::TestDefaultConfig::
test_default_config_has_no_unexpected_keys` fails at HEAD with 4 keys missing from
its expected set — `plex_library_path_mappings`, `pipeline_verify_grace_margin_minutes`,
`pipeline_reconcile_enabled`, `rename_detect_moved_files_enabled` — all added to
`_DEFAULT_CONFIG` by this session's commits without updating the test. (The audit's
other 4 CI "failures" are environmental — PySide6 absent, Linux case-sensitivity,
non-root trash-root — not product defects; out of scope.)

**Fix:** add the 4 keys to the test's expected-key set. Prefer making the test
resilient to this whole class going forward — if feasible, derive the guard so a
new documented config key doesn't silently desync — but at minimum add the 4.

**Testing:** the test passes at HEAD after the change.

---

## Decomposition & sequencing (for the plan)

Grouped to minimize cross-task file conflicts; ordered by risk:

1. **Rename recovery** (Fix 3 + Fix 4 + Fix 8) — all edit `service.py apply()`/
   `process_package`; must be one coordinated task, sequenced internally 3→4→8.
2. **Trash discoverability** (Fix 5) — `fileops.py` only.
3. **Plex prune guard** (Fix 6) — `plex_service.py` only.
4. **Pipeline package verification** (Fix 7) — `pipeline_service.py`.
5. **Auth/WS security** (Fix 1 + Fix 2) — `ws.py`, `dependencies.py`, auth routes,
   `auth_service.py`, frontend auth surface.
6. **Frontend correctness** (Fix 9) — `grouping.ts`, `results.py`,
   `settings/+page.svelte`, `settings.ts`.
7. **Test sync** (Fix 10) — `tests/test_config.py`.

Independent tasks; the plan may split across more than one plan file if it grows
large, but one spec governs. Each task: TDD, adversarial review, then a final
whole-branch review. Deploy and push remain separate, user-gated.
