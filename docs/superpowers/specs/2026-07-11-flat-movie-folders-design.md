# Flat Movie Folders — Design Spec

**Date:** 2026-07-11
**Status:** Approved (design), pending implementation plan

## Goal

Let ScanHound's auto-rename place a **single-file movie** directly into the movie library root (e.g. `/library/movies/Title (Year) [1080p].mkv`) instead of always nesting it inside a per-movie `Title (Year)/` subfolder. TV keeps its `Show (Year)/Season NN/` structure. Off by default — existing behavior is preserved until the user opts in.

## Motivation

The user's movie libraries don't need Plex's per-movie folder convention for a plain single-file movie; the extra subfolder is noise. TV genuinely needs its Show/Season tree, so this applies to movies only.

## Configuration

New key in `backend/config.py` `AppConfig` + `_DEFAULT_CONFIG`:

- `auto_rename_movie_flat: bool` — default `False`.

Default `False` guarantees no behavior change on upgrade. When `True`, applies to **both** movie libraries (`auto_rename_movie_library` and `auto_rename_movie_library_4k`) — a single global toggle, not per-library.

## Placement rule

Implemented in `backend/rename/naming.py` (`_destination`, reached via `build_target`). Given a resolved `meta`:

| Case | Destination directory |
|---|---|
| Movie, `flat=True`, **single file** (no `part`) | `movie_root` (library root, no subfolder) |
| Movie, `flat=True`, **split/multi-file** (`meta["part"]` truthy) | `movie_root/Title (Year)/` (subfolder kept, parts stay grouped) |
| Movie, `flat=False` | `movie_root/Title (Year)/` (today's behavior — unchanged) |
| TV (any flat value) | `tv_root/Show (Year)/Season NN/` (unchanged) |

The single-vs-split decision reuses the **existing** `meta["part"]` signal that `build_target` already consults for the `- Part N` filename suffix — no new multi-file detection is introduced. A truthy `part` ⇒ multi-file ⇒ keep the subfolder.

The **filename** is unchanged in both modes (`Title (Year) [res].ext`, or the custom `auto_rename_template_movie` render if set). Flat mode only changes the directory, orthogonally to filename/template logic.

## Plumbing

- `build_target(meta, *, movie_root, tv_root, template, flat=False)` — gains a keyword-only `flat` parameter (default `False`, so existing callers and tests are unaffected). It passes `flat` (and the already-available `meta`) to `_destination`.
- `_destination(meta, *, movie_root, tv_root, title, year, flat=False)` — gains `flat`; applies the placement rule above.
- `backend/rename/service.py` — every call site that builds a **movie** destination (`conflict_preview` is unaffected; the relevant ones are the preview/apply/`set_destination` paths that call `build_target`) passes `flat=self._cfg.get("auto_rename_movie_flat", False)`. TV-only call paths need no change but passing the flag is harmless (TV ignores it).

## Settings UI

Add a checkbox to the auto-rename section of the Settings page (`frontend/src/routes/settings/+page.svelte`), bound to `auto_rename_movie_flat`:

> **Place movies directly in the library folder** — no per-movie subfolder. Split (multi-part) movies still get their own folder. TV shows are unaffected.

Follows the existing settings-toggle pattern in that section (same store/save plumbing as neighboring booleans like `auto_rename_plex_sort_titles`).

## Unaffected / non-goals

- **Conflict + library-duplicate detection**: match by title/year and by path, both folder-layout-agnostic — `find_library_duplicate`, same-path collision detection, and the dupe-compare analyzer keep working in either mode.
- **Collision safety**: `place_file` still refuses to overwrite; Keep-both dedup naming still applies. In flat mode two *different* movies can't collide (distinct `Title (Year) [res]` names); a genuine same-name collision is handled exactly as today.
- **No migration / no moving existing files**: this only affects where *newly* applied renames land. Already-foldered movies are left in place.
- **No mobile settings change** required (the toggle is a rare set-once preference; desktop settings is sufficient).

## Testing

`tests/` unit coverage for `naming.py`:

1. `build_target` movie, `flat=True`, no part → dest dir == `movie_root` (no subfolder), filename still `Title (Year) [res].ext`.
2. `build_target` movie, `flat=True`, `part=2` → dest dir == `movie_root/Title (Year)`, filename carries `- Part 2`.
3. `build_target` movie, `flat=False` (default) → dest dir == `movie_root/Title (Year)` (regression guard).
4. `build_target` TV, `flat=True` → dest dir unchanged (`tv_root/Show (Year)/Season NN`).
5. A service-level test that `auto_rename_movie_flat` config flows into the destination (flat single-file movie applies into the library root).
