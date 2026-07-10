# download_results id/packageUUID migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let two JDownloader packages with an identical name coexist as two `download_results` rows so duplicates are visible and resolvable per-copy — by repointing the table's identity from `name TEXT PRIMARY KEY` to a surrogate `id` PK + nullable `package_uuid`, and moving every name-keyed consumer to the durable key.

**Architecture:** Surrogate `id INTEGER PRIMARY KEY AUTOINCREMENT` + `package_uuid TEXT` (nullable, partial-unique). Upsert matches on uuid → adopts a legacy NULL-uuid same-name row → else inserts. The schema change is a guarded, crash-safe SQLite table rebuild that can NEVER reach `init_db`'s corrupt-DB quarantine. Per-copy remove by `id` (single-uuid JD removal). Poller emits `id`+stringified uuid to both REST and WS. Keep-best/cancel-rest operate only on the ACTIVE subset.

**Tech Stack:** SQLite (`DatabaseManager`), FastAPI, myjdapi, SvelteKit 5 (runes). Deploy via `docker compose up -d --build` only.

## Global Constraints

- Deploy ONLY via `docker compose up -d --build`.
- **No data loss, ever.** The rebuild MUST NOT be able to trigger `init_db`'s corrupt-DB quarantine ([database.py:538](backend/database.py:538)) — wrap it in its own try/except that raises a `RuntimeError` (which the `sqlite3.OperationalError`/`DatabaseError` handlers do NOT catch). A rollback leaves the old table intact.
- Rebuild is **idempotent** (`PRAGMA table_info` guard) and **crash-safe** (`DROP TABLE IF EXISTS …_new` + `BEGIN IMMEDIATE`).
- Legacy rows whose JD package is gone can never get a uuid — they stay NULL-uuid, keyed by `id`.
- `id` (number) and `package_uuid` (string|null) present + same JSON type in BOTH the REST response and the WS broadcast for a row.
- Never let `package_uuid` be the string `"None"` — normalize at the poller: `str(u) if u is not None else None`.
- Backend tests run on the HOST: `python -m pytest tests/<file> -v` (no `--timeout`; the full `test_api_routes.py` hangs on network tests — run scoped). Frontend: `cd frontend && npx vitest run`, `npm run check`, `npm run build`.
- Tests accompany each unit; existing name-based tests must be updated (they otherwise fail).

---

## File Structure

**Backend (modify):** `backend/database.py` (schema rebuild + indexes; `upsert_download_result`; `get_download_results`; `delete_download_result`), `backend/download_service.py` (`remove_package`; `poll_results`), `backend/api/main.py` (`handed_to_rename`), `backend/api/routes/downloads.py` (remove endpoint).
**Frontend (modify):** `frontend/src/lib/api/types.ts`, `frontend/src/lib/api/client.ts`, `frontend/src/lib/downloads/dupes.ts`, `frontend/src/lib/components/mobile/MobileDownloadsView.svelte`, `frontend/src/routes/downloads/+page.svelte`.
**Tests:** `tests/test_database.py`, `tests/test_download_service.py`, `tests/test_api_routes.py`, `frontend/src/lib/downloads/dupes.test.ts`.

---

## Task 1: Schema rebuild migration + indexes

**Files:** Modify `backend/database.py` (the `download_results` `CREATE TABLE IF NOT EXISTS` ~[:358](backend/database.py:358); the index section ~[:480](backend/database.py:480)). Test `tests/test_database.py`.

**Interfaces:** Produces the new-shape `download_results` table (id PK, package_uuid, name non-unique) on both fresh and migrated DBs; a partial UNIQUE index on package_uuid.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py
import sqlite3
from backend.database import DatabaseManager

def test_migration_preserves_legacy_rows(tmp_path):
    db_path = str(tmp_path / "t.db")
    # Seed an OLD-shape download_results table + rows, then open DatabaseManager.
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE download_results (name TEXT PRIMARY KEY, title TEXT, "
                 "host TEXT, bytes_total INTEGER, bytes_loaded INTEGER, downloaded INTEGER, "
                 "extraction TEXT, state TEXT, error TEXT, updated_at TIMESTAMP)")
    conn.execute("INSERT INTO download_results (name,title,host,bytes_total,bytes_loaded,"
                 "downloaded,extraction,state,error) VALUES "
                 "('Foo [1080p]','Foo','rapidgator',100,100,1,'success','finished',NULL)")
    conn.commit(); conn.close()
    db = DatabaseManager(db_path=db_path)
    cols = {r[1] for r in db.get_connection().execute("PRAGMA table_info(download_results)")}
    assert "id" in cols and "package_uuid" in cols
    rows = db.get_download_results()
    assert len(rows) == 1
    assert rows[0]["name"] == "Foo [1080p]" and rows[0]["package_uuid"] is None
    assert isinstance(rows[0]["id"], int)
    db.close()

