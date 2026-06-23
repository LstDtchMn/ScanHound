# ScanHound on Android — Planning Document

Status: **IN PROGRESS** (only hardware-dependent steps remain — see §9/Remaining) ·
Branch: `claude/review-commit-status-vkxtvi` · Date: 2026-06-23

## Decisions made
- **Packaging:** full **APK via Tauri v2 Android** (not PWA). Backend stays in the
  Docker container; the app is a thin remote front-end.
- **Swipe deck:** right = add to selection tray, left = skip. Footer "Download N
  selected" batches to JDownloader. Dismissals are **remembered across sessions**.
- **List view** is kept; sort/filter remain available.

## Progress (done on this branch)
- ✅ Backend persistent dismissals: `dismissed_items` table + endpoints; `/results`
  hides dismissed; tests.
- ✅ Swipe deck (`SwipeDeck.svelte`): touch-draggable cards, ADD/SKIP overlays,
  undo, selection-tray footer; new `swipe` view mode; deck/list/grid all respect it.
- ✅ Remote connection: `api/endpoint.ts` (stored URL+token win over same-origin),
  WS scheme derivation, `ServerConnection` form, first-run prompt, Settings →
  Connection tab.
- ✅ Tauri Android target: `#[cfg(desktop)]`-gated sidecar/tray, `bundle.android`,
  npm `android:*` scripts, `docs/ANDROID_BUILD.md`.
- ✅ Mobile polish of the dense desktop chrome (FilterBar, secondary screens) —
  see `docs/MOBILE_UI_PLAN.md` (Phases 1–4, all implemented).
- ✅ CORS: the API allows `tauri://localhost` / `https://tauri.localhost` /
  `http://tauri.localhost` plus a localhost dev-port regex, so a bundled APK
  (not same-origin) can reach a remote backend.
- ✅ Playwright mobile harness (§5-C): `frontend/playwright.config.ts` with
  `desktop` + `mobile` (Pixel 7) projects; smoke tests for routing, bottom-nav,
  filter/scan sheets, and no-horizontal-overflow at 390px.
- ✅ Frontend cleanup: deduped `ResultActionSheet`/`ContextMenu` action handlers,
  centralized download-host options, removed the dead `Sidebar` mobile-drawer
  branch.

## Remaining
- ⏳ Build the signed APK on a machine with the Android SDK/NDK (can't run here;
  see `docs/ANDROID_BUILD.md`).
- ⏳ `cargo check` on a desktop with GTK/WebKitGTK libs to confirm the
  `#[cfg(desktop)]` gating compiles cleanly for both targets.
- ⏳ On-device smoke test of the installed APK against the real, remote backend.

## 1. Goal & constraints

- The Python backend (FastAPI + scrapers + Plex + JDownloader) **stays where it is**:
  the Docker container on the home server, reachable at `scanhound.turtleland.us`.
