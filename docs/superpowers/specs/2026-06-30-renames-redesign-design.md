# ScanHound Renames Section Redesign — Design Spec

## 1. Overview & Goal

The Renames page (`frontend/src/routes/renames/+page.svelte`) is ScanHound's review queue for the rename/identify pipeline: it lists `rename_jobs` rows produced when the backend parses a downloaded file, matches it against TMDB, and proposes a Plex-correct `new_filename` + `destination_path`. Today it is a single 497-line monolith rendering a flat divide-y list of plain-text rows with inline-styled badges, five filter tabs, two collapsible "process folder" / "Dolby Vision" panels, and a raw-TMDB-ID rematch form. This redesign turns it into a **rich review queue**: fast, colorful, multi-selectable, with real poster art, a status dashboard with click-to-filter stat cards, category chips with live counts, dense-list and poster-grid views (reusing the Scan page's grid/prefs machinery), and a search-driven Rematch picker. The goal is to make triaging dozens of pending renames a glanceable, batch-friendly operation while reusing every already-built Scan-page primitive (grid prefs, `persisted()`, `Badge`, status variants) instead of reinventing them.

**Deploy mechanism (locked):** every change here ships **only** via `docker compose up -d --build` from `X:\Docker Apps\ScanHound`. The frontend is baked into the image; `docker restart` deploys nothing.

## 2. Current State (code-grounded)

**Frontend** — `frontend/src/routes/renames/+page.svelte` (497 lines, fully inline):
- Header (lines 219–244): title "Renames" + four plain buttons: "Process folder…" (toggles `folderOpen`), "Dolby Vision…" (toggles `dvOpen`), "Re-identify all" (`reidentifyAll()`), "Refresh".
- Filter tabs (lines 371–380): array `filters` of `all / needs_review / matched / applied / failed`, each showing `$renameStatus?.counts[f.value]`; `shown = $derived(...)` filters `$renameJobs` by `status`.
- Job list (lines 388–496): one `<li>` per job with inline-styled status badge (`statusClass(job)`, lines 150–156), optional LLM/duplicate/keep badges, `Math.round(job.match_confidence)`, uppercased `media_type`, `relTime(job.detected_at)`, `original_filename` and `→ new_filename`, warning/error lines, and per-row action buttons (Apply, Re-identify, Rematch, Accept-combined, Accept-correction, Undo, Remove).
- Rematch UI (lines 467–492): expands on `rematchOpenId === job.id`; a manual `<select>` (Movie/TV) + **numeric TMDB ID text input** + Submit/Cancel. `tmdbSearchUrl(job)` (lines 197–202) only builds an external themoviedb.org link. `submitRematch()` does `parseInt()` then `rematchJob(jobId, id, mediaType)`.
- Store: `frontend/src/lib/stores/renames.ts` (143 lines) — `renameJobs`, `renameStatus`, `rematchJob()` (lines 64–67), WebSocket `connection.on('rename:job', …)` (lines 86–99).
- Types: `frontend/src/lib/api/types.ts` (RenameJob, lines 68–114). **No `poster_url`/`poster_path` field.**

**Backend:**
- Schema `backend/database.py:350–381` — `rename_jobs` has `status, media_type, title, year, season, episode, tmdb_id, imdb_id, resolution, match_confidence, match_source, move_method, destination_path, new_filename, original_filename, warning_message, error_message, detected_at, processed_at, reverted_at`, plus JSON columns `proposed_match, suggested_correction, combined_episode, split_file, plex_sort_title`. **No `poster_path`.**
- Service `backend/rename/service.py`: `_identify()` (line 604) → `_normalize_candidate()` (line 476) extracts only `title, year, tmdb_id, media_type` — **poster_path is discarded**. `rematch()` (line 1424) fetches TMDB details, rebuilds target, sets `match_confidence=100.0`, `match_source="manual"`, `status="matched"` — **does NOT re-check the library-not-configured guard** that exists only in `_process_file_inner()` (lines 1314–1327). `apply()` (1357), `undo()` (1407), `reidentify()` (972), `reidentify_all()` (1000, only `needs_review`+`failed`).
- Routes `backend/api/routes/rename.py`: only **single-job** mutations exist (`apply`, `undo`, `rematch`, `accept-combined`, `accept-correction`, DELETE, `reidentify`), plus `reidentify-all`, `process-folder`, `dv-scan-folder`, `dv-scans`, `status`, `health`, `llm/test`. **No bulk endpoints. No TMDB-search endpoint.**

