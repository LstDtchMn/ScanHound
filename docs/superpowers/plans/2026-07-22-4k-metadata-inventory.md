# Authoritative 4K Metadata Inventory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a durable, searchable, non-destructive 4K technical-metadata inventory, preserving historic FEL/MEL seed evidence and supporting one controlled full-library scan.

**Architecture:** Keep `media_probe` and `dv_scan` as compatibility caches while adding durable scan-run, per-item, seed-baseline, and current-inventory records. The scan service creates a persisted Plex-file manifest before probing, stores every terminal outcome, and writes authoritative HDR10+/DV evidence into a queryable projection. Plex labels and Kometa remain a reviewed post-scan reconciliation stage.

**Tech Stack:** Python 3.11/3.12, SQLite, FastAPI, Svelte 5, ffprobe, `dovi_tool`, pinned `hdr10plus_tool`, Docker, pytest, Vitest, Playwright.

## Global Constraints

- Do not enable Auto-rename, auto-grab, RSS-primary, RSS auto-grab, or file tagging.
- Never move, rename, delete, or write a media file; tests use generated files only.
- Use additive schema migration; old `media_probe`/`dv_scan` callers must remain compatible.
- Treat unknown/failed HDR10+ and DV analysis as retryable evidence gaps, never negatives.
- Scan one file at a time by default; maximum two only after the pilot is accepted.
- Keep Plex label writes and Kometa execution outside the scan path and behind explicit review.
- Follow red-green-refactor for every production behavior change.

---

### Task 1: Durable schema and seed baseline

**Files:**
- Modify: `backend/database.py`
- Modify: `scripts/import_dv_seed.py`
- Create: `tests/test_metadata_inventory_schema.py`

**Interfaces:**
- Produces `DatabaseManager.create_metadata_scan_run`, `create_metadata_scan_items`, `get_metadata_scan_run`, `list_metadata_scan_items`, `upsert_media_inventory`, `search_media_inventory`, and `backfill_dv_seed_baseline`.
- Produces tables `dv_seed_baseline`, `metadata_scan_runs`, `metadata_scan_items`, and `media_inventory`.

- [ ] **Step 1: Write failing migration and provenance tests**

```python
def test_init_db_backfills_seed_rows_without_overwriting_live_scan(tmp_path):
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    db.upsert_dv_scan("/movie.mkv", "fel", source="seed")
    db.init_db()
    assert db.get_dv_seed_baseline("/movie.mkv")["seed_layer"] == "fel"
    db.upsert_dv_scan("/movie.mkv", "mel", source="scan")
    assert db.get_dv_seed_baseline("/movie.mkv")["seed_layer"] == "fel"

def test_scan_run_item_is_terminal_or_resumable(tmp_path):
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    run = db.create_metadata_scan_run(scope="pilot", expected_count=1)
    db.create_metadata_scan_items(run["run_uuid"], [{"path": "/m.mkv"}])
    assert db.list_metadata_scan_items(run["run_uuid"])[0]["status"] == "pending"
```

- [ ] **Step 2: Run the tests and verify expected failure**

Run: `pytest -q tests/test_metadata_inventory_schema.py`

Expected: failure because the new database methods and tables do not exist.

- [ ] **Step 3: Add additive DDL and transactional helpers**

Implement only the tables and methods above. Use parameterized SQL, UUID run IDs, an explicit check-constrained status vocabulary, indexes for the documented filter columns, and a transaction for each state transition. `init_db()` copies legacy `dv_scan.source='seed'` rows into `dv_seed_baseline` with `INSERT OR IGNORE`.

- [ ] **Step 4: Run the schema tests**

Run: `pytest -q tests/test_metadata_inventory_schema.py`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add backend/database.py scripts/import_dv_seed.py tests/test_metadata_inventory_schema.py
git commit -m "Add durable metadata scan inventory schema"
```

### Task 2: Detailed probe and authoritative HDR10+ state

**Files:**
- Modify: `Dockerfile`
- Modify: `backend/rename/mediainfo.py`
- Create: `backend/rename/hdr10plus_detect.py`
- Modify: `tests/test_mediainfo.py`
- Create: `tests/test_hdr10plus_detect.py`

**Interfaces:**
- Produces `probe_detailed(path, db=None) -> dict` with all-stream facts and `hdr10plus_state` in `present|absent|unknown`.
- Produces `detect_hdr10plus(path) -> {state, method, tool_version, error}`.
- Keeps `probe_specs()` output backward compatible.

- [ ] **Step 1: Write failing detector tests**

```python
def test_first_frame_miss_is_not_an_hdr10plus_negative(monkeypatch, tmp_path):
    monkeypatch.setattr(subject, "_quick_frame_evidence", lambda _: False)
    monkeypatch.setattr(subject, "_full_extract", lambda _: {"state": "unknown", "error": "timeout"})
    assert subject.detect_hdr10plus(str(tmp_path / "movie.mkv"))["state"] == "unknown"