- Android is a **thin front-end only** — it renders the existing UI and talks to the
  remote API. No Python/scraping runs on the phone (it can't).
- The real work is **adapting the interface for a phone-sized screen** and packaging it
  as something installable on Android.

## 2. Architecture decision

**Recommendation: ship a PWA (installable web app) first, served by the existing
container.** Optionally graduate to a wrapped APK (Tauri v2 Android or Capacitor/TWA)
later if a Play Store / native shell is wanted.

Why PWA first:

- The frontend is already a static SvelteKit build (`@sveltejs/adapter-static`) served
  by the API container behind the Cloudflare tunnel. Loading it on the phone is just
  opening `scanhound.turtleland.us` in Chrome.
- Because it loads **same-origin**, the existing connection + auth model "just works":
  `resolveApiBase()` returns `''` (same origin) and the WebSocket uses
  `window.location.host` (`frontend/src/lib/api/client.ts:7`,
  `frontend/src/lib/stores/connection.ts:5`). No remote-URL config, no token entry,
  no CORS — all of which a bundled APK *would* require.
- It reuses 100% of the existing Svelte UI and stores. Effort concentrates exactly
  where the user wants it: the mobile layout.
- "Add to Home Screen" gives a full-screen, icon-on-launcher app indistinguishable
  from native for this use case.

Trade-off: a PWA is not on the Play Store and has no native push/share-target unless
added. If those matter, the **Tauri v2 Android** path (we already use Tauri v2 for
desktop) reuses the same Svelte frontend to produce a sideloadable APK — but it then
needs the connectivity rework in §4 because the bundled app is **not** same-origin.

Decision still open — see §9.

## 3. Current-state assessment (what's already there vs. gaps)

### Already mobile-aware ✅
- `+layout.svelte` has a **mobile top bar** (`flex md:hidden`) with a hamburger and a
  **slide-in drawer** `Sidebar` (`mobile` prop, `fly` transition) — desktop shows the
  fixed 16px rail. (`frontend/src/routes/+layout.svelte:88`, `Sidebar.svelte:55`)
- Scan results **grid view** uses `auto-fill, minmax(160px, 1fr)` — already reflows to
  ~2 columns on a phone. (`+page.svelte:70`)
- Scan results **table view** progressively hides columns: poster `hidden sm:`, Res
  `hidden md:`, Size `hidden lg:`. (`+page.svelte:330-334`)
- `app.html` already sets `viewport width=device-width, initial-scale=1`.
- Theme is responsive to `prefers-color-scheme` and persisted.

### Gaps that block a good phone experience ❌
1. **No PWA packaging** — no `manifest.webmanifest`, no service worker, no maskable
   icons, no `theme-color`/`apple-mobile-web-app-*` tags. Not installable today.
2. **Touch interactions missing** — results use right-click `oncontextmenu` +
   `clientX/clientY` for the context menu (`+page.svelte:229`, `:307`, `:362`). There
   is no long-press / tap-to-act path on touch; `ContextMenu` is positioned at mouse
   coords. Hover-only affordances won't surface on touch.
3. **Dense, desktop-first secondary screens** — `FilterBar`, `DetailPanel`, `settings`,
   `downloads`, `watchlist`, `ScanControls` have ~0–1 responsive utilities each (grep
   count). They are built for wide layouts and need small-screen passes (stacking,
   wrapping, bottom-sheet patterns, larger tap targets).
4. **Table view ergonomics** — even with hidden columns, a `<table>` in
   `overflow-x-auto` is awkward on a phone. Mobile should likely default to / prefer a
   **card list** instead of the table.
5. **Tap target sizing** — many controls are `text-[10px]` / `p-1.5`; below the 44px
   touch-target guideline.
6. **No safe-area handling** — needs `viewport-fit=cover` + `env(safe-area-inset-*)`
   padding for notches/gesture bars in standalone mode.
7. **Keyboard-only flows** — arrow-key nav, `?` shortcuts, `Ctrl+A/D` (`+layout` /
   `+page`) are fine to keep but must not be the *only* way to do something.
8. **No mobile test setup** — `package.json` references `test:e2e:mobile` and a
   `mobile` Playwright project, but **no `playwright.config` and no tests exist**.

## 4. Connectivity & auth (only relevant if we wrap into an APK)

For the PWA path this section is **N/A** (same-origin). For a bundled APK:

- Add a configurable **server base URL** (persisted in `localStorage`) and feed it into
  `resolveApiBase()` and `wsUrl()` instead of same-origin.
- Add a **token/login screen**: the server already supports `Bearer <nonce>` auth via
  `SCANHOUND_AUTH_NONCE` (`backend/api/main.py:251`, `dependencies.py:109`). The APK
  would prompt for the URL + token and call `setAuthNonce()`.
- The live host currently returns **403** to unauthenticated requests (likely
  Cloudflare Access in front of the tunnel) — confirm what auth layer guards the public
  hostname before designing the APK login. (For the PWA, the browser carries the
  existing session/cookie, so this is transparent.)
- CORS on the API would need to allow the app origin for a non-same-origin client.

## 5. Workstreams

### A. PWA packaging (small, enables install)
- `frontend/static/manifest.webmanifest`: name, short_name, `display: standalone`,
  `theme_color`/`background_color` matched to the dark theme, `start_url`, scope.
- Maskable + standard icons (192/512) generated from `assets/icon.svg`.
- Service worker (app-shell cache; **network-first for API**, never cache `/api`,
  `/ws`, `/results`). Use `@vite-pwa/sveltekit` or a hand-rolled SW — leaning
  hand-rolled to keep the static adapter simple and avoid caching live data.
- `app.html`: add manifest link, `theme-color`, `apple-mobile-web-app-capable`,
  `apple-mobile-web-app-status-bar-style`, and `viewport-fit=cover`.
- Verify the API container serves `/manifest.webmanifest` and the SW with correct
  MIME types and scope.

### B. Mobile UX adaptation (the bulk of the work) — per screen
- **Global**: safe-area padding; min 44px tap targets for primary actions; replace
  hover-only affordances with always-visible mobile controls; bump base font sizes on
  `< sm`.
- **Scan (`/`)**: default to **card list** on mobile; make `ScanControls` collapse into
  a compact bar + expandable sheet; `FilterBar` → horizontally scrollable chips or a
  filter bottom-sheet; pagination already OK.
- **Touch context actions**: add **long-press** (and/or a per-row "⋯" button) to open
  `ContextMenu` as a **bottom sheet** anchored to the screen, not mouse coords. Keep
  right-click for desktop.
- **DetailPanel**: full-screen sheet on mobile (slide up), large close target.
- **Downloads / Watchlist**: stack rows into cards; move row actions into the card;
  ensure batch-select works by tap.
- **Analytics**: charts/grids to single-column; already has 6 responsive utils — finish
  the pass.
- **Settings**: single-column form, section accordions, larger inputs.

### C. Testing
- Add `frontend/playwright.config.ts` with `desktop` + `mobile` (Pixel-class viewport)
  projects to match the existing npm scripts.
- Write smoke e2e: install/nav drawer, scan card list renders, long-press → sheet,
  detail sheet open/close, settings reachable — run under the `mobile` project.
- Manual device pass on a real Android phone via the live hostname.

### D. (Optional) APK shell — only if §9 chooses it
- `tauri android init` + build, OR Capacitor/TWA wrapper.
- Implement §4 connectivity + login.
- Icon/splash, signing, sideload/Play distribution.

## 6. Suggested sequencing

1. **Phase 0 (done):** fix the failing Linux path test so CI is green.
2. **Phase 1:** PWA packaging (A) — makes it installable on the phone immediately.
3. **Phase 2:** Mobile UX core (B: Scan card list, touch context sheet, DetailPanel
   sheet, nav polish) — the highest-value adaptation.
4. **Phase 3:** Secondary screens (Downloads, Watchlist, Analytics, Settings) passes.
5. **Phase 4:** Playwright mobile harness + smoke tests (C).
6. **Phase 5 (optional):** APK shell + remote-URL/login (D) if a native app is wanted.

## 7. Risks / watch-items
- **Service worker caching live data** — must exclude API/WS/results or the app shows
  stale scan results. Network-first/no-store for data routes.
- **WebSocket through the tunnel** — confirm `wss://scanhound.turtleland.us/ws` upgrades
  cleanly through Cloudflare (it should; verify under standalone PWA).
- **Auth in standalone mode** — confirm the Cloudflare/403 layer keeps the session in a
  PWA (cookies in standalone WebViews can differ). Test early.
- **Touch + desktop parity** — keep desktop keyboard/right-click flows intact while
  adding touch; don't regress the existing desktop/Tauri app.

## 8. Out of scope
- Running any scraping/Plex logic on-device.
- Rewriting the UI in a native toolkit (Kotlin/Compose) — rejected as a second codebase.
- Offline scanning (backend is required and remote).

## 9. Decisions made (resolved during implementation — see §0 / `docs/HANDOFF.md` §2)
1. **Packaging target:** full APK via Tauri v2 Android (not PWA) — done.
2. **If APK:** the app prompts for a server URL + token (`SCANHOUND_AUTH_NONCE`)
   on first launch / Settings → Connection; whatever sits in front of the public
   hostname (e.g. Cloudflare Access) is the user's deployment concern, called out
   in `docs/ANDROID_BUILD.md`.
3. **Mobile default view:** swipe deck (not card list or table) — phones default
   to `SwipeDeck.svelte`; list/grid stay available and keep the saved desktop
   preference.
4. **Scope of first cut:** all five screens adapted (`docs/MOBILE_UI_PLAN.md`
   Phases 1–4).
