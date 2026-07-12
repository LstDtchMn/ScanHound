# Find Other Resolution (TV) — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan

## Goal

While browsing a TV show at one resolution (e.g. a 4K release too large to justify keeping), one click searches for the SAME show + season at a different (typically smaller) resolution — cache-first, live Site Search fallback.

## Scope

TV only, matching the user's own example (a movie's "other resolution" is a much less common need and the existing Compare/duplicate-comparison feature already surfaces resolution alternatives for movies via `find_library_duplicate`). Button appears on TV season/episode rows and in the detail panel.

## Behavior

1. **Local cache first (per user decision):** search the already-loaded `results`/cached background-scan items for the same show (matched by `imdb_id` else normalized title) + same season + a DIFFERENT resolution than the current item. If found, surface it immediately (scroll to it / highlight it in the existing list — no navigation needed, it's instant).
2. **Live Site Search fallback:** if nothing found locally, reuse the EXISTING `searchThisSite(query, source)` (built this session for the empty-state search fallback) with a constructed query `"{show title} S{season:02d}"` (matching common season-pack naming conventions) against the currently-selected source. This is the SAME mechanism, just a different query-construction path (pre-filled + auto-triggered instead of typed by the user) — no new backend endpoint.
3. Which "other resolution" to target: if the current item is 2160p/4K, search implies 1080p (and vice versa) — a simple toggle, not a full resolution picker, matching the user's own stated use case exactly (4K found, want 1080p instead).

## Components

- **`frontend/src/lib/renames/findOtherResolution.ts`** (or a `results/`-adjacent home matching existing helper conventions) — pure function `findCachedAlternative(items, target: {imdbId, title, season, excludeResolution}) -> ScanResult | null`, unit-tested.
- A button on the TV item's row/detail panel — "Find 1080p version" (label reflects the actual target resolution) — calls the local-search helper first; if null, calls `searchThisSite()` with the constructed query.

## Testing

- `findCachedAlternative`: matches by imdb_id when present, falls back to normalized title, requires exact season match, excludes the current resolution, returns null when nothing qualifies (never a false match to a different season or show).
- Query construction: `"{title} S{season:02d}"` formatting, verified against a couple of real show-title edge cases (titles with colons/special characters — reuse existing title-sanitization if the search endpoint needs it, matching how other site-search paths already handle this).

## Non-goals (YAGNI)

- No movie support in this first cut (TV only, per scope decision).
- No arbitrary resolution picker — just "the other one" (4K↔1080p), matching the stated use case. A fuller picker can be a follow-up if ever needed.
- No new backend endpoint — entirely a frontend composition of already-existing pieces (local filtering + the existing Site Search fallback).