def test_migration_idempotent_and_indexes_on_fresh_db(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "t.db"))  # fresh → new schema directly
    idx = {r[1] for r in db.get_connection().execute("PRAGMA index_list(download_results)")}
    assert "idx_download_results_uuid" in idx
    # second open is a no-op (idempotent)
    db.close(); db2 = DatabaseManager(db_path=str(tmp_path / "t.db")); db2.close()

def test_orphan_new_table_does_not_break_rebuild(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE download_results (name TEXT PRIMARY KEY, title TEXT, host TEXT, "
                 "bytes_total INTEGER, bytes_loaded INTEGER, downloaded INTEGER, extraction TEXT, "
                 "state TEXT, error TEXT, updated_at TIMESTAMP)")
    conn.execute("CREATE TABLE download_results_new (x INTEGER)")  # planted orphan
    conn.commit(); conn.close()
    db = DatabaseManager(db_path=db_path)  # must not raise
    assert "id" in {r[1] for r in db.get_connection().execute("PRAGMA table_info(download_results)")}
    db.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_database.py -k "migration or orphan" -v`
Expected: FAIL (old schema has no `id`/`package_uuid`; no `idx_download_results_uuid`).

- [ ] **Step 3: Implement**

(a) Replace the `download_results` `CREATE TABLE IF NOT EXISTS` ([:358-371](backend/database.py:358)) with the new shape:
```python
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS download_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        package_uuid TEXT,
                        name TEXT,
                        title TEXT,
                        host TEXT,
                        bytes_total INTEGER DEFAULT 0,
                        bytes_loaded INTEGER DEFAULT 0,
                        downloaded INTEGER DEFAULT 0,
                        extraction TEXT DEFAULT 'na',
                        state TEXT DEFAULT 'queued',
                        error TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # ── download_results: name-PK → surrogate-id rebuild (once) ──
                # Guarded, crash-safe, and self-contained: a failure raises
                # RuntimeError (NOT sqlite3.*Error), so it can never reach the
                # corrupt-DB quarantine below (which would wipe the whole DB).
                dr_cols = {r[1] for r in cursor.execute("PRAGMA table_info(download_results)")}
                if dr_cols and "id" not in dr_cols:
                    try:
                        cursor.execute("DROP TABLE IF EXISTS download_results_new")
                        if conn.in_transaction:
                            conn.commit()
                        cursor.execute("BEGIN IMMEDIATE")
                        cursor.execute('''
                            CREATE TABLE download_results_new (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                package_uuid TEXT, name TEXT, title TEXT, host TEXT,
                                bytes_total INTEGER DEFAULT 0, bytes_loaded INTEGER DEFAULT 0,
                                downloaded INTEGER DEFAULT 0, extraction TEXT DEFAULT 'na',
                                state TEXT DEFAULT 'queued', error TEXT,
                                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
                        ''')
                        cursor.execute('''
                            INSERT INTO download_results_new
                                (package_uuid, name, title, host, bytes_total, bytes_loaded,
                                 downloaded, extraction, state, error, updated_at)
                            SELECT NULL, name, title, host, bytes_total, bytes_loaded,
                                   downloaded, extraction, state, error, updated_at
                            FROM download_results
                        ''')
                        cursor.execute("DROP TABLE download_results")
                        cursor.execute("ALTER TABLE download_results_new RENAME TO download_results")
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.exception("download_results rebuild failed")
                        raise RuntimeError("download_results migration failed") from e
```
*(Note: this is the ONLY `download_results` CREATE — the guard `dr_cols and "id" not in dr_cols` is False for a fresh DB just created new-shape, and True only for an old-shape existing table.)*

(b) In the index section (after [:480](backend/database.py:480)), add:
```python
                cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_download_results_uuid '
                               'ON download_results(package_uuid) WHERE package_uuid IS NOT NULL')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_download_results_name '
                               'ON download_results(name)')
