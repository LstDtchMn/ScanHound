# Downloads: first-grabbed label + source link — review request

**Repository:** `LstDtchMn/ScanHound`
**Branch:** `feat/download-first-grabbed-and-link`
**Base:** `6a4eb00` (`main`)

## What this does

Two operator-visible additions to the Downloads views:

1. **"first grabbed 3d ago"** replaces a bare, unlabelled SQLite timestamp on the
   history list, and adds the same label to the live download list (desktop and
   mobile), which previously showed no date at all.
2. **A link to the release's source page** — from the history title, and from the
   live rows. Nothing on either view was clickable before.

## Why the date is trustworthy

`downloads.date_added` is genuinely the *first* grab: `add_to_history()`'s
`ON CONFLICT(url) DO UPDATE` bumps `last_grabbed_at` and deliberately does not
touch `date_added`. A second write path exists — the one-time
`INSERT OR IGNORE ... 'Unknown (Migrated)'` legacy import, which would carry a
fabricated date — but production currently holds **0** such rows out of 579, so
no row on screen has an invented date.

Verified on the live DB: 579 history rows, 0 migrated placeholders, span
2026-06-30 → 2026-08-12, and exactly **1** row has ever been regrabbed. That last
number is why no "regrabbed" badge was added — it would be blank on 578 of 579
rows.

## The design decision worth arguing with

The live list only knows a JDownloader **package name**; the url and first-grab
date live on `downloads`. `get_download_source_links()` resolves name → release,
and **refuses to resolve when a name maps to more than one url.**

This is deliberately *not* a reuse of `pipeline_service._match_download_results()`.
That matcher answers a different question — *which attempt* a grab became — and
so needs uuid pinning, `last_grabbed_at` windows and excluded-uuid lists. This one
answers *which release page a name came from*, which is stable across regrabs and
needs none of that. Reviewer: say so if you think that separation is wrong and the
two should be unified.

The ambiguity refusal is the safety property. Either candidate url renders as a
perfectly working link, so a wrong guess is indistinguishable from a right one at
the point of use — a missing link is recoverable, a confidently wrong one is not.

## The bug found mid-implementation

The Downloads page replaces its whole result list from the `download:results`
WebSocket push. Annotating only the REST endpoint would have made the link appear
on the 5s poll and vanish on the next progress push — visible flicker, and the
kind of defect that reads as "sometimes broken" rather than "wrong". Both
transports now annotate through one shared helper (`backend/download_links.py`),
and the annotation is applied *after* the change-detection signature is computed
so it cannot affect what triggers a broadcast.

## Precedent for the ambiguity rule

The `jd_confirmed_name` backfill migration already resolves names to releases with
the identical policy — it collects candidate urls per folded name and writes only
`if len(hits) == 1`. This change did not copy that code (it needs a different
direction and no folding), but it deliberately matches its rule.

**Known limitation, deliberate:** the backfill folds names before comparing
(`fold_name()`) because JD sanitizes punctuation, whereas this lookup matches
exactly. A row whose `jd_confirmed_name` has not yet been captured, and whose
`package_name` differs from JD's reported name only in punctuation, therefore
resolves to nothing and renders no link. That degrades to "no link", never to a
wrong one, and self-heals once the name is captured. Reviewer: worth folding here
too, or is the added ambiguity surface a bad trade for a cosmetic link?

## A second bug found while reviewing my own diff

The two new indexes were first placed beside `idx_downloads_date`, which sits
**before** the migrations that add `package_name` and `jd_confirmed_name`. On a
fresh database `downloads` is created as `(url, title, date_added)` only, so
`CREATE INDEX ... ON downloads(package_name)` there raises "no such column" and
takes startup down — on new installs specifically. Moved below both migrations,
with a regression test asserting the indexes exist on a freshly built database.
Verified by mutation: re-creating the index early errors 12 of the 14 tests.

## Verification

- `tests/test_download_source_links.py` — 14 tests.
- Mutation-verified. The two behavioural mutations each kill exactly one test —
  precise, not blunt:
  - dropping the ambiguity guard (`url_count == 1` → `True`) fails only
    `test_a_name_used_by_two_different_releases_maps_to_neither`;
  - letting a regrab reset `date_added` fails only
    `test_a_regrab_does_not_move_the_first_grab_date`.
  - the schema mutation (index created before its column migration) errors 12 of
    14, since every test needing a database dies with it — which is the point.
- The regrab test pins `date_added` to a known past value first, because the
  column is second-resolution and two calls in the same second would agree no
  matter what the `ON CONFLICT` clause did. It also asserts `last_grabbed_at`
  *moved*, as a positive control that the second write happened at all.

## Specific questions

1. Is the name→release resolution sound, or is there a reachable case where two
   different releases share a package name *and* a url, making the single-url
   check pass on a genuinely ambiguous pair?
2. `jd_confirmed_name` and `package_name` are unioned as candidate names. Can a
   row's `package_name` collide with a *different* row's `jd_confirmed_name` in a
   way that silently suppresses a link that should resolve?
3. Enrichment runs on every changed broadcast. In-flight package counts are small,
   but is there a load case where this lookup is hot enough to matter?
4. `safeHttpUrl()` gates rendering to http(s) since stored urls are scraped
   third-party strings. Is rendering-time scheme checking the right layer, or
   should it be enforced at write time?
