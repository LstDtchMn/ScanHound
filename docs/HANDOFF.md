# Session Handoff — ScanHound Android front-end + mobile UI

For a fresh Claude session continuing this work. Read this top to bottom before
touching code. Everything described is already committed and pushed.

## 0. Ground rules (from the task setup)
- **Develop only on branch `claude/review-commit-status-vkxtvi`.** Create it
  locally from origin if needed. Never push to `main`.
- Push with `git push -u origin claude/review-commit-status-vkxtvi`; retry on
  network errors with backoff. **Do not open a PR** unless the user asks.
- Repo: `LstDtchMn/ScanHound`. GitHub ops go through the GitHub MCP tools
  (`mcp__github__*`, load via ToolSearch) — no `gh` CLI here.
- The environment is an ephemeral cloud container; nothing runs on the user's PC.
  Commit/push anything worth keeping.

## 1. What this project is
ScanHound compares a Plex library against scraped release sites to find missing
content / upgrades and send downloads to JDownloader. Stack:
- **Backend**: Python FastAPI (`backend/api/`) + services (`backend/`), SQLite
  (`backend/database.py`). Runs in the user's Docker container.
- **Frontend**: SvelteKit 5 (runes) + Tailwind v4, static-adapter build
  (`frontend/`), packaged with **Tauri v2** for desktop and now Android.
- Legacy PySide6/QML desktop UI in `ui/` (not relevant to this work).

## 2. The task being worked
Build an **Android front-end** for the app. User decisions, in order:
1. **Full APK** via Tauri v2 Android (not a PWA). Backend stays remote in Docker;
   the app is a thin client.
2. **Swipe deck** (Tinder-style) for triaging results: **swipe right = add to a
   selection tray; swipe left = skip**. Footer "Download N selected" batches to
   JDownloader. **Left-swipes are remembered across sessions** (server-persisted).
3. Keep list/grid views; sort/filter stay available.
4. **Mobile UI polish**: bottom tab bar; phones default to the swipe deck; all
   screens adapted; touch context actions included.

## 3. What's DONE (commit by commit on the branch)
```
fix(test):   Linux app-dir case-insensitive path test
feat(api):   persistent swipe-to-dismiss store (dismissed_items table + endpoints)
feat(ui):    Tinder swipe deck (SwipeDeck.svelte) + results-store wiring
feat(android): Tauri Android target + remote server connection (URL+token)
docs:        ANDROID_PLAN / ANDROID_BUILD
feat(mobile) x3: bottom nav, filter/scan bottom sheets, responsive secondary
             screens, touch action sheet, StatusBar hidden on mobile
docs:        MOBILE_UI_PLAN, CODE_REVIEW_PLAN
fix(ui):     swipe deck correctness (from code review — see §6)
```

### Key implementation facts
- **Dismissals**: `dismissed_items` SQLite table keyed by **release URL**. Methods
  in `backend/database.py` (mirror `scanned_urls`). Endpoints in
  `backend/api/routes/results.py`: `POST /results/dismiss` (`dismissed` true/false
  to add/remove), `GET /results/dismissed`, `DELETE /results/dismissed`.
  `GET /results` hides dismissed by default (`include_dismissed=true` to override);
  `stats` now derives from the non-dismissed visible set.
- **Frontend dismissals**: `dismissedUrls` store in
  `frontend/src/lib/stores/results.ts`, hydrated via `hydrateDismissed()` on the
  scan page mount, folded into `filteredResults`. `deckResults` = filtered ∧
  actionable (missing/upgrade) ∧ not-selected ∧ not-dismissed. Helpers
  `dismissItem`/`restoreItem` (optimistic + POST).
- **Swipe deck**: `frontend/src/lib/components/SwipeDeck.svelte`. Right→
  `toggleSelect` (leaves deck), left→`dismissItem`. Download footer calls
  `downloadBatch` then `markDownloaded` + `deselectAll`.
- **Connectivity** (so the APK can reach a remote server): `frontend/src/lib/api/
  endpoint.ts` resolves HTTP/WS base — a stored server URL wins, else same-origin
  (prod) / dev ports. `client.ts` resolves `apiBase()` per request and seeds the
  token from storage; `connection.ts` derives the WS URL at connect time.
  `stores/server.ts` + `components/ServerConnection.svelte` manage URL+token
  (tested against `/health` before saving). First-run prompt in `+layout.svelte`;
  Settings → Connection tab.
- **Tauri Android**: `src-tauri/src/lib.rs` gates the Python sidecar + tray behind
  `#[cfg(desktop)]`; mobile just loads the webview. `tauri.conf.json` has
  `bundle.android.minSdkVersion 24`. npm `android:init/dev/build` scripts.
- **Mobile UI**: `BottomSheet.svelte`, `MobileTabBar.svelte`, `ResultActionSheet
  .svelte`, `stores/media.ts`, `stores/theme.ts`, `lib/icons.ts`. FilterBar &
  ScanControls keep the desktop row (`hidden md:flex`) and add a compact mobile
  bar + bottom sheet (`flex md:hidden`). Phones default to swipe via
  `viewModeExplicit`. StatusBar hidden on mobile.
