# Title Bookmarks — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan

## Goal

Let the user bookmark a title while browsing scan results, independent of which specific release/resolution they're looking at — "I want this movie/show, I'm not ready to grab it yet, remind me later." Distinct from Watchlist (which tracks titles you don't have found yet and searches for them); this is for titles you HAVE found and want to remember.

## Scope

**Per-title**, not per-release (per user decision). A bookmark keys on the same title-identity concept already used elsewhere in this codebase (imdb_id first, normalized-title+year fallback — matching `find_library_duplicate`'s and the new identity-based conflict-detection's own pattern) so bookmarking "Dune Part Two" from a 1080p listing and later seeing the 4K Remux listing both show the SAME bookmarked state.

## Backend

- New table `bookmarks`: `id INTEGER PRIMARY KEY`, `imdb_id TEXT`, `title TEXT`, `year INTEGER`, `media_type TEXT`, `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`. Unique constraint on `(imdb_id)` when present, else on `(normalized_title, year, media_type)` — mirrors the existing identity-key fallback pattern rather than inventing a new one.
- `DatabaseManager` methods: `add_bookmark(imdb_id, title, year, media_type)`, `remove_bookmark(imdb_id_or_title_key)`, `list_bookmarks()`, `is_bookmarked(imdb_id, title, year) -> bool`.
- Endpoints: `POST /results/bookmark` (`{imdb_id?, title, year?, media_type}`, toggles on/off — mirrors the existing `/results/dismiss` toggle pattern), `GET /results/bookmarks` (list).
- `_shape_results` (or wherever items are annotated before returning) gains a `bookmarked: bool` computed the same way `library_duplicate`/other computed-at-read-time flags already work — checked against the identity key, not the URL.

## Frontend

- A star/bookmark icon button on each result row (desktop `ResultRow.svelte`, mobile equivalent, and `DetailPanel.svelte`'s action row) — toggles via the new endpoint, optimistic update matching the existing `dismissItem`/`restoreItem` pattern in `stores/results.ts`.
- A "Bookmarked" quick-filter chip (alongside the existing `quickFilters` — 4k/hdrdv/inplex — in `FilterBar.svelte`), so the user can view just their bookmarked titles across scans.
- A `bookmarkedTitles` store (a `Set` of identity keys, hydrated on load, same shape as `dismissedUrls`) drives the star's filled/unfilled state client-side without a round-trip per row render.

## Testing

- Backend: identity-key collision behavior (bookmarking the same title via two different releases toggles the SAME bookmark, not two), toggle on/off round-trip.
- Frontend: `bookmarkedTitles` store toggle logic, quick-filter integration (pure function tests matching existing `filteredResults` derivation patterns).

## Non-goals (YAGNI)

- No bookmark notes/tags/folders — a plain on/off flag per title, matching the simplicity of the existing dismiss/skip mechanism.
- No cross-device sync beyond the existing single-DB architecture (bookmarks live in the same SQLite DB as everything else, already shared across the app/web/mobile views).
