# ScanHound v2.0 Design Specification

## Overview

ScanHound v2.0 is a full frontend rewrite from PySide6/QML to Tauri v2 + Svelte 5 + TypeScript + Tailwind CSS, with a new FastAPI API layer wrapping the existing Python backend. The backend services, scrapers, matching engine, and database remain unchanged — only the transport (Qt signals → WebSocket/REST) and presentation (QML → Svelte) layers are replaced.

**Target audience:** Power users in a small community.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Frontend framework | Svelte 5 + TypeScript | Minimal boilerplate, reactive by default, smallest bundle, built-in transitions |
| Desktop shell | Tauri v2 | Native performance, small binary, built-in tray/notification support |
| Styling | Tailwind CSS | Utility-first, theme via CSS custom properties, no component library dependency |
| Backend API | FastAPI (Python) | Async, auto-docs, WebSocket support, wraps existing services directly |
| Communication | REST + WebSocket | REST for actions, single multiplexed WS for real-time streaming |
| Sidecar | PyInstaller exe | Users don't need Python installed; Tauri manages process lifecycle |
| Migration | Parallel build | New frontend imports existing backend modules; v1.x QML untouched until v2.0 ships |

## Phased Release Plan

### Phase 1 — v2.0 "Core"

The launch release. Full parity with v1.x core features in the new stack.

- FastAPI backend wrapping existing services
- Full scan workflow (deep/incremental/loaded/site search)
- Results view (grid + list) with filtering, sorting, search
- Plex connection and library matching
- Metadata enrichment (TMDB/OMDb/RT)
- Downloads: JDownloader integration, link scraping, auto-grab
- Settings management (all current config options)
- Desktop notifications (via Tauri) + Discord webhooks
- Scheduler (periodic scans)
- Tauri packaging with bundled Python sidecar
- System tray (via Tauri native tray)

### Phase 2 — v2.1 "Polish"

Analytics, history, and watchlist features.

- Analytics dashboard and library health metrics
- Scan history with trends
- Watchlist management (Trakt/Letterboxd/IMDb import)
- Improved theming and UI animations
- Enhanced system tray integration

### Phase 3 — v2.2 "Expand"

New capabilities and integrations.

- Jellyfin/Emby support (abstract media server interface)
- New source plugins
- RSS feed monitoring
- API key management UI
- Community source sharing

---

## Architecture

### Project Structure

```
ScanHound/
├── backend/                    # Python backend (existing + new API layer)
│   ├── api/                    # NEW: FastAPI application
│   │   ├── __init__.py
│   │   ├── main.py             # FastAPI app, lifespan, CORS, WebSocket
│   │   ├── routes/
│   │   │   ├── scanner.py      # POST /scan/start, /scan/stop, GET /scan/status
│   │   │   ├── results.py      # GET /results, /results/{id}, POST /results/filter
│   │   │   ├── plex.py         # POST /plex/connect, GET /plex/libraries, /plex/stats
│   │   │   ├── downloads.py    # POST /download, /download/batch, GET /download/history
│   │   │   ├── settings.py     # GET/PUT /settings, GET /settings/schema
│   │   │   ├── sources.py      # GET /sources, PUT /sources/{id}/toggle
│   │   │   └── system.py       # GET /health, /version, POST /shutdown
│   │   ├── ws.py               # WebSocket hub: scan progress, logs, notifications
│   │   └── dependencies.py     # Shared DI: service singletons, config
│   ├── sources/                # Existing source plugins (unchanged)
│   ├── app_service.py          # Existing (unchanged)
│   ├── scanner_service.py      # Existing (unchanged)
│   ├── download_service.py     # Existing (unchanged)
│   ├── matching.py             # Existing (unchanged)
│   ├── database.py             # Existing (unchanged)
│   └── requirements.txt        # Add: fastapi, uvicorn, websockets
│
├── frontend/                   # NEW: Svelte + Tauri app
│   ├── src/
│   │   ├── lib/
│   │   │   ├── api/            # Typed fetch wrappers
│   │   │   ├── stores/         # Svelte stores
│   │   │   ├── components/     # Reusable UI components
│   │   │   ├── layouts/        # Page layouts
│   │   │   └── utils/          # Formatters, helpers
│   │   ├── routes/             # SvelteKit pages
│   │   │   ├── +layout.svelte  # App shell (sidebar, notifications, WS)
│   │   │   ├── +page.svelte    # Scanner page (default)
│   │   │   ├── settings/
│   │   │   ├── history/
│   │   │   └── downloads/
│   │   └── app.css             # Tailwind base + custom theme
│   ├── src-tauri/              # Tauri Rust shell
│   │   ├── src/main.rs         # Sidecar lifecycle, tray, window management
│   │   ├── tauri.conf.json     # App config, sidecar, permissions
│   │   └── Cargo.toml
│   ├── svelte.config.js
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   └── package.json
│
├── tests/                      # Existing test suite (unchanged)
├── assets/                     # Icons, images
└── config.example.json
```

### Backend API Layer

The FastAPI layer is a thin wrapper — no business logic duplication. Route handlers call existing service methods and relay results.

#### Service Initialization