```
(Keep the existing `idx_download_results_updated` line — it's recreated here after the rebuild's DROP.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_database.py -k "migration or orphan or index" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/database.py tests/test_database.py
git commit -m "feat(db): rebuild download_results to surrogate id PK + package_uuid (crash-safe migration)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: `upsert_download_result` (adopt-or-insert) + get/delete by id

**Files:** Modify `backend/database.py` (`upsert_download_result` ~[:951](backend/database.py:951), `get_download_results` ~[:978](backend/database.py:978), `delete_download_result` ~[:991](backend/database.py:991)). Test `tests/test_database.py`.

**Interfaces:**
- Produces: `upsert_download_result(name, package_uuid=None, …) -> int | None` (row id, None on failure).
- `get_download_results(limit=200)` rows include `id`, `package_uuid`.
- `delete_download_result(id: int) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database.py  (uses the db_manager fixture from conftest, or DatabaseManager(tmp))
def test_upsert_two_same_name_uuids_coexist(db_manager):
    a = db_manager.upsert_download_result("Foo", package_uuid="111", state="downloading")
    b = db_manager.upsert_download_result("Foo", package_uuid="222", state="downloading")
    assert a != b
    rows = db_manager.get_download_results()
    assert {r["package_uuid"] for r in rows} == {"111", "222"}

def test_upsert_update_by_uuid_and_name_change(db_manager):
    i = db_manager.upsert_download_result("Foo", package_uuid="111")
    j = db_manager.upsert_download_result("Foo RENAMED", package_uuid="111")
    assert i == j
    row = [r for r in db_manager.get_download_results() if r["id"] == i][0]
    assert row["name"] == "Foo RENAMED"

def test_upsert_adopts_legacy_null_uuid_row(db_manager):
    # Legacy row (no uuid), then a live poll of the same name with a uuid → adopts it.
    legacy = db_manager.upsert_download_result("Foo", package_uuid=None)
    adopted = db_manager.upsert_download_result("Foo", package_uuid="111")
    assert adopted == legacy
    rows = db_manager.get_download_results()
    assert len(rows) == 1 and rows[0]["package_uuid"] == "111"

def test_unique_uuid_index_rejects_second_row(db_manager):
    db_manager.upsert_download_result("A", package_uuid="111")
    # A direct duplicate-uuid insert must be rejected by the partial unique index.
    import sqlite3, pytest
    with pytest.raises(sqlite3.IntegrityError):
        db_manager.get_connection().execute(
            "INSERT INTO download_results (package_uuid, name) VALUES ('111','B')")

def test_delete_by_id_removes_one_of_two_same_name(db_manager):
    a = db_manager.upsert_download_result("Foo", package_uuid="111")
    db_manager.upsert_download_result("Foo", package_uuid="222")
    assert db_manager.delete_download_result(a) == 1
    assert {r["package_uuid"] for r in db_manager.get_download_results()} == {"222"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_database.py -k "upsert or delete_by_id or unique_uuid" -v`
Expected: FAIL (`upsert_download_result` uses ON CONFLICT(name); `delete_download_result` takes name).

- [ ] **Step 3: Implement**

Replace `upsert_download_result` with a one-lock-hold adopt-or-insert (RLock is reentrant; single connection):
```python
    def upsert_download_result(self, name, package_uuid=None, title=None, host=None,
                               bytes_total=0, bytes_loaded=0, downloaded=0,
                               extraction="na", state="queued", error=None):
        """Insert/update a JD package's download outcome; returns the row id (int)
        or None on failure. Identity is package_uuid when present, else the row is
        adopted-by-name (a legacy NULL-uuid row) or inserted. Runs the whole
        lookup-then-write under one lock hold to avoid poller-vs-remove races."""
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return None
                cur = conn.cursor()
                row = None
                if package_uuid is not None:
                    cur.execute("SELECT id FROM download_results WHERE package_uuid = ?",
                                (package_uuid,))
                    row = cur.fetchone()
                    if row is None:
                        cur.execute("SELECT id FROM download_results "
                                    "WHERE package_uuid IS NULL AND name = ? "
                                    "ORDER BY updated_at DESC LIMIT 1", (name,))
                        row = cur.fetchone()
                else:
                    cur.execute("SELECT id FROM download_results WHERE name = ? "
                                "ORDER BY (package_uuid IS NULL) DESC, updated_at DESC LIMIT 1",
                                (name,))
                    row = cur.fetchone()
                if row is not None:
                    rid = row[0]
                    cur.execute(
                        "UPDATE download_results SET "
                        "package_uuid = COALESCE(?, package_uuid), name = ?, title = ?, "
                        "host = ?, bytes_total = ?, bytes_loaded = ?, downloaded = ?, "
                        "extraction = ?, state = ?, error = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        (package_uuid, name, title, host, bytes_total, bytes_loaded,
                         downloaded, extraction, state, error, rid))
                    conn.commit()
                    return rid
                cur.execute(
                    "INSERT INTO download_results (package_uuid, name, title, host, "
                    "bytes_total, bytes_loaded, downloaded, extraction, state, error, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (package_uuid, name, title, host, bytes_total, bytes_loaded,
                     downloaded, extraction, state, error))
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logger.error("DB Error (upsert_download_result): %s", e)
            return None
