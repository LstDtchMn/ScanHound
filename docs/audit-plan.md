# ScanHound Codebase Audit Plan

> **Audit date**: 2026-03-11
> **Scope**: Backend Python, UI Controllers/Models, QML, Frontend v2 (Svelte/Tauri)
> **Agents used**: 4 parallel auditors (backend, controllers, QML, frontend)

## Issue Registry

| ID | Severity | Area | File | Lines | Summary |
|----|----------|------|------|-------|---------|
| B1 | CRITICAL | Backend | `backend/plex_service.py` | 632-634 | Undefined `movie_age`/`tv_age` — NameError at runtime |
| B2 | HIGH | Backend API | `backend/api/routes/scanner.py` | 47-50, 169 | `_scan_state` dict modified without `_scan_lock` |
| B3 | HIGH | Backend API | `backend/api/ws.py Storm` | 48-61 | `broadcast_sync()` message dict captured by reference |
| B4 | MEDIUM | Backend API | `backend/api/routes/results.py` | 17, 62, 85-103 | `_selected` set accessed without lock |
| B5 | MEDIUM | Backend API | `backend/api/routes/plex.py` | 51-52 | Missing null guard — `plex` can be None |
| B6 | MEDIUM | Backend API | `backend/api/routes/results.py` | 25-79 | Stats computed from unfiltered items (design ambiguity) |
| C1 | CRITICAL | Controller | `ui/controllers/scanner_controller.py` | 453-475, 949 | `_all_items` list read/written across threads without lock |
| C2 | HIGH | Controller | `ui/controllers/scanner_controller.py` | 205 | Lambda returns live `_all_items` reference to other threads |
| C3 | HIGH | Controller | `ui/controllers/source_search_controller.py` | 43-59 | Async event loop not cleaned up on thread interrupt |
| C4 | MEDIUM | Controller | `ui/controllers/scanner_controller.py` | 78-92 | `_parse_size()` edge case: empty string after regex |
| C5 | MEDIUM | Controller | `ui/controllers/source_search_controller.py` | 742-754 | `_parse_size()` empty string before `float()` |
| C6 | MEDIUM | Controller | `ui/controllers/source_search_controller.py` | 794-797 | Stale poster worker reference; no quit/wait before clearing |
| Q1 | CRITICAL | QML | `ui/qml/components/SourceSearchWindow.qml` | 269 | `indexOf` filter matching may fail on type/casing mismatch |
| Q2 | CRITICAL | QML | `ui/qml/SettingsDialog.qml` | 350-372 | Plex server ComboBox init-fire race |
| Q3 | HIGH | QML | `ui/qml/components/WatchlistDialog.qml` | 93 | Sort mode guard fires before `refreshWatchlist()` completes |
| Q4 | HIGH | QML | `ui/qml/components/settings/LogTab.qml` | 19-24 | Bulk log load blocks UI thread |
| Q5 | MEDIUM | QML | All 19 QML files | Multiple | No accessibility labels on any interactive element |
| Q6 | MEDIUM | QML | `ui/qml/SettingsDialog.qml` | 122-150 | ComboBox currentIndex binding loops on every change |
| F1 | CRITICAL | Frontend | `frontend/src/routes/downloads/+page.svelte` | 36-38 | `$effect()` with no deps creates infinite loop |
| F2 | CRITICAL | Frontend | `frontend/src/lib/api/client.ts` + downloads page | 61-65, 29 | Batch download sends group_keys instead of `{url, title}` |
| F3 | HIGH | Frontend | `frontend/src/lib/stores/connection.ts` | — | No listener for Tauri `sidecar-terminated` event |
| F4 | HIGH | Frontend | `frontend/src/lib/stores/settings.ts` | 21-26 | `saveSettings()` updates originalSettings even on API failure |
| F5 | HIGH | Frontend | `frontend/src/lib/stores/results.ts` | 62-74 | `selectAll`/`deselectAll` no error handling |
| F6 | HIGH | Frontend | `frontend/src/lib/stores/settings.ts` | 23, 30 | Uses subscribe+unsubscribe instead of `get()` |
| F7 | MEDIUM | Frontend | `frontend/src/lib/stores/connection.ts` | 44-55 | WS parse errors silently dropped |
| F8 | MEDIUM | Frontend | `frontend/src/lib/api/client.ts` | request() | No Content-Type check before `.json()` |
| F9 | MEDIUM | Frontend | `frontend/src/lib/components/ResultTile.svelte` | 19-24 | `getVariant()` crashes on null/undefined status |
| F10 | MEDIUM | Frontend | `frontend/src/lib/stores/connection.ts` | 40-42 | `onopen` doesn't set state; relies on server `connected` msg |

