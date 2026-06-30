# Feature Kickoff: Fix & Enhance ScanHound's Scan-Results Tile/Grid View

## Goal

ScanHound's grid (tile) view of scan results is broken: instead of a responsive multi-column wall of compact poster cards, it collapses to a single oversized tile that fills the viewport width, with one giant poster. This brief seeds a brainstorming + design session to (1) **fix** the grid so it renders as a proper responsive poster grid, and (2) **enhance** it with a configurability layer (tile size, poster aspect, poster-only vs. poster+meta, badges/stats, density, grouping) that slots into ScanHound's existing dual-config model (frontend-persisted localStorage prefs vs. server-side `AppConfig`). The fix is well-understood and should be locked down; the enhancement design is deliberately left open for the session. You are an engineer/AI with no prior context — everything you need is below.

## Background: where the grid lives

- View-mode state (`'grid' | 'list' | 'swipe'`) is a localStorage-backed store: `frontend/src/lib/stores/results.ts:47-54` (`viewMode`, `viewModeExplicit`, `setViewMode`). Phones default to `swipe` unless the user explicitly picks a view.
- The page renders the three views from `frontend/src/routes/+page.svelte` (~lines 449-656): `swipe` → `SwipeDeck`; `grid` → the tile grid; `:else` → the list/table.
- The grid template is computed at `+page.svelte:100-105`:
  ```ts
  let tileColumns = $derived(($settings.tile_columns as number) || 0);
  let gridStyle = $derived(
    tileColumns > 0
      ? `grid-template-columns: repeat(${tileColumns}, 1fr)`
      : 'grid-template-columns: repeat(auto-fill, minmax(160px, 1fr))'
  );
  ```
  This logic is **correct** — `tile_columns` is a server config (`AppConfig`), default `5` (`backend/config.py:376`), and `0` falls back to responsive `auto-fill`.
- Each result card is `frontend/src/lib/components/ResultTile.svelte`. The poster is an `aspect-[2/3]` container (line 135) with a `w-full h-full object-cover` `<img>` (lines 137-144).

## BUG: single oversized tile instead of a grid

**Verified root cause (authoritative — this supersedes the earlier "tile lacks `max-width`" diagnosis):** the grid's *direct children* are unclassed wrapper `<div>`s that cannot shrink below their content, so they never respect their grid cell, and the unconstrained `ResultTile` inside expands to fill the row — forcing the `auto-fill`/`repeat(N,1fr)` track resolution down to a single column.

**Critical-path culprit — wrapper divs have no classes (not even `min-w-0`):**
- `+page.svelte:529-532` (flat tiles):
  ```svelte
  <div oncontextmenu={(e) => handleContextMenu(e, item)}>
    <ResultTile {item} ... />
  </div>
  ```
- `+page.svelte:520-523` (tiles inside an **expanded** duplicate group's nested grid) — same unclassed wrapper.

A grid item with default `min-width: auto` refuses to shrink below its content's intrinsic size. Because the wrapper has no `min-w-0`, the grid cell can't constrain it; the cell grows to the tile's content width, and `repeat(auto-fill, minmax(160px, 1fr))` resolves to one fat column.

**Contributing factor — the tile itself has no width bound:**
- `ResultTile.svelte:125-129` — the root `<div>` (`bg-[var(--bg-secondary)] rounded-lg overflow-hidden border ...`) has **no** `max-w-*`/`w-*` class. With `overflow-hidden` and the internal `aspect-[2/3]` poster (line 135) acting as the sizing anchor, the tile fills whatever horizontal space the unconstrained wrapper hands it, and the `w-full h-full` poster (line 140) scales up with it — producing the "one giant poster" symptom.

**In short:** the wrapper's missing `min-w-0` is the structural blocker (the grid can't squeeze its children); the tile's missing width bound is what lets the resulting cell balloon. Both expanded-group and flat paths share the defect, so any fix must cover *both* `+page.svelte:520-523` and `:529-532`, plus the tile root at `ResultTile.svelte:125-129`. (Hardcoded `gap-4` at `+page.svelte:519` is correct and only marginally affects column count.)

## Desired correct behavior

The grid should render as a **responsive wall of compact poster cards**, conceptually:

- A CSS grid that fits as many columns as the container allows (the existing `repeat(auto-fill, minmax(<min>, 1fr))` pattern is the right shape), honoring the server `tile_columns` override when set.
- Each **tile width is genuinely constrained** to a card-sized box — the wrapper shrinks to its cell (`min-w-0` on the grid child) and the tile respects an upper bound — so many cards tile across the row at typical desktop widths.
- A **fixed poster aspect ratio** (today `2/3`) per card, with the poster filling the card's top region via `object-cover`, never driving the card to viewport width.
- Below the poster, a **tidy, bounded meta line** (title + year, then a compact stats/badges row) that truncates cleanly rather than expanding the card.
- **Grouped (duplicate) releases** still render correctly: a collapsed group header summary, and on expand a nested grid of the same compact cards (not full-width rows).

Leave the exact spacing, card chrome, and badge styling to the brainstorming session — lock down only: wrappers shrink to their cells, tiles are width-bounded, posters keep a fixed aspect, and the grid is multi-column at desktop widths.

## Configurability