**Shortcomings this redesign fixes:** no poster art; no multi-select / batch operations (every action is one-at-a-time); no glanceable status summary; no category facets; list-only (no grid); rematch forces the user to leave the app, find a numeric ID on themoviedb.org, and paste it back blind with no preview; rematch can silently place files into an unconfigured library.

## 3. Scope

**In scope (this redesign):**
- `rename_jobs` migration: add **`poster_path`** (the only missing column).
- Backend: capture `poster_path` during identification; new bulk endpoints (apply / reidentify / delete / set-destination); `apply-confident` endpoint; a rematch TMDB-search endpoint; library-guard re-check inside `rematch()`.
- Frontend: status dashboard stat cards (click-to-filter) + DV inventory card; category/search/sort filter bar; multi-select + bulk-action bar; list and poster-grid views with a persisted toggle; redesigned colorful row + badge cluster with real poster thumbnails; search-picker Rematch modal with live filename/destination preview.
- Component decomposition of the monolith into small single-purpose components.

**Explicitly out of scope (adjacent DV next-steps track — separate spec):** validating `dovi_tool` output, populating `dv_scan.year`, FEL/MEL Plex labeling / Kometa badges, and any deeper Dolby Vision detection work. The DV **inventory card** (FEL/MEL counts read from the existing `dv_scan` inventory via `GET /rename/dv-scans`) and the existing "Dolby Vision" scan trigger are in scope only as read-only surfaces; their underlying detection pipeline is the separate track. Per locked decision, do not fold the DV next-steps in.

## 4. Architecture & Data Flow

### 4.1 Frontend component breakdown

Decompose the monolith. `+page.svelte` becomes a thin orchestrator (loads data, owns top-level derived state, lays out the children). All new components live under `frontend/src/lib/components/renames/`.

**Files that change:**
- `frontend/src/routes/renames/+page.svelte` — slimmed to orchestrator: header, dashboard, filter bar, bulk bar, and a `{#if viewMode==='grid'}` switch between list and grid containers. Keeps the existing `process-folder` / `dv-scan` calls but moves their UI into `ProcessMenu` / DV card.
- `frontend/src/lib/stores/renames.ts` — add multi-select + view-mode + bulk action functions (see §6).
- `frontend/src/lib/api/types.ts` — add `poster_url?: string | null` to `RenameJob`; add `TmdbSearchResult` and bulk-response types.
- `frontend/src/lib/api/client.ts` — add bulk/apply-confident/search/set-destination calls (see §5).

**New components (small, single-purpose):**
- `RenamesHeader.svelte` — `Process ▾` split-button (folder / files / paste-path) + "Dolby Vision" + "Re-identify all" + view toggle + Refresh.
- `ProcessMenu.svelte` — the `Process ▾` dropdown body (the three modes), wrapping the existing `process-folder` path input/preview logic.
- `StatusDashboard.svelte` — the four colored stat cards + DV inventory card.
- `StatCard.svelte` — one clickable colored count card (used ×4).
- `RenameFilterBar.svelte` — category chips (All/Movies/TV/4K/1080p/Remux) + title search box + sort `<select>`.
- `BulkBar.svelte` — appears when ≥1 selected; the five bulk actions.
- `RenameRow.svelte` — dense list row (checkbox, poster thumb, title, badge cluster, old→new diff, per-row Apply/Rematch).
- `RenameCard.svelte` — poster-grid tile (poster, title, badge cluster overlay, checkbox).
- `BadgeCluster.svelte` — the status/confidence/media·resolution/DV/keep·dup badge group, shared by row + card (DRY).
- `RematchModal.svelte` — search picker, media-type toggle, season/episode override, live preview, confirm.
- `RenamePoster.svelte` — poster `<img>` with placeholder fallback, built from `poster_url`.

### 4.2 Backend changes

- `database.py`: one new migration entry + `poster_path` in the create-table column list.
- `rename/service.py`: `_normalize_candidate()` keeps `poster_path`; `_identify()`/`_process_file_inner()` persist it into the job; `rematch()` also stores `poster_path` and re-runs the library guard; new `apply_confident()`, `set_destination()`, and bulk helpers (thin loops over existing single-job methods, single-flighted via `_bulk_lock`); `search_tmdb_public()` wrapping `_search_tmdb`/`tmdb_client.search()` and returning poster paths.
- `api/routes/rename.py`: new bulk, apply-confident, set-destination, and search routes.