```
Update `get_download_results` SELECT to include `id, package_uuid`:
```python
            "SELECT id, package_uuid, name, title, host, bytes_total, bytes_loaded, "
            "downloaded, extraction, state, error, updated_at "
            "FROM download_results ORDER BY updated_at DESC LIMIT ?",
```
Change `delete_download_result(self, name)` → `delete_download_result(self, id_)` (delete `WHERE id = ?`, return rows affected — keep the existing cursor/rowcount pattern).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_database.py -v`
Expected: PASS (new tests + the migration tests). **Update any existing `test_database.py` cases that upsert/delete by name (~:272-336) to the new signatures.**

- [ ] **Step 5: Commit**

```bash
git add backend/database.py tests/test_database.py
git commit -m "feat(db): upsert_download_result adopt-or-insert by uuid; get/delete by id

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: `remove_package(id)` + `poll_results` emits id, caches by uuid

**Files:** Modify `backend/download_service.py` (`remove_package` [:637](backend/download_service.py:637); `poll_results` [:663](backend/download_service.py:663)). Test `tests/test_download_service.py`.

**Interfaces:** `remove_package(id_)` (was `name`); `poll_results` rows gain `id` + `package_uuid`.

- [ ] **Step 1: Write the failing test** (mirror the existing download_service test harness / mocked device)

```python
def test_remove_package_by_id_single_uuid(download_service, db_manager, monkeypatch):
    rid = db_manager.upsert_download_result("Foo", package_uuid="111", state="downloading")
    calls = {}
    class _Dev:
        class downloads:
            @staticmethod
            def remove_links(links, uuids): calls["uuids"] = uuids
    monkeypatch.setattr(download_service, "_connect_jd_device", lambda: _Dev())
    out = download_service.remove_package(rid)
    assert out["ok"] and calls["uuids"] == [111]           # int, not "111"
    assert db_manager.get_download_results() == []

def test_poll_two_same_name_two_rows_with_ids(download_service, db_manager, monkeypatch):
    # mock query_packages to return two same-name packages with distinct uuids;
    # assert two rows persist and each returned row has an int id + str uuid.
    ...
```

- [ ] **Step 2: Run to verify it fails** — `python -m pytest tests/test_download_service.py -k "remove_package_by_id or poll_two_same_name" -v` → FAIL.

- [ ] **Step 3: Implement**

`remove_package`:
```python
    def remove_package(self, id_: int) -> dict:
        """Remove a single tracked download by its row id: remove ONLY that
        package from JD (by its uuid) and delete its result row. Idempotent."""
        row = None
        try:
            rows = self.db.get_download_results(limit=100000) if self.db else []
            row = next((r for r in rows if r.get("id") == id_), None)
        except Exception:
            row = None
        uuid = (row or {}).get("package_uuid")
        if uuid:
            try:
                device = self._connect_jd_device()
                device.downloads.remove_links([], [int(uuid)])   # JD expects the native int64
                self._log(f"JDownloader: removed package uuid {uuid}", "info")
            except Exception as e:
                logger.warning("remove_package JD step failed for id %s (uuid %s): %s", id_, uuid, e)
                self._invalidate_jd_cache()
        removed = 0
        try:
            removed = self.db.delete_download_result(id_) if self.db else 0
        except Exception as e:
            logger.warning("remove_package DB delete failed for id %s: %s", id_, e)
        return {"ok": True, "removed": removed}
