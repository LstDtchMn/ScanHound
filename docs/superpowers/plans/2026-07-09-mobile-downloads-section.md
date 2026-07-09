# Mobile Downloads Section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a mobile Downloads section (live progress + global JD controls + duplicate handling) reachable from a new mobile bottom tab bar that also reserves a slot for a future Renames tab.

**Architecture:** The backend already serves live per-package progress via `GET /download/results` and global controls via `POST /download/jd-control`; this feature adds one backend capability (remove a single JD package) and a mobile-only UI: a route-based bottom tab bar in the layout, and a `MobileDownloadsView` rendered by the existing `/downloads` route when on a phone. Duplicate grouping is computed client-side from the polled results.

**Tech Stack:** SvelteKit 5 (runes: `$state`/`$derived`/`$effect`/`$props`), FastAPI, myjdapi, vitest, pytest.

## Global Constraints

- Deploy in-app changes ONLY via `docker compose up -d --build`.
- The download API router is mounted at prefix `/download` (singular) — all endpoints/paths use `/download/...`.
- v1 uses **polling** of `GET /download/results` (~2.5s while visible); no new WebSocket channel.
- The desktop `/downloads` page and desktop navigation must be UNCHANGED.
- The mobile fork uses the `mobile` store from `$lib/stores/media` (same gate the scan page uses); the tab bar itself is placed in the layout's existing `md:hidden` region.
- Every new request/response field must be declared on its Pydantic model (avoid `extra="forbid"` 422s).
- TDD: each task writes a failing test first, then the minimal code, then commits.
- Backend tests run in a throwaway `scanhound:latest` container with the repo `docker cp`-ed to `/work` (pytest/httpx not in the prod image); frontend tests run on the host via `npm run test:unit` / `npm run check`. See [[scanhound-testing]].

**Reference — `download_results` row shape** (from `GET /download/results`, type `DownloadResult` in `frontend/src/lib/api/types.ts:500`):
`{ name, title, host, bytes_total, bytes_loaded, downloaded (0|1), extraction, state, error, updated_at }` where `state ∈ 'queued'|'downloading'|'downloaded'|'extracting'|'extracted'|'failed'`.

---

## File Structure

- **Create** `frontend/src/lib/downloads/dupes.ts` — pure duplicate-grouping logic (normalize title, group, detect exact dups, pick best). Unit-tested in isolation.
- **Create** `frontend/src/lib/downloads/dupes.test.ts` — tests for the above.
- **Create** `frontend/src/lib/components/mobile/MobileDownloadsView.svelte` — the mobile Downloads view (fetch/poll, controls, grouped list).
- **Create** `frontend/src/lib/components/mobile/MobileTabBar.svelte` — fixed bottom tab bar (Scan / Downloads / Renames-disabled).
- **Modify** `backend/database.py` — add `delete_download_result(name)`.
- **Modify** `backend/download_service.py` — add `remove_package(name)`.
- **Modify** `backend/api/routes/downloads.py` — add `POST /download/results/remove`.
- **Modify** `frontend/src/lib/api/client.ts` — add `removeDownloadResult(name)`.
- **Modify** `frontend/src/routes/downloads/+page.svelte` — fork to `MobileDownloadsView` when `$mobile`.
- **Modify** `frontend/src/routes/+layout.svelte` — mount `MobileTabBar` in the `md:hidden` region + add bottom padding to main.
- **Test** `tests/test_database.py`, `tests/test_download_service.py`, `tests/test_api_routes.py` (or `tests/test_api_downloads.py` if present).

---

## Task 1: DB — delete a download_result row by name

**Files:**
- Modify: `backend/database.py` (near `clear_download_results`, ~line 987)
- Test: `tests/test_database.py`

**Interfaces:**
- Produces: `DatabaseManager.delete_download_result(name: str) -> int` (rows affected).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_database.py` (inside the class that holds download-result tests, or a new `TestDownloadResults` class):

```python
def test_delete_download_result_removes_only_named_row(self, db_manager):
    db_manager.upsert_download_result(name="Foo [1080p]", title="Foo", host="rg.net",
                                      bytes_total=100, bytes_loaded=50, downloaded=0,
                                      extraction="na", state="downloading", error=None)
    db_manager.upsert_download_result(name="Bar [4K]", title="Bar", host="rg.net",
                                      bytes_total=100, bytes_loaded=100, downloaded=1,
                                      extraction="success", state="extracted", error=None)
    n = db_manager.delete_download_result("Foo [1080p]")
    assert n == 1
    names = {r["name"] for r in db_manager.get_download_results()}
    assert names == {"Bar [4K]"}

