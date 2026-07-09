# Mobile Renames Review — Design

**Status:** Approved (design phase). v1 scope.
**Date:** 2026-07-09

## Goal

Make the Renames screen usable on a phone by turning the crammed desktop list
into a **focused, one-at-a-time review of the items that need a decision** — and
give the most common blocker, "a file already exists at the destination," a real
resolution: a **side-by-side technical-spec comparison of both files** followed by
an **Overwrite** or **Keep both** choice. The current mobile screen renders the
desktop row/grid: match confidence and the warning are truncated or absent,
filenames are clipped, tap targets are tiny, and a conflict can only be "held for
review" with no way to act.

## Architecture

Two parts, both grounded in code that already exists:

**Frontend (mobile UI).** The Renames route forks on `isPhone` (the Scan page's
store: `max-width:767px` AND `pointer:coarse`). On a phone, the desktop review
chrome — list/grid, `StatusDashboard`, `RenameFilterBar`, `BulkBar` — is replaced
by a **summary hero** plus a **full-screen review deck**. `RenamesHeader`
(Process ▾ / Dolby Vision / Re-identify all), the Dolby Vision scan surface, and
`TrashPanel` are kept as-is below the hero. The desktop layout is untouched.

**Backend (conflict resolution).** Three additions, all built on existing
primitives:
1. A structured file-spec probe (`ffprobe`, already in the image) so the UI can
   compare the existing on-disk file against the incoming one.
2. A no-persistence `conflict-preview` endpoint (mirrors the existing
   `rematch-preview`) that returns both specs + a recommendation.
3. An optional `conflict_strategy` on apply (`overwrite` / `keep_both` / `skip`),
   where **overwrite trashes the displaced file via the existing `_trash()`
   primitive — never deletes** — and **keep_both** disambiguates the target name.

The existing apply flow is a background worker (`queue_apply` → `_worker` →
`RenameService.apply`) with progress over WebSocket; the collision guard at
[service.py:1224](backend/rename/service.py:1224) is the single branch point for
`conflict_strategy`.

## Tech Stack

SvelteKit 5 (runes), FastAPI, `ffprobe` (bundled: [Dockerfile:27](Dockerfile:27))
and `dovi_tool` (bundled: [Dockerfile:35](Dockerfile:35)), the existing `renames`
store / `api` client / `RematchModal`, the `rename:job` / `rename:progress`
WebSocket events. Deploy via `docker compose up -d --build` only.

## Global Constraints

- Deploy in-app changes ONLY via `docker compose up -d --build`.
- The desktop `/renames` page and desktop navigation must be unchanged; the mobile
  UI fork is gated strictly on `isPhone`.
- **Data safety is absolute: Overwrite MUST route the displaced file through
  `fileops._trash()` (recoverable, same-volume, honors
  `deletions_require_confirmation`). Never `os.remove` a destination file.**
- **The apply request body MUST be optional/defaulted.** The existing bodyless
  `POST /rename/jobs/{id}/apply` (client `applyRename(id)` with no body) must keep
  validating — a *required* body param would 422 every current caller.
- "Ready to apply" means **`match_confidence >= 100`**, via the shared
  `classifyJob` helper — not the server's ≥95% apply-confident threshold.
- `ffprobe` probes are **fail-safe and time-boxed** (return null on missing file /
  missing binary / timeout ~20–30s). **`dovi_tool` (FEL/MEL extraction) is NEVER
  run synchronously in a request** — it streams the whole file (up to 1800s); use
  only the cached `dv_scan` layer, else show "Dolby Vision" without the FEL/MEL
  sub-label.
- Reuse existing modules — `conflicts.py` (ranking), `fileops._trash`,
  `RematchModal`, the `renames` store actions. Do not duplicate their logic.
- Rename Pydantic models use Pydantic v2 default `extra='ignore'` (only
  `settings.py` uses `extra='forbid'`), so adding fields is safe.
- Works in both the responsive web app and the Tauri/Android wrapper.
- Tests accompany each unit; deploy only after the changed-module suite is green.

---

## Job classification (shared helper)

