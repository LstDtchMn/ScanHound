# download_results: name → durable id/packageUUID migration — Design

**Status:** Approved (design phase), revised after an adversarial spec review. v1 scope.
**Date:** 2026-07-10

## Goal

Let two JDownloader packages with an identical name (an accidental double-grab of
the same release) coexist as two distinct `download_results` rows, so the mobile
and desktop Downloads views can see and resolve the duplicate, and "cancel one
copy" removes exactly one. Today `download_results` uses `name TEXT PRIMARY KEY`
([database.py:360](backend/database.py:360)), so the second same-name package
silently overwrites the first's row on every poll — the merged row's progress
flip-flops between the two JD jobs, and `frontend/src/lib/downloads/dupes.ts`'s
exact-name duplicate detection can never fire because the DB collapsed the pair
before the API returns. Pre-existing, rare (0 in a live 200-download queue),
non-destructive, but real.

## Architecture

Repoint the table's identity from the natural key (`name`) to a **surrogate
`id` primary key** plus a nullable **`package_uuid`** column (JD's per-package
`uuid`, already read by `poll_results`/`remove_package`). Upserts match on
`package_uuid` when present, else **adopt** a legacy `package_uuid IS NULL` row
of the same `name` (healing old rows in place), else insert — so two live
same-name packages get distinct UUIDs → two rows. Every consumer that keyed off
`name` (the poller's change-detection + title caches, the auto-rename
`handed_to_rename` set, the delete/remove paths, the REST API, **the WebSocket
broadcast**, and both frontend surfaces) moves to the durable key
(`package_uuid` in-memory; **`id` over both the REST response and the WS push**).

SQLite can't repoint a primary key in place, so the schema change is a guarded,
**self-contained** table rebuild that must never reach `init_db`'s
corrupt-DB quarantine path.

## Tech Stack

SQLite (`DatabaseManager`), FastAPI, myjdapi (JD `query_packages` / `remove_links`
by uuid), SvelteKit 5 (runes). Deploy via `docker compose up -d --build` only.

## Global Constraints

- Deploy in-app changes ONLY via `docker compose up -d --build`.
- **No data loss, ever.** Every existing `download_results` row survives the
  rebuild. **The rebuild MUST NOT be able to trigger `init_db`'s corrupt-DB
  quarantine** ([database.py:538-567](backend/database.py:538)), which renames
  the whole DB aside and rebuilds it fresh (wiping `auth_credentials`,
  `downloads`, `rename_jobs`, `plex_cache`, …). This exact failure class caused a
  prior full wipe; treat it as the primary hazard.
- **Idempotent + crash-safe.** Guarded by a `PRAGMA table_info` check; a crash
  mid-rebuild must not brick subsequent startups.
- **Backward-compatible with un-backfillable legacy rows:** a row whose JD package
  was already cleared can never get a `package_uuid`; it stays keyed by `id`,
  deletes by `id`, and its JD-removal is a no-op.
- The desktop `/downloads` page keeps working across every WebSocket push and
  every action — only the row key + remove identifier change.
- The frontend `DownloadResult` type matches the JSON emitted by BOTH the REST
  endpoint and the WS broadcast, and `package_uuid` is the same JSON type (string)
  in both.
- Tests accompany each unit; deploy only after the changed-module suites are green
  (`test_database.py`, `test_download_service.py`, `test_api_routes.py` scoped
  subset — the full file hangs on network tests).

---

## Components

### 1. Schema + crash-safe rebuild migration (`backend/database.py`)

New shape (also the `CREATE TABLE IF NOT EXISTS` for fresh DBs):
```sql
CREATE TABLE IF NOT EXISTS download_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_uuid TEXT,            -- JD package uuid (stringified); NULL for legacy/cleared rows
    name TEXT,                    -- JD package name (no longer unique)
    title TEXT, host TEXT,
    bytes_total INTEGER DEFAULT 0, bytes_loaded INTEGER DEFAULT 0,
    downloaded INTEGER DEFAULT 0,
    extraction TEXT DEFAULT 'na', state TEXT DEFAULT 'queued', error TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
- **Indexes go in the shared idempotent index section** ([database.py:472-486](backend/database.py:472)),
  NOT only in the rebuild branch, so fresh installs and post-quarantine rebuilds
  also get them:
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS idx_download_results_uuid
      ON download_results(package_uuid) WHERE package_uuid IS NOT NULL;
  CREATE INDEX IF NOT EXISTS idx_download_results_name ON download_results(name);
  ```
  (Plus keep any existing `download_results` index in that section.)
