# Mobile Downloads Section — Design

**Status:** Approved (design phase). v1 scope.
**Date:** 2026-07-09

## Goal

Add a Downloads section to the ScanHound mobile app that shows current downloads
with live progress, offers basic JDownloader controls, and helps the user spot
and remove duplicate downloads — reachable from a new mobile bottom tab bar that
is designed to also host a future Renames tab.

## Architecture

The backend already exposes everything the view needs to *read*: `GET
/downloads/results` returns per-package rows polled from JDownloader
(`name, title, host, bytes_total, bytes_loaded, downloaded, extraction, state,
error, updated_at`), a background poller keeps them fresh, and `POST
/downloads/jd-control` drives global start/stop/pause/resume. The desktop
`/downloads` page already renders this. This feature is therefore mostly a
**mobile UI** addition plus **one new backend capability**: removing a single
JDownloader package (to act on a flagged duplicate).

Navigation is **route-based**: a fixed bottom tab bar (mobile only) navigates
between existing SvelteKit routes. Each route renders a mobile-optimized view
when `isPhone`; the desktop pages are untouched.

## Tech Stack

SvelteKit 5 (runes), FastAPI, myjdapi (JDownloader), existing WebSocket for
scan/download events. Deploy via `docker compose up -d --build` only.

## Global Constraints

- Deploy in-app changes ONLY via `docker compose up -d --build`.
- The desktop `/downloads` page and desktop navigation must be unchanged.
- v1 uses **polling** of `GET /downloads/results` (no new WebSocket channel).
- Works in both the responsive web app and the Tauri/Android wrapper.
- Every new backend endpoint field must be present in its request/response model
  (avoid the `extra="forbid"` 422 class of bug).
- Tests accompany each unit; deploy only after the changed-module suite is green.

---

## Components

### 1. `MobileTabBar.svelte` (new)

- Rendered by `+layout.svelte` inside the existing `md:hidden` region as a
  **fixed bottom bar**, respecting `env(safe-area-inset-bottom)`. Hidden on
  desktop (`md:` breakpoint) and on the `/login` route (bare chrome).
- Tabs (icon + label), highlighting the active one from `$page.url.pathname`:
  - **Scan** → `/`
  - **Downloads** → `/downloads`
  - **Renames** → `/renames`, rendered **disabled** with a "Soon" affordance
    (placeholder only; not navigable in v1).
- Main content gets bottom padding equal to the bar height so the last row isn't
  obscured.

**Depends on:** `$page` (route), `isPhone`/CSS breakpoint. **Interface:** no
props; reads the current route, calls `goto()`.

### 2. `MobileDownloadsView.svelte` (new)

The `/downloads` route renders this when `isPhone` (else the existing desktop
page). Responsibilities:

- **Fetch + poll:** load `GET /downloads/results` on mount and every ~2.5s while
  the document is visible (`visibilitychange` pauses polling when hidden; resume
  on focus). Errors trigger a toast and a short backoff; the last good list stays
  on screen.
- **Summary header:** "N downloading · M queued", plus global controls
  **Pause / Resume / Stop** (`POST /downloads/jd-control`) and **Clear finished**.
- **List:** one card per download (or per duplicate group, see §3):
  - title · host · **progress bar** (`pct = bytes_total>0 ? round(bytes_loaded/bytes_total*100) : 0`)
  - `12.3 / 40 GB` (loaded/total), **state chip** (Queued / Downloading /
    Extracting / Finished / Failed), and error text when `state==failed`.
  - Text-forward; no poster art in v1 (`download_results` carries none).
- **Empty state:** "No active downloads."

**Depends on:** `api.downloadResults()`, `api.jdControl()`, the new
`api.removeDownloadResult()` (§4), `addToast`. **Interface:** no props.

### 3. Duplicate handling (v1: #1 same-title, #2 exact package)

Client-side, computed from the polled `download_results` list:

- **Normalize** each row's `title` (reuse the app's title-normalization notion:
  lowercase, strip year/punctuation) to a group key.
- **Exact-same-package (#2):** two rows with the identical `name` → the extra is
  flagged **"duplicate"** inline (an accidental double-grab).
- **Same-title, different releases (#1):** a group with >1 distinct release is
  rendered as a **collapsible group** with a **"duplicates" badge** and a count.
- **Resolution actions:**
  - Per-item **Cancel** → removes that download (see §4).
  - Group-level **"Keep best, cancel rest"** → keeps the highest-quality release
    (rank by resolution then size, matching the existing `_res_rank` ordering)
    and cancels the others in one tap (with a confirm, since it removes from JD).
- A single (non-duplicate) download renders as a plain card.

Selection of "best" is display-only heuristic; the user can always override by
cancelling specific items instead.

### 4. Backend: remove a single JDownloader package (new)

- **`DownloadService.remove_package(identifier)`** — resolve the package's
  `packageUUID`(s) from the linkgrabber + downloads lists (the code already reads
  these with `packageUUID`), call JDownloader `removeLinks` for those UUIDs, then
  `db.delete_download_result(name)`. **Idempotent:** removing a package that is
  already gone from JD and/or the table returns success.
- **`DatabaseManager.delete_download_result(name)`** — delete the row(s) by
  package `name`.
- **`POST /downloads/results/remove`** — body `{ name: str }` (package name as
  the stable identifier shown in the UI); returns `{ ok: bool }`. Add
  `removeDownloadResult(name)` to the frontend API client.
- **Clear finished** reuses this per-item removal over the finished rows, OR a
  `DELETE /downloads/results?state=finished` variant — implementation detail for
  the plan; behavior: only finished/failed rows are cleared, active ones remain.

Global start/stop/pause/resume already exist (`jd_control`); no change there.

---

## Data Flow

1. Background poller (unchanged) polls JD → upserts `download_results`.
2. `MobileDownloadsView` polls `GET /downloads/results` while visible → renders.
3. User taps a global control → `POST /downloads/jd-control` → next poll reflects
   the new state.
4. User cancels a duplicate → `POST /downloads/results/remove {name}` →
   `remove_package` removes from JD + deletes the row → next poll shows it gone.

## Error Handling

- Any API failure surfaces a toast; the poll loop backs off (e.g., 2.5s → 5s) on
  consecutive errors and recovers on the next success.
- `remove_package` is idempotent, so a double-tap or a race with the poller can't
  error the user.
- Empty/failed JD connection shows the existing JD-status messaging (reused from
  the desktop page's status source) rather than a blank list.

## Testing

- **Backend:** `remove_package` calls JD `removeLinks` with the resolved UUID(s)
  and deletes the result row; idempotent when the package/row is already absent;
  `POST /downloads/results/remove` happy path + missing-name. Reuse the existing
  download-service test patterns (mocked JD device).
- **Frontend:** duplicate grouping (same-title grouping; exact-`name` duplicate
  detection; "keep best" picks highest res/size), progress-% computation
  (including `bytes_total==0`), and poll lifecycle (starts on mount, pauses on
  `visibilitychange`, backs off on error). Follow the existing store/component
  test patterns (vitest).

## Out of Scope (deferred)

- **v2:** Re-grab vs Plex (#3) — flag a download whose title+quality is already in
  the library (reuses the scan/Plex matcher).
- **Renames tab (future):** enable the placeholder route's mobile view, and
  on-disk finished-duplicate reconciliation (#4), which belongs to the
  library/rename stage.
- **Optional enhancement:** have the poller `broadcast` a `download:results`
  WebSocket event so the view can update by push instead of polling.
