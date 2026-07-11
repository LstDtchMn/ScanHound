# Desktop Rename-Conflict Resolution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the raw truncated "file already exists" string on the desktop Renames row with a human-readable conflict summary + a Compare button that opens the existing `RenameReviewCard` comparison modal, and give both platforms a structured conflict signal instead of string-sniffing the message prose.

**Architecture:** Additive backend change (2 nullable `rename_jobs` columns + a reworded, human-readable warning message set at apply-time collision) plus frontend wiring that reuses the already-built `RenameReviewCard`/`conflictView.ts`/`conflict-preview`/`apply(conflict_strategy)` stack. No new backend infrastructure; no conflict *resolution* behavior change.

**Tech Stack:** FastAPI + SQLite (`DatabaseManager`), SvelteKit 5 (runes). Deploy via `docker compose up -d --build` only.

## Global Constraints

- Deploy ONLY via `docker compose up -d --build`.
- Schema changes are additive-only (2 new nullable columns, no rebuild, no backfill).
- Backend byte-formatting MUST match the frontend `formatBytes` output (KB/MB/GB/TB, 1 decimal) so the two never disagree.
- Identical/same-size files stay flagged for review — never silently dropped.
- `hasDestinationConflict()` must keep working for pre-migration rows (legacy `/already exists/` prose + `destination_conflict` annotation) AND new structured rows.
- The reworded message no longer contains "already exists" — the structured `conflict_kind` signal is what new-row detection keys off.
- Reuse the existing `RenameReviewCard` and `renames.ts` store actions — do NOT fork a second copy of the conflict load/resolve wiring.
- Backend tests run on the HOST: `python -m pytest tests/<file> -v` (no `--timeout`). Frontend: `cd frontend && npx vitest run`, `npm run check`, `npm run build`.

---

## File Structure

**Backend (modify):** `backend/database.py` (2 `_column_migrations` entries + `_RENAME_FIELDS` + `_deserialize_rename_row` bool coercion), `backend/rename/service.py` (`_fmt_size` helper, reworked collision message + set/clear the structured columns). Tests: `tests/test_database.py`, `tests/test_rename_service.py` (or the file that already tests `queue_apply` collisions — verify).

**Frontend (modify):** `frontend/src/lib/api/types.ts` (`RenameJob` fields), `frontend/src/lib/renames/review.ts` (`hasDestinationConflict`), `frontend/src/lib/components/renames/RenameRow.svelte` (conflict summary + chip + Compare button). Tests: `frontend/src/lib/renames/review.test.ts`.

**Frontend (new):** `frontend/src/lib/components/renames/ConflictModal.svelte` (thin `ModalOverlay` + `RenameReviewCard` wrapper).

---

## Task 1: Backend — structured conflict columns

**Files:** Modify `backend/database.py` — `_column_migrations` (~line 552), `_RENAME_FIELDS` (~line 1982), `_deserialize_rename_row` (~line 2006). Test `tests/test_database.py`.

**Interfaces:**
- Produces: `rename_jobs.conflict_kind TEXT` (nullable; `'destination_exists'` or NULL) and `rename_jobs.conflict_same_size INTEGER` (nullable; 1/0/NULL) columns, carried by `create`/`update_rename_job`/`get_rename_job`/`list_rename_jobs`. `conflict_same_size` deserializes to a Python `bool`/`None` so the API emits JSON `true`/`false`/`null`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_database.py (add)
def test_rename_job_conflict_columns_roundtrip(db_manager):
    jid = db_manager.create_rename_job({
        "original_path": "/src/a.mkv", "status": "needs_review",
        "conflict_kind": "destination_exists", "conflict_same_size": True,
    })
    job = db_manager.get_rename_job(jid)
    assert job["conflict_kind"] == "destination_exists"
    assert job["conflict_same_size"] is True  # coerced to bool, not 1

