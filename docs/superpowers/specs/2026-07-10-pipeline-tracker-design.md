# Pipeline Tracker — Design

**Status:** Approved (design phase), revised after an adversarial review found
the v1 draft's regrab/join mechanism wouldn't actually function. v1 scope.
**Date:** 2026-07-10

## Goal

Give the user one browsable, categorized list of every grab that didn't cleanly
make it end-to-end — download failed, dead/offline links, extraction stuck,
rename failed, or (the genuinely new check) **renamed and applied but never
verified in Plex** — with a **Re-grab** action and a **Search other sources**
action per item. Today these failure signals exist but are scattered: a failed
download shows only in the Downloads list, a failed rename only in Renames, and
nothing checks whether an applied rename actually reached Plex. A failed grab
also permanently reads as "already downloaded" (`downloads.status='completed'`
is written at *send* time, not completion — [download_service.py:2000-2003](backend/download_service.py:2000)),
so the exact items worth re-grabbing silently stop being offered — and, as
detailed below, silently **block** a same-URL re-grab too.

## Architecture

**No new grab machinery** — but the existing grab path (`download_item`) has
two dedup gates that must be explicitly bypassed for a pipeline-initiated grab,
or the buttons this feature exists to add are no-ops (see §4). Re-grab and
"grab alternative" both call the same internal `dl.download_item(...)` the
existing `POST /download` route calls, backgrounded the same way
(`background_tasks.add_task`, [downloads.py:126](backend/api/routes/downloads.py:126))
with a new `force=True` parameter.

**The reconcile is a read-mostly join**, not a new pipeline stage. A new
`backend/pipeline_service.py` joins four existing tables, keyed primarily by
**`package_uuid`** (uuid-first, the same fix `download_results` itself already
uses — see §2) with `package_name` + a per-attempt timestamp as the initial
discovery path, since `package_uuid` is only known *after* JD assigns it.

