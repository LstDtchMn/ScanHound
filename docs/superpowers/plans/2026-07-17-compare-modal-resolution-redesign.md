# Compare Modal Resolution Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the Compare modal's implicit resolution buttons with an explicit In-Plex / Downloaded comparison and a Keep-Plex / Keep-downloaded / Keep-both choice, with full parity across both conflict kinds and inline recoverable-trash cleanup of the unused download.

**Architecture:** FastAPI backend `apply()` gains a `replace_library_dup` placement strategy (trash the existing library file, place the download) reusing the existing trash + `_restore_overwritten_original` machinery; a separate `resolve_keep_plex()` archives the job and trashes the redundant download (not a placement, so it bypasses the apply queue). SvelteKit `RenameReviewCard` renders the new labels + radio choice, mapping each choice to the right backend action via a pure `strategyForChoice(kind, choice)` helper.

**Tech Stack:** Python 3.12 / FastAPI / SQLite (`backend/rename/service.py`, `backend/database.py`, `backend/api/routes/rename.py`); SvelteKit 5 runes + Tailwind (`frontend/src/lib/...`). Tests: backend pytest in throwaway `scanhound:latest` container (docker cp, never bind-mount, live DB read-only); frontend vitest on host.

## Global Constraints

- Never hard-delete: all removals go through `_fileops._trash()` (recoverable). — copied from app invariant.
- `find_library_duplicate` is **movies-only**; `library_duplicate` kind never occurs for TV. `replace_library_dup` must no-op-guard non-movie / non-library_duplicate jobs.
- The shared `RenameReviewCard` renders in BOTH desktop `ConflictModal` and the mobile `RenameReviewDeck` — every change must hold at narrow widths.
- Deploy is `docker compose up -d --build` only (frontend baked into image); deploy/push are separate, user-gated steps — NOT part of this plan.
- Preserve existing behavior of `overwrite` / `keep_both` / `skip` and of `undo()` for non-`replace_library_dup` jobs.

---

### Task 1: Backend — `conflict_replaced_path` column + `replace_library_dup` strategy + undo symmetry

**Files:**
- Modify: `backend/database.py` (migrations list ~line 629; updatable-columns list ~line 2321)
- Modify: `backend/rename/service.py` (`apply()` ~1396, `undo()` ~1681)
- Test: `tests/test_rename_service.py`

**Interfaces:**
- Produces: `apply(job_id, conflict_strategy='replace_library_dup')`; job column `conflict_replaced_path TEXT` (nullable).
- Consumes: `find_library_duplicate(job, rows)` (`backend/rename/conflicts.py`), `translate_plex_path(path, cfg['plex_library_path_mappings'])`, `_fileops._trash(path)`, `self._restore_overwritten_original(trashed_to, restore_slot, job_id, base_error)`, `db.list_plex_cache_movies()`.

- [ ] **Step 1: Migration + updatable column.** Add `'ALTER TABLE rename_jobs ADD COLUMN conflict_replaced_path TEXT'` to the migrations list beside the `conflict_kind` entry (database.py ~629). Add `"conflict_replaced_path"` to the updatable-columns set (database.py ~2321). Run the existing migration test to confirm it applies.

- [ ] **Step 2: Write failing test** `test_replace_library_dup_trashes_library_and_places_download`: seed a movie job whose computed `dst` is free but whose title matches a `plex_cache_movies` row pointing at an on-disk temp file; call `apply(job_id, conflict_strategy='replace_library_dup')`; assert the library file is gone from its path (moved to trash), the download now sits at `dst`, job status `applied`, and `conflict_replaced_path` == the library path. Run: expect FAIL.

- [ ] **Step 3: Implement the strategy.** In `apply()`, AFTER the destination guard and BEFORE the `if os.path.lexists(dst)` block, add:
```python
restore_slot = dst  # slot to name if a post-trash failure strands a file
if conflict_strategy == "replace_library_dup":
    from backend.rename.conflicts import find_library_duplicate
    from backend.rename.path_translation import translate_plex_path
    match = find_library_duplicate(job, db.list_plex_cache_movies()) if db else None
    lib_path = translate_plex_path(match["file_path"], self._cfg.get("plex_library_path_mappings")) if match and match.get("file_path") else None
    if not lib_path or not os.path.lexists(lib_path):
        # Duplicate vanished since preview — never place blindly. Hold for review.
        db.update_rename_job(job_id, status="needs_review",
            warning_message="The library duplicate could no longer be located — nothing was changed.")
        self._broadcast(job_id)
        return {"ok": False, "error": "Library duplicate not found"}
    trashed_to = _fileops._trash(lib_path)
    restore_slot = lib_path
    db.update_rename_job(job_id, conflict_replaced_path=lib_path)
```
Then let execution continue to the normal placement path (dst is free). In the two existing post-`trashed_to` failure branches, pass `restore_slot` (not `dst`) to `_restore_overwritten_original(...)` so the stranded-file message names the correct slot. `trashed_to`/`restore_slot` default to `None`/`dst` so `overwrite` and other strategies are unaffected.