- **Rebuild (existing old-shape DBs only), self-contained and crash-safe.** Runs
  AFTER the base `CREATE TABLE IF NOT EXISTS` (so the `PRAGMA table_info` guard
  never fires on a just-created fresh table). Wrap the whole thing in its OWN
  `try/except`; on ANY error, roll back and **re-raise a controlled startup error
  distinct from the corruption path** — do NOT let a `sqlite3.DatabaseError`
  propagate to `init_db`'s corruption handler, and do NOT attempt degraded
  old-schema operation:
  ```python
  cols = {r[1] for r in cursor.execute("PRAGMA table_info(download_results)")}
  if cols and "id" not in cols:            # old-shape table exists → rebuild
      try:
          cursor.execute("DROP TABLE IF EXISTS download_results_new")  # clear any aborted leftover
          cursor.execute("BEGIN IMMEDIATE")
          cursor.execute("CREATE TABLE download_results_new (…new schema…)")
          cursor.execute(
              "INSERT INTO download_results_new "
              "(package_uuid, name, title, host, bytes_total, bytes_loaded, "
              " downloaded, extraction, state, error, updated_at) "
              "SELECT NULL, name, title, host, bytes_total, bytes_loaded, "
              " downloaded, extraction, state, error, updated_at FROM download_results")
          cursor.execute("DROP TABLE download_results")
          cursor.execute("ALTER TABLE download_results_new RENAME TO download_results")
          conn.commit()
      except Exception:
          conn.rollback()
          logger.exception("download_results rebuild failed")
          raise RuntimeError("download_results migration failed")  # controlled, NOT the corruption path
  ```
  `DROP IF EXISTS …_new` is provably safe: the copy+drop+rename commit atomically,
  so a surviving `_new` is always an empty aborted leftover, never the only copy.
  Do NOT "fix" the orphan with `CREATE TABLE IF NOT EXISTS …_new` + re-copy — that
  reopens a row-duplication path. Indexes are (re)created afterward by the shared
  idempotent section, so the `DROP TABLE` discarding them is fine.
- **Note on the actual init flow (verify at implementation):** confirm whether a
  transaction is already open when this runs and whether `conn.commit()`/
  `rollback()` here interferes with the surrounding `init_db` transaction; the
  guarded `BEGIN IMMEDIATE`…`commit` must nest cleanly or be issued where no outer
  txn is open. If the surrounding code holds one transaction for all of
  `init_db`, do the rebuild as plain statements within it and let its
  commit/rollback own the outcome — but STILL catch and convert the exception so
  it can't reach the corruption handler.

### 2. `upsert_download_result` (adopt-or-insert, one lock hold)

Signature gains `package_uuid`; returns the row `id`:
`upsert_download_result(name, package_uuid=None, title=None, host=None, bytes_total=0, bytes_loaded=0, downloaded=0, extraction="na", state="queued", error=None) -> int`.

- **Runs the whole lookup-then-write under ONE lock hold** (a single
  `db.transaction()` / one `_lock` acquisition), not a `_query` + separate
  `_mutate` — otherwise the poller thread and the remove endpoint race
  (adopt-after-delete → lost update, TOCTOU). `_mutate` returns bool, so add a
  helper that returns `cursor.lastrowid`.
- Resolution order:
  1. `package_uuid` not None and a row with it exists → UPDATE by id (**SET `name`
     too** — JD can rename a package; the old upsert never set name because it
     was the key).
  2. `package_uuid` not None and a `package_uuid IS NULL` row of the same `name`
     exists → **adopt**: UPDATE it, setting `package_uuid` + all fields. If
     several, pick the most-recently-updated (`ORDER BY updated_at DESC LIMIT 1`).
  3. `package_uuid` is None → match by `name` (NULL-uuid rows first, else
     most-recently-updated, `LIMIT 1`) → UPDATE, else INSERT.
  4. uuid present, no uuid or NULL-uuid-name match → INSERT with the uuid.
