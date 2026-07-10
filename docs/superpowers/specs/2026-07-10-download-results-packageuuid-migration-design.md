# download_results: name → durable id/packageUUID migration — Design

**Status:** Approved (design phase). v1 scope.
**Date:** 2026-07-10

## Goal

Let two JDownloader packages with an identical name (an accidental double-grab of
the same release) coexist as two distinct `download_results` rows, so the mobile
and desktop Downloads views can actually see and resolve the duplicate, and
"cancel one copy" removes exactly one. Today `download_results` uses
`name TEXT PRIMARY KEY` ([database.py:360](backend/database.py:360)), so the
second same-name package silently overwrites the first's row on every poll — the
merged row's progress flip-flops between the two JD jobs, and
`frontend/src/lib/downloads/dupes.ts`'s exact-name duplicate detection can never
fire because the DB collapsed the pair before the API returns. This is a
pre-existing architectural limitation (predates the 2026-07-09 mobile-downloads
feature), rare (0 in a live 200-download queue) and non-destructive, but real.

## Architecture

Repoint the table's identity from the natural key (`name`) to a **surrogate
`id` primary key** plus a nullable **`package_uuid`** column (JD's durable
per-package `uuid`, already read by `poll_results`/`remove_package`). Upserts
match on `package_uuid` when present, else **adopt** a legacy `package_uuid IS
NULL` row of the same `name` (healing old rows in place), else insert — so two
live same-name packages get distinct UUIDs → two rows. Every consumer that keyed
off `name` (the poller's change-detection + title caches, the auto-rename
`handed_to_rename` set, the delete/remove paths, the API, and both frontend
surfaces) moves to the durable key (`package_uuid` in-memory, `id` over the API).

SQLite can't repoint a primary key in place, so the schema change is a guarded
**table rebuild** (create new → copy old rows → drop → rename) run from the
existing versioned-migration path (`PRAGMA user_version` / `SCHEMA_VERSION`,
[database.py:254](backend/database.py:254), [:534](backend/database.py:534)).

## Tech Stack

SQLite (via `DatabaseManager`), FastAPI, myjdapi (JDownloader `query_packages` /
`remove_links` by uuid), SvelteKit 5 (runes). Deploy via
`docker compose up -d --build` only.

## Global Constraints

- Deploy in-app changes ONLY via `docker compose up -d --build`.
- **No data loss on migration:** every existing `download_results` row survives
  the rebuild (copied in, `id` auto-assigned, `package_uuid` NULL).
- **Backward-compatible with un-backfillable legacy rows:** a row whose JD package
  was already cleared can never get a `package_uuid`; it stays keyed by `id`,
  deletes by `id`, and its JD-removal is a no-op (package already gone).
- The migration is **idempotent** and guarded (`PRAGMA table_info` check) so a
  second startup doesn't rebuild again or fail.
- The desktop `/downloads` page's existing behavior is preserved (same data, same
  actions) — only the row key + remove identifier change.
- Every new API field is declared on its Pydantic response/request model (avoid
  the `extra="forbid"`/silent-drop class of bug), and the frontend `DownloadResult`
  type matches the emitted JSON.
- Tests accompany each unit; deploy only after the changed-module suites are green
  (`test_database.py`, `test_download_service.py`, `test_api_routes.py` scoped
  subset — the full `test_api_routes.py` hangs on network tests).

---

## Components

### 1. Schema + rebuild migration (`backend/database.py`)