def test_completed_full_extract_with_no_metadata_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(subject, "_full_extract", lambda _: {"state": "absent"})
    assert subject.detect_hdr10plus(str(tmp_path / "movie.mkv"))["state"] == "absent"
```

- [ ] **Step 2: Run the detector tests and verify expected failure**

Run: `pytest -q tests/test_hdr10plus_detect.py`

Expected: failure because the detector module does not exist.

- [ ] **Step 3: Implement the minimum detector and detailed probe**

Pin `hdr10plus_tool` in `Dockerfile` with a release checksum. Use the existing ffprobe frame probe only as a positive shortcut. For PQ/HEVC files without a quick positive, invoke the full parser; a failed/unsupported parser is `unknown`, not `absent`. Extend the detailed result with all audio/subtitle streams and color/mastering facts. Preserve the compact `probe_specs()` shape used by rename code.

- [ ] **Step 4: Run focused probe tests**

Run: `pytest -q tests/test_hdr10plus_detect.py tests/test_mediainfo.py tests/test_conflicts_rank.py`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile backend/rename/mediainfo.py backend/rename/hdr10plus_detect.py tests/test_mediainfo.py tests/test_hdr10plus_detect.py
git commit -m "Add authoritative HDR10 plus probe evidence"
```

### Task 3: Persistent scan service and resume semantics

**Files:**
- Modify: `backend/plex_metadata_scan.py`
- Modify: `backend/api/routes/plex.py`
- Modify: `tests/test_plex_metadata_scan.py`
- Create: `tests/test_metadata_scan_runs.py`

**Interfaces:**
- `PlexMetadataScanJob.start(scope, targets) -> run dict`
- `pause(run_uuid)`, `resume(run_uuid)`, `cancel(run_uuid)`, and `status_dict(run_uuid=None)`
- `POST /plex/metadata-scans`, `POST /plex/metadata-scans/{run_uuid}/pause`, `resume`, `cancel`, `retry-failures`; old `/scan-metadata` routes remain compatible.

- [ ] **Step 1: Write failing run-state tests**

```python
def test_cancelled_run_retains_pending_items_for_resume(db, target):
    job = PlexMetadataScanJob(db)
    run = job.start("pilot", [target])
    job.cancel(run["run_uuid"])
    assert db.get_metadata_scan_run(run["run_uuid"])["status"] == "cancelled"
    assert db.list_metadata_scan_items(run["run_uuid"])[0]["status"] in {"pending", "cancelled"}

def test_probe_failure_is_durable_and_retryable(db, target, monkeypatch):
    monkeypatch.setattr(mediainfo, "probe_detailed", lambda *_: (_ for _ in ()).throw(OSError("denied")))
    run = PlexMetadataScanJob(db).start("pilot", [target])
    assert wait_for_terminal(db, run)["failed_count"] == 1
```

- [ ] **Step 2: Run and verify expected failure**

Run: `pytest -q tests/test_metadata_scan_runs.py`

Expected: failure because persisted run operations do not exist.

- [ ] **Step 3: Replace in-memory-only accounting**

Create the manifest before worker start. Persist `stat`, `ffprobe`, `hdr10plus`, `dovi`, `persist`, and terminal transitions. Re-stat after analysis and record `source_changed` if signature differs. Set abandoned running runs to `interrupted` at startup. Maintain one active run, default concurrency one, and derive progress from the database.

- [ ] **Step 4: Run scanner/API tests**

Run: `pytest -q tests/test_metadata_scan_runs.py tests/test_plex_metadata_scan.py tests/test_api_rename.py tests/test_api_routes.py`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add backend/plex_metadata_scan.py backend/api/routes/plex.py tests/test_plex_metadata_scan.py tests/test_metadata_scan_runs.py
git commit -m "Persist metadata scan progress and failures"
```

### Task 4: Current inventory search API and export

**Files:**
- Modify: `backend/database.py`
- Modify: `backend/api/routes/plex.py`
- Create: `tests/test_media_inventory_api.py`

**Interfaces:**
- `GET /media-inventory?q=&library=&resolution=&hdr=&hdr10plus_state=&dv_layer=&dv_profile=&scan_state=&discrepancy=&page=&page_size=&sort=`
- `GET /media-inventory/facets`
- `GET /media-inventory/export.csv`
- `GET /plex/metadata-scans/{run_uuid}/items` and `/discrepancies`

- [ ] **Step 1: Write failing query/API tests**

```python
def test_inventory_filters_hdr10plus_and_failed_state(client, db):
    db.upsert_media_inventory({"path": "/a.mkv", "hdr10plus_state": "present", "scan_state": "current"})
    db.upsert_media_inventory({"path": "/b.mkv", "hdr10plus_state": "unknown", "scan_state": "failed"})
    assert [x["path"] for x in client.get("/media-inventory?hdr10plus_state=present").json()["items"]] == ["/a.mkv"]
    assert [x["path"] for x in client.get("/media-inventory?scan_state=failed").json()["items"]] == ["/b.mkv"]