def test_delete_download_result_missing_is_noop(self, db_manager):
    assert db_manager.delete_download_result("nope") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_database.py -k delete_download_result -q`
Expected: FAIL (`AttributeError: 'DatabaseManager' object has no attribute 'delete_download_result'`).

- [ ] **Step 3: Write minimal implementation**

Add to `backend/database.py` right after `clear_download_results`:

```python
    def delete_download_result(self, name):
        """Delete the tracked download/extraction outcome for a single package
        by its JDownloader package ``name``. Returns rows affected (0 if none)."""
        return self._mutate(
            "DELETE FROM download_results WHERE name = ?", (name,),
            label="delete_download_result")
```

Note: confirm `_mutate` returns the affected row count (it is used elsewhere returning counts, e.g. `reset_applying_rename_jobs`). If `_mutate` returns something else, return `cursor.rowcount` via the same pattern the other delete helpers use.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_database.py -k delete_download_result -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/database.py tests/test_database.py
git commit -m "feat(db): delete_download_result(name)"
```

---

## Task 2: DownloadService.remove_package + endpoint

**Files:**
- Modify: `backend/download_service.py` (near `jd_control`, ~line 608)
- Modify: `backend/api/routes/downloads.py` (near the `DELETE /results` handler, ~line 432)
- Test: `tests/test_download_service.py`, `tests/test_api_routes.py`

**Interfaces:**
- Consumes: `DatabaseManager.delete_download_result(name)` (Task 1); `self._connect_jd_device()`.
- Produces: `DownloadService.remove_package(name: str) -> dict` → `{"ok": bool, "removed": int, "error"?: str}`; endpoint `POST /download/results/remove` body `{"name": str}` → `{"ok": bool, "removed": int}`.

- [ ] **Step 1: Write the failing test** (`tests/test_download_service.py`)

```python
class TestRemovePackage:
    def _svc_with_device(self, packages):
        svc = _make_service(db=MagicMock())
        device = MagicMock()
        device.downloads.query_packages.return_value = packages
        svc._connect_jd_device = MagicMock(return_value=device)
        return svc, device

    def test_remove_package_removes_matching_uuid_and_row(self):
        svc, device = self._svc_with_device([
            {"name": "Foo [1080p]", "uuid": 111},
            {"name": "Bar [4K]", "uuid": 222},
        ])
        svc.db.delete_download_result.return_value = 1
        out = svc.remove_package("Foo [1080p]")
        assert out["ok"] is True
        device.downloads.remove_links.assert_called_once_with([], [111])
        svc.db.delete_download_result.assert_called_once_with("Foo [1080p]")

    def test_remove_package_absent_in_jd_still_deletes_row_idempotent(self):
        svc, device = self._svc_with_device([{"name": "Bar [4K]", "uuid": 222}])
        svc.db.delete_download_result.return_value = 1
        out = svc.remove_package("Foo [1080p]")  # not in JD
        assert out["ok"] is True
        device.downloads.remove_links.assert_not_called()
        svc.db.delete_download_result.assert_called_once_with("Foo [1080p]")

    def test_remove_package_jd_unreachable_still_deletes_row(self):
        svc = _make_service(db=MagicMock())
        svc._connect_jd_device = MagicMock(side_effect=Exception("no jd"))
        svc.db.delete_download_result.return_value = 1
        out = svc.remove_package("Foo [1080p]")
        assert out["ok"] is True   # DB row cleared even when JD is down
        svc.db.delete_download_result.assert_called_once_with("Foo [1080p]")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_download_service.py -k RemovePackage -q`
Expected: FAIL (`AttributeError: ... 'remove_package'`).

- [ ] **Step 3: Write minimal implementation** (`backend/download_service.py`, after `jd_control`)