- [ ] **Step 4: Run Step-2 test** → expect PASS.

- [ ] **Step 5: Failing test** `test_replace_library_dup_restores_on_place_failure`: monkeypatch `place_file` to raise after the trash; assert `ok False`, the library file is restored to `lib_path`, job not left `applying`. Run → FAIL, then confirm the shared `_restore_overwritten_original(trashed_to, restore_slot, ...)` path already covers it → PASS (adjust only if the message/slot is wrong).

- [ ] **Step 6: Undo symmetry.** Failing test `test_undo_replace_library_dup_restores_library`: apply `replace_library_dup`, then `undo(job_id)`; assert the placed file is removed AND the library file is restored to `conflict_replaced_path`. Implement in `undo()`: when `job.get("conflict_replaced_path")`, after `undo_place`, locate the most-recent restorable trash entry whose `original_path` matches `conflict_replaced_path` and restore it (reuse the existing dst-keyed lookup block, keyed on `conflict_replaced_path` when set); surface `restore_warning` on failure. Run → PASS.

- [ ] **Step 7: Guard test** `test_replace_library_dup_no_duplicate_holds`: no matching `plex_cache_movies` row → job returns to `needs_review`, nothing trashed/placed. Run → PASS.

- [ ] **Step 8: Commit** `feat(rename): replace_library_dup strategy + conflict_replaced_path + undo symmetry`.

### Task 2: Backend — `resolve_keep_plex()` service method + endpoint

**Files:**
- Modify: `backend/rename/service.py` (new method near `apply()`)
- Modify: `backend/api/routes/rename.py` (new route; `ApplyRequest` Literal — see Task 3 Step 1 note)
- Test: `tests/test_rename_service.py`

**Interfaces:**
- Produces: `resolve_keep_plex(job_id) -> {"ok": bool, "warning": str|None}`; `POST /rename/{job_id}/keep-plex`.
- Consumes: `db.archive_rename_jobs([id])`, `_fileops._trash(path)`.

- [ ] **Step 1: Failing test** `test_keep_plex_archives_and_trashes_download`: seed a needs_review job with an on-disk `original_path`; call `resolve_keep_plex(job_id)`; assert job archived, `original_path` moved to trash (gone from its path, present in trash), library/destination untouched, `ok True`. Run → FAIL.

- [ ] **Step 2: Implement** `resolve_keep_plex(job_id)`: load job; if missing → `{"ok": False}`. Archive FIRST (`db.archive_rename_jobs([job_id])`) so it leaves the `detect_moved_source_files` scan set. Then, if `original_path` exists, `_fileops._trash(original_path)` inside try/except — on failure keep `ok True` but set `warning` (job stays archived, library untouched). Best-effort: if a download/pipeline row maps to this job, settle its verdict so it isn't re-flagged (skip if no clean mapping — the kept copy is already in Plex, so the pipeline sees the title present). `self._broadcast(job_id)`. Return `{"ok": True, "warning": warning}`.

- [ ] **Step 3: Run Step-1 test** → PASS.

- [ ] **Step 4: Trash-failure test** `test_keep_plex_trash_failure_still_archives`: monkeypatch `_trash` to raise; assert job archived, `ok True`, `warning` set. Run → PASS.

- [ ] **Step 5: Route.** Add `@router.post("/rename/{job_id}/keep-plex")` calling `_service(reg).resolve_keep_plex(job_id)`, returning its dict. Mirror the auth/reg dependency of `apply_job`.

- [ ] **Step 6: Commit** `feat(rename): keep_plex resolution (archive + recoverable-trash the download)`.

### Task 3: Frontend — API/store plumbing + `strategyForChoice` helper

**Files:**
- Modify: `backend/api/routes/rename.py` (`ApplyRequest.conflict_strategy` Literal)
- Modify: `frontend/src/lib/api/client.ts` (`applyRename` union; new `keepPlex`)
- Modify: `frontend/src/lib/stores/renames.ts` (`applyJob` union; new `keepPlexJob`)
- Modify/Test: `frontend/src/lib/renames/conflictView.ts` (+ `conflictView.test.ts`)

**Interfaces:**
- Produces: `type ResolveChoice = 'keep_plex' | 'keep_downloaded' | 'keep_both'`; `strategyForChoice(kind, choice): {action:'apply'; strategy?: 'overwrite'|'keep_both'|'replace_library_dup'} | {action:'keepPlex'}`; `api.keepPlex(id)`, `keepPlexJob(id)`.

- [ ] **Step 1:** Add `"replace_library_dup"` and `"keep_plex"` to `ApplyRequest.conflict_strategy` Literal (rename.py:134) — keep it exhaustive even though `keep_plex` uses its own route (defensive). Add `'replace_library_dup'` to `api.applyRename` body union (client.ts:381) and `applyJob` strategy union (renames.ts:104). Add `keepPlex: (id) => POST /rename/{id}/keep-plex` to the client, and `export async function keepPlejob`→`keepPlexJob(id)` in the store calling it + `refreshRenames()`.