**One additive schema change**: `downloads` gains two nullable columns —
`package_name TEXT` and `last_grabbed_at TIMESTAMP` — written at grab time (the
name string is already computed at the call site; this just persists it, at
its actual truncated form). Old rows keep both `NULL` and simply can't
reconcile-link (they predate this feature; they don't crash it).

**Verdicts persist** in a new `pipeline_verdicts` table (hybrid design, keyed by
`downloads.url`), which ALSO stores the discovered `package_uuid` once found —
this is what turns the join uuid-stable after the first successful match, so a
same-name collision (e.g. a regrab, or an alternative release of the same
title/year/resolution) can't cross-contaminate a different grab's verdict on
later passes.

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
  never blank the list or crash the pass — categorize it `unknown`, log, move
  on. Mirrors `_run_maintenance_pass`'s per-subtask try/except.
- **`package_name`/`last_grabbed_at` are nullable and best-effort.** Grabs made
  before this ships get `NULL` and are excluded from the reconcile — not
  retroactively backfilled.
- **One canonical package-name computation.** `send_to_jdownloader` truncates
  the name to 50 chars in BOTH delivery paths
  ([download_service.py:303](backend/download_service.py:303),
  [:326](backend/download_service.py:326)) before JD ever sees it — so
  persisting the *untruncated* string (the original draft's bug) silently
  breaks the join for any title/year/resolution combination over 50 chars (a
  real, common case — e.g. "The Lord of the Rings: The Return of the King
  (2003) [2160p]" is 61 chars). A single `compute_package_name(title, year,
  resolution) -> str` helper, truncated identically, is used at BOTH the
  `downloads`-persist call site and the `send_to_jdownloader` call site, so
  they can never drift apart again.
- **A grace period gates the "not in Plex" check, and it gates on Plex-cache
  FRESHNESS, not elapsed time.** The original "6 hours since `processed_at`"
  design is wrong: `plex_cache` is only refreshed by a full scan
  ([plex_service.py](backend/plex_service.py), [scanner_service.py:353-405](backend/scanner_service.py:353)),
  and the background scanner that would do this automatically is **off by
  default**. A user who applies a rename and doesn't scan again for days would
  see `not_in_plex` after 6 hours even though Plex ingested the file that
  night. The gate must instead compare `rename_jobs.processed_at` against
  `DatabaseManager.get_plex_cache_max_timestamp()` — only emit `not_in_plex`
  when the cache has actually been refreshed *since* the rename applied (plus a
  small margin for Plex's own scan lag). See §2 for the exact comparison.
- **Re-grab and grab-alternative reuse `dl.download_item(...)` with
  `force=True`** — no new send-to-JD / clipboard / browser fallback logic, but
  a new parameter to bypass the two dedup gates that otherwise make both
  actions silent no-ops (§4).
- **`search_all()` is currently unreferenced anywhere in the codebase** — this
  feature is its first real caller. Its endpoint must handle a source throwing,
  timing out, or returning garbage without taking down the request (per-source
  isolation already exists in `search_all` itself, verified —
  [registry.py:296-305](backend/sources/registry.py:296)); partial results are
  still returned. **Sources requiring an authenticated Selenium session
  (adithd) are excluded from v1 search-sources** — that plumbing only exists
  inside the scraper's own flow ([download_service.py:1694-1707](backend/download_service.py:1694))
  and a bare registry call would just time out unauthenticated; document this
  as an expected gap rather than a bug.
- Every new API field is declared on its Pydantic model.
- URL/query-string identifiers go in the **request body**, never a path
  parameter — this deployment sits behind NPM + a Cloudflare tunnel, where an
  encoded release URL (with slashes) as a path segment is fragile.
- Tests accompany each unit; deploy only after the changed-module suites are
  green (backend on host: `python -m pytest tests/<file> -v`, no `--timeout`;
  frontend: `npx vitest run`, `npm run check`, `npm run build`).

---

## Components

### 1. Schema (`backend/database.py`)

- `downloads` gains two columns via the existing guarded `_column_migrations`
  `ALTER TABLE` list (additive, no PK change — a plain guarded `ALTER TABLE ADD
  COLUMN`, same mechanism as every other optional column in this table):
  - `package_name TEXT` — the canonical (truncated) name, written at grab time.
  - `last_grabbed_at TIMESTAMP` — bumped on every grab **attempt** for this
    url (initial grab AND every regrab), used to disambiguate which
    `download_results`/`rename_jobs` rows belong to *this* attempt when the
    name string collides with an earlier attempt or a different release of the
    same title/year/resolution.
- New table:
  ```sql
  CREATE TABLE IF NOT EXISTS pipeline_verdicts (
      url TEXT PRIMARY KEY REFERENCES downloads(url),
      category TEXT NOT NULL,       -- see Categories below
      detail TEXT,                  -- error text / stalled-at description
      package_uuid TEXT,            -- discovered download_results.package_uuid, once matched
      plex_rating_key TEXT,         -- set once verified
      checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      dismissed INTEGER DEFAULT 0
  )
  CREATE INDEX IF NOT EXISTS idx_pipeline_verdicts_category ON pipeline_verdicts(category)
  ```
  A fresh, additive table — no migration risk, no existing data to preserve.
- `compute_package_name(title, year, resolution) -> str` — new pure helper
  (module-level, `backend/download_service.py`), extracted verbatim from the
  existing inline computation at
  [download_service.py:1976-1980](backend/download_service.py:1976):
  `f"{title} ({year}) [{resolution}]"[:50] if year else f"{title} [{resolution}]"[:50]`
  (match the existing conditional exactly — do not change today's naming
  format, only make it reusable and consistently truncated). Called from BOTH
  the existing `download_item` call site and the new `save_to_history`
  persist call.
- `add_to_history` gains `package_name=None` params, included in the
  `INSERT`/`ON CONFLICT DO UPDATE`:
  `package_name = COALESCE(excluded.package_name, downloads.package_name)`
  (never let a later status-only update null out an already-known name), and
  `last_grabbed_at = CURRENT_TIMESTAMP` **unconditionally** in both the INSERT
  values and the `ON CONFLICT DO UPDATE SET` list (every call — success,
  clipboard, browser, or failed-send — is a genuine new attempt; this is what
  makes a regrab's timestamp move forward even though `date_added` is
  deliberately preserved by the existing `ON CONFLICT` behavior).
- New CRUD:
  - `get_pipeline_verdicts(category=None, include_dismissed=False)` — joined
    read for the API.
  - `upsert_pipeline_verdict(url, category, detail, package_uuid, plex_rating_key, dismissed=False)`.
  - `dismiss_pipeline_verdict(url)`.
  - `clear_pipeline_verdict(url)` — used by regrab/grab-alternative to force a
    fresh reconcile of the new attempt.
  - `get_downloads_needing_reconcile(limit)` — rows with `package_name IS NOT
    NULL` and (`last_grabbed_at` more than 30 minutes ago) and (no verdict, OR
    a non-`dismissed` non-`verified` verdict whose `checked_at` predates
    `last_grabbed_at` — i.e. stale relative to the current attempt).
  - `get_plex_cache_max_timestamp()` — **already exists**
    ([database.py:853](backend/database.py:853)), reused as-is; returns
    `{content_type: max_last_updated_epoch_float}`.
- **`downloads.clear_history()` cascades to `pipeline_verdicts`** (`DELETE FROM
  pipeline_verdicts` alongside `DELETE FROM downloads`, same transaction) — a
  "Clear history" click must not leave orphaned verdicts pointing at rows that
  no longer exist.

### 2. `backend/pipeline_service.py` (new)

**Matching a grab to its `download_results`/`rename_jobs` rows** (used by
`reconcile_batch`, not a separate public function — inlined per-item, but
specified precisely here since it's the crux fix):

1. If `pipeline_verdicts.package_uuid` is already recorded for this url, match
   `download_results` by `package_uuid` directly (O(1), collision-proof, exactly
   how `download_results`' own uuid identity works post-migration).
2. Else (first reconcile for this attempt, or the uuid'd row aged out of
   `download_results`), match by `download_results.name = downloads.package_name
   AND download_results.updated_at >= downloads.last_grabbed_at - 5s` (the 5s
   margin absorbs clock/ordering slop between the grab request and JD's first
   poll), taking the row with `MAX(updated_at)`. If more than one candidate
   remains, prefer the one whose `state` is furthest along
   (`extracted > extracting > downloading > queued > failed`), same rationale
   as picking "the row that's actually this attempt, not a stale sibling."
3. Once a `download_results` row is matched, if it has a non-null
   `package_uuid`, persist it onto `pipeline_verdicts.package_uuid` so step 1
   applies on the next pass.
4. `rename_jobs` rows are matched by `package_name` only (that table has no
   `package_uuid` column — threading uuid identity into the rename subsystem is
   real, separate follow-up work, out of scope here). This keeps a narrow,
   documented residual collision window: two DIFFERENT `downloads` urls whose
   `package_name` string is byte-identical (same title+year+resolution) AND
   whose rename jobs were created within the same reconcile-eligible window
   could show each other's rename status. This is materially narrower than the
   original all-time name collision (bounded by the 30-minute
   `last_grabbed_at` window, not "ever"), and only affects the rename-status
   display, never the download/JD-state display (which is uuid-exact per
   step 1-3) or which url an action button acts on (actions always operate on
   `downloads.url`, never a rename_jobs row). Accepted for v1; flagged as a
   known limitation, not silently ignored.

**`categorize(download_row, result_row, rename_rows, plex_max_ts, jd_method, grace_margin_minutes=30) -> (category, detail, package_uuid, plex_rating_key)`**
— pure function, the core logic, unit-tested exhaustively. `rename_rows` is a
LIST (a package can span multiple files — season packs create one `rename_jobs`
row per file, [rename/service.py:515,545](backend/rename/service.py:515)).

- No `download_results` row at all:
  - If `jd_method != 'api'` (folder/crawljob mode — the poller never writes
    `download_results` in this mode, [main.py:268](backend/api/main.py:268)) →
    `unknown` (documented blind spot: this mode structurally can't be
    reconciled without a results row; do not mis-flag as `never_started`).
  - Else if `downloads.last_grabbed_at` is more than 30 minutes ago →
    `never_started`.
  - Else → no verdict written yet this pass (too soon to judge).
- `result_row.state == 'failed'` → `download_failed` (detail = `result_row.error`
  — this is where dead/offline links surface, per the existing
  offline/not-found/blocked detection already in `poll_results`,
  [download_service.py:764-767](backend/download_service.py:764)).
- `result_row.state` in `('queued', 'downloading', 'extracting')` →
  `in_progress`.
- `result_row.state == 'downloaded'` (finished, extraction not yet run/needed)
  → `in_progress` (transitional; give it the next pass or two before judging).
- `result_row.state == 'extracted'` and `rename_rows` is empty → `pending_rename`.
- Any row in `rename_rows` has `status in ('failed', 'needs_review')` →
  `rename_failed` (detail = that row's `error_message` or `warning_message`).
- All rows in `rename_rows` have `status == 'applied'`:
  - `latest_processed_at = max(r.processed_at for r in rename_rows)`, parsed as
    UTC (SQLite `CURRENT_TIMESTAMP` is UTC-naive text — **must** attach
    `tzinfo=timezone.utc` before comparing to the epoch floats from
    `get_plex_cache_max_timestamp()`, or the comparison silently skews by the
    server's local UTC offset).
  - `content_type = 'TV Shows' if rename_rows[0].media_type == 'tv' else 'Movies'`.
  - `cache_fresh_enough = plex_max_ts.get(content_type, 0) >= latest_processed_at.timestamp() + grace_margin_minutes * 60`
    (the margin is Plex's own scan lag after a cache refresh, not "time since
    rename" — this is the corrected gate).
  - If not `cache_fresh_enough` → `in_progress` (cache hasn't caught up yet;
    re-checked next pass, cheap).
  - Else, look up `plex_row` by `imdb_id` (from the rename job) — if absent,
    fall back to `title` (Python-side `normalize_title()`, NOT raw SQL
    equality — `plex_cache.title` is Plex's clean title, `rename_jobs.title` is
    TMDB's, they won't always match verbatim) + `year`, **AND** matching
    `season` (for TV — `plex_cache` rows are per-season) **AND** a resolution
    check (`plex_cache.res` vs `rename_jobs.resolution`, normalized so `'2160p'`
    and `'4K'` are treated as equal — without this, a 2160p grab whose rename
    never landed silently "verifies" against the library's existing 1080p copy,
    the one false-positive class the spec must not allow into the terminal
    `verified` state). Found → `verified` (`plex_rating_key` = the match).
    Not found → `not_in_plex`.
- Anything else unresolved/malformed → `unknown` (never raises).

`reconcile_batch(db, limit=500) -> int` — pulls
`get_downloads_needing_reconcile(limit)`, batch-fetches
`download_results`/`rename_jobs` (single `IN (...)` queries keyed by
`package_name`/`package_uuid`, not N+1) and `get_plex_cache_max_timestamp()`
once for the whole batch, runs the matching + `categorize` per row, upserts
verdicts. Returns the count processed. Wrapped in the caller's try/except (§3)
— this function itself doesn't swallow, so tests see real errors.

### 3. Maintenance-loop hook (`backend/app_service.py`)

- In `_run_maintenance_pass`, add one more fail-safe block (mirrors the trash
  sweep and WAL-checkpoint blocks immediately above it), gated on
  `pipeline_reconcile_enabled`:
  ```python
  try:
      if self.db is not None and self.config.get("pipeline_reconcile_enabled", True):
          from backend.pipeline_service import reconcile_batch
          n = reconcile_batch(self.db)
          if n:
              logger.info("Pipeline reconcile: checked %d grab(s)", n)
  except Exception:
      logger.exception("Pipeline reconcile failed (non-fatal)")
  ```
  Runs at startup and hourly, same cadence as trash sweep — no new thread.

### 4. `download_item` gains `force` (`backend/download_service.py`)

Both dedup gates in `download_item` ([:1890-1929](backend/download_service.py:1890))
must be bypassed for a pipeline-initiated grab, or regrab/grab-alternative are
silent no-ops:
- The exact-URL short-circuit (`is_downloaded(url)` → `method: "duplicate"`,
  [:1893-1906](backend/download_service.py:1893)) — this is why regrab (same
  url) does nothing today: `downloads.status='completed'` is set at *send*
  time and never flips back for an async JD-side failure, so `is_downloaded`
  returns `True` forever for a `download_failed` item.
- The title-level "not an upgrade" short-circuit
  (`_best_prior_grab`/`_is_quality_upgrade`, [:1907-1929](backend/download_service.py:1907))
  — this is why grab-alternative (a same-resolution release from a different
  host) does nothing today.

Add `force: bool = False` to `download_item`'s signature; when `True`, skip
BOTH gates entirely (they exist to prevent *accidental* re-grabs; an explicit
pipeline action is the user overriding that on purpose — the same reasoning
`download_item`'s own docstring already uses for "a prior failed grab doesn't
count"). No other behavior changes.

### 5. API (`backend/api/routes/pipeline.py`, new router)

All identifier-bearing routes take the `url` in the request body, not a path
segment.

- `GET /pipeline/items?category=&include_dismissed=` → verdict rows joined back
  to their `downloads`/`rename_jobs` display fields (title, year, poster if a
  rename job has one, resolution, the original grab url, the error/detail
  text) — one query, no N+1.
- `GET /pipeline/counts` → `{category: count}` for the nav badge / chip counts
  (dismissed excluded).
- `POST /pipeline/dismiss {url}` → `dismiss_pipeline_verdict(url)`.
- `POST /pipeline/regrab {url}` → loads the `downloads` row's stored
  title/season/year/resolution/size/hdr/dovi, `clear_pipeline_verdict(url)`
  (so the reconcile treats this as a fresh attempt — cheap, synchronous, no
  scraping), then **backgrounds** the grab exactly like the existing route
  (`background_tasks.add_task(lambda: dl.download_item(..., force=True))`),
  returns `{"status": "started"}` immediately. The outcome surfaces over the
  existing WS `download:*` notification channel, same as any other grab —
  `download_item` runs a Selenium scrape + Cloudflare wait (30s+), so it must
  never run synchronously inside a request handler (the existing route already
  knows this; this endpoint must match it, not "wait for success then clear
  the verdict" as originally drafted — that ordering is impossible given the
  background execution model).
- `POST /pipeline/search-sources {url}` → looks up the `downloads` row's
  title/season, calls `registry.search_all(title, mode)` with a request-level
  timeout, EXCLUDES sources requiring Selenium/auth (adithd — see Global
  Constraints), flattens `{source: PageResult}` into one ranked list (dedupe by
  url) plus a per-source errors list for the frontend. Never raises for a
  single source's failure (isolation already exists in `search_all` itself).
- `POST /pipeline/grab-alternative` → body is a `ParsedRelease.to_dict()` shape
  (`display_title`/`url`/`year`/`res`/`size`/`dovi`/`hdr`/`season`); maps onto
  `DownloadRequest` (`display_title→title`, `res→resolution`, etc.) and calls
  the same backgrounded `force=True` grab path as regrab.

### 6. Config (`backend/config.py`)

- `pipeline_verify_grace_margin_minutes` (default 30) — Plex's own scan lag
  margin added on top of cache-freshness (see §2's `cache_fresh_enough`).
- `pipeline_reconcile_enabled` (default True) — off switch, same pattern as
  other maintenance toggles.

### 7. Frontend — new route `/pipeline`

- `frontend/src/routes/pipeline/+page.svelte`: category chips across the top
  with live counts (from `GET /pipeline/counts`), a searchable list below
  (poster/title/year, the stalled stage + detail text, Re-grab / Search sources /
  Dismiss buttons). `verified` is collapsed by default (it's the success bucket,
  not something to act on). Mirrors the existing `StatusDashboard` +
  `RenameFilterBar` visual pattern from the Renames page for consistency.
- `frontend/src/lib/components/pipeline/SourceSearchModal.svelte`: opened by
  "Search sources"; shows a loading state, then the ranked alternative releases
  (source, resolution, size, HDR/DV badges — reusing existing badge components),
  a per-source error line if a source failed but others succeeded (and an
  explicit "adithd requires the desktop scraper — not searched here" note if
  excluded), and a Grab button per row that posts to `grab-alternative` then
  closes.
- Desktop: new sidebar nav entry. Mobile: a query-param switch inside the
  EXISTING `/downloads` route (`?view=pipeline`) rather than a new route or a
  7th tab-bar slot — the tab bar highlights by exact path match
  ([MobileTabBar.svelte:13](frontend/src/lib/components/MobileTabBar.svelte:13)),
  so embedding via query param (not navigating to a separate `/pipeline` URL on
  mobile) keeps the "Downloads" tab correctly highlighted while showing the
  Pipeline view. Desktop keeps the separate `/pipeline` route + sidebar entry.
- `frontend/src/lib/api/types.ts` gains `PipelineItem`, `PipelineCounts`,
  `AlternativeRelease` (mirrors `ParsedRelease.to_dict()`); `client.ts` gains
  the five calls above (all POSTs send `{url}` in the body).

---

## Categories (v1, fixed set)

`never_started` · `download_failed` · `in_progress` · `pending_rename` ·
`rename_failed` · `not_in_plex` · `verified` (collapsed/success) · `unknown`
(reconcile couldn't determine, or a structurally unreconcilable state like
folder-mode-with-no-results-row — logged, shown last, expected to be non-zero)

## Data Flow

1. A grab writes `downloads.package_name` (via `compute_package_name`) and bumps
   `last_grabbed_at` at send time — for every attempt, not just the first.
2. Hourly (+ once at startup) maintenance pass calls `reconcile_batch`, which
   matches each un-terminal/stale-relative-to-last_grabbed_at grab to its
   `download_results` row (uuid-first, name+time fallback), its `rename_jobs`
   rows, and a Plex-cache-freshness check, and upserts a verdict.
3. `/pipeline` page reads verdicts (fast — no live joins on page load).
4. Re-grab / grab-alternative → clear the old verdict synchronously, background
   `download_item(..., force=True)` → the next reconcile pass matches the new
   attempt fresh via `last_grabbed_at`.
5. Search-sources → `registry.search_all` (auth-requiring sources excluded) →
   picker → grab-alternative.

## Error Handling

- Reconcile: per-item categorize never raises (falls to `unknown`); the
  maintenance-loop wrapper swallows and logs so a reconcile bug never breaks
  trash sweep / WAL checkpoint / startup.
- Search-sources: partial results (some sources ok, some errored) are still
  returned; a total failure (all sources errored/timed out) returns an empty
  list + the errors, not a 500 — the modal shows "no results, try again."
- Regrab/grab-alternative: the HTTP response only confirms the grab was
  *queued* (backgrounded); success/failure surfaces over the existing WS
  `download:*` notification channel, identically to a normal grab from the Scan
  page — no new failure-reporting path.

## Testing

- **`compute_package_name`**: matches the exact existing inline format
  (with/without year), truncates at 50 chars identically to
  `send_to_jdownloader`'s two call sites.
- **`pipeline_service.categorize`**: one test per category boundary, INCLUDING:
  the folder-mode-no-results-row → `unknown` (not `never_started`); the
  Plex-cache-freshness gate (cache refreshed BEFORE the rename → `in_progress`
  even after 6+ hours elapsed; cache refreshed AFTER the rename + margin → the
  real check runs) — using literal UTC-naive `processed_at` strings and epoch
  floats to pin the timezone-conversion correctness; a multi-row `rename_rows`
  case where one file failed → `rename_failed` even though others applied; the
  resolution-normalization check (`'2160p'` vs `'4K'`) preventing a false
  `verified` against a lower-quality library copy; a `download_results` row
  whose error text contains "offline"/"not found" reaching `download_failed`
  with that detail preserved; malformed/partial input never raising (falls to
  `unknown`).
- **Matching logic**: a `package_uuid`-recorded verdict matches directly
  (single query, no name/time fallback needed); a fresh reconcile with a
  name-collision between two DIFFERENT `downloads.url` rows (same
  title/year/resolution, different `last_grabbed_at`) correctly picks the
  `download_results` row within its own attempt's time window, not the other
  url's.
- **`reconcile_batch`**: batched (no N+1) — assert query count; a single
  malformed row doesn't stop the rest of the batch; a `dismissed` verdict is
  not recomputed; a `verified` verdict is not recomputed (terminal).
- **`download_item(force=True)`**: bypasses BOTH the `is_downloaded` and
  `_best_prior_grab` gates and actually re-sends; `force=False` (the existing
  default) behavior is completely unchanged (regression-test the two gates
  still block a normal accidental duplicate).
- **API**: `/pipeline/items` filter + join correctness; `/pipeline/regrab`
  clears the old verdict synchronously and backgrounds the grab (assert
  `background_tasks.add_task` was called, not that the grab completed inline);
  `/pipeline/search-sources` returns partial results when one mocked source
  raises, and excludes adithd; `/pipeline/grab-alternative` correctly maps a
  `ParsedRelease` dict onto a `force=True` grab call; `clear_history()` also
  clears `pipeline_verdicts`.
- **Frontend**: category chip counts render; Dismiss removes an item and
  persists (mock the API); the search modal renders partial results + a
  per-source error without crashing; Grab-alternative closes the modal on
  success; the mobile `?view=pipeline` switch keeps the Downloads tab
  highlighted.

## Out of Scope (deferred)

- Retroactively backfilling `package_name`/`last_grabbed_at` on pre-existing
  `downloads` rows — not reliably derivable; those grabs simply won't
  reconcile-link.
- Threading `package_uuid` through `rename_jobs` to fully close the narrow
  residual name-collision window on the download→rename edge (documented in
  §2) — real, separate follow-up work touching the rename subsystem.
- Bulk actions (regrab-all in a category) — v1 is per-item.
- Auto-selecting/auto-grabbing the "best" alternative from search — v1 is
  always a manual picker (matches the earlier decision on this).
- Wiring `search_all` into the main Scan flow beyond this feature, or adding
  Selenium/auth support so adithd can participate in search-sources.
- Pruning/aging `verified` verdicts — they currently accrete forever; revisit
  if the table grows large enough to matter.