```
`poll_results`: normalize the uuid, prime the change-cache ONLY after a successful write, keep a `uuid → id` map, and put `id` + `package_uuid` in every returned row. Replace the persist block ([:774-785](backend/download_service.py:774)) so it:
- computes `u = pkg.get("uuid"); package_uuid = str(u) if u is not None else None`; `cache_key = package_uuid or name`;
- builds `row` with `"id": None` and `"package_uuid": package_uuid` added;
- when `record and self.db`: if `self._results_cache.get(cache_key) != change_key`, call `rid = self.db.upsert_download_result(**{fields}, package_uuid=package_uuid)`; if `rid is not None`: `self._results_cache[cache_key] = change_key; self._uuid_id[cache_key] = rid`; else leave the cache unset (retry next poll). Set `row["id"] = self._uuid_id.get(cache_key)`; if still None, `SELECT id` via a new `db.get_download_result_id(package_uuid, name)` (add it) and if that misses, re-run the upsert.
- Change `_best_titles`/`_results_cache`/new `_uuid_id` to key by `cache_key` and prune all three by the live `cache_key` set (replace the name-based prune at [:791-793](backend/download_service.py:791)).

Add `self._uuid_id: Dict[str, int] = {}` in `__init__` beside `_results_cache`. Add `DatabaseManager.get_download_result_id(package_uuid, name) -> int | None` (SELECT id by uuid, else by NULL-uuid name most-recent).

- [ ] **Step 4: Run** — `python -m pytest tests/test_download_service.py -v` → PASS (update existing name-based cases ~:1859-1890, ~:2058-2067).

- [ ] **Step 5: Commit**

```bash
git add backend/download_service.py backend/database.py tests/test_download_service.py
git commit -m "feat(downloads): remove_package by id (single-uuid JD removal); poll emits id, caches by uuid

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Auto-rename hook `handed_to_rename` keyed by uuid

**Files:** Modify `backend/api/main.py` (`_start_results_poller`, `handed_to_rename` [:252](backend/api/main.py:252), the `name`-gated hand-off [:277](backend/api/main.py:277)).

- [ ] **Step 1: Implement** — key `handed_to_rename` by the row's `package_uuid` (fall back to `name` when a row has no uuid), so two same-name extracted packages are each handed to auto-rename; prune it to the current poll's live uuids each cycle. (The row dicts now carry `package_uuid` from Task 3.)
- [ ] **Step 2: Verify** — `python -m pytest tests/test_api_routes.py -k "results or poller" -v` (scoped) + confirm no name-collision skip. If no direct test exists, add a focused one asserting two same-uuid-distinct same-name extracted packages are both handed off (mock the rename service).
- [ ] **Step 3: Commit**

```bash
git add backend/api/main.py tests/test_api_routes.py
git commit -m "fix(downloads): auto-rename hook keys handed_to_rename by uuid, not name

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: Remove endpoint by id

**Files:** Modify `backend/api/routes/downloads.py` (`RemoveResultRequest` [:440](backend/api/routes/downloads.py:440), the route [:444](backend/api/routes/downloads.py:444)). Test `tests/test_api_routes.py`.

- [ ] **Step 1: Failing test** — `POST /download/results/remove {"id": <rid>}` returns `{ok:true}` and deletes that row; a missing/unknown id returns `{ok:true, removed:0}`.
- [ ] **Step 2: Run** → FAIL (model still `{name: str}`).
- [ ] **Step 3: Implement**
```python
class RemoveResultRequest(BaseModel):
    id: int

@router.post("/results/remove")
def remove_download_result(req: RemoveResultRequest, reg: ServiceRegistry = Depends(get_registry)):
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    return dl.remove_package(req.id)
```
- [ ] **Step 4: Run** → PASS (update the existing name-based endpoint test ~[:831-836](tests/test_api_routes.py:831)).
- [ ] **Step 5: Commit**
```bash
git add backend/api/routes/downloads.py tests/test_api_routes.py
git commit -m "feat(api): /download/results/remove takes {id}

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Frontend types + client

