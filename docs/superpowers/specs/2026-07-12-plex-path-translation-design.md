# Plex Library Path Translation — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan

## Goal

`plex_cache.file_path` (populated via Plex's own API) reports paths in Plex's own terms — Windows drive letters, or NTFS junction-folder aliases Plex was configured with (e.g. `C:\1080p Drives\1080p Bismark\...`), or a NAS UNC path. None of these are directly readable from inside the ScanHound container even after today's docker-compose volume-mount work, because the container sees these same files at different, container-local paths (e.g. `/library/plex-source/l-1080p-bismark/...`). Two features need a real, on-disk file to do their job — the reactive duplicate-comparison probe (`backend/rename/conflict_analyzer.py`) and the bulk Plex-library metadata scan (`backend/plex_metadata_scan.py`) — and both currently pass the raw, untranslated `file_path` straight to `probe_specs()`/`dv_detect`, which silently fails to find the file. This spec adds the missing translation layer.

## Scope decisions

- **Reuse the existing `RenameService._translate_path()` pattern** (a simple `host => container` textarea, one mapping per line, longest-prefix-wins, already proven in production for the unrelated JD-download-path translation) rather than resurrecting `PlexManager`'s dataclass-based `PathMapping`/`translate_path()` mechanism, which is unused dead code today (confirmed: nothing populates `_path_mappings`, no Settings endpoint writes to it, and a comment in `backend/rename/dv_paths.py` already flags it as dead). A new, small, independent config key avoids depending on unverified persistence plumbing.
- **A new config key, `plex_library_path_mappings`**, kept separate from `auto_rename_path_mappings` — the two serve different purposes (one translates JD's own download-destination path; this one translates Plex-reported *existing*-library paths) and merging them risks ambiguous prefix collisions. The underlying longest-prefix-match parsing logic is shared via one small helper function, not duplicated.
- **Two call sites, both identified precisely this session:**
  - `backend/rename/conflict_analyzer.py:84-87` — `existing_path = match["file_path"]` (from `find_library_duplicate()`'s match), used two lines later at `probe_specs(existing_path, db=db)`. Translate right after assignment.
  - `backend/plex_metadata_scan.py`'s target-list builder (`_movie_targets_for_scope` in `backend/api/routes/plex.py`, which builds the dicts `PlexMetadataScanJob` consumes) — translate each `file_path` before it's added to the target list.
- **Seeded with the 23 mappings already confirmed working this session** (see Mapping Set below) as the default value of the new Settings field — not hardcoded in Python, so future changes (a renamed drive, a newly-resolved NAS share, a brand-new physical drive) are a Settings edit, not a code change.
- **Auto-detection is a gap-finder, not an auto-mounter.** It can tell you "here's a file path prefix with no matching mapping" using data already inside the container (`plex_cache`); it cannot create a new docker-compose volume mount or WSL2-level NAS mount by itself — that's inherently a host-level action outside the running container, exactly like today's manual mount work.

## Mapping Set

23 lines, `host_prefix => container_prefix`, one per line:

```
C:\1080p Drives\1080p Bismark => /library/plex-source/l-1080p-bismark
C:\1080p Drives\1080p Eastwood & Gengis Khan => /library/plex-source/b-1080p-eastwood-gengis-khan
C:\1080p Drives\1080p Kennedy & Van Buren => /library/plex-source/k-1080p-kennedy-van-buren
C:\1080p Drives\1080p Nixon & Maclom => /library/plex-source/m-1080p-nixon-maclom
C:\1080p Drives\1080p Tony Montana => /library/plex-source/f-1080p-tony-montana
C:\1080p Drives\1080p Walter White => /library/plex-source/w-1080p-walter-white
C:\1080p Drives\1080p Zepplin => /library/plex-source/h-1080p-zepplin
C:\4K Drives\4K Columbo => /library/plex-source/e-4k-hdr-columbo
C:\4K Drives\4K Gambino => /library/plex-source/a-4k-gambino
C:\4K Drives\4K Jefferson & Truman BU => /library/plex-source/j-4k-jefferson-truman-bu
C:\4K Drives\4K Quantum => /library/plex-source/q-4k-quantum
C:\4K Drives\4K Rickover => /library/plex-source/r-4k-rickover
C:\4K Drives\4K Ulysses & Yuri Gagarin BU => /library/plex-source/u-4k-ulysses-yuri-gagarin-bu
C:\4K Drives\4k HDR Arnold => /library/plex-source/i-4k-hdr-arnold
G:\Movies 1 => /library/plex-source/g-movies-1
\\TURTLELANDSRV2\1080p John Paul Jones => /library/plex-source/nas-1080p-john-paul-jones
\\TURTLELANDSRV2\1080p Lincoln => /library/plex-source/nas-1080p-lincoln
\\TURTLELANDSRV2\1080p Faraday => /library/plex-source/nas-1080p-faraday
\\TURTLELANDSRV2\1080p Icarus => /library/plex-source/nas-1080p-icarus
\\TURTLELANDSRV2\1080p Nathan Hale => /library/plex-source/nas-1080p-nathan-hale
\\TURTLELANDSRV2\1080p Picasso aka Newton => /library/plex-source/nas-1080p-picasso-aka-newton
\\TURTLELANDSRV2\4K HDR Geronimo => /library/plex-source/nas-4k-hdr-geronimo
\\TURTLELANDSRV2\4K Magellan => /library/plex-source/nas-4k-magellan
```

Every one of these was verified end-to-end this session: `os.path.exists()` against the translated path returns `True` for the corresponding real files, and a full `plex_cache` sweep resolved 16,091/16,091 movies (100%) using exactly this mapping set.

## Architecture

A new small module-level function, `translate_plex_path(raw_path: str, mappings_text: str) -> str`, factored out so both `RenameService._translate_path()` (existing JD-mapping consumer) and the new Plex-library consumer share the same longest-prefix-match parsing rather than duplicating it. Signature deliberately takes the raw mappings text (not a config object), so it's pure and trivially unit-testable. A thin wrapper reads `plex_library_path_mappings` from config and calls it, mirroring `RenameService._translate_path()`'s own existing shape.

- `conflict_analyzer.py`: after `existing_path = match["file_path"]` (line 87), translate before it reaches `probe_specs()`.
- `plex_metadata_scan.py`'s target-builder (in `backend/api/routes/plex.py`): translate each `file_path` when building the target dict.

## Settings UI

A new "Plex Library Path Mappings" panel on the Settings page, placed near the existing "Download path mappings" panel it mirrors — same textarea, same `host => container` format, same styling. Pre-filled with the 23-line mapping set above as its default value (so a fresh install or a reset field isn't empty and non-functional). A short helper caption explains the format and links conceptually to "Library Metadata Scan" (the feature this unblocks).

## Auto-detection (maintenance check)

`find_unmapped_plex_path_prefixes(plex_cache_rows, mappings_text) -> list[str]`: for each movie row's `file_path`, computes a coarse grouping key — the drive letter plus its immediate subfolder for a local path (e.g. `C:\1080p Drives\1080p Bismark\...` groups as `C:\1080p Drives\1080p Bismark`; `A:\Movie.mkv` groups as `A:\`), or the server plus share name for a UNC path (e.g. `\\TURTLELANDSRV2\1080p Lincoln\...` groups as `\\TURTLELANDSRV2\1080p Lincoln`) — then returns the distinct set of these keys for which `translate_plex_path()` is a no-op (i.e., no configured mapping's host prefix matches). Two triggers:
- **On-demand:** a "Check for unmapped paths" button next to the new Settings panel, calling a new lightweight endpoint that runs this check and returns the list directly for immediate display.
- **Scheduled:** folded into the existing maintenance loop at a low frequency (daily, not per-scan-cycle — library-drive structure changes rarely) — on a non-empty result, pushes a notification (reusing the existing toast/notification mechanism) naming the unmapped prefix(es), so a newly added drive that got no mapping doesn't silently produce a permanently-empty scan for those files.

## Error handling

- No configured mapping matches a given path: `translate_plex_path()` returns the input unchanged (passthrough), exactly matching `RenameService._translate_path()`'s existing behavior for the JD-mapping case. `probe_specs()` then reports `present: False` for that (still-untranslated, therefore not found) path — the same code path already handles a genuinely missing file today; no new error branch needed anywhere downstream.
- A malformed mapping line (no `=>`, or an empty host/container side) is skipped during parsing, matching the existing JD-mapping parser's tolerance.
- The auto-detection check must never raise on an empty or missing `plex_library_path_mappings` config value — an empty config is a valid (if unhelpful) starting state, not an error.

## Testing

- **`translate_plex_path()`** (pure function): longest-prefix-wins when two configured mappings could both match, passthrough when nothing matches, correct handling of a mapping with no trailing slash vs. one with, malformed-line skipping (missing `=>`, blank host/container).
- **`conflict_analyzer.py`**: a regression test confirming `analyze_job_conflict()`'s existing-path `probe_specs()` call receives the *translated* path, not the raw `plex_cache.file_path` (mock `translate_plex_path`, assert it was called with the raw value and its return value is what reaches `probe_specs`).
- **`plex_metadata_scan.py`'s target-builder**: same shape — assert the target list contains translated paths, not raw ones.
- **`find_unmapped_plex_path_prefixes()`**: a fixture `plex_cache` with one deliberately-unmapped prefix among several mapped ones, asserting exactly the unmapped one is returned.
- **Settings**: the new textarea round-trips through save/load like the existing JD-mapping field (no new persistence mechanism, reuses the existing generic settings-save path).

## Non-goals (YAGNI)

- No automatic docker-compose editing or container recreation from inside the app — a genuinely new physical drive always needs the same manual host-level step this session already established (add a mount, `docker compose up -d`), which the auto-detection check can only flag, not perform.
- No revival of `PlexManager`'s dataclass-based `PathMapping` mechanism — confirmed dead code, not being un-deprecated here.
- No change to `RenameService._translate_path()` itself beyond factoring its shared parsing logic into the new common helper — its own config key (`auto_rename_path_mappings`) and behavior are untouched.