- Normalize `package_uuid = str(uuid)` at the boundary (myjdapi emits an int64;
  SQLite TEXT affinity + JS-safe-integer are fine, but one type everywhere).

### 3. Reads / deletes (`backend/database.py`)

- `get_download_results(limit=200)` → SELECT includes `id, package_uuid`.
- `delete_download_result(id: int) -> int` (was `name`) — delete by `id`.
- `clear_download_results()` — unchanged.

### 4. `DownloadService.remove_package` → per-row by id (`backend/download_service.py`)

`remove_package(id_)`: load the row's `package_uuid`; if present, JD
`downloads.remove_links([], [uuid])` for **only that uuid**
([download_service.py:648-651](backend/download_service.py:648) no longer
removes "all packages matching this name"); then `delete_download_result(id_)`.
Idempotent (missing row / already-gone package → `{ok: True}`).

### 5. Poller: emit `id`, key caches by uuid (`backend/download_service.py`)

- `poll_results` passes `package_uuid=str(pkg.get("uuid"))` to the upsert **and
  must put the DB `id` into every returned `row`** (the row dict is what both the
  REST response and the WS broadcast carry). The upsert is skipped when the
  change-detection `change_key` is unchanged ([:776](backend/download_service.py:776)),
  so keep a `uuid → id` map: update it from the upsert's returned id when a write
  happens, and `SELECT id` once for a cache-suppressed row not yet in the map.
  Emit `package_uuid` (stringified) in the row too.
- `_results_cache` and `_best_titles` ([:776](backend/download_service.py:776),
  [:792-793](backend/download_service.py:792)) → key by uuid; prune by the set of
  live uuids.

### 6. WebSocket broadcast + auto-rename hook (`backend/api/main.py`)

- The `_start_results_poller` broadcast ([main.py:258-268](backend/api/main.py:258))
  sends the poller's row dicts to `download:results`; with §5 those rows now carry
  `id` + stringified `package_uuid`, so the desktop page's keyed list + remove
  keep working. No structural change here beyond §5 feeding it correct rows.
- `handed_to_rename` ([main.py:252](backend/api/main.py:252)) → key by uuid (so
  two same-name extracted packages are each handed to auto-rename; today the
  second is skipped). Prune it to live uuids so it doesn't grow unbounded.

### 7. API (`backend/api/routes/downloads.py`)

- `GET /download/results` returns raw dicts (no Pydantic response model,
  [:424-429](backend/api/routes/downloads.py:424)) — the new `id`/`package_uuid`
  columns flow through automatically; no model change needed there.
- Remove endpoint `RemoveResultRequest` ([:440-441](backend/api/routes/downloads.py:440))
  body becomes `{ id: int }` (was `{ name: str }`); calls `remove_package(id)`.

### 8. Frontend (`frontend/src/lib`) — ALL name-keying moves to `id`

- `types.ts` `DownloadResult` ([:500](frontend/src/lib/api/types.ts:500)) gains
  `id: number` and `package_uuid: string | null`.
- `client.ts` `removeDownloadResult(id: number)` → posts `{ id }`
  ([:226](frontend/src/lib/api/client.ts:226)).
- **Desktop `/downloads` `+page.svelte`:** the WS handler `dlResults = d.results`
  ([:339](frontend/src/routes/downloads/+page.svelte:339)) now receives rows with
  `id`; change the `{#each}` key ([~:528](frontend/src/routes/downloads/+page.svelte:528))
  from `(r.name)` to `(r.id)` and every remove/compare to `id`.
- **`MobileDownloadsView.svelte`:** move EVERY name comparison/filter to `id` —
  the remove calls ([:82](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:82),
  [:89](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:89)), the
  optimistic filter `results.filter(x => x.name !== r.name)`
  ([~:90](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:90)) → by
  `id`, and the keep-best comparison `r.name !== g.best.name`
  ([~:99](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:99)) → by
  `id`. (Without this, "Keep best" on exact-name duplicates cancels nothing and a
  single-copy cancel visually removes both.)
- **`dupes.ts`:** grouping stays title/name-based; each item carries a stable
  `id`. The exact-name duplicate branch now actually fires; keep-best/cancel-rest
  compares + removes by `id`.