- [ ] **Step 2: Failing tests** in `conflictView.test.ts` for all six `(kind, choice)` pairs:
  - `same_path` + keep_plex → `{action:'keepPlex'}`; + keep_downloaded → `{action:'apply', strategy:'overwrite'}`; + keep_both → `{action:'apply', strategy:'keep_both'}`.
  - `library_duplicate` + keep_plex → `{action:'keepPlex'}`; + keep_downloaded → `{action:'apply', strategy:'replace_library_dup'}`; + keep_both → `{action:'apply'}` (no strategy → plain apply).
  Run → FAIL.

- [ ] **Step 3: Implement** `strategyForChoice(kind, choice)` per the table. Run → PASS.

- [ ] **Step 4: Commit** `feat(renames): keep_plex/replace_library_dup plumbing + strategyForChoice`.

### Task 4: Frontend — RenameReviewCard redesign + ConflictModal wiring

**Files:**
- Modify: `frontend/src/lib/components/renames/RenameReviewCard.svelte`
- Modify: `frontend/src/lib/components/renames/ConflictModal.svelte`

**Interfaces:**
- Consumes: `strategyForChoice`, `preview.kind`, `preview.recommended`, `preview.existing`/`incoming` (`present`, `original_filename`), `specRows`, `keepPlexJob`, `applyJob`.

- [ ] **Step 1: Column headers.** In the compare table, replace `Existing`/`Incoming` headers with a two-line header per column: **In Plex** + sublabel `📀 current library copy` + truncated `existing.original_filename`; **Downloaded** + `⬇ new file` + `incoming.original_filename`. Keep the ★ recommended-column green highlight exactly as today.

- [ ] **Step 2: Radio choice UI.** Replace the current `{#if actions.overwrite}…Overwrite/Keep both/Apply anyway/Skip` button row with three selectable option cards (`keep_plex` / `keep_downloaded` / `keep_both`), each with a one-line consequence caption. Local `let choice = $state<ResolveChoice>(...)` initialized from `preview.recommended` (`existing`→keep_plex, `incoming`→keep_downloaded, `tie`/`null`→keep_both). Show a "Recommended" chip on the pre-selected card. Keep the `preview.existing.present === false` "Destination is free — Apply" fast path and the `showDvScanButton` unchanged.

- [ ] **Step 3: Primary button.** One solid `bg-[var(--accent)]` "Apply choice" button + a quiet Cancel. On click, call the new `onResolve(choice)` prop. Disable ONLY on this card's own `busy`; while busy show `Working…` label (never a silent full-row dim).

- [ ] **Step 4: Modal wiring.** In `ConflictModal`, add `onResolve={(choice) => { const r = strategyForChoice(preview.kind, choice); r.action === 'keepPlex' ? act(() => keepPlexJob(job.id)) : act(() => applyJob(job.id, r.strategy)); }}`. (Pass `preview.kind` down, or compute the mapping inside the card and hand the modal an `{action,strategy}` — pick one and keep it single-sourced.) Remove now-dead `onOverwrite`/`onKeepBoth` props if unused after the redesign.

- [ ] **Step 5:** `npm run check` (0 errors) + `npm run build`. Commit `feat(renames): In-Plex/Downloaded compare + Keep-Plex/Downloaded/Both choice UI`.

### Task 5: Verify + changelog

**Files:**
- Modify: `frontend/src/lib/changelog.ts`
- Test: run suites

- [ ] **Step 1:** Backend — throwaway `scanhound:latest` container, `docker cp backend/. <n>:/app/backend/` + `docker cp tests/. <n>:/app/tests/`, `pip install pytest pytest-timeout`, run `python -m pytest tests/test_rename_service.py -q` (subset, `--timeout=60`). All green.
- [ ] **Step 2:** Frontend — `npx vitest run src/lib/renames/conflictView.test.ts src/lib/api/client.test.ts`, then `npm run check` + `npm run build`. All green.
- [ ] **Step 3:** Render the redesigned card at a mobile viewport (headless Edge harness) to confirm the radio UI holds narrow — no overflow, buttons reachable.
- [ ] **Step 4:** Add a changelog entry (new version bump) summarizing the Compare redesign + keep-Plex cleanup. Commit `chore: changelog for compare-modal resolution redesign`.

## Self-Review

- **Coverage:** labels (T4.1), choice + parity (T1, T3, T4.2), keep-Plex cleanup (T2, T3), affordance fix (T4.3), undo safety (T1.6), tests (all). ✓
- **Type consistency:** `strategyForChoice` return shape is used identically in T3 tests and T4.4 wiring; `conflict_replaced_path` written in T1.3 and read in T1.6. ✓
- **No placeholders:** each code step carries concrete names/paths. ✓
