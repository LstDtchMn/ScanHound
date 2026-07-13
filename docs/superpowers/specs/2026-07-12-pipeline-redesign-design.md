# Pipeline Feature Fix & Redesign — Design Spec

**Date:** 2026-07-12
**Status:** Approved by user (sections presented and approved individually)

## Problem

A deep-dive investigation (17-agent workflow, findings adversarially verified against
code and live production data) found the Pipeline feature partially broken and
under-informative:

1. **Critical:** `compute_package_name()` (`backend/download_service.py:26`) omits
   season, so multiple seasons of one show collapse onto one join key. Confirmed live:
   *Joey* S01/S02 and *Little House* S01/S05 collide.
2. **Critical:** JDownloader sanitizes punctuation in package names (`:` → `;`, etc.)
   before reporting results back, but ScanHound compares the raw un-sanitized name —
   titles with a colon can never match. Confirmed live: all 14 *Law & Order: LA* rows
   stuck at `never_started` (93% of that bucket is this one bug).
3. **Important:** `in_progress` conflates "still downloading" with "renamed, waiting on
   Plex cache refresh" (both confirmed present in live data; UI can't distinguish).
4. **Important:** `pipeline_reconcile_enabled` and `pipeline_verify_grace_margin_minutes`
   exist in `backend/config.py` (lines 136-137, defaults at 480-481) and are read live
   by the maintenance loop (`backend/app_service.py:595-600`), but `SettingsUpdate`
   (`backend/api/routes/settings.py`, `extra="forbid"`) doesn't declare them — they are
   unreachable from the API/UI.
5. **UI:** rows show only a title string although year/season/resolution/checked_at are
   already returned by `GET /pipeline/items` (see `PipelineItem`,
   `frontend/src/lib/api/types.ts:681-695`); no posters; no color-coding; one generic
   empty state; `never_started` rows carry no `detail` text.
6. **Minor:** `find_plex_match` (`backend/pipeline_service.py:31`) swallows all
   exceptions into a "not in Plex" verdict indistinguishable from a real miss.

Non-issues confirmed during investigation (do NOT change): Dismiss wiring is correct;
Re-grab/Search-sources gating on in-flight rows is deliberate safety design; mobile nav
placement (Pipeline as a `?view=pipeline` switch inside Downloads) is an intentional
v2.25.0 decision.

## Design

### 1. Backend correctness fixes

**Season-aware package name.** `compute_package_name(title, year, resolution)` gains a
`season: Optional[int] = None` parameter. When `season is not None`, the season is
embedded in the name (format: `{title} ({year}) S{season:02d} [{resolution}]`). The
50-char cap is preserved, but truncation now trims the *title* portion and keeps the
year/season/resolution suffix intact — a plain tail-truncation could chop `S{nn}` off a
long title and silently recreate the collision this fix exists to prevent. The
docstring's invariant stands: this remains the single place the string is computed, so
persisted and sent values cannot drift. The one production call site
(`backend/download_service.py:2003`, where `season` is already in scope) passes it
through. Tests updated/added for TV vs movie and long-title truncation.

**JD-confirmed name capture (Approach C — empirical, chosen by user over guessing JD's
sanitization rules).** New nullable TEXT column `jd_confirmed_name` on the `downloads`
table. The existing results poller (`backend/api/main.py:268-331`) gains a capture hook:
each poll cycle, for any `downloads` row that has no `jd_confirmed_name` yet, if a poll
result `r["name"]` matches the row's computed `package_name` under punctuation folding
(compare after removing every non-alphanumeric character and casefolding — robust to any
character-for-character substitution JD performs, known or future), persist `r["name"]`
verbatim as that row's `jd_confirmed_name`. The capture is once per row; subsequent polls
skip rows that already have it. Note: the poller's join key is the package *name* under
folding, not URL — `poll_results()` rows carry no URL.

**Matching precedence.** `backend/pipeline_service.py`'s reconcile matching uses
`jd_confirmed_name` when present, else falls back to the locally computed
`package_name`. This makes matching immune to JD-side name transformations while
remaining correct for grabs that never appeared in JD's queue.

