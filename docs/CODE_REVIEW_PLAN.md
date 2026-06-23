# Code Review Plan — `claude/review-commit-status-vkxtvi`

Scope: the 9 commits on this branch vs `main` (~1,900 LOC across 37 files):
swipe-to-dismiss backend, swipe deck UI, remote-server connection + Tauri Android
target, and the mobile UI pass (Phases 1–4).

## How to review
```bash
git log --oneline main..HEAD            # commit-by-commit
git diff main...HEAD                     # whole-branch diff
git diff main...HEAD -- <path>           # per-file
cd frontend && npm run check && npm run build   # type + build gate
python -m pytest -q                      # backend suite (expect green)
```
Review **commit-by-commit** (they're scoped and ordered) for narrative, then do a
**second pass by concern** (security, desktop-regression, a11y) across the diff.

Two things CI here could NOT confirm — verify deliberately:
1. **Rust** (`src-tauri/src/lib.rs`) never compiled (no GTK libs in the sandbox).
   Run `cargo check` on a desktop and, ideally, `tauri android build` once.
2. **Mobile layout** was type/build-checked only, never rendered on a device.

---

## Area 1 — Backend: persistent dismissals
Files: `backend/database.py`, `backend/api/routes/results.py`,
`tests/test_database.py`, `tests/test_api_routes.py`.

- [ ] **Schema**: `dismissed_items` created idempotently (`CREATE TABLE IF NOT
  EXISTS`); no `SCHEMA_VERSION` bump needed since it's additive — confirm that
  matches the table's migration conventions.
- [ ] **DB methods** mirror `scanned_urls` (params, not string interpolation →
  no SQLi; `_mutate`/`_query` locking). Check `INSERT OR IGNORE` idempotency and
  `get_dismissed_urls()` returning a set.
- [ ] **`GET /results` filtering**: dismissed removed only when
  `reg.db is not None` and the set is non-empty; `include_dismissed=true` bypass.
- [ ] **Stats semantics changed**: `stats` now derives from `visible_items`
  (post-dismissal, pre-filter) instead of all raw items. Confirm no other
  consumer relied on dismissed items being counted.
- [ ] **Identity = release URL**: is URL the right dedup key? Items with empty/
  missing `url` are never dismissed (intended). A re-scrape with a new URL for the
  same title reappears (intended per design) — confirm that's desired.
- [ ] **Auth**: new endpoints sit behind the global auth middleware (only
  `/health` is exempt) — verify `POST/GET/DELETE /results/dismiss[ed]` require the
  token when one is set.
- [ ] **Test isolation**: the autouse `_reset_dismissed` fixture clears the shared
  DB between tests — confirm it can't bleed into non-API tests.

## Area 2 — Frontend connectivity + auth
Files: `lib/api/endpoint.ts`, `lib/api/client.ts`, `lib/stores/connection.ts`,
`lib/stores/server.ts`, `lib/components/ServerConnection.svelte`,
`routes/+layout.svelte`, `routes/settings/+page.svelte`.

- [ ] **SECURITY — token at rest**: auth token + server URL live in
  `localStorage` (XSS-readable). Acceptable for this app? Note WS auth passes the
  token as a `?token=` query param (can land in proxy/access logs) — pre-existing
  pattern, but call it out.
- [ ] **`apiBase()`/`wsBase()`**: stored URL wins → same-origin → dev ports.
  `wsBase` does `replace(/^http/i, 'ws')` (https→wss correct). Trailing slashes
  stripped. SSR-safe (`typeof window/localStorage` guards).
- [ ] **Token seeding**: `client.ts` seeds `authNonce` from storage at module
  load; `setAuthNonce` (Tauri sidecar) still overrides on desktop. Confirm no
  ordering bug where a stale token wins.
- [ ] **Reconnect on change**: `saveServerConfig` disconnects + reconnects the WS.
  `testServerConnection` validates `/health` before persisting (good). Check the
  10s/15s timeouts and the 401 message path.
- [ ] **First-run prompt** (`maybePromptServer`): only when in Tauri + no remote
  configured + health fails after a 3s grace. Confirm desktop (sidecar answers
  health) never shows it, and it's dismissible.
- [ ] **CORS**: a non-same-origin client (APK) needs the API to allow its origin —
  not handled in this branch; verify whether that's needed for your deployment.

## Area 3 — Tauri Android / Rust
Files: `src-tauri/src/lib.rs`, `tauri.conf.json`, `Cargo.lock`, `package.json`,
`docs/ANDROID_BUILD.md`.

- [ ] **`#[cfg(desktop)]` gating**: sidecar, tray, `tauri-plugin-shell/process`,
  `get_auth_nonce`, and `on_window_event` are all desktop-only; the mobile path
  just runs the webview. **Compile both targets** to confirm.
- [ ] **Graceful mobile auth**: frontend `invoke('get_auth_nonce')` has no handler
  on mobile → confirm the `try/catch` in `+layout` swallows it.
- [ ] **Config**: `bundle.android.minSdkVersion 24`, identifier
  `com.scanhound.app`; `android:*` npm scripts. `Cargo.lock` only added `tokio`
  to the package's dep list (sync, not a version bump).

## Area 4 — Swipe deck + results store
Files: `lib/components/SwipeDeck.svelte`, `lib/stores/results.ts`,
`lib/api/client.ts` (dismiss methods), `routes/+page.svelte`.

- [ ] **Deck membership**: `deckResults` = filtered ∧ actionable ∧ not-selected ∧
  not-dismissed. Right-swipe adds to `selectedKeys` (leaves deck); left-swipe
  dismisses (leaves via `filteredResults`). Confirm no card resurfaces.
- [ ] **Optimistic dismiss**: `dismissItem`/`restoreItem` update the set then POST,
  reverting on failure. Check the revert path and that undo (`restoreItem`/
  `toggleSelect`) truly reverses each action type.
- [ ] **Gesture/animation**: pointer capture, `animating` guard, tap-vs-drag
  threshold (`TAP_SLOP`), `window.innerWidth` SSR guard. **Risk**: `commit()` uses
  `setTimeout(220)` then mutates stores — if the deck changes underneath during
  that window, does it act on the wrong/stale `top`? Timeouts aren't cleared on
  unmount (minor leak) — worth a look.
- [ ] **Batch download footer**: pulls selected from `results` (not deck), maps to
  `downloadBatch`, clears selection. Same host source as FilterBar.
- [ ] **`viewModeExplicit`**: phones default to swipe unless user chose a view;
  confirm desktop saved preference is untouched and the `setViewMode` wrapper is
  used at every switch point (FilterBar buttons + layout `g`/`l` shortcuts).

## Area 5 — Mobile UI pass
Files: `BottomSheet`, `MobileTabBar`, `ResultActionSheet`, `icons.ts`,
`stores/media.ts`, `stores/theme.ts`, `Sidebar`, `FilterBar`, `ScanControls`,
`StatusBar`, `ResultTile`, `+layout`, `+page`, secondary `+page`s, `app.html`.

- [ ] **Desktop regression (highest priority)**: every change is meant to be
  additive behind `md:` (`hidden md:flex` / `flex md:hidden`). Scan the diff for
  any unguarded change to shared markup. Desktop should look identical.
- [ ] **Theme refactor**: logic moved from `Sidebar` into `stores/theme.ts`; init
  now in `+layout.onMount` via `initTheme()`. Check for FOUC on load and that the
  mobile top-bar toggle + desktop sidebar toggle stay in sync.
- [x] **Dead code**: `Sidebar.svelte`'s unused `mobile` drawer branch removed —
  `MobileTabBar` is the only mobile nav now.
- [ ] **BottomSheet a11y**: backdrop click + Esc close; drag handle. No real focus
  trap — acceptable? Verify `svelte-ignore` comments are justified.
- [ ] **FilterBar/ScanControls**: the mobile sheets reuse the *same* handlers/state
  as desktop (no logic duplication) — verify. Active-filter badge count correct.
- [x] **Touch actions**: `ResultActionSheet` and `ContextMenu` now share a single
  action-handler module (deduped). Long-press→`contextmenu`→sheet on mobile; `⋯`
  on tiles; desktop right-click unchanged.
- [ ] **Stacked chrome**: on the mobile swipe view, confirm the deck still gets
  usable height (StatusBar hidden on mobile; tab bar + scan/filter bars remain).

## Cross-cutting checklist
- [ ] **Security**: token storage/transport (Area 2); no secrets logged; new
  endpoints authed.
- [ ] **SSR safety**: every `window`/`document`/`localStorage`/`matchMedia` access
  guarded (static adapter prerenders).
- [x] **Duplication**: download-host options (`Rapidgator/Nitroflare/1Fichier`)
  centralized in a shared constant, no longer hardcoded per-component.
- [ ] **a11y**: tab bar `aria-current`, sheet labels, tap-target sizes, the
  several `svelte-ignore` suppressions.
- [x] **Tests**: backend covered; frontend now has vitest unit tests (dismiss
  store / `deckResults` derivation, `endpoint.ts` base-URL resolution) and a
  Playwright e2e harness (desktop + mobile), both wired into CI.
- [ ] **Docs accuracy**: `ANDROID_PLAN`, `ANDROID_BUILD`, `MOBILE_UI_PLAN` match
  what shipped.

## Known gaps already flagged
- Rust not compiled here; mobile not device-tested — still true, needs the
  user's machine.
- ~~No CORS work for a non-same-origin APK client.~~ Fixed: the API allows
  `tauri://localhost` / `https://tauri.localhost` / `http://tauri.localhost`
  plus a localhost dev-port regex.
- ~~Playwright mobile harness deferred.~~ Fixed: `frontend/playwright.config.ts`
  with desktop + mobile (Pixel 7) projects, run green in-sandbox once (18/18).
- ~~`setTimeout`-based swipe commit + uncleared timers (Area 4).~~ Fixed in the
  `fix(ui): swipe deck correctness` commit — tracked in `actionTimer`, cleared
  on next action + `onDestroy`.

## Sign-off
- [ ] `npm run check` + `npm run build` green · [ ] `pytest` green ·
  [ ] `cargo check` (desktop) green · [ ] one APK build · [ ] on-device smoke ·
  [ ] desktop visually unchanged.
