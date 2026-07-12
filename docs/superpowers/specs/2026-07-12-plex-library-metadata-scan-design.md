# Plex Library Metadata Scan — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan
**Depends on:** `2026-07-12-audio-hdr-metadata-design.md` (extends `probe_specs()` with `audio_profile`/HDR10+ — this feature is the bulk-driver that calls the extended probe across the existing library)

## Goal

Proactively populate rich technical metadata (resolution, audio profile, HDR/HDR10+, bitrate) for files ALREADY in the Plex library, instead of only probing reactively when a duplicate conflict happens to trigger it. Covers "all" or a user-selected subset.

## Scope decision

**Fast metadata only, by default** — resolution, audio codec/profile (incl. Atmos), HDR/HDR10+, bitrate: all sourced from the existing single-shot + one lightweight frame-level ffprobe call (≈0.1-0.2s/file per the audio/HDR spec's measurements). **DV FEL/MEL layer detection is explicitly NOT part of this bulk scan** — it stays reserved for the existing smart on-demand trigger (`needs_dv_layer_scan`), which only fires `dovi_tool` when the layer is the sole remaining tiebreaker in an actual comparison. Running the full-file RPU extraction across an entire DV library in one bulk pass would take a genuinely long time (potentially hours to days depending on library size) and isn't what was asked for.

## Data source

`plex_cache.file_path` (added by the earlier dupe-compare feature) already gives every cached Plex item's on-disk path. `list_plex_cache_movies()` already exists. The scan is: for each targeted row with a `file_path`, call `probe_specs(file_path, db=db)` — the SAME function every other feature already uses, with its EXISTING `media_probe` cache (keyed by mtime+size) meaning a re-run is a no-op for files that haven't changed since their last probe.

## Backend

- New endpoint `POST /plex/scan-metadata` — body `{scope: 'all' | 'selected', ids?: number[]}` (`ids` = `plex_cache` row ids or rating_keys when `scope === 'selected'`).
- Runs as a background thread (mirrors the existing DV host-detector / background-scan pattern — long-running, must not block the request). Reports progress via the existing WebSocket progress-broadcast mechanism already used by scans (`ws_manager`).
- Movies only for the first cut, matching `list_plex_cache_movies()`'s existing movies-only scope (the dupe-compare feature's `find_library_duplicate` made the same movies-only call for the same underlying reason — TV episode-level probing would be a much larger fan-out; can be revisited later if wanted).
- Per-file failures (unreadable file, ffprobe error) are logged and skipped — one bad file must not abort the whole batch.

## Frontend

- A small panel — likely Settings page (a natural home alongside the Plex connection section, matching where the corrected-away "trigger Plex scan" idea was headed) or a dedicated section of the existing Plex-related UI. Two entry points:
  - **"Scan all"** button — kicks off `scope: 'all'`.
  - **Selection-based**: wherever Plex library items are already listable/selectable in the app (needs a small UI to pick specific titles/folders — reuse an existing item-picker pattern if one exists, otherwise a simple checklist), a "Scan selected" action.
- A progress indicator (reusing the existing scan-progress UI patterns already in the app) — count processed / total, since a full-library pass over thousands of items, even at ~0.1-0.2s each, could take several minutes.

## Non-goals (YAGNI)

- No FEL/MEL bulk option in this first cut (explicit user decision).
- No TV episode-level bulk probing in this first cut (movies only, matching existing infra).
- No new matching/ranking logic — this only POPULATES data that `_quality_score()`/the Compare modal already know how to consume once present.