A single pure function classifies each `RenameJob`, used by the summary counts and
the deck's scope filter. Lives in `frontend/src/lib/renames/review.ts`
(unit-testable in isolation), alongside the `matchesQuery(job, q)` moved out of
`+page.svelte` and exported so both forks share it.

- **`ready`** — `status === 'matched'` AND `(match_confidence ?? 0) >= 100` AND no
  `warning_message` AND not `destination_conflict`.
- **`needsReview`** — still-active (not `applied`/`reverted`/`pending`) AND NOT
  `ready`: `needs_review`, `failed`, or a `matched` job with confidence < 100, a
  `warning_message`, or a `destination_conflict`.
- **`inactive`** — `applied`, `reverted`, `pending` (excluded from both).

```ts
export type ReviewBucket = 'ready' | 'needsReview' | 'inactive';
export function classifyJob(job: RenameJob): ReviewBucket;
export function partitionJobs(jobs: RenameJob[]): { ready: RenameJob[]; needsReview: RenameJob[] };
export function hasDestinationConflict(job: RenameJob): boolean; // warning matches /already exists/ OR destination_conflict
```

Deck scope: **Under 100%** = `needsReview`; **All** = `ready` then `needsReview`.
Order within each: `match_confidence` ascending (lowest-confidence first), nulls
first.

---

## Backend components

### B1. `probe_specs(path)` — structured file specs (new: `backend/rename/mediainfo.py`)

One `ffprobe -v quiet -print_format json -show_format -show_streams <path>` call,
mapped to a stable dict; fail-safe (null on missing file / missing ffprobe /
timeout). Follows the established pattern of `llm_identify.probe_video_width`
([llm_identify.py:141](backend/rename/llm_identify.py:141)).

```python
def probe_specs(path: str) -> dict | None:
    # {
    #   "present": bool, "size_bytes": int|None, "container": str|None,
    #   "duration_min": float|None, "bitrate": int|None,          # overall (format.bit_rate)
    #   "resolution": str|None,      # "2160p"/"1080p"/... from video width/height
    #   "video_codec": str|None,     # "HEVC"/"AVC"/...
    #   "hdr": str|None,             # "HDR10"/"HLG"/"Dolby Vision"/None (color_transfer + DOVI side_data)
    #   "dv_layer": str|None,        # "FEL"/"MEL"/"P8"/"P5" from the dv_scan CACHE only, else None
    #   "audio": str|None,           # "TrueHD 7.1"/"EAC3 5.1"/... (primary track codec+channels)
    # }
```

- Prefer `format.bit_rate` (overall) — per-stream MKV bitrate is often absent.
- `hdr`: PQ/HLG from `color_transfer`; "Dolby Vision" when a video-stream
  `DOVI`/`dovi` `side_data` entry is present (ffprobe sees DV presence instantly).
- `dv_layer`: read the **cached** `dv_scan` row via `get_dv_scan(path)` +
  `dv_scan_is_current(path, mtime, size)` ([database.py:1590](backend/database.py:1590)).
  If no current row, leave null — do NOT invoke `dv_detect.detect_layer` inline.

### B2. `conflict_preview(job_id)` + `POST /rename/jobs/{id}/conflict-preview` (new)

Mirrors `rematch_preview` ([service.py:1373](backend/rename/service.py:1373)) — a
pure compute, no DB write, no migration.

- Resolve `dst = destination_path + (new_filename or basename(src))` exactly as
  `apply()` does.
- `incoming = probe_specs(job.original_path)`; `existing = probe_specs(dst)` if
  `os.lexists(dst)` else a `{present: False}` spec.
- **Recommendation** via `conflicts.recommend_keep([existing_job, incoming_job])`,
  but feed **explicit probed spec fields** so the tag-stripped library file is
  judged on its real specs, not its Plex filename (see B4).
- Return `ConflictComparison { existing, incoming, recommended, reason }` where
  `recommended ∈ 'existing'|'incoming'|'tie'|null` and `reason` is the
  `_quality_reason`-style chip (e.g. "Incoming: 2160p · Dolby Vision · Remux").
- The route mirrors `rematch_preview`'s route
  ([rename.py:255](backend/api/routes/rename.py:255)); POST, no body needed.