---

## Decision Questionnaire

Before implementation, choose a strategy for these multi-approach fixes:

### Decision 1: `_all_items` thread safety (C1 + C2)

| Option | Approach | Pros | Cons |
|--------|----------|------|------|
| **A** | `threading.Lock` around all reads/writes | Simple, minimal change | Locks on every property getter (UI thread) |
| **B** | Copy-on-write: `_on_scan_finished` atomically replaces list ref; lambda returns `list(self._all_items)` | No lock contention on UI thread reads | Extra allocations on scan complete + every lambda call |
| **C** | `QReadWriteLock` — readers share, writer exclusive | Best concurrency for read-heavy (many property getters) | More complex API, PySide6-specific |

**Recommendation**: **B** — Python's GIL makes single-ref assignment atomic. The lambda snapshot prevents iteration-during-mutation. No lock overhead on the frequent UI property reads.

### Decision 2: Stats vs filtered results (B6)

| Option | Approach |
|--------|----------|
| **A** | Stats always show **all** items (current behavior) — document it |
| **B** | Stats reflect **filtered** items only |
| **C** | Return both `stats` (all) and `filtered_stats` (filtered) |

**Recommendation**: **A** — The frontend FilterBar uses stats to show counts on filter buttons (e.g., "Missing (12)"), which should always show totals regardless of active filter.

### Decision 3: Frontend connection state on WS open (F10)

| Option | Approach |
|--------|----------|
| **A** | Set `connected` immediately on `onopen` |
| **B** | Keep current: wait for server's `connected` message (confirms backend is alive, not just TCP) |

**Recommendation**: **B** — The server `connected` message confirms the backend API is initialized, not just that the TCP socket opened. More meaningful.

---

## Phased Execution Plan

### Phase 1: CRITICAL Fixes (6 items)

These will crash or infinite-loop at runtime. Fix first.

#### B1: Undefined `movie_age`/`tv_age` in plex_service.py

**File**: `backend/plex_service.py:632-634`

**Problem**: Code references `movie_age` and `tv_age` which don't exist. The refactored code creates `ages` dict with `"movies"` and `"tv"` keys.

**Fix**:
```python
# Replace lines 632-634:
# OLD:
return False, (
    f"Cache is {max(movie_age, tv_age):.1f}h old but "
    f"{len(new_items)} new item(s) detected in Plex since last cache."
)

# NEW:
max_age = max(ages.values()) if ages else 0
return False, (
    f"Cache is {max_age:.1f}h old but "
    f"{len(new_items)} new item(s) detected in Plex since last cache."
)
```

**Verify**: `python -m py_compile backend/plex_service.py`

---

#### C1: Race condition on `_all_items` list

**File**: `ui/controllers/scanner_controller.py:453-475, 949`

**Problem**: `_all_items` written from worker thread, read from UI thread property getters.

**Fix** (Option B — copy-on-write):
```python
# In _on_scan_finished (line 949):
# Assignment is atomic under GIL, readers get old or new ref, never partial
self._all_items = list(results)  # Already a new list — this is fine as-is

# No lock needed on property getters since they read a stable reference.
# The real fix is C2 (lambda snapshot).
```

**Verify**: `python -m py_compile ui/controllers/scanner_controller.py`

---

#### Q1: indexOf filter binding in SourceSearchWindow

**File**: `ui/qml/components/SourceSearchWindow.qml:269`

**Problem**: `indexOf(modelData)` may fail on type or whitespace mismatch.

**Fix**:
```qml
// Add helper function near top of SourceSearchWindow:
function isFilterActive(filterName) {
    var filters = sourceSearch.activeFilters
    for (var i = 0; i < filters.length; i++) {
        if (String(filters[i]).trim() === String(filterName).trim()) return true
    }
    return false
}

// Line 269 — replace:
//   checked: sourceSearch.activeFilters.indexOf(modelData) >= 0
// with:
checked: isFilterActive(modelData)
```

---

#### Q2: Plex server ComboBox init-fire

**File**: `ui/qml/SettingsDialog.qml:350-372`

**Problem**: `onCurrentTextChanged` can fire before `applyServers()` completes.

