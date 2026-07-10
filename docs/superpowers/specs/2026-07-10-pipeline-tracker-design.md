# Pipeline Tracker — Design

**Status:** Approved (design phase). v1 scope.
**Date:** 2026-07-10

## Goal

Give the user one browsable, categorized list of every grab that didn't cleanly
make it end-to-end — download failed, dead/offline links, extraction stuck,
rename failed, or (the genuinely new check) **renamed and applied but never
verified in Plex** — with a **Re-grab** action and a **Search other sources**
action per item. Today these failure signals exist but are scattered: a failed
download shows only in the Downloads list, a failed rename only in Renames, and
nothing checks whether an applied rename actually reached Plex. A failed grab
also permanently reads as "already downloaded" on future scans (it's written to
`downloads` history at send-time, not completion), so the exact items worth
re-grabbing silently stop being offered.

## Architecture

**No new grab machinery.** Both actions reuse `POST /download` (`DownloadRequest`
— url/title/season/year/resolution/size/hdr/dovi), the same endpoint the Scan
page already uses. Re-grab replays the original grab's stored parameters;
picking an alternative from search replays a `ParsedRelease`'s fields. Neither
path needs new JDownloader integration.

**The reconcile is a read-mostly join**, not a new pipeline stage. A new
`backend/pipeline_service.py` joins four existing tables by the `package_name`
string that already flows through the system (`send_to_jdownloader` computes it
once; JD echoes it back as `download_results.name`; the auto-rename hook passes
it to `process_package` as `rename_jobs.package_name`):

`downloads` (grab, root of the chain) → `download_results` (JD state, by
package_name) → `rename_jobs` (by package_name) → `plex_cache` (by imdb_id,
else title+year — a real row match, not a live PlexAPI call, so the reconcile
is pure SQL).