Note the on-disk-only case: `destination_conflict` is job-vs-job only and does NOT
fire for an untracked library file already on disk — the preview's authority is
`os.lexists(dst)`, matching what `apply()` collides on.

### B3. Apply `conflict_strategy` (modify: route + service + fileops)

- **Route body (optional):** in [rename.py](backend/api/routes/rename.py) add
  `class ApplyRequest(BaseModel): conflict_strategy: Optional[Literal['overwrite','keep_both','skip']] = None`
  and declare `apply_job(job_id, body: ApplyRequest = Body(default=ApplyRequest()), reg=...)`
  so the current bodyless POST still validates.
- **Thread it:** `queue_apply(ids, conflict_strategy=None)` → `_worker` →
  `apply(job_id, automatic=False, conflict_strategy=None)`.
- **Branch at the collision guard** ([service.py:1224](backend/rename/service.py:1224)):
  - `None` (default): unchanged — hold for review.
  - `'skip'`: leave as `needs_review` (or mark skipped) and return without placing.
  - `'overwrite'`: `_fileops._trash(dst)` (recoverable) to clear the destination,
    **then** the normal `place_file(src, dst, ...)` (dst now free; its
    FileExistsError guard stays intact). Guard the `src`/`dst` same-inode
    re-apply edge (skip if already the same file).
  - `'keep_both'`: compute a unique dst via a new shared
    `fileops.dedupe_dest(dst) -> str` (extract the ` ({n}){ext}` loop from
    `_trash`, [fileops.py:224](backend/rename/fileops.py:224)), write the new
    basename back to `job.new_filename` (keep DB/undo/conflict bookkeeping
    consistent), then `place_file` to the unique path.
- **Undo symmetry (data safety):** undo of an overwrite-applied job runs
  `undo_place` to remove the new placement — which frees `dst` — then restores the
  displaced original from Trash by its recorded `original_path` (reuse the existing
  `/rename/trash/restore` machinery). The overwritten file is therefore always
  recoverable.

### B4. Rank on real specs, not stripped filenames (modify: `conflicts.py`)

Extend `_quality_score` ([conflicts.py:41](backend/rename/conflicts.py:41)) to
**optionally consume explicit spec fields** when present on the job dict —
`dv_layer` mapped to a rank (`fel:3, mel:2, profile8:1, profile5:1, none:0`)
inserted just below resolution and above the binary DV bit, plus `hdr`/`audio`
from the probe — and fall back to today's pure-filename parsing when those keys
are absent (so every existing `test_rename_service.py` ranking test still passes).
`conflict_preview` passes the probed specs; the normal jobs-list annotation path
passes nothing and behaves exactly as before.

---

## Frontend components

### F1. Types (`frontend/src/lib/api/types.ts`)

Beside `RematchPreviewResponse`:

```ts
export interface FileSpec {
  present: boolean; path: string; size_bytes: number | null;
  resolution: string | null; video_codec: string | null; hdr: string | null;
  dv_layer: string | null; audio: string | null;
  duration_min: number | null; bitrate: number | null;
}
export interface ConflictComparison {
  existing: FileSpec | null;   // null / present:false = destination free
  incoming: FileSpec;
  recommended: 'existing' | 'incoming' | 'tie' | null;
  reason: string | null;
}
```

`RenameJob` already carries every other field the deck needs.

### F2. API client (`frontend/src/lib/api/client.ts`)

- `conflictPreview(id)` → `request<ConflictComparison>('/rename/jobs/${id}/conflict-preview', { method: 'POST' })` (mirror `rematchPreview`).
- `applyRename(id, body?: { conflict_strategy?: 'overwrite' | 'keep_both' | 'skip' })`
  → passes an optional JSON body (`request` auto-sets Content-Type when a body is
  present); backward compatible.
- Store: `applyJob(id, strategy?)` forwards to `api.applyRename`.

### F3. `MobileRenamesView.svelte` (new)

Rendered by `renames/+page.svelte` when `$isPhone`.

- Reuses the page's `loadRenameJobs()`/`loadRenameStatus()`/`loadDvScans()` on
  mount, and keeps the existing `$renameQueue` apply-progress banner.