```

- [ ] **Step 2: Run and verify expected failure**

Run: `pytest -q tests/test_media_inventory_api.py`

Expected: failure because the inventory routes do not exist.

- [ ] **Step 3: Implement indexed queries and safe CSV**

Use an allowlisted sort map, bounded page size, parameterized filters, FTS title/path search, stable `path` tiebreaking, and RFC-compliant CSV that prefixes formula-leading cells with a single quote. Never return raw tool stderr in public responses.

- [ ] **Step 4: Run API tests**

Run: `pytest -q tests/test_media_inventory_api.py tests/test_database.py tests/test_api_routes.py`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add backend/database.py backend/api/routes/plex.py tests/test_media_inventory_api.py
git commit -m "Expose searchable media inventory API"
```

### Task 5: Seed/live discrepancy and label reconciliation evidence

**Files:**
- Modify: `backend/rename/dv_labeler.py`
- Modify: `backend/database.py`
- Modify: `tests/test_dv_labeler.py`
- Create: `tests/test_metadata_seed_reconciliation.py`

**Interfaces:**
- `DatabaseManager.list_metadata_discrepancies(run_uuid=None)`
- Label dry-run includes `seed_layer`, `scan_layer`, `discrepancy`, desired and existing labels.

- [ ] **Step 1: Write failing provenance tests**

```python
def test_seed_fel_live_mel_is_reported_before_label_write(db):
    db.insert_dv_seed_baseline("/movie.mkv", "fel")
    db.upsert_dv_scan("/movie.mkv", "mel", source="scan")
    assert db.list_metadata_discrepancies()[0]["discrepancy"] == "seed_fel_live_mel"

def test_dry_run_never_changes_plex_labels_on_disagreement(fake_plex, db):
    report = sync_dv_labels(fake_plex, db, dry_run=True)
    assert report["writes"] == 0
```

- [ ] **Step 2: Run and verify expected failure**

Run: `pytest -q tests/test_metadata_seed_reconciliation.py`

Expected: failure because discrepancy reporting is absent.

- [ ] **Step 3: Implement closed-set reconciliation report**

Preserve the managed label set. Do not write labels automatically from a scan. Include P5/P8 in dry-run output, preserve non-managed labels, and make live/seed disagreement visible to the caller.

- [ ] **Step 4: Run label tests**

Run: `pytest -q tests/test_metadata_seed_reconciliation.py tests/test_dv_labeler.py tests/test_dv_scan_db.py`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add backend/rename/dv_labeler.py backend/database.py tests/test_dv_labeler.py tests/test_metadata_seed_reconciliation.py
git commit -m "Report seed and live metadata discrepancies"
```

### Task 6: Inventory UI and scan controls

**Files:**
- Create: `frontend/src/routes/media-inventory/+page.svelte`
- Create: `frontend/src/lib/stores/mediaInventory.ts`
- Create: `frontend/src/lib/components/media-inventory/InventoryTable.svelte`
- Create: `frontend/src/lib/components/media-inventory/InventoryFilters.svelte`
- Create: `frontend/src/lib/components/media-inventory/InventoryEvidenceDrawer.svelte`
- Modify: `frontend/src/lib/api/client.ts`
- Modify: `frontend/src/lib/api/types.ts`
- Create: `frontend/src/lib/stores/mediaInventory.test.ts`

**Interfaces:**
- Store exposes `loadInventory`, `setFilters`, `startPilot`, `pauseRun`, `resumeRun`, `retryFailures`.
- URL query is the source of truth for filters and pagination.

- [ ] **Step 1: Write failing store tests**

```ts
it('serializes selected filters into the inventory request', async () => {
  await inventoryStore.setFilters({ hdr10plus_state: 'present', dv_layer: 'fel' });
  expect(api.get).toHaveBeenCalledWith(expect.stringContaining('hdr10plus_state=present'));
  expect(api.get).toHaveBeenCalledWith(expect.stringContaining('dv_layer=fel'));
});
```

- [ ] **Step 2: Run and verify expected failure**

Run: `cd frontend; npm run test:unit -- mediaInventory.test.ts`

Expected: failure because the store does not exist.

- [ ] **Step 3: Implement accessible inventory UI**

Use a dedicated route, not the Renames page. Render coverage counts, filter chips, server-paginated results, run status, evidence drawer, mobile cards, empty/error guidance, and keyboard-visible controls. Use the approved Seed → Live → Plex → Kometa evidence rail. Do not expose raw paths or tool errors to unauthenticated users.

- [ ] **Step 4: Run frontend checks**

Run: `cd frontend; npm run check; npm run test:unit; npm run build`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "Add searchable media inventory interface"
```