### 4.3 Request/response flow — bulk actions

1. User multi-selects rows → `selectedJobIds` (Set<number>) in the store.
2. Bulk bar button → store action (e.g. `bulkApply()`) → `client.bulkApply([...ids])` → `POST /rename/jobs/bulk/apply`.
3. Backend single-flights via `_bulk_lock`, loops `apply(id)` per id, collects per-id `{id, ok, error}`.
4. Response `{ results: [...], applied: n, failed: m }` → store toasts a summary, clears selection, `refresh()`.
5. WebSocket `rename:job` events already stream per-job updates as each completes, so the list updates live during a long bulk run.

### 4.4 Request/response flow — rematch search

1. User opens `RematchModal` for a job → seeds query from the job title, media-type from `job.media_type`.
2. Debounced typing → `client.searchTmdb(query, mediaType)` → `GET /rename/search-tmdb` → results with `id, title, year, media_type, poster_path`.
3. User clicks a result (or pastes a TMDB/IMDB id) → modal calls `client.rematchPreview(jobId, {tmdb_id, media_type, season?, episode?})` → backend builds (without persisting) the would-be `new_filename` + `destination_path` and returns them + a `library_configured` flag.
4. Live preview renders. On **Confirm** → `POST /rename/jobs/{id}/rematch` (existing path, extended) which **re-checks the library guard**; if unconfigured it returns the job in `needs_review` with the warning rather than a bad placement.

## 5. Backend Changes

### 5.1 Schema migration (the ONLY missing column)

`rename_jobs` already has `status, media_type, resolution, match_confidence, imdb_id, tmdb_id, destination_path, warning_message` (verified `database.py:350–381`). The **only** missing column is `poster_path`.

- Add to the create-table column list in `database.py` (so fresh installs have it):
  `poster_path TEXT`
- Add an idempotent migration entry alongside the existing `ALTER TABLE rename_jobs ADD COLUMN …` migrations (~line 428):
  `'ALTER TABLE rename_jobs ADD COLUMN poster_path TEXT'`

No other columns are added. `media_type` and `resolution` already power the category chips; `match_confidence` already powers apply-confident.

### 5.2 Capturing `poster_path`

- `_normalize_candidate()` (`service.py:476–486`) currently extracts `title, year, tmdb_id, media_type`. Add `poster_path` to the returned dict, sourced from the raw TMDB result (`result.get("poster_path")`).
- `_identify()` (line 604) propagates `poster_path` through its candidate so the chosen match carries it.
- `_process_file_inner()` (line 1041) writes `poster_path` into the job dict passed to `_create(job)`.
- `rematch()` (line 1424) already fetches TMDB details; capture `details.get("poster_path")` and include it in the `update_rename_job(...)` call.
- The frontend builds a poster **URL** from `poster_path` the same way the Scan page derives `poster_url` (TMDB image base + size, e.g. `https://image.tmdb.org/t/p/w342{poster_path}`). To keep the frontend reusing the Scan page's exact poster source, expose `poster_url` (fully-formed) in the serialized job from `GET /rename/jobs` (build it in the route serializer from stored `poster_path`), mirroring how `ScanResult.poster_url` is produced. The DB stores the raw `poster_path`; the API returns `poster_url`.

### 5.3 New endpoints

All paths under the existing `rename` router. Bulk handlers single-flight via the existing `_bulk_lock` and reuse the single-job service methods.

**Bulk apply**
- `POST /rename/jobs/bulk/apply`
- Request: `{ "ids": [number] }`
- Response: `{ "results": [{ "id": n, "ok": bool, "error": string|null }], "applied": n, "failed": m }`

**Bulk reidentify**
- `POST /rename/jobs/bulk/reidentify` — background, single-flighted.
- Request: `{ "ids": [number] }`
- Response: `{ "ok": true, "queued": n }`

**Bulk delete**
- `POST /rename/jobs/bulk/delete`
- Request: `{ "ids": [number] }`
- Response: `{ "deleted": n }`