Services are initialized once at FastAPI startup via the lifespan event, following the same pattern as the current `main.py`:

1. Load `config.json`
2. Initialize `DatabaseManager`, `AppService`, `ScannerService`, `PlexService`, `DownloadService`, `AutoGrabService`
3. Yield (app runs)
4. Cleanup on shutdown

Services are injected into route handlers via FastAPI's dependency system.

#### REST Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/scan/start` | Start scan (body: `{type, sources, options}`) |
| `POST` | `/scan/stop` | Stop running scan |
| `GET` | `/scan/status` | Current scan state + progress |
| `GET` | `/results` | Paginated results (query: filter, sort, search, page) |
| `GET` | `/results/{group_key}` | Single result detail |
| `POST` | `/results/select` | Batch select/deselect |
| `POST` | `/results/export` | Export CSV |
| `POST` | `/plex/connect` | Connect to Plex server |
| `GET` | `/plex/status` | Connection status + server info |
| `GET` | `/plex/libraries` | Available libraries |
| `GET` | `/plex/stats` | Library statistics |
| `POST` | `/download` | Send single item to JDownloader |
| `POST` | `/download/batch` | Send selected items |
| `POST` | `/download/open-plex` | Open item in Plex Web |
| `GET` | `/download/history` | Download history |
| `GET` | `/settings` | Current config |
| `PUT` | `/settings` | Update config (partial merge) |
| `GET` | `/sources` | List sources + enabled state |
| `PUT` | `/sources/{id}` | Toggle/configure source |
| `GET` | `/health` | Backend health check |
| `POST` | `/shutdown` | Graceful shutdown |

#### WebSocket Channel

Single multiplexed WebSocket at `WS /ws` with typed JSON messages:

**Server → Client:**

| Type | Data | Purpose |
|---|---|---|
| `scan:progress` | `{scanned, total, phase}` | Scan progress updates |
| `scan:result` | `{<result object>}` | Live result streaming |
| `scan:complete` | `{stats, duration}` | Scan finished |
| `scan:error` | `{message, source}` | Scan error |
| `log` | `{level, message, timestamp}` | Log streaming |
| `notification` | `{title, body, priority}` | Notification trigger |
| `plex:status` | `{connected, server}` | Plex connection change |

**Client → Server:**

| Type | Data | Purpose |
|---|---|---|
| `scan:start` | `{scanType, sources}` | Start scan (alt to REST) |
| `scan:stop` | — | Stop scan (alt to REST) |
| `log:set_level` | `{level}` | Change log verbosity |

### Frontend Architecture

#### Svelte Stores

| Store | Responsibility |
|---|---|
| `connection.ts` | WebSocket lifecycle, reconnect logic, message dispatch |
| `scanner.ts` | Scan state (idle/running/stopping), progress, scan type |
| `results.ts` | Result items, active filters, sort order, selection set |
| `settings.ts` | Config mirror, dirty tracking, save/reset |
| `logs.ts` | Log buffer (capped ring buffer), level filter |
| `notifications.ts` | Toast queue, notification history |
| `plex.ts` | Connection status, library list, stats |

The `connection` store owns the WebSocket and dispatches incoming messages to other stores by `type` field. Filtering and sorting happen client-side since the dataset fits in memory.

#### Page Layout

```
┌──────────────────────────────────────────────────────┐
│  ScanHound                          [tray] [settings]│
├────────┬─────────────────────────────────────────────┤
│        │  [Deep v] [Start Scan]  [Stop]              │
│  Scan  │  ─────────────────────────────────────────  │
│        │  [All|Missing|Upgrades|Library|New] [Search] │
│  Down  │  ─────────────────────────────────────────  │
│  loads │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐       │
│        │  │poster│ │poster│ │poster│ │poster│       │
│  Hist  │  │title │ │title │ │title │ │title │       │
│  ory   │  │meta  │ │meta  │ │meta  │ │meta  │       │
│        │  └──────┘ └──────┘ └──────┘ └──────┘       │
│  ──    │  ─────────────────────────────────────────  │
│  Log   │  Missing: 12 | Upgrades: 5 | Library: 340   │
├────────┴─────────────────────────────────────────────┤
│  > Log panel (collapsible)                           │
└──────────────────────────────────────────────────────┘
```

**Key UI changes from v1:**
- Sidebar navigation (replaces tab bar)
- Collapsible log panel at bottom
- Settings as full page (not modal dialog)
- Download history as its own page

#### Component Hierarchy

```
+layout.svelte                — App shell: sidebar, toasts, WS connection
├── +page.svelte              — Scanner page (default)
│   ├── ScanControls          — Type selector, start/stop, progress bar
│   ├── FilterBar             — Status tabs, search input, view toggle
│   ├── ResultsGrid           — CSS Grid of ResultTile components
│   │   └── ResultTile        — Poster, title, metadata, status badge, actions
│   ├── ResultsList           — Table of ResultRow components
│   │   └── ResultRow         — Checkbox, title, year, res, size, rating, actions
│   ├── SelectionBar          — Floating bar when items selected
│   └── StatusBar             — Count breakdown by status
├── settings/+page.svelte     — Settings page
│   ├── SettingsNav           — Category sidebar
│   └── SettingsSection       — Dynamic form per category
├── downloads/+page.svelte    — Download history
│   └── HistoryTable          — Sortable table
└── Shared: Snackbar, Badge, Dialog, Tooltip, ThemeToggle
```

