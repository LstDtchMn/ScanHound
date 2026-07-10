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
- `id` (number) and `package_uuid` (string | null) are present and the SAME JSON
  type in BOTH the REST response and the WS broadcast for a given row. (The two
  channels already differ on other fields — WS rows carry `save_to` and omit
  `updated_at`, REST is the inverse; that's pre-existing, so mark `updated_at` and
  `save_to` optional in `DownloadResult` rather than claiming exact parity.)
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
          if conn.in_transaction:      # future-proof: BEGIN raises if a txn is already open
              conn.commit()
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
      except Exception as e:
          conn.rollback()
          logger.exception("download_results rebuild failed")
          raise RuntimeError("download_results migration failed") from e  # controlled, NOT the corruption path
  ```
  `DROP IF EXISTS …_new` is provably safe: the copy+drop+rename commit atomically,
  so a surviving `_new` is always an empty aborted leftover, never the only copy.
  Do NOT "fix" the orphan with `CREATE TABLE IF NOT EXISTS …_new` + re-copy — that
  reopens a row-duplication path. Indexes are (re)created afterward by the shared
  idempotent section, so the `DROP TABLE` discarding them is fine.
- **Init-flow transaction nesting (resolved — implement exactly this):** the
  connection is `sqlite3.connect(db_path, check_same_thread=False)` with no
  `isolation_level` → legacy transaction control. `init_db` runs only PRAGMA /
  `CREATE TABLE IF NOT EXISTS` / guarded `ALTER TABLE` (all DDL/PRAGMA, no DML)
  before this point, so `conn.in_transaction` is False here and `BEGIN IMMEDIATE`
  is safe; the mid-init `conn.commit()` and the later `PRAGMA user_version` stamp
  ([database.py:534](backend/database.py:534)) + final commit are unaffected.
  The `integrity_check` at [database.py:247](backend/database.py:247) runs BEFORE
  the rebuild, so a genuinely corrupt DB is already quarantined and can't be
  masked here. Do NOT add the "do it as plain statements within an outer txn"
  alternative — it's moot and less safe. The `if conn.in_transaction: conn.commit()`
  line above future-proofs against someone later adding DML earlier in `init_db`
  (without it, `BEGIN` would raise "cannot start a transaction within a
  transaction" and needlessly fail startup).

### 2. `upsert_download_result` (adopt-or-insert, one lock hold)

Signature gains `package_uuid`; returns the row `id`:
`upsert_download_result(name, package_uuid=None, title=None, host=None, bytes_total=0, bytes_loaded=0, downloaded=0, extraction="na", state="queued", error=None) -> int`.

- **Runs the whole lookup-then-write under ONE lock hold** — do it inside the
  existing `DatabaseManager.transaction()` context manager
  ([database.py:52](backend/database.py:52), reentrant RLock) or a single
  `with self._lock:` block (the pattern `delete_download_result` already uses at
  [database.py:999](backend/database.py:999)), NOT a `_query` + separate `_mutate`
  (which release the lock between statements → poller-vs-remove TOCTOU,
  adopt-after-delete lost update). For the INSERT branch, reuse the existing
  `_insert_returning_id` helper ([database.py:194](backend/database.py:194)) —
  don't add a new one. (Note `_insert_returning_id` returns None on failure; the
  caller in §5 must treat a None id as "write failed", see there.)
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
- **Never let `package_uuid` become the string `"None"`.** The poller normalizes
  `u = pkg.get("uuid"); package_uuid = str(u) if u is not None else None` (§5) —
  a `str(None)` here would be a fake non-NULL uuid that collides every uuid-less
  package onto one row via step 1, sits wrongly inside the partial UNIQUE index,
  and never reaches step 3. So `upsert_download_result` receives either a real
  stringified uuid or a genuine `None`; a `None` uuid takes resolution step 3.

### 3. Reads / deletes (`backend/database.py`)

- `get_download_results(limit=200)` → SELECT includes `id, package_uuid`.
- `delete_download_result(id: int) -> int` (was `name`) — delete by `id`.
- `clear_download_results()` — unchanged.

### 4. `DownloadService.remove_package` → per-row by id (`backend/download_service.py`)

`remove_package(id_)`: load the row's `package_uuid`; if present, JD
`downloads.remove_links([], [int(package_uuid)])` for **only that uuid** —
**convert back to int** at the JD boundary (the column is TEXT but JD's API
expects the native int64 uuid, as the current code passes at
[download_service.py:648-651](backend/download_service.py:648); this no longer
removes "all packages matching this name"). Then `delete_download_result(id_)`.
The JD `remove_links` call is best-effort/try-except today, so a type mismatch
would be **silently swallowed** while the DB row is deleted (UI says "removed",
package keeps downloading) — hence the explicit `int()` and a test asserting the
mocked device received an int. Idempotent (missing row / already-gone package →
`{ok: True}`).

### 5. Poller: emit `id`, key caches by uuid (`backend/download_service.py`)

- **Normalize the uuid once:** `u = pkg.get("uuid"); package_uuid = str(u) if u
  is not None else None`. Pass `package_uuid` to the upsert. (Never `str(None)`.)
- `poll_results` **must put the DB `id` into every returned `row`** (the row dict
  is what both the REST response and the WS broadcast carry). Keep a `uuid → id`
  map, and **prime the change-detection cache only AFTER a successful write**:
  the current code sets `_results_cache[key]` *before* the upsert
  ([:776-783](backend/download_service.py:776)); with `_insert_returning_id`
  returning None on failure, that order would permanently emit an id-less row
  (map never learns the id, cache suppresses retries). Reorder to: call the
  upsert → if it returns a non-None `id`, record `map[uuid]=id` and set the
  cache; if it returns None (write failed), do NOT set the cache (so the next
  poll retries). For a cache-suppressed row not yet in the map, `SELECT id` once;
  if that also misses, re-run the upsert rather than emit an id-less row. A row
  with a `None` uuid falls back to the existing name-keyed cache path.
- Emit `id` + stringified `package_uuid` in every returned `row`.
- `_results_cache`, `_best_titles`, and the new `uuid → id` map
  ([:776](backend/download_service.py:776), [:792-793](backend/download_service.py:792))
  → key by uuid (name for the rare None-uuid row); prune all three by the set of
  live uuids each poll.

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
- **`MobileDownloadsView.svelte`:** move EVERY name-keying to `id` — the keyed
  each **`{#each g.items as r (r.name)}`**
  ([~:129](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:129)) →
  `(r.id)` (two same-name rows otherwise throw Svelte 5 `each_key_duplicate` /
  corrupt list state — this is the feature's whole point), the remove calls
  ([:82](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:82),
  [:89](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:89)), the
  optimistic filter `results.filter(x => x.name !== r.name)`
  ([~:90](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:90)) → by
  `id`, and the keep-best comparison `r.name !== g.best.name`
  ([~:99](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:99)) → by
  `id`. Grep the desktop `+page.svelte` for the equivalent keyed each + name
  compares and convert them too. (Without this, "Keep best" on exact-name
  duplicates cancels nothing and a single-copy cancel visually removes both.)
- **`dupes.ts`:** grouping stays title/name-based; each item carries a stable
  `id`. The exact-name duplicate branch now actually fires; keep-best/cancel-rest
  compares + removes by `id`.

### 9. Duplicate-resolution safety (`dupes.ts` / both views)

- **Keep-best/cancel-rest operate ONLY on the group's ACTIVE subset — both the
  best-selection AND the cancel set.** Define `isActive(row)` = `row.state ∈
  {'queued','downloading','extracting'}`. Then:
  - Compute "best" among the ACTIVE rows only (reusing `dupes.ts`'s existing
    resRank/bytes ordering, [dupes.ts:58-60](frontend/src/lib/downloads/dupes.ts:58)),
    NOT over the whole group.
  - Cancel the *other* active rows; **never auto-cancel a non-active row**
    (`finished`/`failed`, or an already-extracted historical record).
  - Only offer the "Keep best" action when the group has **≥2 active rows**.

  This closes the real I2 hole: restricting only the cancel set (not the
  best-selection) still lets a finished historical row be chosen as "best",
  leaving the live re-grab as the "rest" → cancelled. A `download_results` row
  persists after JD clears its package, so a re-grab weeks later legitimately
  creates a second (new-uuid) row; confining the whole operation to active rows
  means a lone live re-grab beside a historical record simply isn't offered
  keep-best (nothing to auto-resolve), and the user removes the stale record
  manually if they want.
- **`downloaded` decision:** a live JD package can momentarily sit in the interim
  `downloaded` flag between download-finish and extraction, but its `state`
  reports one of the active values in that window, so `isActive` covers it. Rows
  whose `state` is terminal (`finished`/`failed`) are intentionally excluded — a
  double-grab where the loser already finished is resolvable only by manual
  per-copy cancel (acceptable: nothing is downloading to waste, and auto-cancel
  there risks killing a genuinely-wanted second copy).
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
  fallback); a **failed write does not prime the cache** and the row is retried
  (not emitted id-less); a **None JD uuid** yields `package_uuid=None` (never the
  string `"None"`); `remove_package(id)` calls JD `remove_links` with the single
  resolved uuid **as an int** (assert the mocked device received an int) and
  deletes only that row; caches/`handed_to_rename` keyed by uuid don't collide
  across two same-name packages.
- **`test_api_routes.py` (scoped):** `POST /download/results/remove {id}` happy
  path + missing id; `GET /download/results` includes `id`/`package_uuid`.
- **Update existing name-based tests** to the id/uuid API (they will otherwise
  fail): `test_api_routes.py` (the `remove_package`/remove-endpoint cases,
  ~:831-836), `test_database.py` (the upsert-/`delete_download_result("…")`-by-name
  cases, ~:272-336), `test_download_service.py` (~:1859-1890, ~:2058-2067).
- **Frontend (vitest):** `dupes.ts` exact-name duplicate groups two same-name
  rows; keep-best is computed over the ACTIVE subset and picks the more-complete
  ACTIVE row; cancel-rest never targets non-active (finished/failed/historical)
  rows; "Keep best" is not offered with <2 active rows; remove targets by `id`.

## Out of Scope (deferred)

- Any change to how JD packages are added/named — purely the persistence identity.
- Redesigning the duplicate UI beyond making detection fire + remove per-copy.
- Retroactively assigning UUIDs to rows whose JD package is already gone (not
  possible; they stay NULL-uuid, keyed by id).
- Reconciling uuid-churn phantom rows automatically (manual remove is the escape
  hatch; see §9).