**Files:** Modify `frontend/src/lib/api/types.ts` (`DownloadResult` [:500](frontend/src/lib/api/types.ts:500)), `frontend/src/lib/api/client.ts` (`removeDownloadResult` [:226](frontend/src/lib/api/client.ts:226)).

- [ ] **Step 1: Implement**
```ts
// types.ts — DownloadResult
export interface DownloadResult {
  id: number;
  package_uuid: string | null;
  name: string;
  title: string | null;
  host: string | null;
  bytes_total: number;
  bytes_loaded: number;
  downloaded: number;
  extraction: string;
  state: string;
  error: string | null;
  updated_at?: string;   // REST only
  save_to?: string;      // WS only
}
```
```ts
// client.ts
removeDownloadResult: (id: number) =>
  request<{ ok: boolean; removed: number }>('/download/results/remove', {
    method: 'POST', body: JSON.stringify({ id })
  }),
```
- [ ] **Step 2: Verify** — `cd frontend && npm run check` (0 new errors — will surface every remaining `r.name` remove call, fixed in Tasks 8-9).
- [ ] **Step 3: Commit**
```bash
git add frontend/src/lib/api/types.ts frontend/src/lib/api/client.ts
git commit -m "feat(downloads): DownloadResult gains id/package_uuid; removeDownloadResult(id)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: `dupes.ts` — active-subset best-selection

**Files:** Modify `frontend/src/lib/downloads/dupes.ts`. Test `frontend/src/lib/downloads/dupes.test.ts`.

**Interfaces:** `DownloadGroup` gains `activeItems: DownloadResult[]` and `canKeepBest: boolean`; `best` is chosen over the active subset.

- [ ] **Step 1: Write the failing test**
```ts
import { describe, it, expect } from 'vitest';
import { groupDownloads, isActive } from './dupes';
const row = (o: any) => ({ id: 0, package_uuid: null, name: '', title: '', host: '',
  bytes_total: 0, bytes_loaded: 0, downloaded: 0, extraction: 'na', state: 'downloading',
  error: null, ...o });

it('best is chosen among ACTIVE rows, not a finished historical row', () => {
  const g = groupDownloads([
    row({ id: 1, title: 'Foo', name: 'Foo.2160p', state: 'finished' }),   // historical, higher res
    row({ id: 2, title: 'Foo', name: 'Foo.1080p', state: 'downloading' }), // live re-grab
  ])[0];
  expect(g.best.id).toBe(2);          // the live one, NOT the finished 2160p
  expect(g.canKeepBest).toBe(false);  // only 1 active row → not offered
});