ScanHound already has two config planes; new options should be placed deliberately into one of them.

**Plane A — Frontend persisted pref (localStorage, device-specific, instant).** Pattern: the `persisted<T>(key, fallback)` helper in `results.ts:20-76` (existing prefs: `viewMode`, `sortBy`, `density`, `quickFilters`, `categoryFilter`). New stores go in `results.ts`; derived style/class maps and their application go in `+page.svelte` (grid render block ~449-534) and/or `ResultTile.svelte`.

**Plane B — Server config (user-wide, synced across devices).** 4-touchpoint pattern:
1. `backend/config.py` → `AppConfig` TypedDict (`tile_columns` at line ~150; display toggles `show_rating/show_votes/show_rt/show_rg/show_nf/show_links/show_genres` at ~42-49).
2. `backend/config.py` → `_DEFAULT_CONFIG` (`tile_columns: 5` at line 376; toggle defaults ~302-307).
3. `backend/api/routes/settings.py` → `SettingsUpdate` Pydantic model (`extra="forbid"`, so unknown keys 422; add `Optional[...] = None` field).
4. `frontend/src/routes/settings/+page.svelte` → General tab UI (Grid Layout card ~307-316, Display Columns checkboxes ~319-375). Frontend store at `stores/settings.ts` sends **diffs only**.

Proposed options (final placement is a design decision — these are the recommended defaults):

| Option | Suggested plane | Cited integration point |
|---|---|---|
| **Tile size / min-width (S/M/L)** | Frontend pref (new) | `+page.svelte:104` `minmax(160px, 1fr)` → derived from a `tileSize` store |
| **Column count** (already exists) | Server config (keep) | `tile_columns` — `config.py:150/376`, `settings.py`, settings UI `~307-316` |
| **Poster aspect ratio** (2:3 / 16:9 / square) | Frontend pref (new) | `ResultTile.svelte:135` `aspect-[2/3]` → derived aspect class |
| **Poster-only vs poster+meta** | Frontend pref (new) | `ResultTile.svelte:191-299` info `<div>` gated by a `showPosterOnly` store |
| **Density / compact** (extend to grid) | Frontend pref (extend) | `density` store `results.ts:64`; today only `ResultRow.svelte:19-22` (list) consumes it — extend to tile padding/poster |
| **Grid gap** (compact/normal/spacious) | Frontend pref (new) | hardcoded `gap-4` at `+page.svelte:519` (and flat grid) |
| **Group vs flat** | Frontend pref (new) | grouping logic `+page.svelte:113-131`; render branches `~462-534` |
| **Which badges/stats shown** (status / DV / HDR / resolution / rating / votes / genres) | Server config (extend existing toggle family) | overlays `ResultTile.svelte:161-169`; meta gated by `show_*` derived from `$settings` at `ResultTile.svelte:12-15` |

Rule of thumb from the existing codebase: **instant, personal visual tweaks → Plane A** (localStorage); **content/data-visibility toggles that should follow the user across devices → Plane B** (server, joining the `show_*` family).

## MUST-NOT-BREAK

- **List view and swipe view must keep working** — they share `gridStyle`, the `density` store, grouping, and selection state. The list table (`+page.svelte` `:else` branch ~537-616, `ResultRow.svelte`) and `SwipeDeck` must be untouched in behavior. In particular, do **not** regress `ResultRow.svelte:19-22`'s existing density handling.
- **Grouped releases must still render in tile view** — both the collapsed group header summary (`+page.svelte:462-527`) and the expanded nested grid (`+page.svelte:518-525`) must render compact cards. Apply the grid fix to **both** the grouped-expanded wrapper (`:520-523`) and the flat wrapper (`:529-532`).
- **Deploy only via `docker compose up -d --build`** from `X:\Docker Apps\ScanHound`. The Svelte frontend is **baked into the image** — `docker restart` (or `up -d` without `--build`) deploys nothing. Any frontend change is invisible until a rebuild.
- **Server config additions must touch all 4 touchpoints** — skipping `SettingsUpdate` means `extra="forbid"` 422s the save; skipping `_DEFAULT_CONFIG` means undefined reads. Frontend `saveSettings` sends diffs only (`stores/settings.ts:32-38`), so legacy/unknown keys must not be introduced.

## Open design questions

1. Where exactly should the user-facing view controls live — extend the Settings → General tab, add an inline "View Options" popover near the view-mode toggle / FilterBar, or both (server options in Settings, instant prefs inline)?
2. Should `density` be one shared store across list + grid, or a separate `gridDensity`? (Today `density` is list-only despite the generic name.)
3. What does "poster-only" do to grouped tiles and to the selection checkbox / status badge overlays — hide meta only, or also strip overlays?
4. For non-2:3 aspects (16:9 / square), how should posters that are natively 2:3 be treated — `object-cover` crop, letterbox, or fetch a different artwork?
5. Tile-size presets vs. a free `minmax` min-width input — which is more usable, and does tile-size interact with the server `tile_columns` override (size ignored when columns are pinned)?
6. Should "group vs flat" be a persisted pref, or derived from sort (e.g., flat when sorting by date, grouped when sorting by title)?
7. Minimum viable fix scope: ship just the `min-w-0` + tile width-bound correctness fix first, then layer configurability — or design the full option set before touching the render?