**One additive schema change**: `downloads` gains a nullable `package_name`
column, written at grab time (the string is already computed at the call site —
this just persists it). Old rows keep `NULL` and simply can't reconcile-link
(they predate this feature; they don't crash it — see Global Constraints).

**Verdicts persist** in a new `pipeline_verdicts` table (hybrid design, keyed by
`downloads.url`) so "verified in Plex" doesn't re-check forever and **Dismiss**
survives even if the underlying rows later age out or get cleared. A verdict is
recomputed only when its category could plausibly change (not yet dismissed,
not yet terminal-verified).

**The reconcile pass piggybacks the existing maintenance loop**
(`app_service._run_maintenance_pass`, hourly + once at startup, already
fail-safe per sub-task) — one more `try/except`-wrapped step, not a new thread.

## Tech Stack

FastAPI, SQLite (`DatabaseManager`), the existing `sources` registry
(`SourceRegistry.search_all`), SvelteKit 5 (runes). Deploy via
`docker compose up -d --build` only.

## Global Constraints

- Deploy in-app changes ONLY via `docker compose up -d --build`.
- **The reconcile is fail-safe per item.** One unlinkable/malformed grab must
  never blank the list or crash the pass — categorize it `unknown`/skip it, log,
  move on. Mirrors `_run_maintenance_pass`'s per-subtask try/except.
- **`package_name` is nullable and best-effort.** Grabs made before this ships
  (or any future path that doesn't set it) get `NULL` and are excluded from the
  reconcile (no crash, no false category) — not retroactively backfilled.
- **A grace period gates the "not in Plex" check.** A rename applied less than
  `pipeline_verify_grace_hours` (config, default 6) ago is never flagged — Plex
  needs time to scan. This is the single most important false-positive guard.
- **Re-grab and search-sources reuse `POST /download`** — no new send-to-JD /
  clipboard / browser fallback logic.
- **`search_all()` is untested in production usage (currently unreferenced
  anywhere in the codebase)** — its endpoint must handle a source throwing,
  timing out, or returning garbage without taking down the request; partial
  results (some sources succeeded) are still useful and must be returned, not
  discarded because one source failed.
- Every new API field is declared on its Pydantic model.
- Tests accompany each unit; deploy only after the changed-module suites are
  green (backend on host: `python -m pytest tests/<file> -v`, no `--timeout`;
  frontend: `npx vitest run`, `npm run check`, `npm run build`).

---

## Components

### 1. Schema (`backend/database.py`)

- `downloads` gains `package_name TEXT` (nullable) via the existing guarded
  `_column_migrations` `ALTER TABLE` list (additive, not a rebuild — no PK
  change, so none of the prior migration's crash-safety machinery is needed
  here; a plain guarded `ALTER TABLE ADD COLUMN` is sufficient and matches how
  every other optional column in this codebase was added).
- New table:
  ```sql
  CREATE TABLE IF NOT EXISTS pipeline_verdicts (
      url TEXT PRIMARY KEY REFERENCES downloads(url),
      category TEXT NOT NULL,       -- see Categories below
      detail TEXT,                  -- error text / stalled-at description
      plex_rating_key TEXT,         -- set once verified
      checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      dismissed INTEGER DEFAULT 0
  )
  CREATE INDEX IF NOT EXISTS idx_pipeline_verdicts_category ON pipeline_verdicts(category)
  ```
  A fresh, additive table — no migration risk, no existing data to preserve.
- `add_to_history` gains a `package_name=None` parameter, included in the
  `INSERT`/`ON CONFLICT DO UPDATE` (`package_name = COALESCE(excluded.package_name, downloads.package_name)`
  — never let a later status-only update null out an already-known name).
- New CRUD: `get_pipeline_verdicts(category=None, include_dismissed=False)`,
  `upsert_pipeline_verdict(url, category, detail, plex_rating_key, dismissed)`,
  `dismiss_pipeline_verdict(url)`, `get_downloads_needing_reconcile(limit)` (rows
  with `package_name IS NOT NULL` and no verdict, or a non-terminal verdict older
  than the recheck window).

### 2. `backend/pipeline_service.py` (new)

- `categorize(download_row, result_row, rename_row, plex_row, grace_hours) -> (category, detail)`
  — **pure function**, the core logic, unit-tested exhaustively:
  - No `download_results` row at all AND `downloads.date_added` is more than
    30 minutes ago (comfortably past the poller's ~8s active cadence — a grab
    JD genuinely never picked up, not one still waiting for the next poll) →
    `never_started`. Within the 30 minutes, no verdict is written yet (still
    too soon to judge) — the reconcile skips it this pass.
  - `download_results.state == 'failed'` → `download_failed` (detail = JD error
    text — this is where dead/offline links surface, per the existing
    offline/not-found/blocked detection already in `poll_results`).
  - `state` in `downloading|extracting` → `in_progress`.
  - No `rename_jobs` row yet but `download_results.state == 'extracted'` →
    `pending_rename`.
  - `rename_jobs.status in ('failed','needs_review')` → `rename_failed`.
  - `rename_jobs.status == 'applied'`: if `processed_at` is within
    `grace_hours` → `in_progress` (too soon to judge); else check `plex_row` —
    found → `verified` (store `rating_key`); not found → `not_in_plex`.
  - Anything else unresolved/malformed → `unknown` (never raises).
- `reconcile_batch(db, limit=500)` — pulls
  `get_downloads_needing_reconcile(limit)`, joins each against
  `download_results`/`rename_jobs`/`plex_cache` (batched `IN` queries, not N+1),
  calls `categorize`, upserts verdicts. Wrapped in the caller's try/except (see
  §4) — this function itself doesn't swallow, so tests see real errors.

### 3. Maintenance-loop hook (`backend/app_service.py`)

- In `_run_maintenance_pass`, add one more fail-safe block (mirrors the trash
  sweep and WAL-checkpoint blocks immediately above it):
  ```python
  try:
      if self.db is not None:
          from backend.pipeline_service import reconcile_batch
          n = reconcile_batch(self.db)
          if n:
              logger.info("Pipeline reconcile: checked %d grab(s)", n)
  except Exception:
      logger.exception("Pipeline reconcile failed (non-fatal)")
  ```
  Runs at startup and hourly, same cadence as trash sweep — no new thread.

### 4. API (`backend/api/routes/pipeline.py`, new router)

- `GET /pipeline/items?category=&include_dismissed=` → verdict rows joined back
  to their `downloads`/`rename_jobs` display fields (title, year, poster if the
  rename job has one, resolution, the original grab url, the error/detail text)
  — one query, no N+1.
- `GET /pipeline/counts` → `{category: count}` for the nav badge / chip counts
  (dismissed excluded).
- `POST /pipeline/{url}/dismiss` → `dismiss_pipeline_verdict`.
- `POST /pipeline/{url}/regrab` → loads the `downloads` row's stored
  title/season/year/resolution/size/hdr/dovi, calls the SAME internal grab path
  `POST /download` already uses (`dl.download_item(...)`), and — critically —
  **supersedes the old grab so it can re-surface as missing if this retry also
  fails**: after a successful re-send, delete the row's `pipeline_verdicts`
  entry so the next reconcile re-evaluates fresh, and reset
  `downloads.status` for that url is NOT touched (the ON CONFLICT/status='completed'
  write from `save_to_history` on the new attempt naturally supersedes it — the
  same `url` is reused, so no duplicate history row).
- `POST /pipeline/{url}/search-sources` → `registry.search_all(title, mode)`
  (title/mode from the `downloads` row), returns `{source: {releases: [...], errors: [...]}}`
  flattened to a single ranked list (dedupe by url) for the frontend. **Never
  raises for a single source's failure** — `search_all` already isolates
  per-source exceptions into that source's `errors` list
  ([registry.py:296-305](backend/sources/registry.py:296)); this endpoint adds a
  request-level timeout and returns whatever succeeded.
- `POST /pipeline/{url}/grab-alternative` → body is a `ParsedRelease.to_dict()`
  shape; maps directly onto `DownloadRequest` (display_title→title, res→resolution,
  etc.) and calls the same internal grab path as regrab.

### 5. Config (`backend/config.py`)

- `pipeline_verify_grace_hours` (default 6) — the not-in-Plex grace window.
- `pipeline_reconcile_enabled` (default True) — off switch, same pattern as
  other maintenance toggles.

### 6. Frontend — new route `/pipeline`

- `frontend/src/routes/pipeline/+page.svelte`: category chips across the top
  with live counts (from `GET /pipeline/counts`), a searchable list below
  (poster/title/year, the stalled stage + detail text, Re-grab / Search sources /
  Dismiss buttons). `verified` is collapsed by default (it's the success bucket,
  not something to act on). Mirrors the existing `StatusDashboard` +
  `RenameFilterBar` visual pattern from the Renames page for consistency.
- `frontend/src/lib/components/pipeline/SourceSearchModal.svelte`: opened by
  "Search sources"; shows a loading state, then the ranked alternative releases
  (source, resolution, size, HDR/DV badges — reusing existing badge components),
  a per-source error line if a source failed but others succeeded, and a Grab
  button per row that posts to `grab-alternative` then closes.
- Desktop: new sidebar nav entry. Mobile: **a segmented switch inside the
  existing Downloads tab** (`Queue | Pipeline`) rather than a 7th tab bar slot —
  reuses `MobileTabBar`'s existing "Downloads" entry, no tab-bar changes needed.
- `frontend/src/lib/api/types.ts` gains `PipelineItem`, `PipelineCounts`,
  `AlternativeRelease` (mirrors `ParsedRelease.to_dict()`); `client.ts` gains
  the five calls above.

---

## Categories (v1, fixed set)

`never_started` · `download_failed` · `in_progress` · `pending_rename` ·
`rename_failed` · `not_in_plex` · `verified` (collapsed/success) · `unknown`
(reconcile couldn't determine — logged, shown last, rare)

## Data Flow

1. A grab writes `downloads.package_name` at send time (unchanged call site,
   one new arg threaded through).
2. Hourly (+ once at startup) maintenance pass calls `reconcile_batch`, which
   joins the four tables per un-terminal grab and upserts a verdict.
3. `/pipeline` page reads verdicts (fast — no live joins on page load).
4. Re-grab / grab-alternative → `POST /download` (existing path) → the next
   reconcile pass picks up the new attempt fresh (old verdict cleared).
5. Search-sources → `registry.search_all` → picker → grab-alternative.

## Error Handling

- Reconcile: per-item categorize never raises (falls to `unknown`); the
  maintenance-loop wrapper swallows and logs so a reconcile bug never breaks
  trash sweep / WAL checkpoint / startup.
- Search-sources: partial results (some sources ok, some errored) are still
  returned; a total failure (all sources errored/timed out) returns an empty
  list + the errors, not a 500 — the modal shows "no results, try again."
- Regrab/grab-alternative surface `POST /download`'s existing error handling
  verbatim (no new failure modes introduced).

## Testing

- **`pipeline_service.categorize`**: one test per category boundary, INCLUDING
  the grace-period edge (applied 5h59m ago → `in_progress`, not `not_in_plex`;
  applied 6h01m ago + no Plex match → `not_in_plex`), a `download_results` row
  whose error text contains "offline"/"not found" reaching `download_failed`
  with that detail preserved, and a malformed/partial row set never raising
  (falls to `unknown`).
- **`reconcile_batch`**: batched (no N+1) — assert query count; a single
  malformed row doesn't stop the rest of the batch from being verdicted; a
  `dismissed` verdict is not recomputed even if its category would change; a
  `verified` verdict is not recomputed (terminal).
- **API**: `/pipeline/items` filter + join correctness; `/pipeline/{url}/regrab`
  calls the grab path with the stored parameters and clears the old verdict;
  `/pipeline/{url}/search-sources` returns partial results when one mocked
  source raises; `grab-alternative` correctly maps a `ParsedRelease` dict onto
  a grab call.
- **Frontend**: category chip counts render; Dismiss removes an item and
  persists (mock the API); the search modal renders partial results + a
  per-source error without crashing; Grab-alternative closes the modal on
  success.

## Out of Scope (deferred)

- Retroactively backfilling `package_name` on pre-existing `downloads` rows —
  not reliably derivable; those grabs simply won't reconcile-link.
- Bulk actions (regrab-all in a category) — v1 is per-item.
- Auto-selecting/auto-grabbing the "best" alternative from search — v1 is
  always a manual picker (matches the earlier decision on this).
- Wiring `search_all` into the main Scan flow beyond this feature.