it('canKeepBest true only with >=2 active rows', () => {
  const g = groupDownloads([
    row({ id: 1, title: 'Foo', name: 'Foo.2160p', state: 'downloading' }),
    row({ id: 2, title: 'Foo', name: 'Foo.1080p', state: 'downloading' }),
  ])[0];
  expect(g.canKeepBest).toBe(true);
  expect(g.best.id).toBe(1);
});
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**
```ts
const ACTIVE = new Set(['queued', 'downloading', 'extracting']);
export function isActive(r: DownloadResult): boolean { return ACTIVE.has(r.state); }

export interface DownloadGroup {
  key: string; title: string; items: DownloadResult[];
  activeItems: DownloadResult[]; isDuplicate: boolean;
  best: DownloadResult; canKeepBest: boolean;
}
// in groupDownloads, per group:
const activeItems = items.filter(isActive);
const rankPool = activeItems.length ? activeItems : items;
const best = [...rankPool].sort(
  (a, b) => resRank(b.name) - resRank(a.name) || (b.bytes_total || 0) - (a.bytes_total || 0)
)[0];
groups.push({ key, title: items[0].title || items[0].name, items, activeItems,
  isDuplicate: items.length > 1, best, canKeepBest: activeItems.length >= 2 });
```
- [ ] **Step 4: Run** — `cd frontend && npx vitest run dupes` → PASS. **Step 5: Commit**
```bash
git add frontend/src/lib/downloads/dupes.ts frontend/src/lib/downloads/dupes.test.ts
git commit -m "feat(downloads): dupes keep-best chosen over the active subset only

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: `MobileDownloadsView.svelte` — id-keying + active keep-best

**Files:** Modify `frontend/src/lib/components/mobile/MobileDownloadsView.svelte`.

- [ ] **Step 1: Implement**
- Keyed each ([:129](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:129)): `{#each g.items as r (r.id)}`.
- `cancel` ([:89-90](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:89)): `await api.removeDownloadResult(r.id); results = results.filter((x) => x.id !== r.id);`.
- `clearFinished` ([:82](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:82)): `await api.removeDownloadResult(r.id);`.
- `keepBest` ([:97-100](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:97)): cancel the OTHER active rows only —
  `for (const r of g.activeItems) if (r.id !== g.best.id) await cancel(r);`.
- Gate the "Keep best" button ([:126](frontend/src/lib/components/mobile/MobileDownloadsView.svelte:126)) on `g.canKeepBest` (`{#if g.isDuplicate && g.canKeepBest}`).
- [ ] **Step 2: Verify** — `cd frontend && npm run check && npm run build` → 0 errors, build ok.
- [ ] **Step 3: Commit**
```bash
git add frontend/src/lib/components/mobile/MobileDownloadsView.svelte
git commit -m "feat(downloads): mobile view keys + removes by id; keep-best over active rows

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 9: Desktop `/downloads` page — id-keying

**Files:** Modify `frontend/src/routes/downloads/+page.svelte`.

- [ ] **Step 1: Implement** — the WS handler `dlResults = d.results` ([:339](frontend/src/routes/downloads/+page.svelte:339)) now receives rows with `id`; change the results `{#each}` key ([~:528](frontend/src/routes/downloads/+page.svelte:528)) from `(r.name)` to `(r.id)`, and every `removeDownloadResult(...)` / name comparison / keep-best on that page to `id` (grep the file for `.name` on download rows and for `removeDownloadResult`). Apply the same active-subset keep-best rule if the desktop page has its own dedup UI; if it reuses `groupDownloads`, consume `activeItems`/`canKeepBest`.
- [ ] **Step 2: Verify** — `cd frontend && npm run check && npm run build` → 0 errors, build ok.
- [ ] **Step 3: Commit**
```bash
git add frontend/src/routes/downloads/+page.svelte
git commit -m "feat(downloads): desktop page keys + removes downloads by id

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 10: Full verification + deploy

- [ ] **Step 1: Backend** — `python -m pytest tests/test_database.py tests/test_download_service.py -v` and the scoped `tests/test_api_routes.py` download cases → all PASS.
- [ ] **Step 2: Frontend** — `cd frontend && npx vitest run && npm run check && npm run build` → all PASS.
- [ ] **Step 3: Migration dry-run against a COPY of the live DB** — copy the production `download_results` DB file to a temp path, open it with `DatabaseManager(db_path=copy)`, and assert the row count is unchanged and every row now has an `id` + `package_uuid=NULL`. (Do NOT run the migration against the live file outside the container; the container migrates it on startup.)
- [ ] **Step 4: Changelog + version bump** — add a `changelog.ts` entry (duplicate downloads now coexist + are resolvable per-copy; internal DB identity change) and commit.
- [ ] **Step 5: Deploy** — `docker compose up -d --build`; confirm healthy startup logs ("All services initialized", "Application startup complete") AND that the download_results migration ran without error (check `docker logs scanhound` for the rebuild path / no "migration failed").
- [ ] **Step 6: Live check** — open `/downloads` (desktop + mobile viewport): the list renders, states update over WS, cancel removes one row. Confirm the existing 200-download history still shows (migration preserved it).

---

## Self-Review Notes (author)

- **Spec coverage:** schema+migration (T1), upsert/get/delete (T2), remove_package+poll (T3), auto-rename hook (T4), API (T5), types+client (T6), dupes active-subset (T7), mobile (T8), desktop (T9), verify+deploy (T10). All spec §1-§9 map to a task.
- **Data safety:** the rebuild raises `RuntimeError` (T1) — verified it isn't caught by `init_db`'s `sqlite3.OperationalError`/`DatabaseError` handlers, so it can't reach the quarantine. T10 Step-3 dry-runs against a copy of the live DB.
- **Type consistency:** `id: number`/`package_uuid: string|null` identical across `DownloadResult`, the poller rows (REST+WS), and the remove path. `remove_links` gets `int(uuid)` (T3).
- **No-regression:** existing name-based tests are explicitly updated in T2/T3/T5.