**Fix**:
```qml
// Ensure ready is set AFTER all model/index changes:
function applyServers(servers) {
    ready = false
    model = servers
    var idx = servers.indexOf(savedServerName)
    currentIndex = idx >= 0 ? idx : -1
    ready = true
}
Component.onCompleted: applyServers(settings.getPlexServers())
```

---

#### F1: Infinite loop in downloads page

**File**: `frontend/src/routes/downloads/+page.svelte:36-38`

**Problem**: `$effect(() => { loadHistory() })` re-runs on every state change (infinite loop).

**Fix**:
```svelte
// Replace $effect block with onMount:
import { onMount } from 'svelte';

onMount(() => {
    loadHistory();
});

// DELETE:
// $effect(() => {
//     loadHistory();
// });
```

---

#### F2: Batch download type mismatch

**File**: `frontend/src/routes/downloads/+page.svelte:25-33` + `frontend/src/lib/api/client.ts:61-65`

**Problem**: Frontend sends `string[]` (group_keys) but API expects `{url, title}[]`.

**Fix** — change downloads page to build proper objects from results store:
```svelte
// In downloads/+page.svelte, replace downloadSelected():
import { filteredResults } from '$lib/stores/results';

async function downloadSelected() {
    const keys = [...$selectedKeys];
    if (keys.length === 0) {
        addToast('No Selection', 'Select items from the scan results first.', 'warning');
        return;
    }
    // Build proper download items from results
    let allResults: ScanResult[] = [];
    filteredResults.subscribe(r => allResults = r)();
    const items = allResults
        .filter(r => keys.includes(r.group_key) && r.url)
        .map(r => ({ url: r.url, title: r.title }));
    if (items.length === 0) {
        addToast('No URLs', 'Selected items have no download URLs.', 'warning');
        return;
    }
    try {
        await api.downloadBatch(items);
        addToast('Downloads Started', `Queued ${items.length} item(s) for download.`);
    } catch {
        addToast('Error', 'Failed to start downloads.', 'error');
    }
}
```

Also add the import:
```svelte
import type { ScanResult } from '$lib/api/types';
import { get } from 'svelte/store';
```

**Verify**: `npm run build` in `frontend/`

---

### Phase 2: HIGH Fixes — Thread Safety (4 items)

#### B2: `_scan_state` unprotected writes

**File**: `backend/api/routes/scanner.py:47-50, 169`

**Fix**:
```python
# _progress_callback (lines 47-50):
def _progress_callback(progress: float, phase: str) -> None:
    with _scan_lock:
        _scan_state["progress"] = progress
        _scan_state["phase"] = phase
    ws_manager.broadcast_sync({
        "type": "scan:progress",
        "data": {"progress": progress, "phase": phase},
    })

# scan_status endpoint (line 169):
@router.get("/status")
def scan_status():
    with _scan_lock:
        return dict(_scan_state)
```

---

#### B3: `broadcast_sync()` message captured by reference

**File**: `backend/api/ws.py:48-61`

**Fix**:
```python
def broadcast_sync(self, message: Dict[str, Any]) -> None:
    try:
        loop = asyncio.get_running_loop()
        msg_copy = dict(message)  # Snapshot to avoid reference issues
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self.broadcast(msg_copy))
        )
    except RuntimeError:
        pass
    except Exception:
        logger.warning("WebSocket broadcast failed", exc_info=True)
```

---

#### C2: Lambda returns live `_all_items` reference

**File**: `ui/controllers/scanner_controller.py:205`

**Fix**:
```python
# Replace lambda at line 205:
# OLD:
all_items_getter=lambda: self._all_items,
# NEW:
all_items_getter=lambda: list(self._all_items),
```

---

#### C3: Async event loop not cleaned up

**File**: `ui/controllers/source_search_controller.py:43-59`

**Fix**:
```python
def run(self):
    try:
        registry = get_registry()
        source = registry.get_source(self._source_name)
        if source is None:
            self.finished.emit(self._token, self._source_name, [],
                             f"Source '{self._source_name}' is unavailable.")
            return

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(source.search(self._query, self._mode))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

        message = result.errors[0] if result.errors else ""
        self.finished.emit(self._token, self._source_name,
                          list(result.releases), message)
    except Exception as e:
        logger.warning("Source search failed for %s: %s", self._source_name, e)
        self.finished.emit(self._token, self._source_name, [], str(e))
```