**Bulk set-destination**
- `POST /rename/jobs/bulk/set-destination`
- Request: `{ "ids": [number], "destination_root": string }` (the chosen library root; backend rebuilds each job's `destination_path` under it via `_naming.build_target()`, re-running the library guard per job).
- Response: `{ "results": [{ "id": n, "ok": bool, "destination_path": string|null, "error": string|null }], "updated": n }`

**Apply confident**
- `POST /rename/jobs/apply-confident`
- Request: `{}` (operates over all jobs) **or** `{ "ids": [number] }` (restrict to a selection).
- Behavior: selects jobs with `status == "matched"` **AND** `match_confidence >= 95`. **Never** applies `needs_review`. The 95 threshold is enforced server-side (do not trust the client). Then loops `apply(id)`.
- Response: `{ "results": [...], "applied": n, "skipped": k, "failed": m }` where `skipped` counts jobs filtered out by the threshold/status gate.

**Rematch TMDB search**
- `GET /rename/search-tmdb?query={q}&media_type={movie|tv}`
- Wraps `service.search_tmdb_public()` → `tmdb_client.search()`.
- Response: `{ "results": [{ "tmdb_id": n, "title": string, "year": number|null, "media_type": "movie"|"tv", "poster_url": string|null }] }` (route serializer turns `poster_path` into `poster_url`).

**Rematch preview (non-persisting)**
- `POST /rename/jobs/{job_id}/rematch-preview`
- Request: `{ "tmdb_id": number, "media_type": "movie"|"tv", "season"?: number, "episode"?: number }`
- Behavior: fetch TMDB details, build target via `_naming.build_target()` **without** writing the DB; run the library-guard check.
- Response: `{ "new_filename": string, "destination_path": string|null, "library_configured": bool, "warning": string|null }`

**Rematch confirm (existing path, extended)**
- `POST /rename/jobs/{job_id}/rematch`
- Request: `{ "tmdb_id": number, "media_type"?: "movie"|"tv", "season"?: number, "episode"?: number }`
- **Change:** `rematch()` MUST re-run the same library-not-configured guard used in `_process_file_inner()` (lines 1314–1327): for TV check `auto_rename_tv_library`, for Movie check `_movie_root(resolution)`. If unconfigured, set `status="needs_review"` with the warning message instead of `status="matched"`, and do not produce a placement under a bad root. Also persist `poster_path` from the fetched details. Optional `season`/`episode` override the parsed values for TV before `build_target()`.
- Response: `{ "ok": bool, "status": string, "new_filename": string, "destination_path": string|null, "warning": string|null }`

## 6. Frontend Changes

### 6.1 Store additions (`stores/renames.ts`)

Reuse the Scan page's `persisted<T>()` helper (`stores/results.ts:22–33`, SSR-safe).

- `selectedJobIds = writable<Set<number>>(new Set())` + `toggleSelect(id)`, `selectAll(ids)`, `clearSelection()`.
- `viewMode = persisted<'list'|'grid'>('sh-renames-view', 'list')`.
- `renameSort = persisted<'detected_desc'|'detected_asc'|'confidence_desc'|'title_asc'>('sh-renames-sort', 'detected_desc')`.
- `renameCategory = persisted<'all'|'movies'|'tv'|'4k'|'1080p'|'remux'>('sh-renames-category', 'all')`.
- `renameQuery = writable<string>('')` (title search; not persisted).
- Bulk actions: `bulkApply()`, `bulkReidentify()`, `bulkDelete()`, `bulkSetDestination(root)`, `applyConfident(ids?)` — each calls the matching client method then `refresh()` and `clearSelection()`.
- Rematch search state lives **inside `RematchModal.svelte`** (local), not the global store: `query`, `results`, `searchBusy`, `selectedResult`, `mediaType`, `season`, `episode`, `preview`.

For grid prefs, import directly from the Scan page's results store (already exported and reusable):
`import { tileSize, posterAspect, tileShowMeta, gridGap, gridColumns, TILE_MIN_PX, POSTER_ASPECT_CLASS, GRID_GAP_CLASS, GRID_COLUMN_CHOICES, persisted } from '$lib/stores/results';`

### 6.2 Status dashboard (`StatusDashboard.svelte` + `StatCard.svelte`)

Four colored `StatCard`s driven by `$renameStatus.counts` (already loaded): **Needs review**, **Matched**, **Applied**, **Failed**. Each card shows label + count and is a button: clicking sets the **status filter** (the same five-status filter the old tabs drove — keep that status filter as derived state in `+page.svelte`, now surfaced via cards instead of tabs). The active card is highlighted with its status color border (see §7). Plus a fifth **DV inventory card**: reads `GET /rename/dv-scans` layer counts and shows FEL / MEL counts (read-only; clicking it scrolls to / opens the existing Dolby Vision scan surface, it is not a job filter).

### 6.3 Category / filter derivation (`RenameFilterBar.svelte`)

Chips: **All / Movies / TV / 4K / 1080p / Remux**, each with a live count. Counts and membership are derived purely client-side from each job's `media_type` + `resolution` (both already on the job):

- **Movies**: `media_type === 'movie'`.
- **TV**: `media_type === 'tv' || media_type === 'show'`.
- **4K**: `resolution` matches `2160p`/`4k`/`uhd` (case-insensitive).
- **1080p**: `resolution` matches `1080p`.
- **Remux**: `resolution` (or `new_filename`/`original_filename` when resolution lacks it) contains `remux` (case-insensitive). Derive once in a helper `categoryOf(job): Set<category>` since a job can belong to several chips (e.g. a 4K movie counts under Movies and 4K).

Live counts: `count(cat) = $renameJobs.filter(j => categoryOf(j).has(cat)).length`; All = total.

Also in the bar: a **title search box** bound to `renameQuery` (matches `title`, `original_filename`, `new_filename`, case-insensitive) and a **sort `<select>`** bound to `renameSort`.

Final visible set: `shown = $derived(sort(applyQuery(applyCategory(applyStatus($renameJobs)))))`.

### 6.4 Multi-select + bulk bar (`BulkBar.svelte`)

Checkbox on every `RenameRow`/`RenameCard` toggling `selectedJobIds`; a **select-all** checkbox in the list header selects the currently `shown` set. When `selectedJobIds.size > 0`, render `BulkBar` (sticky) with **exactly**: **Apply**, **Re-identify**, **Set destination**, **Apply confident**, **Delete**.

- **Apply** → `bulkApply([...selected])`.
- **Re-identify** → `bulkReidentify([...selected])`.
- **Set destination** → opens a small root-picker (select of known library roots) → `bulkSetDestination(root)`.
- **Apply confident** → `applyConfident([...selected])`; server enforces `matched` + `confidence>=95`. Tooltip states this so the user knows `needs_review`/low-confidence rows are skipped.
- **Delete** → confirm dialog → `bulkDelete([...selected])`.

### 6.5 List + grid views with toggle

Reuse the Scan page's grid machinery verbatim. The grid container computes:
```
let effectiveColumns = $derived($gridColumns !== 'auto' ? $gridColumns : 0);
let gridStyle = $derived(effectiveColumns > 0
  ? `grid-template-columns: repeat(${effectiveColumns}, 1fr)`
  : `grid-template-columns: repeat(auto-fill, minmax(${TILE_MIN_PX[$tileSize]}px, 1fr))`);
let gridGapClass = $derived(GRID_GAP_CLASS[$gridGap]);
```
Grid children wrap with `min-w-0` (the Scan page's overflow fix). The view toggle (`list ⟷ grid`) in `RenamesHeader` writes `viewMode` (persisted). `{#if $viewMode==='grid'}` renders a grid of `RenameCard`; else a divide-y list of `RenameRow`. Poster aspect/tile-size/gap/columns reuse the existing persisted stores so the user's Scan-page grid prefs carry over.

### 6.6 Poster rendering (`RenamePoster.svelte`)

Mirror `ResultTile.svelte` poster logic (lines 135–158): an `<img>` with `class={POSTER_ASPECT_CLASS[$posterAspect]} object-cover`, `src={job.poster_url}`, and a placeholder `<div>` fallback when `poster_url` is null/empty. Same TMDB image source the Scan page already uses (resolved server-side from `poster_path`, §5.2).

### 6.7 Redesigned row + badge cluster (`RenameRow.svelte` / `RenameCard.svelte` / `BadgeCluster.svelte`)

**Row layout (list):** `[checkbox] [poster thumb] [title + (year) | old→new diff] [BadgeCluster] [Apply] [Rematch]`. The old→new diff renders `original_filename` and `new_filename` with the differing segment emphasized.

**BadgeCluster** (shared, built on the existing `Badge` component — `lib/components/Badge.svelte`, variants `default|success|warning|error|accent|info|orange`):
- **Status** badge — `formatStatus(job.status)` + `variant` from the rename status map (§7).
- **Confidence %** — `Math.round(job.match_confidence)%`, variant by threshold (≥95 success, 70–94 warning, <70 error).
- **Media·resolution** — e.g. `MOVIE · 2160p` (info/default).
- **DV layer** — only if known (joined from DV inventory by path): `FEL`/`MEL`/`P8` etc. with the DV color (§7); omitted when unknown.
- **Keep / Duplicate** — `keep_recommended` → success badge `★ Keep`; `destination_conflict` → orange badge `⚠ Duplicate`.

`RenameCard` shows the poster large with title beneath and the same `BadgeCluster` (compact) overlaid/below, plus the checkbox top-left (mirroring `ResultTile` selection UX).

### 6.8 Search-based Rematch modal (`RematchModal.svelte`)

Replaces the numeric-ID form. Fields:
- **Search box** — debounced; seeded from job title. Calls `client.searchTmdb(query, mediaType)`.
- **Media-type toggle** — Movie ⟷ TV (re-runs search).
- **Results list** — each result a clickable card: `poster + title + (year) + media-type`. Also accept a pasted **TMDB id** (numeric) or **IMDB id** (`tt…`) in the search box as a direct pick.
- **Season / Episode override** (TV only) — optional number inputs.
- **Live preview** — on selection, calls `rematch-preview`; shows resulting `new_filename` and `destination_path`, plus a warning banner if `library_configured===false`.
- **Confirm** — calls `POST /rename/jobs/{id}/rematch` (which re-checks the guard server-side); on success closes + toasts + refreshes. **Cancel** closes without changes.

## 7. Colorful / Visual System

Reuse `lib/components/Badge.svelte` variants (`default|success|warning|error|accent|info|orange`) and the existing CSS color vars (`--accent`, `--text-secondary`, status tints) referenced in `constants.ts`/`FilterBar.svelte`. Add a **rename status → variant** map in `constants.ts` (mirroring the existing `STATUS_VARIANTS` pattern at `constants.ts:6–43`; do not overload the scan-status map):

```
export const RENAME_STATUS_VARIANTS: Record<string, BadgeVariant> = {
  needs_review: 'warning',   // amber — action required
  matched:      'accent',    // accent — ready to apply
  applied:      'success',   // green — done
  reverted:     'default',   // neutral
  failed:       'error',     // red
  pending:      'info',      // blue
};
```

- **Stat cards** use the same color per status (Needs review = warning/amber, Matched = accent, Applied = success/green, Failed = error/red), with the active card getting a colored border via the existing `statusBorderColor()`-style helper pattern.
- **Confidence** badge: `success` ≥95, `warning` 70–94, `error` <70.
- **DV layers → variants:** FEL → `error` (strongest, profile-7 dual-layer), MEL → `orange`, P8 → `accent`, P5 → `info`, "No DV"/unknown → `default`. Keep these in a small `DV_LAYER_VARIANTS` map next to `RENAME_STATUS_VARIANTS`.
- **Keep** = `success` (`★`), **Duplicate** = `orange` (`⚠`), **LLM/manual source** = `info`.

Category chips reuse the Scan `FilterBar` quick-chip active styling: active = `bg-[var(--accent)]/15 border-[var(--accent)] text-[var(--accent)]`, inactive = `border-transparent text-[var(--text-secondary)]`.

## 8. Edge Cases & Safety

- **Empty states:** no jobs at all → friendly empty panel with a "Process ▾" call-to-action; a filter/category/search yielding zero → "No jobs match these filters" with a clear-filters button (don't show the bulk bar).
- **Failed jobs:** `failed` rows surface `error_message` (red) and still offer Re-identify / Rematch / Delete; bulk Apply skips non-applyable statuses server-side (reported in `results[].error`).
- **Library-not-configured guard (critical):** enforced server-side in (a) `rematch()` confirm, (b) `rematch-preview` (returns `library_configured:false` + warning so the modal blocks/annotates confirm), and (c) `bulk/set-destination` per job. Never place a file under an unconfigured/empty root; force `needs_review` + warning instead. This closes the verified gap where `rematch()` previously bypassed the guard.
- **Apply-confident safety:** threshold (`matched` + `confidence>=95`) is enforced **server-side**; the client filter is cosmetic only.
- **Bulk concurrency:** all bulk mutations single-flight via the existing `_bulk_lock`; the UI disables the bulk bar while a bulk op is in flight and relies on streamed `rename:job` events for live progress.
- **Fail-safe / no pipeline crash:** capturing `poster_path` is best-effort — if TMDB omits it or the field is missing, store `null` and render the placeholder; identification, build_target, apply, and the migration must never raise because a poster is absent. The migration is idempotent (`ADD COLUMN` guarded like the existing rename_jobs migrations).
- **Deploy reminder:** ship via `docker compose up -d --build` only; `docker restart` will not pick up the rebuilt frontend or new routes.

## 9. Testing Strategy

**Backend:**
- *Migration:* fresh DB has `poster_path`; an existing DB without it gets the column added idempotently (running migrations twice is a no-op).
- *poster_path capture:* `_normalize_candidate()` retains `poster_path`; a matched job persists it; `GET /rename/jobs` serializes a non-null `poster_url` when `poster_path` is set and null otherwise.
- *Bulk endpoints:* `bulk/apply`, `bulk/reidentify`, `bulk/delete`, `bulk/set-destination` each return correct per-id results; partial failures are reported, not fatal.
- *Apply-confident threshold:* jobs at `matched`/96 apply; `matched`/94 skipped; `needs_review`/99 skipped (status gate beats confidence); response `skipped` count is correct.
- *Rematch guard:* with TV library unset, `rematch` confirm and `rematch-preview` both return `needs_review`/`library_configured:false` and produce no placement; with library set, status becomes `matched` and `destination_path` is under the configured root. Season/episode override changes the built TV filename.
- *Search endpoint:* `/rename/search-tmdb` returns results with `poster_url`; empty query / no TMDB client returns `[]` without error.

**Frontend:**
- *Filtering:* `categoryOf()` membership + live counts for Movies/TV/4K/1080p/Remux (incl. a 4K movie counting in both Movies and 4K); status-card filtering; title search; sort order.
- *Multi-select:* toggle, select-all over the `shown` set, clear-on-action; bulk bar visibility tied to `selectedJobIds.size`.
- *Grid toggle:* `viewMode` persists across reload; grid uses the shared `gridStyle`/`gridGapClass`; `min-w-0` prevents overflow; Scan-page tile prefs apply.
- *Rematch preview:* selecting a result populates the live `new_filename`/`destination_path`; `library_configured:false` blocks/annotates Confirm; pasted TMDB/IMDB id short-circuits search; Movie⟷TV toggle re-queries; TV season/episode override reflected in preview.
- *Poster fallback:* null `poster_url` renders the placeholder, never a broken image.

## 10. Locked Decisions & Residual Risks

**Locked decisions (resolved 2026-06-30):**

- **DV layer on rows — join `dv_scan` by path, read-only.** The DV layer badge is derived by joining the existing `dv_scan` inventory on the job's path at serialize time; the badge is omitted when there's no match ("unknown"). **Do NOT add a `dv_layer` column to `rename_jobs`** in this redesign — that stays in the separate DV track's scope (YAGNI here).
- **`set-destination` picker — friendly labels.** The root picker presents friendly names (e.g. `TV`, `Movies 4K`, `Movies 1080p`) mapped to the configured roots (`auto_rename_tv_library`, movie roots by resolution). The backend still rebuilds the real `destination_path` and re-runs the per-job library guard regardless of how the root is labeled in the UI.
- **`apply-confident` default scope — current selection, plus a card shortcut.** The bulk-bar **Apply confident** button operates on the **current selection** only (server still enforces `matched` + `match_confidence >= 95`). Additionally, the **Matched** stat card carries an **"Apply all confident"** shortcut that runs the same threshold across all matched jobs on the page (calls `apply-confident` with no `ids`).

**Residual risk / implementation note:**

- **Poster image size:** reuse the exact TMDB image base + size the Scan page's serializer already requests (so thumbnails come from cache); do not introduce a new size. Verify the exact base/size in the Scan serializer during implementation and mirror it in the rename-job serializer.
