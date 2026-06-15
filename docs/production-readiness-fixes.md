# ScanHound v2.0 — Production Readiness Fix Plan

## Role

You are a senior software engineer implementing fixes for the issues identified in the
production readiness review (`docs/2026-03-13-production-readiness-review.md`). Work
through each severity tier in order. Be surgical — fix the actual issue without
refactoring surrounding code.

## Project Context

ScanHound is a Tauri v2 desktop app with a SvelteKit frontend and a FastAPI Python
backend running as a sidecar process. The backend exposes a localhost HTTP/WebSocket API
that the Tauri webview consumes.

- **Frontend**: SvelteKit, TypeScript, Tailwind CSS (in `frontend/`)
- **Backend**: FastAPI, Python (in `backend/`)
- **Tests**: `tests/` directory, pytest

## Implementation Plan

Execute fixes in this exact order. After each numbered item, run the relevant tests to
confirm no regressions. Do not batch fixes — verify after each one.

---

### Step 1: CRITICAL — Lock Down the Localhost Control Plane

**Files**: `backend/api/main.py`, `backend/api/ws.py`, `backend/api/routes/system.py`,
`backend/api/routes/settings.py`, `backend/api/routes/downloads.py`

**What to do**:

1. **Replace wildcard CORS with a strict origin allowlist.** Only allow the Tauri
   webview origin (typically `tauri://localhost` or `https://tauri.localhost` depending
   on Tauri v2 config). Check the Tauri config for the exact scheme/host the webview
   uses and allowlist only that.

2. **Add an app-issued auth token to all HTTP and WebSocket traffic.**
   - On backend startup, generate a cryptographically random nonce (e.g., 32-byte
     `secrets.token_urlsafe()`). Pass it to the Tauri frontend via the sidecar launch
     mechanism (stdout handshake, env var, or temp file — pick whichever the existing
     sidecar launch already supports).
   - Add a FastAPI dependency that checks for this nonce in an `X-ScanHound-Token`
     header on every request. Return 401 if missing or wrong.
   - For WebSocket: validate the token during the handshake (first message or query
     param). Reject connections that don't present the correct token.

3. **Gate dangerous endpoints.** `/shutdown` and any test/debug endpoints must require
   the auth token (already covered above) AND should only be registered when not in
   production mode, or be behind an explicit `--enable-debug-routes` flag.

4. **Add basic SSRF protection for outbound URLs.** For any endpoint that accepts a
   user-supplied URL (webhooks, download URLs, notification targets):
   - Validate the URL scheme is `http` or `https`.
   - Resolve the hostname and reject if it resolves to loopback (`127.0.0.0/8`),
     link-local (`169.254.0.0/16`), or private ranges (`10.0.0.0/8`,
     `172.16.0.0/12`, `192.168.0.0/16`) unless explicitly configured to allow
     internal targets.
   - Normalize the URL before use (strip credentials, resolve path traversal).

**Tests to add**: At least 3 tests — CORS rejects unknown origin, request without token
returns 401, WebSocket without token is rejected.

---

### Step 2: HIGH — Fix the Broken Scheduler Trigger

**Files**: `backend/api/routes/scheduler.py`, `backend/scanner_service.py`,
`tests/test_api_routes.py`

**What to do**:

1. **Fix the function signature mismatch.** The scheduler route calls
   `scanner.run_scan(scan_type="Incremental", progress_callback=...)` but
   `ScannerService.run_scan()` does not accept `progress_callback` and requires
   `source_type`. Fix the call site to match the actual signature.

2. **Route scheduler-triggered scans through the same pipeline as HTTP-triggered
   scans.** Extract a shared scan runner if one doesn't exist, or call the same
   internal method both the HTTP scan endpoint and the scheduler use. This ensures
   consistent WebSocket notifications, result handling, and post-scan hooks.

3. **Fix the test.** The existing test only asserts thread creation. Change it to
   execute the thread target synchronously and assert the actual scan side effects
   (e.g., scanner service was called with correct args, WebSocket notification was
   sent).

---

### Step 3: HIGH — Return Proper HTTP Status Codes for Errors

**Files**: `backend/api/routes/downloads.py`, `backend/api/routes/results.py`,
`backend/api/routes/plex.py`, `frontend/src/lib/api/client.ts`

**What to do**:

1. **Audit every route that returns `{"error": ...}` or `{"success": false}` with
   status 200.** Change each to raise `HTTPException` or return `JSONResponse` with
   the appropriate 4xx/5xx status code.

2. **Ensure success payloads are structurally distinct from error payloads.** Success
   should never contain an `error` key; errors should never contain a `data` key.

3. **Add contract tests** — at least one per affected route — that trigger a failure
   case and assert the response has a non-2xx status code.

4. **Frontend**: Verify `client.ts` error handling still works after the change. It
   should already throw on non-2xx, so this fix aligns backend behavior with the
   frontend's expectations. No frontend changes should be needed, but confirm.