New shape:
```sql
CREATE TABLE download_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_uuid TEXT,            -- JD package uuid; NULL for legacy/cleared rows
    name TEXT,                    -- JD package name (no longer unique)
    title TEXT, host TEXT,
    bytes_total INTEGER DEFAULT 0, bytes_loaded INTEGER DEFAULT 0,
    downloaded INTEGER DEFAULT 0,
    extraction TEXT DEFAULT 'na', state TEXT DEFAULT 'queued', error TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX idx_download_results_uuid
    ON download_results(package_uuid) WHERE package_uuid IS NOT NULL;
CREATE INDEX idx_download_results_name ON download_results(name);
```
- The `CREATE TABLE IF NOT EXISTS` stays for fresh DBs. For existing DBs, add a
  guarded rebuild: `PRAGMA table_info(download_results)` — if there's no `id`
  column, run `BEGIN; CREATE TABLE download_results_new (…); INSERT INTO
  download_results_new (package_uuid, name, title, host, bytes_total,
  bytes_loaded, downloaded, extraction, state, error, updated_at) SELECT NULL,
  name, title, host, bytes_total, bytes_loaded, downloaded, extraction, state,
  error, updated_at FROM download_results; DROP TABLE download_results; ALTER
  TABLE download_results_new RENAME TO download_results;` then (re)create the
  indexes. Place it beside the existing migration steps so it runs once at init.
- The partial UNIQUE index on `package_uuid` enforces "one row per live UUID"
  while allowing many NULL-uuid (legacy) rows.

### 2. `upsert_download_result` (adopt-or-insert)

Signature gains `package_uuid`:
`upsert_download_result(name, package_uuid=None, title=None, host=None, bytes_total=0, bytes_loaded=0, downloaded=0, extraction="na", state="queued", error=None) -> int` (returns the row `id`).

Resolution order (single transaction):
1. If `package_uuid` is not None and a row with that `package_uuid` exists →
   UPDATE it (by id).
2. Else if `package_uuid` is not None and a `package_uuid IS NULL` row with the
   same `name` exists → **adopt**: UPDATE that row, setting `package_uuid` +
   the new fields (heals the legacy row). If several NULL-uuid same-name rows
   exist, adopt the most-recently-updated one.
3. Else if `package_uuid` is None → match an existing row by `name` (legacy
   path: a poll that somehow lacks a uuid still updates the name-matched row) →
   UPDATE, else INSERT.
4. Else (uuid present, no uuid match, no NULL-uuid name match) → INSERT a new
   row with the uuid.