#### Styling

- **Tailwind CSS** for layout and utilities
- **CSS custom properties** for theme tokens (dark/light), toggled via `data-theme` attribute
- **Svelte transitions** for result animations (fly-in on new, fade on filter)
- No component library — custom components, distinctive look

### Tauri Shell & Sidecar Management

#### Sidecar Lifecycle

```
Startup:
1. Tauri window opens → loading screen
2. Rust spawns Python sidecar (bundled PyInstaller exe)
3. Rust polls GET /health every 500ms
4. Backend responds → frontend WS connects → main UI
5. Timeout after 15s → error screen with retry

Shutdown:
1. User closes window / quit from tray
2. Rust sends POST /shutdown
3. Backend stops scan, flushes DB, exits
4. Timeout 5s → Rust force-kills process
5. Tauri exits
```

#### System Tray (Tauri Native)

- Left-click: show/focus window
- Right-click menu: Show | Start Scan | Stop Scan | Quit
- Icon swaps between idle/scanning states
- Minimize-to-tray on window close (configurable)

Replaces `pystray` and `ui/system_tray.py`.

#### Tauri Configuration

```jsonc
{
  "productName": "ScanHound",
  "identifier": "com.scanhound.app",
  "bundle": {
    "icon": ["icons/icon.png", "icons/icon.ico"],
    "externalBin": ["binaries/scanhound-api"]
  },
  "app": {
    "withGlobalTauri": true,
    "windows": [{
      "title": "ScanHound",
      "width": 1600, "height": 950,
      "minWidth": 1000, "minHeight": 600
    }]
  }
}
```

#### Desktop Notifications

Notifications route through Tauri's native notification plugin instead of Python's `plyer`:

```
Backend triggers notification → ws: {type: "notification"} →
  Frontend → Tauri notification API (OS-native) + in-app Snackbar
```

Discord webhooks remain server-side in Python.

### Packaging & Distribution

```
Build pipeline:
1. PyInstaller bundles backend → scanhound-api.exe (~50-80MB)
2. Tauri builds Svelte + Rust shell, embeds Python exe
3. Output: single installer
   - Windows: .msi via WiX (~60-90MB total)
   - macOS: .dmg (future)
   - Linux: .AppImage or .deb (future)
```

### Development Workflow

```
Two terminals:
  Terminal 1: cd backend && uvicorn api.main:app --reload --port 9721
  Terminal 2: cd frontend && npm run tauri dev

Production build:
  1. cd backend && pyinstaller scanhound-api.spec
  2. cp dist/scanhound-api frontend/src-tauri/binaries/
  3. cd frontend && npm run tauri build
```

Port 9721 (configurable via env var).

---

## Data Flow — End-to-End Scan

```
User clicks "Start Scan"
  │
  ▼ Svelte
ScanControls → scanner.startScan("deep") → POST /scan/start
  │
  ▼ FastAPI
routes/scanner.py → scanner_service.start_scan() in background thread
  │
  ▼ WebSocket broadcasts
Phase 1: {type: "scan:progress", data: {phase: "loading_plex"}}
Phase 2: {type: "scan:progress", data: {phase: "scraping", scanned: 45, total: 200}}
Phase 3: {type: "scan:progress", data: {phase: "matching"}}
Phase 4: {type: "scan:result", data: {<result>}}  (per item, live)
Phase 5: Auto-grab → JDownloader (if configured)
Phase 6: {type: "scan:complete", data: {stats: {...}, duration: 32}}
  │
  ▼ Svelte stores
connection dispatches → scanner store, results store, notifications store
  → UI reactively updates (progress bar, result tiles animate in, status bar)
```

**Signal mapping:** `ScannerService` callbacks are identical to v1. The API layer adapts them from Qt signals to WebSocket broadcasts (~50 lines of adapter code).

---

## Migration for Existing Users

### Zero-effort migration

| Data | Location | Action |
|---|---|---|
| `config.json` | `%APPDATA%\ScanHound\` | Read directly — same format |
| `crawler.db` | `%LOCALAPPDATA%\ScanHound\` | Read directly — same schema, same `DatabaseManager` |
| `scanner.log` | App directory | Fresh start under data dir |

No migration script needed. v2.0 imports the same backend modules and reads the same files.

### Dropped from v2.0

- `ui/qml/` — replaced by Svelte frontend
- `ui/controllers/` — replaced by FastAPI routes
- `ui/models/` — replaced by Svelte stores
- `ui/system_tray.py` — replaced by Tauri native tray
- `pystray` dependency — replaced by Tauri
- `plyer` dependency — replaced by Tauri notifications

### Unchanged

All `backend/` modules except the new `api/` addition. All 30+ test files.

### Backward compatibility during development

Both UIs work simultaneously:
- `python main.py` → v1.x QML app
- `uvicorn backend.api.main:app` → v2.0 API for Svelte frontend