- **Search** bound to `renameQuery`, filtering the active set via the shared
  `matchesQuery`.
- **Summary hero** from `partitionJobs($renameJobs)` (search-filtered): a **Ready**
  card (count + "Apply all" → `api.bulkApply(readyIds)` then `refreshRenames()`,
  hidden at 0) and a **Needs review** card (count + "Review" → opens the deck,
  hidden at 0), plus a **scope toggle** `Under 100% · N` (default) / `All · M`.
- **Empty states:** no jobs → "No rename jobs yet. Use Process ▾ to scan a folder";
  none needing review → "All clear — N ready to apply" + Apply-all.
- **Kept tools:** `RenamesHeader`, the DV scan surface, `TrashPanel` below the hero.

### F4. `RenameReviewDeck.svelte` (new)

Full-screen overlay (fixed inset, safe-area padding — the mobile `DetailSheet`
pattern). Owns the queue, index, scope, **busy state**, and navigation.

- **Props:** `jobs`, `initialScope`, `onClose`.
- Queue derived per scope from `partitionJobs`; header = close (×) + scope toggle +
  `n / N`. Previous/next arrows and horizontal swipe (reuse `gestures.ts`).
- Owns the action wrappers (mirror the page's `run()` pattern: set busy → await
  store action → toast → clear) and passes `busy` to the card. After a resolving
  action (Apply / Overwrite / Keep both / Remove / Accept) → **auto-advance**;
  the `rename:job` WS upsert removes the item from the active set. Index clamps;
  empty queue → completion state ("All reviewed" + Done, plus Apply-all if ready
  items remain).
- Hosts `RematchModal` for the **Rematch** action (`rematchJob` state →
  `refreshRenames()` on close).

### F5. `RenameReviewCard.svelte` (new)

Pure presentation for one job; emits callbacks; no fetching except the lazy
conflict preview (below).

- **Header:** poster (`poster_url`, `ti-photo` fallback), title + `(year)`, the
  **confidence** as the visual hero (large, `confidenceVariant` color),
  `match_source`, and a `dv_layer` badge. Tapping the confidence expands
  `match_reasons`.
- **From → To:** full `original_filename` / `new_filename`, monospace, wrapped,
  never truncated.
- **No conflict:** a normal **Apply** button.
- **Conflict** (`hasDestinationConflict(job)`): a **compare view** —
  - On becoming the active card, lazy-fetch `conflictPreview(id)` (guard with a
    `previewSeq` counter like `RematchModal.loadPreview`); show a
    "Comparing files…" state, then a **two-column Existing / Incoming spec table**
    (Resolution, HDR/DV, Video, Audio, Bitrate, Size, Duration) with the better
    cell per row emphasized and the **recommended** column flagged (★ + `reason`).
    Falls back to a size-only row if a probe returns null.
  - **Actions:** **Overwrite** (danger-styled; subtext "existing file moves to
    Trash — recoverable") → `applyJob(id, 'overwrite')`; **Keep both** ("adds a
    second version") → `applyJob(id, 'keep_both')`; **Skip** → next.
- **Secondary (always):** **Rematch** (manual fix — opens `RematchModal`),
  **Re-identify** (`needs_review`/`failed` → auto re-run), **Accept {code}** (only
  with `combined_episode` / `suggested_correction`), **Remove** (`deleteJob`).

### F6. `renames/+page.svelte` fork (modify)

Wrap the list/grid + `StatusDashboard` + `RenameFilterBar` + `BulkBar` in
`{#if $isPhone}<MobileRenamesView/>{:else}…{/if}`. Shared: the `onMount` loaders,
`RenamesHeader`, DV surface, `TrashPanel`. No desktop behavior changes.

---

## Data Flow

1. `loadRenameJobs()` + the `rename:job` WS handler keep `renameJobs` live.
2. `MobileRenamesView` derives `{ready, needsReview}` → summary + scope toggle.
3. **Apply all** → `api.bulkApply(readyIds)` → queued; items drop out on WS update.
4. **Review** → deck opens at the chosen scope.
5. A conflict card lazy-fetches `POST /conflict-preview` → renders both specs +
   recommendation.
6. **Overwrite** → `applyJob(id,'overwrite')` → worker trashes `dst`, places the
   file, marks applied over WS → deck auto-advances. **Keep both** → unique dst,
   places, applied. **Skip** → next, no change.
7. **Rematch** → `RematchModal` → `refreshRenames()`.

## Error Handling

- Every action mirrors the page's `run()` pattern (busy guard + success/error
  toast); FastAPI `detail` already surfaces through the client's `request<T>`
  wrapper, so "A file already exists…" needs no new plumbing.