The `ON CONFLICT(name)` upsert is replaced by this explicit lookup-then-write
(the old single-statement upsert can't express "adopt only a NULL-uuid row").

### 3. Reads / deletes (`backend/database.py`)

- `get_download_results(limit=200)` → SELECT includes `id, package_uuid` (order by
  `updated_at DESC` unchanged).
- `delete_download_result(id: int) -> int` (was `name`) — delete by `id`, return
  rows affected.
- `clear_download_results()` — unchanged (DELETE all).

### 4. `DownloadService.remove_package` → per-row by id (`backend/download_service.py`)

`remove_package(id_)`:
1. Load the row (`SELECT package_uuid FROM download_results WHERE id=?`).
2. If it has a `package_uuid`, call JD `downloads.remove_links([], [uuid])` for
   **only that uuid** (no longer "all packages matching this name" —
   [download_service.py:648-651](backend/download_service.py:648)).
3. `db.delete_download_result(id_)`. Idempotent: a missing row / already-gone JD
   package returns `{ok: True}`.

### 5. Poller + caches keyed by uuid (`backend/download_service.py`)

- `poll_results`: pass `package_uuid=pkg.get("uuid")` to `upsert_download_result`;
  include `uuid` in the returned `row` dict (for the auto-rename hook + API).
- `_results_cache` and `_best_titles` (change-detection / title caches, currently
  keyed by `name`, [:776](backend/download_service.py:776),
  [:792-793](backend/download_service.py:792)) → key by `uuid` (fall back to
  `name` only when a package has no uuid, which shouldn't happen for a live
  package). Prune by the set of live uuids.

### 6. Auto-rename hook (`backend/api/main.py`)

`handed_to_rename` ([main.py:252](backend/api/main.py:252)) currently a name-set
→ key by `uuid` so two same-name extracted packages are each handed to
auto-rename independently (today the second is skipped as "already handed").

### 7. API (`backend/api/routes` — the `/download` router)

- `GET /download/results` response model gains `id: int` and
  `package_uuid: str | None`.
- Remove endpoint: `POST /download/results/remove` body becomes `{ id: int }`
  (was `{ name: str }`); calls `remove_package(id)`. (We own both callers; no
  external consumer.)

### 8. Frontend (`frontend/src/lib`)

- `types.ts` `DownloadResult` ([types.ts:500](frontend/src/lib/api/types.ts:500))
  gains `id: number` and `package_uuid: string | null`.
- `client.ts` `removeDownloadResult(id: number)` → posts `{ id }`
  ([client.ts:226](frontend/src/lib/api/client.ts:226)).
- Desktop `/downloads` `+page.svelte` and `MobileDownloadsView.svelte`
  ([:82](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:82),
  [:89](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:89)): key
  `{#each}` by `id`, call `removeDownloadResult(r.id)`.
- `dupes.ts`: grouping stays title/name-based, but each item now carries a stable
  `id`; the exact-name duplicate branch now actually fires (two rows, same name),
  and keep-best/cancel-rest removes the losing copies by `id`. "Best" among
  exact-name duplicates = the more-complete one (higher `bytes_loaded`/`downloaded`).

---

## Data Flow

1. Poller reads JD packages (each has `uuid`), calls
   `upsert_download_result(name, package_uuid=uuid, …)`.
2. First same-name package → inserts row1 (uuid-A). Second → no uuid-B match, no
   NULL-uuid same-name row (row1 has uuid-A) → inserts row2 (uuid-B). Two rows.
3. A pre-existing legacy row (uuid NULL) of that name is adopted by whichever live
   package is polled first (sets its uuid), then the other inserts fresh.
4. `GET /download/results` returns both rows (with `id`, `package_uuid`).
5. `dupes.ts` flags them as an exact-name duplicate; the user cancels one →
   `removeDownloadResult(id)` → `remove_package(id)` removes only that uuid from
   JD and deletes only that row.

## Error Handling

- Migration runs in a transaction; on any error it rolls back and leaves the old
  table intact (the app still starts on the old schema — surface a log error).
- `remove_package` is idempotent (missing row / already-gone JD package → ok).
- Upsert is defensive: a poll lacking a uuid still updates by name (no crash, no
  duplicate-row explosion).
- The partial UNIQUE(uuid) index makes a double-insert race fail loudly rather
  than create two rows for one uuid.

## Testing

- **`test_database.py`:** migration rebuild preserves every legacy row (count +
  field values) and assigns `id`, `package_uuid=NULL`; idempotent on second init;
  `upsert_download_result` — insert-by-uuid, update-by-uuid, adopt a NULL-uuid
  same-name legacy row (sets uuid, no new row), two distinct uuids same name →
  two rows, uuid-less poll updates by name; `delete_download_result(id)` removes
  exactly one of two same-name rows; the partial UNIQUE(uuid) index rejects a
  second row for the same uuid.
- **`test_download_service.py`:** `poll_results` passes `uuid` to the upsert and
  two same-name live packages persist as two rows; `remove_package(id)` calls JD
  `remove_links` with the single resolved uuid (mocked device) and deletes only
  that row; caches/`handed_to_rename` keyed by uuid don't collide across two
  same-name packages.
- **`test_api_routes.py` (scoped):** `POST /download/results/remove {id}` happy
  path + missing id; `GET /download/results` includes `id`/`package_uuid`.
- **Frontend (vitest):** `dupes.ts` exact-name duplicate now groups two same-name
  rows and picks the more-complete as keeper; remove targets by `id`.

## Out of Scope (deferred)

- Any change to how JD packages are *added* / named — this is purely the
  persistence identity.
- Collapsing / redesigning the duplicate UI beyond making the existing detection
  fire and remove per-copy.
- Retroactively assigning UUIDs to rows whose JD package is already gone (not
  possible; they remain NULL-uuid, keyed by id).
- A `SCHEMA_VERSION` bump is used only if the existing migration path requires it;
  the rebuild is guarded by a `PRAGMA table_info` presence check regardless.