```python
    def remove_package(self, name: str) -> dict:
        """Remove a single download package from JDownloader (by package name)
        and delete its tracked result row. Idempotent: succeeds even when the
        package is already gone from JD or JD is unreachable — the DB row is
        always cleared so the UI reflects the removal."""
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "No package name"}
        # Best-effort JD removal (never blocks the DB cleanup).
        try:
            device = self._connect_jd_device()
            packages = device.downloads.query_packages([{"name": True, "uuid": True}]) or []
            uuids = [p.get("uuid") for p in packages if p.get("name") == name and p.get("uuid") is not None]
            if uuids:
                device.downloads.remove_links([], uuids)
                self._log(f"JDownloader: removed package '{name}'", "info")
        except Exception as e:
            logger.warning("remove_package JD step failed for %r: %s", name, e)
            self._invalidate_jd_cache()
        removed = 0
        try:
            removed = self.db.delete_download_result(name) if self.db else 0
        except Exception as e:
            logger.warning("remove_package DB delete failed for %r: %s", name, e)
        return {"ok": True, "removed": removed}
```

Note: `device.downloads.remove_links(link_ids, package_ids)` is the myjdapi signature — pass `[]` for links and the package UUID list. `_invalidate_jd_cache` and `_connect_jd_device` already exist (used by `jd_control`/`poll_results`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_download_service.py -k RemovePackage -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Write the failing endpoint test** (`tests/test_api_routes.py` — follow the file's existing `client` fixture + `registry` mocking; model the request off the existing `POST /download/jd-control` test if present)

```python
def test_remove_download_result_endpoint(client, registry):
    registry.download.remove_package.return_value = {"ok": True, "removed": 1}
    r = client.post("/download/results/remove", json={"name": "Foo [1080p]"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "removed": 1}
    registry.download.remove_package.assert_called_once_with("Foo [1080p]")
```

(If `test_api_routes.py` uses network-dependent fixtures that hang, place this in the same file as the existing `/download/*` route tests and run only this test id.)

- [ ] **Step 6: Add the endpoint** (`backend/api/routes/downloads.py`, after the `DELETE /results` handler ~line 436)

```python
class RemoveResultRequest(BaseModel):
    name: str


@router.post("/results/remove")
def remove_download_result(req: RemoveResultRequest, reg: ServiceRegistry = Depends(get_registry)):
    dl = reg.download
    if not dl:
        raise HTTPException(status_code=503, detail="Download service not available")
    return dl.remove_package(req.name)
```

Ensure `BaseModel` is imported at the top of the file (it is, for the other request models like `JdControlRequest`).

- [ ] **Step 7: Run the endpoint test**

Run: `python -m pytest tests/test_api_routes.py -k remove_download_result -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/download_service.py backend/api/routes/downloads.py tests/test_download_service.py tests/test_api_routes.py
git commit -m "feat(downloads): remove_package + POST /download/results/remove"
```

---

## Task 3: Frontend API client — removeDownloadResult

**Files:**
- Modify: `frontend/src/lib/api/client.ts` (near `clearDownloadResults`, ~line 224)

**Interfaces:**
- Consumes: `POST /download/results/remove` (Task 2).
- Produces: `api.removeDownloadResult(name: string): Promise<{ ok: boolean; removed: number }>`.

- [ ] **Step 1: Add the method** (right after `clearDownloadResults`)

```typescript
  removeDownloadResult: (name: string) =>
    request<{ ok: boolean; removed: number }>('/download/results/remove', {
      method: 'POST',
      body: JSON.stringify({ name })
    }),
```

- [ ] **Step 2: Type-check**

Run: `cd frontend && npm run check`
Expected: 0 errors (pre-existing a11y warnings only).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api/client.ts
git commit -m "feat(api): removeDownloadResult(name) client method"
```

---

## Task 4: Duplicate-grouping logic (pure module)

**Files:**
- Create: `frontend/src/lib/downloads/dupes.ts`
- Test: `frontend/src/lib/downloads/dupes.test.ts`

**Interfaces:**
- Consumes: `DownloadResult` from `$lib/api/types`.
- Produces:
  - `normalizeTitle(s: string): string`
  - `resRank(name: string): number` (4K=4, 1080p=3, 720p=2, else 1)
  - `interface DownloadGroup { key: string; title: string; items: DownloadResult[]; isDuplicate: boolean; best: DownloadResult }`
  - `groupDownloads(results: DownloadResult[]): DownloadGroup[]`

- [ ] **Step 1: Write the failing test** (`frontend/src/lib/downloads/dupes.test.ts`)

```typescript
import { describe, it, expect } from 'vitest';
import { normalizeTitle, resRank, groupDownloads } from './dupes';
import type { DownloadResult } from '$lib/api/types';

function r(over: Partial<DownloadResult>): DownloadResult {
  return { name: 'X [1080p]', title: 'X', host: 'rg.net', bytes_total: 100, bytes_loaded: 0,
    downloaded: 0, extraction: 'na', state: 'downloading', error: null, updated_at: '', ...over };
}

describe('normalizeTitle', () => {
  it('lowercases and strips year + punctuation', () => {
    expect(normalizeTitle('Killing Faith (2025)')).toBe('killing faith');
    expect(normalizeTitle('Dr. Quinn, Medicine Woman')).toBe('dr quinn medicine woman');
  });
});

describe('resRank', () => {
  it('ranks 4K > 1080p > 720p > other', () => {
    expect(resRank('Foo [4K]')).toBeGreaterThan(resRank('Foo [1080p]'));
    expect(resRank('Foo [1080p]')).toBeGreaterThan(resRank('Foo [720p]'));
  });
});

describe('groupDownloads', () => {
  it('groups same-title releases and flags duplicates, picking best', () => {
    const items = [
      r({ name: 'Heat (1995) [1080p]', title: 'Heat', bytes_total: 10 }),
      r({ name: 'Heat (1995) [4K]', title: 'Heat', bytes_total: 40 }),
      r({ name: 'Solo (2018) [1080p]', title: 'Solo' }),
    ];
    const groups = groupDownloads(items);
    const heat = groups.find((g) => g.title === 'Heat')!;
    expect(heat.items).toHaveLength(2);
    expect(heat.isDuplicate).toBe(true);
    expect(heat.best.name).toBe('Heat (1995) [4K]');   // higher res wins
    const solo = groups.find((g) => g.title === 'Solo')!;
    expect(solo.isDuplicate).toBe(false);
  });

  it('flags exact-same-name packages as duplicate', () => {
    const items = [r({ name: 'Foo [1080p]', title: 'Foo' }), r({ name: 'Foo [1080p]', title: 'Foo' })];
    const g = groupDownloads(items)[0];
    expect(g.isDuplicate).toBe(true);
    expect(g.items).toHaveLength(2);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/downloads/dupes.test.ts`
Expected: FAIL (cannot resolve `./dupes`).

- [ ] **Step 3: Write the implementation** (`frontend/src/lib/downloads/dupes.ts`)

```typescript
import type { DownloadResult } from '$lib/api/types';

/** Lowercase, strip a trailing/embedded (YYYY) year and punctuation, collapse
 *  whitespace — mirrors the backend's title normalization for grouping. */
export function normalizeTitle(s: string): string {
  return (s || '')
    .toLowerCase()
    .replace(/\((?:19|20)\d{2}\)/g, ' ')
    .replace(/\b(?:19|20)\d{2}\b/g, ' ')
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Rank a package's resolution parsed from its name (JD names carry "[4K]" etc.). */
export function resRank(name: string): number {
  const n = (name || '').toLowerCase();
  if (n.includes('4k') || n.includes('2160p')) return 4;
  if (n.includes('1080p')) return 3;
  if (n.includes('720p')) return 2;
  return 1;
}

export interface DownloadGroup {
  key: string;
  title: string;
  items: DownloadResult[];
  isDuplicate: boolean;
  best: DownloadResult;
}

/** Group downloads by normalized title. A group with >1 item is a duplicate
 *  group (covers both "same title, different releases" and "exact same package
 *  twice"). `best` is the highest-resolution then largest item — the one to keep. */
export function groupDownloads(results: DownloadResult[]): DownloadGroup[] {
  const byKey = new Map<string, DownloadResult[]>();
  for (const r of results) {
    const key = normalizeTitle(r.title || r.name);
    const arr = byKey.get(key);
    if (arr) arr.push(r);
    else byKey.set(key, [r]);
  }
  const groups: DownloadGroup[] = [];
  for (const [key, items] of byKey) {
    const best = [...items].sort(
      (a, b) => resRank(b.name) - resRank(a.name) || (b.bytes_total || 0) - (a.bytes_total || 0)
    )[0];
    groups.push({ key, title: items[0].title || items[0].name, items, isDuplicate: items.length > 1, best });
  }
  return groups;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/downloads/dupes.test.ts`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/downloads/dupes.ts frontend/src/lib/downloads/dupes.test.ts
git commit -m "feat(downloads): client-side duplicate grouping helper"
```

---

## Task 5: MobileDownloadsView component

**Files:**
- Create: `frontend/src/lib/components/mobile/MobileDownloadsView.svelte`

**Interfaces:**
- Consumes: `api.downloadResults`, `api.jdControl`, `api.removeDownloadResult`, `api.clearDownloadResults` from `$lib/api/client`; `groupDownloads`, `resRank` from `$lib/downloads/dupes`; `addToast` from `$lib/stores/notifications`; `DownloadResult` type.
- Produces: a default-exported Svelte component (no props).

- [ ] **Step 1: Write the component**

```svelte
<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import { groupDownloads, type DownloadGroup } from '$lib/downloads/dupes';
  import type { DownloadResult } from '$lib/api/types';

  let results = $state<DownloadResult[]>([]);
  let loaded = $state(false);
  let busy = $state(false);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let delay = 2500;

  const groups = $derived(groupDownloads(results));
  const active = $derived(results.filter((r) => r.state === 'downloading').length);
  const queued = $derived(results.filter((r) => r.state === 'queued').length);

  function pct(r: DownloadResult): number {
    return r.bytes_total > 0 ? Math.min(100, Math.round((r.bytes_loaded / r.bytes_total) * 100)) : 0;
  }
  function gb(bytes: number): string {
    return (bytes / 1e9).toFixed(1);
  }
  function stateLabel(s: string): string {
    return ({ queued: 'Queued', downloading: 'Downloading', downloaded: 'Downloaded',
      extracting: 'Extracting', extracted: 'Finished', failed: 'Failed' } as Record<string, string>)[s] || s;
  }

  async function poll() {
    try {
      results = await api.downloadResults();
      loaded = true;
      delay = 2500;
    } catch {
      delay = Math.min(delay * 2, 10000);   // back off on error, keep last list
    } finally {
      if (document.visibilityState === 'visible') timer = setTimeout(poll, delay);
    }
  }

  function onVisibility() {
    if (document.visibilityState === 'visible') {
      if (!timer) poll();
    } else if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  onMount(() => {
    poll();
    document.addEventListener('visibilitychange', onVisibility);
  });
  onDestroy(() => {
    if (timer) clearTimeout(timer);
    document.removeEventListener('visibilitychange', onVisibility);
  });

  async function control(action: 'pause' | 'resume' | 'stop') {
    busy = true;
    try {
      const r = await api.jdControl(action);
      if (!r.ok) throw new Error(r.error || 'failed');
      addToast('JDownloader', `Sent ${action}`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : `Could not ${action}`, 'error');
    } finally {
      busy = false;
    }
  }

  async function clearFinished() {
    const done = results.filter((r) => r.state === 'extracted' || r.downloaded === 1);
    if (!done.length) return;
    for (const r of done) {
      try { await api.removeDownloadResult(r.name); } catch { /* idempotent; ignore */ }
    }
    await poll();
  }

  async function cancel(r: DownloadResult) {
    try {
      await api.removeDownloadResult(r.name);
      results = results.filter((x) => x.name !== r.name);   // optimistic
      addToast('Removed', r.title || r.name);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Could not remove', 'error');
    }
  }

  async function keepBest(g: DownloadGroup) {
    if (!confirm(`Keep "${g.best.name}" and cancel ${g.items.length - 1} other release(s) of ${g.title}?`)) return;
    for (const r of g.items) if (r.name !== g.best.name) await cancel(r);
  }
</script>

<div class="flex flex-col h-full">
  <!-- Summary + global controls -->
  <div class="flex items-center gap-2 px-3 py-2 border-b border-[var(--border)] text-sm">
    <span class="text-[var(--text-secondary)]">{active} downloading · {queued} queued</span>
    <div class="flex-1"></div>
    <button class="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs" disabled={busy} onclick={() => control('pause')}>Pause</button>
    <button class="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs" disabled={busy} onclick={() => control('resume')}>Resume</button>
    <button class="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs" disabled={busy} onclick={() => control('stop')}>Stop</button>
    <button class="px-2 py-1 rounded bg-[var(--bg-tertiary)] text-xs" onclick={clearFinished}>Clear&nbsp;done</button>
  </div>

  <div class="flex-1 overflow-y-auto p-3 space-y-3">
    {#if loaded && results.length === 0}
      <p class="text-center text-[var(--text-secondary)] mt-10">No active downloads.</p>
    {/if}

    {#each groups as g (g.key)}
      <div class="rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] p-3">
        {#if g.isDuplicate}
          <div class="flex items-center gap-2 mb-2">
            <span class="text-xs font-semibold px-2 py-0.5 rounded bg-amber-500/20 text-amber-400">{g.items.length} duplicates</span>
            <span class="text-sm font-medium truncate">{g.title}</span>
            <div class="flex-1"></div>
            <button class="text-xs px-2 py-1 rounded bg-[var(--accent)] text-white" onclick={() => keepBest(g)}>Keep best</button>
          </div>
        {/if}
        {#each g.items as r (r.name)}
          <div class="py-1.5 {g.isDuplicate ? 'pl-2 border-l-2 border-[var(--border)]' : ''}">
            <div class="flex items-center gap-2">
              {#if !g.isDuplicate}<span class="text-sm font-medium truncate">{r.title || r.name}</span>{/if}
              {#if g.isDuplicate}<span class="text-xs text-[var(--text-secondary)] truncate">{r.name}</span>{/if}
              <div class="flex-1"></div>
              <span class="text-xs {r.state === 'failed' ? 'text-red-400' : 'text-[var(--text-secondary)]'}">{stateLabel(r.state)}</span>
              <button class="text-xs px-2 py-0.5 rounded bg-[var(--bg-tertiary)]" onclick={() => cancel(r)} aria-label="Cancel download">Cancel</button>
            </div>
            <div class="mt-1 h-1.5 rounded bg-[var(--bg-tertiary)] overflow-hidden">
              <div class="h-full bg-[var(--accent)]" style="width: {pct(r)}%"></div>
            </div>
            <div class="mt-0.5 text-[11px] text-[var(--text-secondary)]">
              {gb(r.bytes_loaded)} / {gb(r.bytes_total)} GB · {r.host}{#if r.error} · <span class="text-red-400">{r.error}</span>{/if}
            </div>
          </div>
        {/each}
      </div>
    {/each}
  </div>
</div>
```

- [ ] **Step 2: Type-check**

Run: `cd frontend && npm run check`
Expected: 0 errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/components/mobile/MobileDownloadsView.svelte
git commit -m "feat(mobile): MobileDownloadsView (live progress + controls + dupes)"
```

---

## Task 6: Fork the /downloads route to the mobile view

**Files:**
- Modify: `frontend/src/routes/downloads/+page.svelte` (top of markup)

**Interfaces:**
- Consumes: `MobileDownloadsView` (Task 5); `mobile` store from `$lib/stores/media`.

- [ ] **Step 1: Add the import + mobile store** (in the `<script>` block)

```typescript
  import MobileDownloadsView from '$lib/components/mobile/MobileDownloadsView.svelte';
  import { mobile } from '$lib/stores/media';
```

- [ ] **Step 2: Wrap the existing template** — at the very top of the markup, add the mobile fork so the entire existing desktop markup stays in the `{:else}`:

```svelte
{#if $mobile}
  <MobileDownloadsView />
{:else}
  <!-- ==== existing desktop downloads markup unchanged, indented into this block ==== -->
{/if}
```

(Move the existing top-level markup into the `{:else}` branch verbatim; do not modify it.)

- [ ] **Step 3: Guard the desktop-only onMount so mobile doesn't double-poll**

The existing page's `onMount` starts desktop-only polling (jd status, dlResults, etc.). Add an early return at the top of that `onMount` callback so it no-ops on phones (the `MobileDownloadsView` does its own polling):

```typescript
  import { get } from 'svelte/store';
  // ...inside the existing onMount(async () => { ... }) callback, as the FIRST line:
  if (get(mobile)) return;
```

If the page has multiple `onMount`/interval setups for desktop data, guard each the same way. This keeps the mobile fork from running two pollers.

- [ ] **Step 4: Type-check + visual sanity**

Run: `cd frontend && npm run check`
Expected: 0 errors. Then `npm run build` succeeds.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/routes/downloads/+page.svelte
git commit -m "feat(mobile): render MobileDownloadsView on /downloads when on phone"
```

---

## Task 7: Mobile bottom tab bar

**Files:**
- Create: `frontend/src/lib/components/mobile/MobileTabBar.svelte`
- Modify: `frontend/src/routes/+layout.svelte` (the `md:hidden` region + main padding)

**Interfaces:**
- Consumes: `$app/stores` `page` (current route), `$app/navigation` `goto`.
- Produces: a default-exported component (no props).

- [ ] **Step 1: Write the tab bar** (`frontend/src/lib/components/mobile/MobileTabBar.svelte`)

```svelte
<script lang="ts">
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';

  const tabs = [
    { label: 'Scan', href: '/' },
    { label: 'Downloads', href: '/downloads' },
    { label: 'Renames', href: '/renames', disabled: true },   // v1 placeholder
  ];
  function isActive(href: string): boolean {
    return href === '/' ? $page.url.pathname === '/' : $page.url.pathname.startsWith(href);
  }
</script>

<nav class="md:hidden fixed bottom-0 inset-x-0 z-40 flex border-t border-[var(--border)] bg-[var(--bg-secondary)]"
     style="padding-bottom: env(safe-area-inset-bottom);" aria-label="Sections">
  {#each tabs as t}
    <button
      class="flex-1 py-2.5 text-xs font-medium
        {t.disabled ? 'text-[var(--text-secondary)] opacity-40' : isActive(t.href) ? 'text-[var(--accent)]' : 'text-[var(--text-secondary)]'}"
      disabled={t.disabled}
      aria-current={isActive(t.href) ? 'page' : undefined}
      onclick={() => !t.disabled && goto(t.href)}
    >
      {t.label}{#if t.disabled}<span class="block text-[9px] opacity-70">Soon</span>{/if}
    </button>
  {/each}
</nav>
```

- [ ] **Step 2: Mount it in the layout** (`frontend/src/routes/+layout.svelte`)

Import at the top of `<script>`:

```typescript
  import MobileTabBar from '$lib/components/mobile/MobileTabBar.svelte';
```

Render it just before the closing of the app shell `<div>` (the one opened at ~line 142), but NOT on the login route. Locate the existing login-gating pattern (the file already renders bare chrome for `/login`) and place the tab bar inside the "authenticated chrome" branch:

```svelte
  {#if $page.url.pathname !== '/login'}
    <MobileTabBar />
  {/if}
```

Then add bottom padding to `<main>` (id `main-content`, ~line 170) so the tab bar doesn't cover content — add `pb-14` to its class (tab bar is ~56px):

```svelte
    <main id="main-content" class="flex-1 flex flex-col overflow-hidden min-h-0 pb-14 md:pb-0" ...>
```

(`md:pb-0` removes the padding on desktop where the bar is hidden.)

- [ ] **Step 3: Type-check + build**

Run: `cd frontend && npm run check && npm run build`
Expected: 0 errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/components/mobile/MobileTabBar.svelte frontend/src/routes/+layout.svelte
git commit -m "feat(mobile): bottom tab bar (Scan/Downloads/Renames-soon)"
```

---

## Task 8: Full verification + deploy

**Files:** none (verification + changelog)

- [ ] **Step 1: Backend suite (changed modules)** — in a throwaway container per [[scanhound-testing]]:

Run: `python -m pytest tests/test_database.py tests/test_download_service.py tests/test_api_routes.py -k "download_result or RemovePackage or remove_download" -q`
Expected: all pass. (Run the full `test_download_service.py` + `test_database.py` too.)

- [ ] **Step 2: Frontend unit + check + build**

Run: `cd frontend && npm run test:unit && npm run check && npm run build`
Expected: all pass; 0 type errors; build succeeds.

- [ ] **Step 3: Changelog** — prepend an entry to `frontend/src/lib/changelog.ts` (version bump, e.g. `2.23.0`, `date: "2026-07-09"`, summary "Mobile Downloads section", bullets: live progress + pause/resume/stop, duplicate grouping with keep-best/cancel, new bottom tab bar with a Renames slot coming).

- [ ] **Step 4: Deploy + health-check**

```bash
docker compose up -d --build
docker logs scanhound --tail 15   # expect "Application startup complete" + a /results/cached 200
```

- [ ] **Step 5: Commit changelog + push**

```bash
git add frontend/src/lib/changelog.ts
git commit -m "feat(mobile): Downloads section — changelog 2.23.0"
git push origin main
```

---

## Notes for the implementer

- Do NOT touch the desktop `/downloads` markup (Task 6 only wraps it in `{:else}`).
- The `mobile` store (`$lib/stores/media`) is the same phone gate the scan page uses — reuse it, don't invent a new one.
- All download endpoints are under `/download` (singular). Double-check every new path.
- `remove_package` must stay idempotent — the poller races with the UI; a remove of an already-gone package must succeed.
- If `_mutate` doesn't return a row count in Task 1, match whatever the other single-row delete helpers in `database.py` return and adjust the test's `== 1` expectation accordingly.
