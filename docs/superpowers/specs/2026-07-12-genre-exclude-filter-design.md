# Genre Exclude Filter — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan

## Goal

The existing genre filter (`genreFilter` in `stores/results.ts`, backend `_filter_and_sort`'s `genre` param) is include-only — selecting "Comedy" narrows to ONLY comedies. Add an exclude mode ("never show Reality" without having to include every other genre).

## Root cause of "genres missing for some items"

Confirmed: genre data comes from TMDB matching (`MetadataEnricher`); an item that never got a good TMDB match (same root cause investigated earlier this session for missing posters/ratings — an AKA-title mismatch, a transient scrape hiccup, etc.) has no genres at all. The Rescan button (already built this session) is the existing fix mechanism for an individual item. This spec does NOT add new genre-population machinery — it's a filter-mode feature. An item with no genre data simply never matches an exclude rule (it's not affirmatively excluded, it just doesn't carry the genre being excluded) — this is correct behavior, not a gap this feature needs to solve.

## Design

- `genreFilter` (currently `string[]`, include-only) becomes `{ include: string[]; exclude: string[] }` — a mode toggle per-chip (click = include, shift-click or a small toggle = exclude) rather than two separate lists the user manages independently, matching how most filter UIs handle this (Plex's own genre filters use a similar include/exclude toggle).
- Backend `_filter_and_sort`'s genre handling: `if include: items = [i for i in items if any(g in include_set for g in i.genres)]`; `if exclude: items = [i for i in items if not any(g in exclude_set for g in i.genres)]` — both can be active simultaneously (e.g. include Sci-Fi, exclude Reality wouldn't normally overlap, but the logic composes correctly either way).
- Frontend `availableGenres`-derived chip UI gets a small state indicator per chip (three states: neutral / included / excluded) instead of the current binary toggle.

## Testing

- Backend: `_filter_and_sort` with include-only (regression, existing behavior unchanged), exclude-only, both combined, an item with no genres passes exclude filters (never wrongly excluded) but fails include filters (correctly hidden when include is active and it has no matching genre — matches today's existing include behavior for genre-less items, unchanged).
- Frontend: chip three-state cycle, `genreFilter` store shape migration (existing persisted `string[]` value needs a one-time migration to the new `{include, exclude}` shape — check the `persisted` store helper's existing migration pattern if any, or default gracefully to `{include: oldArray, exclude: []}` on first load post-upgrade).

## Non-goals (YAGNI)

- No new genre-population/backfill feature (the Rescan button already covers per-item fixing; a bulk version is a separate, unrequested feature).
- No genre-exclude equivalent for language/other facets in this pass — genre only, per what was asked.
