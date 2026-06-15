# ScanHound v2.0 Production Readiness Review

> **Date**: 2026-03-13
> **Scope**: Full code-review audit of the Tauri v2 + SvelteKit frontend and FastAPI + Python backend

## CRITICAL

- **[backend/api/main.py:142](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/main.py#L142), [backend/api/ws.py:69](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/ws.py#L69), [backend/api/routes/system.py:22](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/system.py#L22), [backend/api/routes/settings.py:71](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/settings.py#L71), [backend/api/routes/downloads.py:17](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/downloads.py#L17)** - The localhost control plane is effectively unauthenticated.
  - **Problem**: The API allows `*` CORS, the WebSocket accepts any client, `/shutdown` is open, and multiple test/download endpoints can trigger outbound HTTP/SMTP/browser/JDownloader activity with caller-supplied values. In a desktop app this creates a localhost attack surface: any malicious webpage opened in the user's browser can drive the sidecar and make it send notifications, hit internal hosts, enqueue downloads, or shut the app down.
  - **Fix**: Lock the API to trusted origins only, require an app-issued auth token or nonce on both HTTP and WebSocket traffic, reject cross-origin WebSocket handshakes, and gate dangerous endpoints behind explicit auth plus production guards. Add SSRF protections for outbound URLs: allowlist schemes/hosts where possible, reject loopback/private ranges for webhook-like inputs, and normalize or validate URLs before use.

## HIGH

- **[backend/api/routes/scheduler.py:97](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/scheduler.py#L97), [backend/scanner_service.py:220](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/scanner_service.py#L220), [tests/test_api_routes.py:1179](/c:/Users/NLSur/OneDrive/Documents/MediaScout/tests/test_api_routes.py#L1179)** - Manual scheduler trigger is broken and the tests do not execute the failing path.
  - **Problem**: The scheduler route starts a thread that calls `scanner.run_scan(scan_type="Incremental", progress_callback=...)`, but `ScannerService.run_scan()` does not accept `progress_callback` and requires `source_type`. This will fail on a real invocation. It also bypasses the normal scan route pipeline, so even a signature fix would still skip shared behavior like consistent WebSocket/result handling and any post-scan hooks. The test only asserts that a thread was created, so it never catches the runtime failure.
  - **Fix**: Route scheduler-triggered scans through the same backend scan orchestration used by the HTTP scan endpoint, or extract a shared application-level scan runner and call that from both places. Update the test to execute the thread target synchronously and assert the actual scan side effects.

- **[backend/api/routes/downloads.py:43](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/downloads.py#L43), [backend/api/routes/results.py:125](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/results.py#L125), [backend/api/routes/plex.py:47](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/plex.py#L47), [frontend/src/lib/api/client.ts:16](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/lib/api/client.ts#L16)** - Several backend failures are returned with HTTP 200, so the frontend treats them as success.
  - **Problem**: The API client throws only on non-2xx responses. Routes that return `{"error": ...}` or `{"success": false}` with status 200 silently violate that contract, so UI flows can show success while the backend has failed. Downloads and Plex actions are especially risky because they trigger external side effects.
  - **Fix**: Raise `HTTPException` or return proper `JSONResponse` with 4xx/5xx for all failure paths and make success payloads structurally distinct from errors. Add a contract test that asserts representative failure cases return non-2xx.

- **[backend/api/routes/settings.py:38](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/settings.py#L38), [backend/api/routes/plex.py:92](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/plex.py#L92), [frontend/src/lib/stores/settings.ts:5](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/lib/stores/settings.ts#L5)** - Configuration writes are effectively schema-less.
  - **Problem**: Raw dictionaries are accepted, merged into shared config, and persisted with minimal validation. On the frontend the settings store is `Record<string, unknown>`, so TypeScript cannot protect field names or value types. That combination makes it easy to persist bad data that background threads then consume.
  - **Fix**: Introduce a typed settings schema end to end. Use Pydantic models for request validation on every config-mutating route, reject unknown keys, coerce and validate types centrally, and generate or hand-maintain matching TypeScript interfaces for the settings domain.

## MEDIUM

- **[frontend/src/lib/stores/connection.ts:48](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/lib/stores/connection.ts#L48), [frontend/src/lib/stores/connection.ts:92](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/lib/stores/connection.ts#L92), [frontend/src/lib/stores/connection.ts:117](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/lib/stores/connection.ts#L117)** - WebSocket reconnects leak Tauri listeners and reconnect even after intentional disconnect.
  - **Problem**: `listen()` handlers are installed on each reconnect without storing or unsubscribing them, and `disconnect()` closes the socket without suppressing the `onclose` reconnect path. Over time this can duplicate event handling and create confusing reconnect behavior.
  - **Fix**: Store Tauri unlisten callbacks, register them once per store lifecycle, and add an explicit manual-disconnect flag so `onclose` can distinguish intentional shutdown from network failure.

- **[frontend/src/routes/watchlist/+page.svelte:24](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/routes/watchlist/+page.svelte#L24), [frontend/src/routes/watchlist/+page.svelte:94](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/routes/watchlist/+page.svelte#L94), [frontend/src/lib/api/client.ts:148](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/lib/api/client.ts#L148)** - Server-side watchlist search bypasses the status filter.
  - **Problem**: Once a search is active, the page renders `searchResults` directly and no longer applies `statusFilter`. The UI still shows the status dropdown, so users are given a control that silently stops working.
  - **Fix**: Include status in the search API and request it from the frontend, or apply the status filter client-side to `searchResults` before rendering.

- **[frontend/src/routes/analytics/+page.svelte:44](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/routes/analytics/+page.svelte#L44), [frontend/src/routes/analytics/+page.svelte:169](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/routes/analytics/+page.svelte#L169)** - The analytics date-range selector updates the chart but not the summary values.
  - **Problem**: The label changes to `Scans (7d)` or `Scans (90d)`, but the number still comes from the original summary payload rather than the selected trend range. That is a correctness bug, not just a copy issue.
  - **Fix**: Either recompute the summary cards from the selected trend payload or fetch a matching summary endpoint for the chosen range. Avoid relabeling a 30-day total as 7-day data.

- **[backend/api/routes/downloads.py:49](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/routes/downloads.py#L49), [backend/download_service.py:95](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/download_service.py#L95)** - Download progress callbacks are stored globally on a shared service.
  - **Problem**: Each request overwrites `_progress_fn` on the singleton `DownloadService`. Concurrent batch and single download requests can steal each other's progress channel or produce cross-talk in the UI.
  - **Fix**: Make progress callbacks request-scoped. Pass the callback into the work method invocation rather than mutating shared service state, or key callbacks by job id.

- **[backend/api/ws.py:40](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/ws.py#L40)** - WebSocket broadcasts hold the manager lock while awaiting client sends.
  - **Problem**: One slow client can delay all broadcasts and block connect or disconnect cleanup because the same lock is held across awaited network I/O.
  - **Fix**: Copy the connection list under lock, release the lock, send to each client outside the critical section, then prune dead sockets in a follow-up pass.

- **[tests/test_api_routes.py:1179](/c:/Users/NLSur/OneDrive/Documents/MediaScout/tests/test_api_routes.py#L1179), [tests/test_api_analytics_watchlist.py:126](/c:/Users/NLSur/OneDrive/Documents/MediaScout/tests/test_api_analytics_watchlist.py#L126), [frontend/src/lib/api/types.ts:138](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/lib/api/types.ts#L138)** - Some tests are too shallow to catch frontend/backend contract drift.
  - **Problem**: The scheduler test never executes the worker target, and analytics trend tests use mocked payload shapes that do not match what the frontend types expect. That makes the "2568 tests passing" signal weaker than it looks for integration risk.
  - **Fix**: Add thin contract tests that serialize real route responses and compare them against the TypeScript-facing schema, plus at least one test per background path that runs the actual target function instead of only asserting thread creation.

## LOW

- **[frontend/src/lib/components/ResultTile.svelte:102](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/lib/components/ResultTile.svelte#L102), [frontend/src/lib/components/ResultTile.svelte:112](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/lib/components/ResultTile.svelte#L112)** - Scan result tiles still have avoidable accessibility and mobile usability gaps.
  - **Problem**: The selection control is a clickable `div`, not a native control, and tile actions are hover-revealed. That hurts keyboard access and makes core actions harder to discover on touch devices.
  - **Fix**: Use semantic buttons or checkboxes with visible focus treatment, and make actions persistently visible or expose them through a touch-friendly overflow menu on smaller viewports.

## ARCHITECTURE OBSERVATIONS

- **[backend/api/dependencies.py:91](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/dependencies.py#L91), [backend/api/ws.py:66](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/api/ws.py#L66)** - The singleton-heavy service registry fits a Tauri sidecar, but it makes app-instance isolation and tests fragile. Lifecycle ownership is spread across module globals instead of a single app container.
- **[frontend/src/routes/settings/+page.svelte:1](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/routes/settings/+page.svelte#L1)** - The settings page has become a large orchestration hub. It is carrying data loading, validation, tab state, focus handling, and rendering for many domains at once. Splitting it by tab or domain would reduce regression risk and make typed settings models easier to enforce.
- The v1/v2 coexistence looks workable, but shared backend services mean any stateful behavior change should be treated as cross-client API design, not just frontend work. The scheduler and config issues above are examples where one surface can accidentally bypass invariants expected by the other.

## POSITIVE PATTERNS

- **[backend/scanner_service.py](/c:/Users/NLSur/OneDrive/Documents/MediaScout/backend/scanner_service.py)** - The scanner service shows good intent around explicit scan-state locking and separation of scan modes. The recent handling to keep `Site Search` from polluting incremental history is the right kind of defensive correction.
- **[frontend/src/routes/+layout.svelte](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/routes/+layout.svelte)** - The app shell is organized cleanly, and the shortcut and log-panel work suggests good attention to operator workflows.
- **[frontend/src/routes/watchlist/+page.svelte:24](/c:/Users/NLSur/OneDrive/Documents/MediaScout/frontend/src/routes/watchlist/+page.svelte#L24)** - The search flow correctly guards against stale async responses by checking the active filter before applying results.
- Across the frontend routes, the team has added real empty, loading, and error states rather than assuming happy-path data. That polish matters, and it is noticeably improving operational UX.