- `conflictPreview` failures show an inline "couldn't compare files — retry" state
  with the byte-size fallback; a stale preview is re-guarded by `previewSeq` and,
  ultimately, by `apply()`'s live `os.lexists(dst)` re-check.
- Apply is queued server-side; the deck advances optimistically and the
  `$renameQueue` banner + `rename:job` events reflect the real outcome; a failed
  apply reappears as a `failed` job on refresh.
- Overwrite can never lose data: the displaced file is trashed (recoverable), and
  undo restores it.

## Testing

**Backend**
- `probe_specs`: parses a representative `ffprobe` JSON (resolution/codec/hdr/
  audio/bitrate/size); returns null on missing file and when ffprobe is absent
  (monkeypatch `shutil.which`); reads `dv_layer` only from a current `dv_scan`
  cache row and never shells `dovi_tool`.
- `conflict_preview`: existing-present vs destination-free (`existing.present`
  false); recommendation uses probed specs so a Plex-named 2160p-DV library file
  is NOT recommended for overwrite by a tag-rich 1080p incoming (the correctness
  trap); no DB write occurs.
- `_quality_score` extension: explicit `dv_layer`/`hdr`/`audio` change the ranking;
  **absence of those keys reproduces every existing ranking test** (DV>non-DV,
  remux>web-dl, res>source, identical→tie).
- apply `conflict_strategy`: `overwrite` trashes the existing file (assert a Trash
  entry, assert not deleted) then places; `keep_both` places at a deduped name and
  rewrites `new_filename`; `skip` leaves the job unplaced; default (None)
  reproduces today's hold-for-review; bodyless POST still 200s. `dedupe_dest`
  produces a case-insensitively-unique name preserving the extension. Undo of an
  overwrite restores the displaced original.

**Frontend**
- `review.ts`: `classifyJob` bucket boundaries (matched-100 + warning → needsReview;
  matched-99 → needsReview; applied/pending → inactive); `partitionJobs` ordering;
  `hasDestinationConflict`.
- `MobileRenamesView`: counts under a search filter; Apply-all sends exactly the
  ready IDs; hero cards hide at 0; empty states.
- `RenameReviewDeck`: queue per scope; auto-advance after a resolving action; index
  clamp; completion state; Rematch opens `RematchModal`.
- `RenameReviewCard`: only status-valid actions render; confidence color + reasons
  expand; **conflict card fetches `conflictPreview` once on activation (previewSeq
  guard), renders the two-column table, flags the recommended side, and Overwrite/
  Keep-both call `applyJob` with the right strategy**; non-conflict card shows plain
  Apply.
- Follow the existing vitest store/component patterns.

## Out of Scope (deferred)

- **Free-text filename editing** — needs a manual set-filename endpoint; the manual
  fix stays Rematch (re-pick the title).
- **Bulk conflict resolution** — `conflict_strategy` is scoped to the single-job
  apply path; bulk-apply stays as-is in v1.
- **Desktop parity for the compare view / Overwrite / Keep-both** — the backend
  endpoints are shared and available, but wiring the desktop `RenameRow`/`RenameCard`
  to them is a follow-up; v1 ships the mobile deck.
- **FEL/MEL in the live preview when uncached** — shown only from the `dv_scan`
  cache; running `dovi_tool` on demand (slow) is out of scope.
- **Plex named-edition tagging for Keep-both** — v1 uses a numeric-suffix version in
  the same folder; a `{edition-…}` naming scheme is a later refinement.
- **A browsable phone list** (the A+C hybrid) and **redesigning the DV/Trash/Process
  panels for mobile** — reused as-is.
