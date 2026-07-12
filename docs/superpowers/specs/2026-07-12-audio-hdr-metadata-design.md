# Richer Audio Profile + HDR10+ Detection — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan

## Goal

`probe_specs()` (the ffprobe-based spec extraction feeding the duplicate-comparison Compare modal and `_quality_score()`'s ranking) currently can't distinguish Dolby Atmos from plain TrueHD/EAC3, can't distinguish DTS-HD MA/DTS:X from plain DTS, and can't detect HDR10+ (it only reports generic "HDR10"). Add all three, sourced from real, verified ffprobe signals — and let them influence which duplicate copy is recommended, not just display.

## Verified technical grounding (tested against real library files, not assumed)

- **Atmos / DTS sub-profiles:** NOT reliably exposed via ffprobe's structured `profile` field (empty for both TrueHD and DTS audio streams in tested real files). **IS** reliably present in the audio stream's `tags.title` field for well-muxed releases — confirmed real values `"TrueHD 7.1 Atmos"`, `"DDP 5.1 Atmos"` on an actual library file. This is muxer-authored track metadata (commonly set by release groups), not a universal guarantee, but it's a real signal the scanner currently reads zero of (`mediainfo.py` never touches `audio.get('tags')` today).
- **HDR10+:** NOT visible via the existing single stream-level ffprobe call (confirmed: a real file with "HDR10+" in its filename showed nothing in `side_data_list` at the stream level, only `color_transfer: smpte2084`, same as plain HDR10). **IS** reliably visible via a frame-level probe (`ffprobe -show_frames -read_intervals '%+#1' -select_streams v:0`) — confirmed real output: `side_data_type: 'HDR Dynamic Metadata SMPTE2094-40 (HDR10+)'`. Measured cost: **0.1s**, barely more than the existing stream probe's 0.09s — cheap enough to run unconditionally on every probe, no smart-gating needed (unlike DV FEL/MEL's minutes-long full-file `dovi_tool` scan).

## Backend changes

### `backend/rename/mediainfo.py` — `probe_specs()`

1. Read `audio.get('tags', {}).get('title')` and `audio.get('profile')` from the EXISTING ffprobe call (no extra cost). Detect:
   - Atmos: `'atmos'` (case-insensitive) in either field.
   - DTS sub-profile: `'dts-hd'`/`'dts:x'`/`'dts hd'` etc. in either field, distinguishing from plain `'dts'`.
2. Add ONE new frame-level ffprobe call (`-show_frames -read_intervals '%+#1' -select_streams v:0`, ~0.1s measured) checking `side_data_list` for a `side_data_type` containing `"HDR10+"` / `"SMPTE2094-40"`. Skip this call entirely when the stream-level probe already determined `hdr == "Dolby Vision"` (HDR10+ dynamic metadata and DV don't coexist on the same stream in practice — confirmed by the DV-tagged test file showing no HDR10+ frame metadata) — bounds the extra cost to plain-HDR10 files only.
3. Extend the returned dict's `hdr` field to report `"HDR10+"` as a distinct value (alongside the existing `"Dolby Vision"`/`"HDR10"`/`"HLG"`/`None`).
4. Add a new `audio_profile: str | None` field carrying the richer detected profile (e.g. `"TrueHD 7.1 Atmos"`, `"DTS-HD MA 5.1"`) — the existing `audio` field (codec + channel count) is unchanged for backward compatibility; `audio_profile` is additive.
5. `media_probe` cache (Task 1/2 of the original dupe-compare feature) already caches the full `probe_specs()` result JSON keyed by (path, mtime, size) — the new fields ride along in that same cache with no schema change needed (it stores the whole dict as JSON).

### `backend/rename/conflicts.py` — `_quality_score()`

**Global constraint: tuple LENGTH and POSITION are unchanged** — `(res_rank, dv, dv_layer_rank, hdr, source, audio, edition)` stays a 7-tuple in this exact order (existing regression tests index into specific positions, per the function's own docstring). Only the VALUE RANGE of the existing `hdr` and `audio` fields deepens:

- **`hdr`** (currently binary 0/1): becomes 0/1/2 — `2` when `job.get('hdr') == "HDR10+"` (probed) or the filename matches `/\bhdr10\+|\bhdr10plus\b/i` (fallback, mirroring the existing probed-first/filename-fallback pattern DV layer already uses); `1` for any other truthy HDR signal (Dolby Vision, HDR10, HLG, or a generic filename HDR match) — i.e. today's behavior, unchanged, for everything except the new HDR10+ case. A Dolby Vision file's `hdr` value is UNCHANGED at `1` (DV's own precedence is carried entirely by the separate `dv`/`dv_layer_rank` fields ahead of `hdr` in the tuple — this must not double-count).
- **`audio`** (currently 0-3, filename-regex only): sourced from the NEW probed `audio_profile`/Atmos/DTS-sub-profile data FIRST when `probe_specs()` has run for this job, falling back to today's exact filename regex when no probed data is present (mirroring the `dv_layer_rank`/`hdr` probed-first pattern already established in this function). Semantic buckets are UNCHANGED (3=TrueHD-or-Atmos-class, 2=DTS-HD/DTS:X-class, 1=DDP/EAC3-class, 0=other) — this is "make the existing tiers reliable," not "add new finer-grained tiers," to keep the change conservative.

## Testing (mandatory rigor — this touches ranking logic)

Per this project's established, hard-won practice for `_quality_score`/`rank_conflict` changes (documented history: a past ranking change passed shallow review while recommending overwriting a 4K DV file with 1080p; only caught by a review that EXECUTED edge cases): this implementation MUST go through the same discipline already used for `needs_dv_layer_scan()` and the identity-based conflict-detection fix earlier this session — TDD, then a dedicated adversarial execution pass on the most capable available model, explicitly re-tracing:
- A DV file's `hdr`/`dv` values are unchanged before/after this change (no double-counting regression).
- An HDR10+ file now correctly outranks an otherwise-identical plain-HDR10 file.
- An Atmos-tagged file (via track title, no filename hint) now correctly outranks an otherwise-identical non-Atmos file of the same base codec.
- A file with NO probed data (probe_specs never ran / ffprobe unavailable) falls back to byte-identical filename-regex behavior as before this change — full regression run of `test_conflicts_rank.py`.

## Non-goals (YAGNI)

- No scan-results-list badges (Compare modal only, per decision).
- No new binary/tool dependency (HDR10+ and Atmos/DTS-profile detection both use ffprobe, already required).
- No change to DV FEL/MEL detection, `needs_dv_layer_scan()`, or `find_library_duplicate()`.
- No UI settings/toggle — this is always-on, matching how HDR/DV detection already behaves.