**Verify**: `python -m py_compile ui/controllers/source_search_controller.py`

---

### Phase 3: HIGH Fixes — Frontend + QML (6 items)

#### F3: Missing sidecar termination handler

**File**: `frontend/src/lib/stores/connection.ts`

**Fix** — add Tauri event listener in `connect()`:
```typescript
// Add import at top:
// Only import if running inside Tauri (check window.__TAURI__)
async function listenForSidecarEvents() {
    try {
        const { listen } = await import('@tauri-apps/api/event');
        listen('sidecar-terminated', () => {
            state.set('disconnected');
            // Dispatch to notification store
            const fns = handlers.get('sidecar:terminated');
            if (fns) fns.forEach((fn) => fn({}));
        });
    } catch {
        // Not running in Tauri (dev browser mode) — skip
    }
}

// Call in connect():
function connect() {
    if (ws?.readyState === WebSocket.OPEN) return;
    state.set('connecting');
    listenForSidecarEvents();  // Add this line
    // ... rest unchanged
}
```

---

#### F4: Settings save error handling

**File**: `frontend/src/lib/stores/settings.ts:21-26`

**Fix**:
```typescript
import { get } from 'svelte/store';

export async function saveSettings() {
    const current = get(settings);
    await api.updateSettings(current);
    originalSettings.set(structuredClone(current));
}
```

This also fixes **F6** (improper subscribe pattern). If the API throws, `originalSettings` is not updated, preserving dirty state.

---

#### F5: selectAll/deselectAll no error handling

**File**: `frontend/src/lib/stores/results.ts:62-74`

**Fix**:
```typescript
export async function selectAll() {
    await api.selectAll();
    const items = get(results);
    const keys = new Set(items.map((i) => i.group_key));
    selectedKeys.set(keys);
}

export async function deselectAll() {
    await api.deselectAll();
    selectedKeys.set(new Set());
}
```

Add `import { get } from 'svelte/store';` at top. If API throws, the caller's catch block handles it. Local state is not updated on failure.

---

#### Q3: WatchlistDialog sort mode init-fire

**File**: `ui/qml/components/WatchlistDialog.qml:93`

**Fix** — move `_ready = true` AFTER refresh:
```qml
Component.onCompleted: {
    refreshWatchlist()
    _ready = true
}
```

---

#### Q4: LogTab bulk load blocks UI

**File**: `ui/qml/components/settings/LogTab.qml:19-24`

**Fix** — chunk the appends:
```qml
Component.onCompleted: {
    var history = settings.getLogHistory()
    var chunk = 50
    function appendChunk(startIdx) {
        for (var i = startIdx; i < Math.min(startIdx + chunk, history.length); i++) {
            logModel.append({ message: history[i].message, level: history[i].level })
        }
        if (startIdx + chunk < history.length) {
            Qt.callLater(function() { appendChunk(startIdx + chunk) })
        }
    }
    if (history.length > 0) appendChunk(0)
}
```

---

### Phase 4: MEDIUM Fixes (11 items)

#### B4: `_selected` set unprotected

**File**: `backend/api/routes/results.py`

**Fix**: Add `_selected_lock = threading.Lock()` and wrap all `_selected` accesses:
```python
import threading
_selected_lock = threading.Lock()

# In select_items:
with _selected_lock:
    if req.selected:
        _selected.update(req.group_keys)
    else:
        _selected.difference_update(req.group_keys)
    count = len(_selected)

# In select_all:
with _selected_lock:
    for item in raw_items:
        gk = getattr(item, "group_key", None) or (item.get("group_key") if isinstance(item, dict) else None)
        if gk:
            _selected.add(gk)
    count = len(_selected)

# In deselect_all:
with _selected_lock:
    _selected.clear()

# In get_results, for each item:
with _selected_lock:
    selected_snapshot = set(_selected)
# Then use selected_snapshot for lookups (outside lock)
```

---

#### B5: Missing null guard in plex routes

**File**: `backend/api/routes/plex.py:51-52`

**Fix**:
```python
# Line 51-52 — add plex guard:
"movie_count": len(plex.plex_movies) if plex and plex.plex_movies else 0,
"tv_count": len(plex.plex_tv) if plex and plex.plex_tv else 0,
```

---

#### B6: Stats vs filtered inconsistency