### Task 7: Kometa configuration and production-safe operational tooling

**Files:**
- Modify: `X:/Docker Apps/Kometa/config/dv-layer.yml` only after a dry-run/report gate
- Create: `docs/feature-pack-review/4K_METADATA_PILOT_AND_FULL_SCAN_RUNBOOK.md`
- Create: `tests/test_metadata_scan_runbook.py`

**Interfaces:**
- Runbook defines pilot manifest, database backup, scan command/API call, dry-run Plex label check, label apply check, Kometa invocation, and evidence package.

- [ ] **Step 1: Write failing runbook contract test**

```python
def test_runbook_requires_pilot_before_full_scan():
    text = Path("docs/feature-pack-review/4K_METADATA_PILOT_AND_FULL_SCAN_RUNBOOK.md").read_text()
    assert "Pilot acceptance" in text
    assert "Auto-rename remains disabled" in text
    assert "Plex label dry run" in text
```

- [ ] **Step 2: Run and verify expected failure**

Run: `pytest -q tests/test_metadata_scan_runbook.py`

Expected: failure because the runbook does not exist.

- [ ] **Step 3: Write the runbook and validate Kometa assets without production execution**

Document a 25-50 item pilot, storage-load telemetry, backup paths discovered at execution time, a zero-write Plex label dry-run, and all full-scan stop conditions. Add P5/P8 badge references only after files exist and a Kometa config syntax check passes. Do not run Kometa or change production settings in this task.

- [ ] **Step 4: Run documentation and focused tests**

Run: `pytest -q tests/test_metadata_scan_runbook.py tests/test_dv_labeler.py`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add docs/feature-pack-review/4K_METADATA_PILOT_AND_FULL_SCAN_RUNBOOK.md tests/test_metadata_scan_runbook.py
git commit -m "Document controlled 4K metadata scan rollout"
```

### Task 8: Integrated verification and handoff

**Files:**
- Modify: `docs/feature-pack-review/RENAMING_PIPELINE_AND_4K_METADATA_AUDIT_2026-07-22.md`

- [ ] **Step 1: Run backend focused suite**

Run: `pytest -q tests/test_metadata_inventory_schema.py tests/test_hdr10plus_detect.py tests/test_metadata_scan_runs.py tests/test_media_inventory_api.py tests/test_metadata_seed_reconciliation.py tests/test_plex_metadata_scan.py tests/test_mediainfo.py tests/test_dv_labeler.py`

Expected: pass.

- [ ] **Step 2: Run unrestricted backend suite**

Run: `pytest -q`

Expected: pass with only documented environment skips.

- [ ] **Step 3: Run frontend and container verification**

Run:

```bash
cd frontend && npm run check && npm run test:unit && npm run build
docker build -t scanhound:metadata-inventory .
docker run --rm scanhound:metadata-inventory sh -lc 'ffprobe -version && dovi_tool --version && hdr10plus_tool --version'
```

Expected: all commands exit zero.

- [ ] **Step 4: Update audit evidence**

Record exact SHA, changed files, test counts, image tool versions, and the fact that no production scan, Plex write, Kometa run, or Auto-rename change occurred.

- [ ] **Step 5: Commit and push normally**

```bash
git add docs/feature-pack-review/RENAMING_PIPELINE_AND_4K_METADATA_AUDIT_2026-07-22.md
git commit -m "Document metadata inventory implementation evidence"
git push origin HEAD
```

## Plan self-review

- Spec coverage: Tasks 1-5 cover durable provenance, analysis, scanning, search, and reconciliation; Task 6 covers UI; Task 7 covers Kometa/runbook; Task 8 covers verification and evidence.
- Safety coverage: Every task preserves disabled destructive features; production Plex/Kometa actions remain explicit post-implementation gates.
- Type consistency: API and database methods introduced in Tasks 1-4 are consumed by Tasks 3, 5, and 6 using the same run UUID, path, and inventory terminology.
- No-placeholder scan: no unfinished or deferred implementation placeholders remain.
