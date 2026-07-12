# Plex Library Metadata Scan — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan
**Depends on:** `2026-07-12-audio-hdr-metadata-design.md` (extends `probe_specs()` with `audio_profile`/HDR10+ — this feature is the bulk-driver that calls the extended probe across the existing library)

## Goal

Proactively populate rich technical metadata (resolution, audio profile, HDR/HDR10+, bitrate) for files ALREADY in the Plex library, instead of only probing reactively when a duplicate conflict happens to trigger it. Covers "all" or a user-selected subset.

## Scope decision (revised — full heavy scan by default)

**The full metadata scan — including DV FEL/MEL layer detection — runs by default.** Per explicit user correction: this is a deliberate, proactive bulk pass, not a quick action, and the user wants complete data (resolution, audio profile/Atmos, HDR/HDR10+, bitrate, AND the FEL/MEL layer for every Dolby Vision file) populated across the library, accepting the time cost. This is DIFFERENT from the existing reactive `needs_dv_layer_scan()` smart-gate (which stays exactly as-is, unchanged, for its own purpose — avoiding a redundant `dovi_tool` call during a live two-file comparison when the layer wouldn't change the outcome): that gate answers "is this comparison-specific scan worth it right now," while this bulk feature is unconditionally populating standalone data for every DV-flagged library file, one at a time, with no comparison context to gate against.

**Consequence — this must be designed as a genuinely long-running background job, not a quick action:**
- Runs sequentially (or at a small bounded concurrency, e.g. 2 at once) — `dovi_tool`'s full-file RPU extraction is disk-I/O-heavy; unbounded parallelism would thrash the disk and could take even longer, not less.
- For a large 4K/DV library this can realistically take many hours. The UI must communicate this honestly (an ETA based on files-processed-so-far, not a fake fast progress bar) and must support **pause/cancel/resume** — a user starting an overnight scan needs to be able to stop it if needed the next morning without losing all progress (resume = skip files whose `media_probe`/`dv_scan` cache is already current, exactly like the existing caches already support).
- Runs as a background thread with its own stop-flag (mirrors the existing scan-cancellation pattern already used by regular site scans — reuse that mechanism, don't invent a new one).

For a file whose stream-level probe doesn't indicate Dolby Vision at all, the FEL/MEL step is naturally skipped (nothing to detect) — the "heavy" cost only applies to genuinely DV-tagged files, which is the correct, already-narrow set (not every file in the library pays the dovi_tool cost, only DV ones).

## Data source

`plex_cache.file_path` (added by the earlier dupe-compare feature) already gives every cached Plex item's on-disk path. `list_plex_cache_movies()` already exists. The scan is: for each targeted row with a `file_path`, call `probe_specs(file_path, db=db)` — the SAME function every other feature already uses, with its EXISTING `media_probe` cache (keyed by mtime+size) meaning a re-run is a no-op for files that haven't changed since their last probe.

## Backend

- New endpoints: `POST /plex/scan-metadata` (body `{scope: 'all' | 'selected', ids?: number[]}`, starts the job), `POST /plex/scan-metadata/cancel` (sets the stop-flag), `GET /plex/scan-metadata/status` (progress: processed/total, current file, elapsed, whether a DV/FEL-MEL step is in progress for the current file).
- Runs as a background thread with a stop-flag (mirrors the existing scan-cancellation pattern already used by regular site scans — reuse it, don't invent a new mechanism). Reports progress via the existing WebSocket progress-broadcast mechanism (`ws_manager`).
- Per targeted file: call `probe_specs(file_path, db=db)` (fast fields, cached by mtime/size — a no-op re-run for unchanged files). If the result's `hdr == "Dolby Vision"`, additionally call `_dv.detect_layer(file_path)` (the same function the reactive on-demand path already uses) and persist via the existing `dv_scan` cache table — so a file already FEL/MEL-scanned (cache current) is also skipped on a resumed/re-run scan, exactly like `media_probe` already behaves.
- Sequential or small bounded concurrency (e.g. 2) for the DV-layer step specifically, given its disk-I/O-heavy full-file read — do not parallelize this the way the fast ffprobe-only pass safely could.
- Movies only for the first cut, matching `list_plex_cache_movies()`'s existing movies-only scope (same reasoning `find_library_duplicate` already uses).
- Per-file failures (unreadable file, ffprobe/dovi_tool error) are logged and skipped — one bad file must not abort the whole batch.
- Resume-safe by construction: cancelling and re-starting simply re-walks the target set; every already-current file (per its own cache signature) is a fast no-op, so a resumed scan only does new work.

## Frontend

- A small panel — likely Settings page (a natural home alongside the Plex connection section) or a dedicated section of the existing Plex-related UI. Two entry points:
  - **"Scan all"** button — kicks off `scope: 'all'`.
  - **Selection-based**: wherever Plex library items are already listable/selectable in the app (needs a small UI to pick specific titles/folders — reuse an existing item-picker pattern if one exists, otherwise a simple checklist), a "Scan selected" action.
- A progress panel showing processed/total, current file, and an honest ETA (based on observed per-file rate so far, not a naive linear guess — the DV-layer step's cost varies wildly file to file) — plus **Pause/Cancel** controls, since this can run for hours. "Pause" is really "cancel + the resume-safe re-run behavior already covers picking back up."

## Non-goals (YAGNI)

- No TV episode-level bulk probing in this first cut (movies only, matching existing infra).
- No new matching/ranking logic — this only POPULATES data that `_quality_score()`/the Compare modal already know how to consume once present.
- No change to the existing REACTIVE `needs_dv_layer_scan()` smart-gate — it keeps working exactly as before for its own comparison-time purpose; this bulk feature is a separate, unconditional pass.