**Decision**: Keep current behavior (Option A). Add a comment:
```python
# Stats always reflect ALL scan results (used for filter tab counts),
# independent of the active filter/search/pagination.
```

---

#### C4: `_parse_size()` edge case in scanner_controller

**File**: `ui/controllers/scanner_controller.py:78-92`

**Fix**:
```python
@staticmethod
def parse_size(size_str: str) -> float:
    try:
        if not size_str or size_str in ["-", "?", "Unknown"]:
            return 0.0
        s = str(size_str).upper().replace(" ", "").replace(",", "")
        numeric_match = re.search(r'[\d.]+', s)
        if not numeric_match:
            return 0.0
        val = float(numeric_match.group())
        if "TIB" in s or "TB" in s:
            return val * 1024
        elif "GIB" in s or "GB" in s:
            return val
        elif "MIB" in s or "MB" in s:
            return val / 1024
        return val
    except (ValueError, TypeError, AttributeError):
        return 0.0
```

---

#### C5: `_parse_size()` empty string in source_search_controller

**File**: `ui/controllers/source_search_controller.py:742-754`

**Fix**: Add empty check before `float()`:
```python
s = upper.replace("TB", "").replace("GB", "").replace("MB", "").strip()
if not s:
    return 0.0
val = float(s)
```

---

#### C6: Stale poster worker reference

**File**: `ui/controllers/source_search_controller.py:794-797`

**Fix**:
```python
if self._poster_worker is not None:
    try:
        self._poster_worker.finished.disconnect()
    except RuntimeError:
        pass
    self._poster_worker.quit()
    self._poster_worker.wait(2000)
    self._poster_worker.deleteLater()
    self._poster_worker = None
```

---

#### F7: WS parse errors silently dropped

**File**: `frontend/src/lib/stores/connection.ts:52-54`

**Fix**: Already logs to console. Low impact — no change needed beyond existing `console.error`.

---

#### F8: No Content-Type check in API client

**File**: `frontend/src/lib/api/client.ts`

**Fix**: Add guard in `request()`:
```typescript
if (!resp.ok) {
    throw new Error(`API error: ${resp.status} ${resp.statusText}`);
}
const ct = resp.headers.get('content-type');
if (ct && !ct.includes('application/json')) {
    throw new Error(`Expected JSON response, got ${ct}`);
}
return resp.json();
```

---

#### F9: `getVariant()` crashes on null status

**File**: `frontend/src/lib/components/ResultTile.svelte:19-24` and `ResultRow.svelte`

**Fix**:
```typescript
function getVariant(status: string) {
    if (!status) return 'default' as const;
    for (const [key, val] of Object.entries(statusVariant)) {
        if (status.toLowerCase().includes(key)) return val;
    }
    return 'default' as const;
}
```

---

#### Q5: Missing accessibility labels

**Scope**: All 19 QML files. Deferred to Phase 2 (polish) of the v2.0 roadmap — not a runtime bug.

---

#### Q6: ComboBox binding overcomplexity

**Scope**: Performance optimization. Low priority — no runtime bug.

---

## Verification Steps

After each phase:

```bash
# Python compilation check
python -m py_compile backend/plex_service.py backend/api/routes/scanner.py backend/api/ws.py backend/api/routes/results.py backend/api/routes/plex.py ui/controllers/scanner_controller.py ui/controllers/source_search_controller.py

# Python tests
python -m pytest tests/ -x --tb=short

# Frontend build
cd frontend && npm run build

# Rust check
cd frontend/src-tauri && cargo check
```

---

## Summary Table

| Phase | Fix IDs | Files | Est. Lines Changed |
|-------|---------|-------|--------------------|
| 1: CRITICAL | B1, C1, Q1, Q2, F1, F2 | 6 | ~45 |
| 2: HIGH Thread Safety | B2, B3, C2, C3 | 4 | ~35 |
| 3: HIGH Frontend+QML | F3, F4, F5, F6, Q3, Q4 | 5 | ~50 |
| 4: MEDIUM | B4, B5, B6, C4, C5, C6, F8, F9 | 8 | ~60 |
| **Total** | **28 fixes** | **~18 unique files** | **~190 lines** |

### Cross-Dependencies

- C2 depends on C1 (same `_all_items` issue — apply together)
- F4 and F6 are the same fix (use `get()` instead of subscribe pattern)
- F3 depends on Tauri `@tauri-apps/api` being available (already installed)
- Q5 and Q6 are deferred — not runtime bugs