**One-time backfill (capture-side only — corrected at plan time).** A schema migration
(following `backend/database.py`'s existing guarded-ALTER pattern) adds the column, then
best-effort backfills `jd_confirmed_name`: for each `downloads` row, fold-match its
`package_name` against existing `download_results.name` values and capture only when the
fold-match is UNIQUE. Ambiguous matches (the collided multi-season rows — JD-side names
carry no season either, so they are genuinely indistinguishable retroactively) are
skipped, left NULL. Deliberately do NOT recompute `package_name` for existing rows: the
old computed value is the live join key to `download_results.name` and
`rename_jobs.package_name` (both store JD-side names from the old format), and rewriting
it would orphan every healthy old grab. New grabs get season-aware names naturally at
send time (`save_to_history` persists `package_name` on every grab), so **Re-grab is the
working fix for currently-collided rows** — after this change it sends a unique
season-aware name that the poller then captures and matches. The startup reconcile pass
(`app_service.py:566`) re-categorizes whatever the backfill un-stuck.

### 2. Category model & settings plumbing

**Split `in_progress`.** Two distinct category values replace it:
- `downloading` — JD reports the package as actively downloading/extracting (the
  pre-rename branch of `categorize()`).
- `awaiting_plex_refresh` — all rename jobs applied, waiting out the Plex cache grace
  window (the `categorize()` branch at `backend/pipeline_service.py:164-165`).

Every reference to `in_progress` (backend categorize/tests, frontend `PipelineList.svelte`,
category labels/filters) is updated. Existing DB rows with `category='in_progress'` are
re-categorized naturally by the next reconcile pass (categories are recomputed each pass;
no data migration needed for this).

**`never_started` gets real `detail` text.** At categorize-time, write a human-readable
reason into the existing `detail` column: distinguish at minimum "links were never sent
to JDownloader (send failed)" from "sent but never appeared in JDownloader's queue".
Exact wording drafted at implementation time per the project's Fable-copy practice.

**Settings plumbing.** Add to `SettingsUpdate` (matching the existing Optional-field
pattern): `pipeline_reconcile_enabled: Optional[bool] = None` and
`pipeline_verify_grace_margin_minutes: Optional[int] = None`. Settings page gains a small
"Pipeline" section: a checkbox (reconcile enabled) and a number input (grace margin,
minutes), following the page's existing checkbox/number-input patterns. Round-trip tests
mirror `tests/test_dv_settings.py`.

### 3. Frontend/UX redesign

Scope: full redesign to match the Renames page's visual language (user-selected).
`PipelineList.svelte` is shared by the desktop `/pipeline` route and the mobile
`?view=pipeline` switch in Downloads, so one redesign covers both surfaces.

**Stat cards.** Replace flat category chips with the existing
`frontend/src/lib/components/renames/StatCard.svelte` (reuse, not a new pattern).
Variant mapping across all nine category values: `verified` → `success`;
`rename_failed`, `download_failed`, and `not_in_plex` → `error`; `pending_rename`,
`awaiting_plex_refresh`, `never_started` → `warning`; `downloading` → `accent`;
`unknown` → `default`. Cards are clickable filters (toggle behavior identical to
Renames' `StatusDashboard.svelte`); a card whose count is 0 still renders (same as
Renames), keeping the layout stable.

**Per-row detail.** Render the already-fetched `PipelineItem` fields: year, season
(TV only, `S{nn}` form), resolution, and a relative "checked Xm ago" from `checked_at`.
`never_started` rows show their new `detail` text.

**Posters.** Reuse `frontend/src/lib/components/renames/RenamePoster.svelte`. Backend:
`GET /pipeline/items` adds a `poster_path` field, populated by joining the matched
rename job's stored poster for items that have reached a rename job (`pending_rename`,
`rename_failed`, `awaiting_plex_refresh`, `verified`, `not_in_plex`); null otherwise.
Frontend shows the poster when non-null. Deliberately absent for `downloading` /
`never_started`: no identified title exists yet, so no placeholder is shown either.

**Differentiated empty states.** One message per category filter instead of a single
generic message (e.g. "Nothing downloading right now" / "No stuck grabs" / "Nothing
waiting on Plex"), following Renames' per-status empty-state pattern; exact copy drafted
at implementation time (Fable-copy practice).

**Action wiring: unchanged.** Dismiss, Re-grab, Search-sources, Grab-alternative keep
their current wiring and gating.

### 4. Data flow, error handling, testing

**Data flow.** Reconcile trigger model unchanged (maintenance-loop timer + startup pass).
The only new write path is the poller's once-per-row `jd_confirmed_name` capture.
`GET /pipeline/items` gains exactly one new field (`poster_path`).

**Error handling.** Narrow `find_plex_match`'s exception handling: a genuine no-match
still yields `not_in_plex`, but an actual error (DB failure, bad data) is logged and
yields `unknown` instead of masquerading as a confirmed library miss. All other fail-soft
behavior is preserved.

**Testing.**
- Unit: season-aware `compute_package_name()` (TV with season, movie without, 50-char
  truncation preserved); punctuation-folded matching (colon case, question-mark case,
  no-fold-needed case); `jd_confirmed_name` precedence over computed name in matching.
- Migration: backfill recomputes `package_name` for existing rows and a simulated
  multi-season collision resolves after backfill + reconcile.
- Categorize: `downloading` vs `awaiting_plex_refresh` split; `never_started` detail
  text present; `find_plex_match` error → `unknown` not `not_in_plex`.
- Settings: round-trip for both new keys (per `test_dv_settings.py` pattern).
- Frontend: `npm run check`, `npm run build`, `vitest` for row rendering, stat-card
  filtering, and empty states.

## Out of scope

- Mobile bottom-tab entry for Pipeline (intentional existing design).
- `regrab_item`'s full-table scan (latent smell at 246 rows; not worth touching now).
- Changing dismiss/regrab/search-sources behavior or gating.