def test_rename_job_conflict_same_size_false_and_null(db_manager):
    jid_f = db_manager.create_rename_job({
        "original_path": "/src/b.mkv", "status": "needs_review",
        "conflict_kind": "destination_exists", "conflict_same_size": False})
    jid_n = db_manager.create_rename_job({
        "original_path": "/src/c.mkv", "status": "needs_review"})
    assert db_manager.get_rename_job(jid_f)["conflict_same_size"] is False
    j_n = db_manager.get_rename_job(jid_n)
    assert j_n["conflict_kind"] is None
    assert j_n["conflict_same_size"] is None

def test_update_rename_job_clears_conflict_columns(db_manager):
    jid = db_manager.create_rename_job({
        "original_path": "/src/d.mkv", "status": "needs_review",
        "conflict_kind": "destination_exists", "conflict_same_size": True})
    db_manager.update_rename_job(jid, status="applied",
                                 conflict_kind=None, conflict_same_size=None)
    job = db_manager.get_rename_job(jid)
    assert job["conflict_kind"] is None
    assert job["conflict_same_size"] is None
```

(Confirm the exact `create_rename_job` signature/helper name in `tests/test_rename_service.py`'s harness or `database.py` — the summary notes `db.create_rename_job({...dict...})`. Use whatever the real API is.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_database.py -k conflict -v`
Expected: FAIL (columns don't exist; `update`/`create` drop the unknown keys via `_RENAME_FIELDS`).

- [ ] **Step 3: Implement**

Add to the `_column_migrations` list (near line 552, alongside the other `ALTER TABLE rename_jobs ADD COLUMN` entries):

```python
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_kind TEXT',
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_same_size INTEGER',
```

Add both names to the `_RENAME_FIELDS` tuple (~line 1982) so `create`/`update_rename_job` persist them (they filter on this tuple — an unlisted column is silently dropped):

```python
        "conflict_kind", "conflict_same_size",
```

Coerce `conflict_same_size` (stored INTEGER 1/0/NULL) to a real `bool`/`None` in `_deserialize_rename_row` (~line 2006), after the existing JSON-field decode loop:

```python
        if row.get("conflict_same_size") is not None:
            row["conflict_same_size"] = bool(row["conflict_same_size"])
```

(SQLite stores no native bool; `create`/`update` write Python `True`/`False` which SQLite persists as 1/0, and this coercion turns them back into JSON-clean booleans on read. `None` passes through untouched.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_database.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/database.py tests/test_database.py
git commit -m "feat(rename): add conflict_kind/conflict_same_size columns to rename_jobs

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: Backend — human-readable message + set/clear the signal

**Files:** Modify `backend/rename/service.py` — module-level `_fmt_size` helper; the collision branch (~lines 1322-1342); the success-apply `status="applied"` update in the same apply path; the same-file no-op path (~line 1303-1306) and rematch path (clear the columns). Test `tests/test_rename_service.py` (or wherever `queue_apply` collision behavior is already tested — verify by grepping for `already exists` / `needs_review` in tests).

**Interfaces:**
- Consumes: `rename_jobs.conflict_kind`/`conflict_same_size` (Task 1).
- Produces: reworded `warning_message`; `conflict_kind='destination_exists'` + `conflict_same_size` set at collision, cleared to NULL on successful apply / same-file no-op / rematch.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rename_service.py (add — match the file's existing _service(db, ...) harness)
from backend.rename.service import _fmt_size

def test_fmt_size_units():
    assert _fmt_size(0) == "0 B"
    assert _fmt_size(512) == "512 B"
    assert _fmt_size(1024) == "1.0 KB"
    assert _fmt_size(14359773138) == "13.4 GB"
    assert _fmt_size(1024 ** 4) == "1.0 TB"

# Collision behavior: build a job whose destination already holds a file, queue_apply
# with no strategy (hold-for-review), assert the reworded message + structured signal.
# Mirror the existing collision test in this file for setup (tmp dirs, create_rename_job,
# a real file placed at dst). The three assertions that matter:
def test_collision_same_size_sets_signal_and_message(tmp_path, ...):
    # ... set up src and dst as two files of IDENTICAL size ...
    out = svc.queue_apply([job_id])  # no conflict_strategy -> hold for review
    job = db.get_rename_job(job_id)
    assert job["status"] == "needs_review"
    assert job["conflict_kind"] == "destination_exists"
    assert job["conflict_same_size"] is True
    assert "same size" in job["warning_message"]
    assert "likely a duplicate" in job["warning_message"]
    assert "already in the library" in job["warning_message"].lower()
    assert "bytes" not in job["warning_message"]  # no raw byte counts

def test_collision_different_size_sets_false(tmp_path, ...):
    # ... src and dst of DIFFERENT sizes ...
    svc.queue_apply([job_id])
    job = db.get_rename_job(job_id)
    assert job["conflict_kind"] == "destination_exists"
    assert job["conflict_same_size"] is False
    assert "same size" not in job["warning_message"]

def test_overwrite_apply_clears_conflict_signal(tmp_path, ...):
    # first collide (hold), then queue_apply with conflict_strategy="overwrite"
    svc.queue_apply([job_id])                          # -> needs_review + signal set
    svc.queue_apply([job_id], conflict_strategy="overwrite")  # -> applied
    job = db.get_rename_job(job_id)
    assert job["status"] == "applied"
    assert job["conflict_kind"] is None
    assert job["conflict_same_size"] is None
```

(Fill in the `...` setup from the existing collision test already in this file — grep `already exists` / `conflict_strategy` in `tests/` to find and copy its fixture scaffolding verbatim. Do NOT invent a new harness.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_rename_service.py -k "fmt_size or collision or clears_conflict" -v`
Expected: FAIL (`_fmt_size` missing; message/columns not set).

- [ ] **Step 3: Implement**

Add the module-level helper near the top of `backend/rename/service.py` (after imports):

```python
def _fmt_size(n: int) -> str:
    """Human file size — mirrors the frontend conflictView.formatBytes
    (KB/MB/GB/TB, 1 decimal) so the two never disagree."""
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    v = n / 1024.0
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.1f} {units[i]}"
```

Replace the collision `else:` branch (currently ~lines 1322-1342). New version:

```python
            else:
                # None → hold for review (existing behavior); 'skip' → same,
                # explicit — either way the file at dst is left untouched and
                # the job goes back to needs_review (never left 'applying').
                same_size = None
                try:
                    existing_size = os.path.getsize(dst)
                    candidate_size = os.path.getsize(src)
                    same_size = existing_size == candidate_size
                    if same_size:
                        msg = (f"A copy is already in the library at the same size "
                               f"({_fmt_size(existing_size)}) — likely a duplicate. "
                               f"Review to replace or keep.")
                    else:
                        msg = (f"A copy is already in the library "
                               f"(existing {_fmt_size(existing_size)} vs. new "
                               f"{_fmt_size(candidate_size)}). Review to replace or keep.")
                except OSError:
                    msg = "A copy is already in the library. Review to replace or keep."
                # Append to (never clobber) a warning already on the job — e.g. a
                # year-mismatch note set at creation time — so the collision guard
                # never silently discards an earlier reason the file needs review.
                existing = job.get("warning_message")
                combined = f"{existing}; {msg}" if existing else msg
                db.update_rename_job(
                    job_id, status="needs_review", warning_message=combined,
                    conflict_kind="destination_exists", conflict_same_size=same_size)
                self._broadcast(job_id)
                return {"ok": False, "error": msg}
```

Clear the signal wherever the job leaves the conflict state:

1. Same-file no-op (~line 1303-1306, the `os.path.samefile` branch): add the two clears to its `update_rename_job`:
   ```python
                    db.update_rename_job(job_id, status="applied", processed_at=_now(),
                                         conflict_kind=None, conflict_same_size=None)
   ```
2. Successful placement: find the `status="applied"` update at the end of the apply/move-success path (after the move succeeds — grep `status="applied"` in this function) and add `conflict_kind=None, conflict_same_size=None` to that `update_rename_job` call. This covers the overwrite and keep_both resolve paths, which fall through to normal placement.
3. Rematch: in the rematch path's `update_rename_job` (grep `def rematch` / where a new destination is committed), add `conflict_kind=None, conflict_same_size=None` so a rematch to a fresh destination drops a stale conflict marker.

(If any of these update sites are hard to disambiguate, prefer clearing at the single `status="applied"` transition plus the rematch commit — those are the only ways a job legitimately leaves `needs_review`-conflict. Leaving a marker on a job that stays `needs_review` is correct.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_rename_service.py -v`
Expected: PASS (new + existing collision tests; update any existing test that asserted the OLD "already exists at the destination … bytes" wording).

- [ ] **Step 5: Commit**

```bash
git add backend/rename/service.py tests/test_rename_service.py
git commit -m "feat(rename): human-readable conflict message + set/clear structured signal

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: Frontend — types + structured conflict detection

**Files:** Modify `frontend/src/lib/api/types.ts` (`RenameJob`), `frontend/src/lib/renames/review.ts` (`hasDestinationConflict`). Test `frontend/src/lib/renames/review.test.ts`.

**Interfaces:**
- Consumes: `conflict_kind`/`conflict_same_size` from the jobs API (Tasks 1-2).
- Produces: `hasDestinationConflict(job)` preferring the structured signal.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/lib/renames/review.test.ts (add)
import { hasDestinationConflict } from './review';

const base = { id: 1, status: 'needs_review' } as any;

it('detects a conflict via the structured conflict_kind signal', () => {
  expect(hasDestinationConflict({ ...base, conflict_kind: 'destination_exists' })).toBe(true);
});
it('still detects the legacy destination_conflict annotation', () => {
  expect(hasDestinationConflict({ ...base, destination_conflict: true })).toBe(true);
});
it('still detects a pre-migration "already exists" prose warning', () => {
  expect(hasDestinationConflict({ ...base, warning_message: 'A file already exists at the destination: /x' })).toBe(true);
});
it('is false for a non-conflict needs-review job', () => {
  expect(hasDestinationConflict({ ...base, warning_message: 'Year mismatch: parsed 2024 vs TMDB 2023' })).toBe(false);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib/renames/review.test.ts`
Expected: FAIL on the `conflict_kind` case (field unknown / not checked).

- [ ] **Step 3: Implement**

In `frontend/src/lib/api/types.ts`, add to the `RenameJob` interface (near `warning_message`/`destination_conflict`):

```ts
  conflict_kind?: 'destination_exists' | null;
  conflict_same_size?: boolean | null;
```

In `frontend/src/lib/renames/review.ts`, update `hasDestinationConflict` (lines 46-49) to prefer the structured signal, keeping the legacy checks as backward-compat:

```ts
export function hasDestinationConflict(job: RenameJob): boolean {
  if (job.conflict_kind === 'destination_exists') return true;
  if (job.destination_conflict) return true;
  return /already exists/i.test(job.warning_message ?? '');
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && npx vitest run src/lib/renames/review.test.ts && npm run check`
Expected: PASS, 0 type errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api/types.ts frontend/src/lib/renames/review.ts frontend/src/lib/renames/review.test.ts
git commit -m "feat(rename): structured conflict detection in hasDestinationConflict

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Frontend — desktop conflict summary, Compare button, and modal

**Files:** Create `frontend/src/lib/components/renames/ConflictModal.svelte`. Modify `frontend/src/lib/components/renames/RenameRow.svelte`.

**Interfaces:**
- Consumes: `hasDestinationConflict`/`conflict_same_size` (Task 3), the existing `RenameReviewCard`, `ModalOverlay`, and `renames.ts` store actions (`applyJob(id, strategy?)`, `deleteJob`, `acceptCombinedJob`, `acceptCorrectionJob`, `refreshRenames`) + `api.reidentifyRename`.

- [ ] **Step 1: Implement `ConflictModal.svelte`**

A thin wrapper mounting the existing `RenameReviewCard` in `ModalOverlay`. `RenameReviewCard` self-loads its own `conflict-preview` and DV-scan; this wrapper only supplies the 10 callbacks (mirroring `RenameReviewDeck.svelte:194-206`) and closes on resolve.

```svelte
<!-- frontend/src/lib/components/renames/ConflictModal.svelte -->
<script lang="ts">
  import ModalOverlay from '$lib/components/ModalOverlay.svelte';
  import RenameReviewCard from './RenameReviewCard.svelte';
  import { api } from '$lib/api/client';
  import {
    applyJob, deleteJob, acceptCombinedJob, acceptCorrectionJob, refreshRenames
  } from '$lib/stores/renames';
  import type { RenameJob } from '$lib/api/types';

  let { job, onClose }: { job: RenameJob; onClose: () => void } = $props();

  let busy = $state(false);

  // Run a resolve action, then close so the (refreshed) list reflects the outcome.
  // refreshRenames is already called inside the store actions; we close after.
  async function act(fn: () => Promise<unknown> | unknown) {
    busy = true;
    try {
      await fn();
      onClose();
    } finally {
      busy = false;
    }
  }
</script>

<ModalOverlay onclose={onClose}>
  <div class="w-full max-w-lg bg-[var(--bg-secondary)] border border-[var(--border)] rounded-xl shadow-2xl"
       role="dialog" aria-modal="true" tabindex="-1">
    <RenameReviewCard
      {job}
      {busy}
      onApply={() => act(() => applyJob(job.id))}
      onOverwrite={() => act(() => applyJob(job.id, 'overwrite'))}
      onKeepBoth={() => act(() => applyJob(job.id, 'keep_both'))}
      onSkip={onClose}
      onRematch={onClose}
      onReidentify={() => act(async () => { await api.reidentifyRename(job.id); await refreshRenames(); })}
      onAcceptCombined={() => act(() => acceptCombinedJob(job.id))}
      onAcceptCorrection={() => act(() => acceptCorrectionJob(job.id))}
      onRemove={() => act(() => deleteJob(job.id))}
    />
  </div>
</ModalOverlay>
```

(Verify the exact store export names against `frontend/src/lib/stores/renames.ts` before finalizing — `applyJob`/`deleteJob`/`acceptCombinedJob`/`acceptCorrectionJob`/`refreshRenames` and `api.reidentifyRename` were confirmed present, but confirm signatures. If `applyJob` already calls `refreshRenames` internally, the extra close is still correct. `onRematch` intentionally just closes — the row keeps its own Rematch affordance; do NOT build a second rematch flow here.)

- [ ] **Step 2: Wire the summary + Compare button into `RenameRow.svelte`**

Read the current file first. In the `{:else if job.warning_message}` branch (lines 61-64), branch on `hasDestinationConflict(job)`:

```svelte
<script lang="ts">
  // ...existing imports...
  import { hasDestinationConflict } from '$lib/renames/review';
  import ConflictModal from './ConflictModal.svelte';
  // ...existing props/state...
  let showConflict = $state(false);
  let isConflict = $derived(hasDestinationConflict(job));
</script>
```

Replace the warning-render branch:

```svelte
    {:else if isConflict}
      <div class="flex items-center gap-1.5 text-xs">
        <span class="px-1.5 py-0.5 rounded bg-[var(--warning-bg,transparent)] text-[var(--warning)] font-medium">⚠ Conflict</span>
        {#if job.conflict_same_size}
          <span class="px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)]">likely duplicate</span>
        {/if}
        <span class="text-[var(--text-secondary)] truncate" title={job.warning_message}>{job.warning_message}</span>
        <button class="ml-auto shrink-0 px-2 py-0.5 rounded bg-[var(--accent)] text-white"
          onclick={() => (showConflict = true)}>Compare</button>
      </div>
    {:else if job.warning_message}
      <div class="text-xs text-[var(--warning)] truncate" title={job.warning_message}>
        {job.warning_message}
      </div>
    {/if}
```

Mount the modal at the end of the component markup:

```svelte
{#if showConflict}
  <ConflictModal {job} onClose={() => (showConflict = false)} />
{/if}
```

(Match the row's existing Tailwind/CSS-var conventions — check the real `--warning`/`--accent`/`--bg-tertiary` names in `app.css`; the row already uses `text-[var(--warning)]`, so reuse whatever exists. Keep the non-conflict warning branch exactly as it was.)

- [ ] **Step 3: Verify**

Run: `cd frontend && npm run check && npm run build`
Expected: 0 errors, build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/components/renames/ConflictModal.svelte frontend/src/lib/components/renames/RenameRow.svelte
git commit -m "feat(rename): desktop conflict summary + Compare modal reusing RenameReviewCard

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: Verification + deploy

**Files:** none (verification + changelog + deploy).

- [ ] **Step 1: Backend suite**

Run: `python -m pytest tests/test_database.py tests/test_rename_service.py -v` (plus any other rename test file touched). Expected: all PASS.

- [ ] **Step 2: Frontend suite + typecheck + build**

Run: `cd frontend && npx vitest run && npm run check && npm run build`. Expected: all PASS, clean build.

- [ ] **Step 3: Migration dry-run on a copy of the live DB**

Copy `/dbvol/crawler.db` out of the container to a temp path, open it with the HOST's new `DatabaseManager(db_path=<tmp>)` (runs the additive migration), assert `conflict_kind`/`conflict_same_size` columns now exist and `rename_jobs` row count is unchanged. (Per [[scanhound-testing]]: additive columns, no rebuild — low risk, but confirm no data loss before deploy.)

- [ ] **Step 4: Live device checklist** (after deploy, or against a dev preview)
  - Desktop Renames: a destination-conflict row shows `⚠ Conflict` + the human-readable summary (no raw bytes); a same-size conflict also shows the `likely duplicate` chip; `Compare` opens the comparison card; Overwrite / Keep both / Skip resolve and the row updates.
  - A non-conflict `needs_review` row (e.g. year mismatch) still renders the plain warning line, no Compare button.
  - Mobile review deck still detects and renders the conflict (regression — `hasDestinationConflict` structured path).

- [ ] **Step 5: Changelog + deploy**

Add a `frontend/src/lib/changelog.ts` entry (next version) summarizing: desktop rename conflicts now show a readable summary + a one-click Compare that opens the existing side-by-side file comparison, with same-size duplicates clearly flagged.

```bash
git add frontend/src/lib/changelog.ts
git commit -m "chore: changelog — desktop conflict resolution

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

Deploy: `docker compose up -d --build`. Confirm healthy startup ("All services initialized", "Application startup complete") and that the `ALTER TABLE rename_jobs` migrations applied without error in `docker logs scanhound`.

---

## Self-Review Notes (author)

- **Spec coverage:** structured signal + message rework (T1-T2), frontend detection (T3), desktop summary + Compare modal (T4), verify + deploy (T5). Both resolved spec decisions (explicit `conflict_same_size` boolean; desktop-only chip) are implemented; mobile card is untouched (its Size row already covers same-size).
- **Type/name consistency:** `conflict_kind`/`conflict_same_size` identical across migration, `_RENAME_FIELDS`, deserializer, API, `RenameJob` type, `hasDestinationConflict`, and the row chip. `applyJob(id, strategy?)` signature matches `RenameReviewDeck`'s existing usage.
- **Regression guard:** T3 keeps legacy `/already exists/` + `destination_conflict` detection working for pre-migration rows; T2 clears the signal on every non-conflict exit so a stale marker can't linger; the non-conflict warning branch in T4 is unchanged.
- **DRY:** the modal reuses `RenameReviewCard` and the existing store actions verbatim — no forked conflict load/resolve logic.
- **Data safety:** additive columns only; no behavior change to overwrite (trash-not-delete) / keep-both / skip.