---

### Step 4: HIGH — Add Schema Validation to Configuration Writes

**Files**: `backend/api/routes/settings.py`, `backend/api/routes/plex.py`,
`frontend/src/lib/stores/settings.ts`

**What to do**:

1. **Define a Pydantic model for the settings schema.** Include all known config keys
   with their types, defaults, and constraints. Use `model_config =
   ConfigDict(extra="forbid")` to reject unknown keys.

2. **Apply the model as the request body type on every config-mutating endpoint.**
   FastAPI will automatically validate and return 422 for invalid payloads.

3. **Frontend** (if time permits): Add a TypeScript interface matching the Pydantic
   model. Replace `Record<string, unknown>` in the settings store with the typed
   interface. This is lower priority than the backend validation.

---

### Step 5: MEDIUM — Fix WebSocket Reconnect Listener Leaks

**Files**: `frontend/src/lib/stores/connection.ts`

**What to do**:

1. **Store Tauri `unlisten` callbacks** returned by `listen()` calls. Keep them in a
   module-level or store-level variable.

2. **Register listeners only once** per store lifecycle (e.g., in store initialization
   or a dedicated `setup()` method), not on every reconnect.

3. **Add a manual-disconnect flag.** Set it in `disconnect()`, check it in `onclose`.
   If the flag is set, do not reconnect. Clear the flag when `connect()` is called
   explicitly.

4. **Call stored `unlisten` callbacks in `disconnect()`** to clean up Tauri event
   handlers.

---

### Step 6: MEDIUM — Fix Watchlist Search + Status Filter Interaction

**Files**: `frontend/src/routes/watchlist/+page.svelte`,
`frontend/src/lib/api/client.ts`

**What to do**:

1. **Apply the status filter to search results before rendering.** When `searchResults`
   is active AND `statusFilter` is set, filter `searchResults` client-side by status
   before displaying.

2. Alternatively, if the search API supports a status parameter, pass `statusFilter`
   to the search request. Pick whichever approach is simpler given the existing API.

3. **Add a test or manual verification** that searching with a status filter active
   returns only items matching both the search query and the status.

---

### Step 7: MEDIUM — Fix Analytics Date-Range Summary Mismatch

**Files**: `frontend/src/routes/analytics/+page.svelte`

**What to do**:

1. When the user selects a date range (7d, 30d, 90d), **recompute the summary card
   values from the trend data for that range**, or fetch a matching summary from the
   backend.

2. Ensure the label and the value always correspond to the same time range.

---

### Step 8: MEDIUM — Make Download Progress Callbacks Request-Scoped

**Files**: `backend/api/routes/downloads.py`, `backend/download_service.py`

**What to do**:

1. **Remove the shared `_progress_fn` attribute** from `DownloadService`.

2. **Pass the progress callback as a parameter** to the download method, or key
   callbacks by a job/request ID so concurrent downloads don't overwrite each other.

---

### Step 9: MEDIUM — Fix WebSocket Broadcast Lock Contention

**Files**: `backend/api/ws.py`

**What to do**:

1. **Copy the connection list under the lock**, then release the lock before sending.
2. Send to each client outside the critical section.
3. Collect dead sockets during the send loop and prune them in a follow-up pass (can
   re-acquire the lock briefly for removal).

---

### Step 10: MEDIUM — Strengthen Shallow Tests

**Files**: `tests/test_api_routes.py`, `tests/test_api_analytics_watchlist.py`,
`frontend/src/lib/api/types.ts`

**What to do**:

1. **Scheduler test**: Execute the thread target synchronously instead of only
   asserting thread creation. Assert actual side effects.

2. **Analytics tests**: Update mocked payload shapes to match what
   `frontend/src/lib/api/types.ts` expects. If the types define specific field names
   or structures, the mock must mirror them.

3. **Add at least one contract test** that serializes a real route response and
   compares its shape against the TypeScript type definition (manual comparison is
   fine — just verify key names and value types match).

---

## Verification

After all fixes are complete:

1. Run the full backend test suite: `python -m pytest tests/ -x -q`
2. Run the frontend build: `cd frontend && npm run build`
3. Run frontend tests if they exist: `cd frontend && npm test`
4. Manually verify no regressions in the list of positive patterns from the review:
   - Scanner service scan-state locking still works
   - App shell layout is intact
   - Watchlist stale-async guard still works
   - Empty/loading/error states still render

## Constraints

- Fix one issue at a time. Run tests after each fix.
- Do not refactor surrounding code. Keep changes minimal and focused.
- Do not auto-commit. Present changes for review.
- If a fix requires a design decision (e.g., how to pass the auth token from sidecar
  to frontend), state the options and pick the simplest one, noting the tradeoff.
- Prioritize correctness over elegance. A working fix now beats a perfect fix later.