### 9. Duplicate-resolution safety (`dupes.ts` / both views)

- **Cancel-rest only touches rows in ACTIVE states** (`queued`/`downloading`/
  `extracting`). A `download_results` row persists after JD clears the package, so
  a re-grab a month later creates a second (new-uuid) row and the UI flags a
  duplicate; "best = more-complete" would otherwise pick the finished historical
  row and cancel the live re-download. Restricting cancel-rest to active rows
  prevents killing a live download and cancelling a stale record.
- Accept and document the uuid-churn tradeoff: if JD reassigns a package's uuid
  (merge/split, linkgrabber→downloads re-add, some restarts), one logical package
  can surface as a live row + a phantom stale row. The old name-PK silently
  absorbed this; the new design surfaces it as a benign duplicate. Manual
  per-copy remove is the escape hatch.

---

## Data Flow

1. Poller reads JD packages (each has `uuid`), upserts with
   `package_uuid=str(uuid)`, gets back the row `id`, and emits rows carrying
   `id` + `package_uuid` over both REST and the WS broadcast.
2. First same-name package → row1 (uuid-A). Second → no uuid-B match, no
   NULL-uuid same-name row → row2 (uuid-B). Two rows.
3. A legacy NULL-uuid row of that name is adopted by whichever live package polls
   first; the other inserts fresh.
4. `dupes.ts` flags an exact-name duplicate; cancel one → `removeDownloadResult(id)`
   → `remove_package(id)` removes only that uuid from JD and deletes only that row.

## Error Handling

- The rebuild is self-contained: its own try/except rolls back and raises a
  CONTROLLED startup error that never reaches `init_db`'s corruption/quarantine
  handler (which would wipe the whole DB). A rollback leaves the old table intact
  (no row loss). `DROP TABLE IF EXISTS download_results_new` clears any aborted
  leftover so a re-run can't fail "table already exists."
- `remove_package` is idempotent.
- The upsert runs under one lock hold (no TOCTOU with the poller/remove).
- The partial UNIQUE(uuid) index makes a double-insert race fail loudly rather
  than create two rows for one uuid.

## Testing

- **`test_database.py`:** rebuild preserves every legacy row (count + field
  values), assigns `id`, `package_uuid=NULL`; **idempotent** on second init;
  **a pre-existing `download_results_new` leftover doesn't break the rebuild**
  (DROP-IF-EXISTS); a rebuild failure does NOT quarantine the DB (other tables
  intact) — simulate by forcing an error in the rebuild and asserting
  `auth_credentials`/`rename_jobs` survive. Upsert: insert-by-uuid, update-by-uuid
  (incl. a name change), adopt a NULL-uuid same-name legacy row (sets uuid, no new
  row), two distinct uuids same name → two rows, uuid-less poll precedence; the
  partial UNIQUE(uuid) index rejects a second row for one uuid; `delete_download_result(id)`
  removes exactly one of two same-name rows; the unique/name indexes exist on a
  FRESH DB (not just a migrated one).
- **`test_download_service.py`:** `poll_results` passes `str(uuid)` to the upsert,
  two same-name live packages persist as two rows, and the returned rows carry
  `id`; a cache-suppressed poll still emits the correct `id` (uuid→id map / SELECT
  fallback); `remove_package(id)` calls JD `remove_links` with the single resolved
  uuid (mocked device) and deletes only that row; caches/`handed_to_rename` keyed
  by uuid don't collide across two same-name packages.
- **`test_api_routes.py` (scoped):** `POST /download/results/remove {id}` happy
  path + missing id; `GET /download/results` includes `id`/`package_uuid`.
- **Frontend (vitest):** `dupes.ts` exact-name duplicate groups two same-name
  rows, keep-best picks the more-complete ACTIVE row, and cancel-rest skips
  non-active (finished/failed) rows; remove targets by `id`.

## Out of Scope (deferred)

- Any change to how JD packages are added/named — purely the persistence identity.
- Redesigning the duplicate UI beyond making detection fire + remove per-copy.
- Retroactively assigning UUIDs to rows whose JD package is already gone (not
  possible; they stay NULL-uuid, keyed by id).
- Reconciling uuid-churn phantom rows automatically (manual remove is the escape
  hatch; see §9).