- **Convention used everywhere**: additive behind the Tailwind `md` breakpoint
  (`hidden md:flex` / `flex md:hidden`) so **desktop is unchanged**. Preserve this.

## 4. How to build / test / run IN THIS CONTAINER
Python deps (note the quirks):
```bash
pip install "setuptools<81"                      # distutils shim
grep -v undetected-chromedriver requirements.txt > /tmp/r.txt && pip install -r /tmp/r.txt
pip install httpx                                 # FastAPI TestClient needs it
python -m pytest -q                               # expect ~2657 passed, 2 skipped
```
`undetected-chromedriver` won't build (no distutils/headless) — it's lazy-loaded,
tests don't need it. Don't fight it.

Frontend:
```bash
cd frontend && npm install        # node 22 present; revert package-lock.json churn after
npm run check                     # svelte-check — must be 0 errors/0 warnings
npm run build                     # static build to frontend/build
```

Run the API live (for endpoint smoke tests):
```bash
python -m backend.api --no-auth --port 9721 &    # dev mode, no token
# or with auth:
SCANHOUND_AUTH_NONCE=secret python -m backend.api --port 9722 &
curl -s localhost:9721/health
```
Already smoke-tested live: dismissals CRUD works; `/health` is open while
`/results/dismiss[ed]` require `Authorization: Bearer <nonce>` (401 without).

**Cannot do here**: render the frontend (no browser/device), `cargo check`
(missing GTK libs — gdk-3.0), or build the APK (no Android SDK/NDK/`ANDROID_HOME`).

## 5. Verification status
- ✅ Python suite green; backend dismissals verified live (CRUD + auth).
- ✅ `npm run check` + `npm run build` green.
- ❌ Frontend never rendered on a device — mobile layout/gestures unverified.
- ❌ Rust never compiled (`#[cfg(desktop)]` gating unconfirmed by compiler).
- ❌ APK never built.

## 6. Code review already done
Ran `/code-review` (high effort). Three SwipeDeck correctness bugs were **found
and FIXED** (commit `fix(ui): swipe deck correctness`):
1. downloaded items reappearing after `deselectAll` → now `markDownloaded()`.
2. double-tap double-commit → `commit()` guards on `animating`, buttons disabled.
3. fly-off `setTimeout` not cleared → tracked in `actionTimer`, cleared on next
   action + `onDestroy`.

Two **cleanup findings left UNADDRESSED** (intentionally):
- `ResultActionSheet.svelte` duplicates `ContextMenu.svelte`'s action handlers →
  extract a shared module both consume.
- Download-host options (Rapidgator/Nitroflare/1Fichier) hardcoded in 4 places
  (ResultTile, ResultRow, FilterBar desktop+sheet) → centralize in a constant.

## 7. Suggested next steps (pick with the user)
1. **Cleanup findings** above (quick, safe).
2. **CORS**: a bundled APK is not same-origin; the FastAPI app needs to allow the
   app origin. Not done. Check `backend/api/main.py` middleware. Likely needed for
   the APK to work against the remote server.
3. **Playwright mobile harness**: repo references `test:e2e:mobile` but there's no
   `playwright.config.ts` and no tests. Can run headless here IF `npx playwright
   install chromium` is allowed by egress. Add desktop+mobile(Pixel) projects +
   smoke tests (nav, sheets open, no horizontal overflow at 390px).
4. **On-device / APK build** — requires the user's machine (`docs/ANDROID_BUILD.md`).
5. Frontend unit tests for the dismiss store / endpoint resolution / swipe logic
   (none exist).
6. Possible future: per-user dismissals/selection (currently global server state).

## 8. Gotchas
- `npm install` rewrites `frontend/package-lock.json` (drops a stale
  `adapter-auto` entry). Revert it before committing unless intentional:
  `git checkout frontend/package-lock.json`.
- `cargo check` rewrote `Cargo.lock` once (added `tokio` to the package deps —
  a correct sync, already committed).
- **Test DB path on Linux**: `backend/config.py` data dir is
  `~/.local/share/scanhound` (lowercase). conftest only isolates the Windows
  `APPDATA`/`LOCALAPPDATA`, so on Linux tests touch the real (container) home —
  fine in this ephemeral box. The `_reset_dismissed` autouse fixture in
  `tests/test_api_routes.py` clears the shared dismissals table between tests.
- Svelte 5 runes throughout (`$state`/`$derived`/`$props`/snippets). Match it.
- SSR safety: the static adapter prerenders — guard every
  `window`/`document`/`localStorage`/`matchMedia` access (`typeof … !== 'undefined'`).

## 9. Doc index (all on the branch)
- `docs/ANDROID_PLAN.md` — overall plan + decisions + progress.
- `docs/ANDROID_BUILD.md` — how to build/sign/run the APK; first-launch server flow.
- `docs/MOBILE_UI_PLAN.md` — the mobile UI plan (implemented Phases 1–4).
- `docs/CODE_REVIEW_PLAN.md` — the by-area review checklist.
- `docs/HANDOFF.md` — this file.
