# Design proposal — unblocking RSS promotion from permanent "undetermined" misses

**Repository:** `LstDtchMn/ScanHound`
**Status:** DESIGN for review (no code yet). Author: Claude. Date: 2026-08-11.
**Subsystem:** HDEncode RSS shadow readiness gate (`backend/hdencode_shadow.py`,
`backend/database.py` `get_hdencode_rss_readiness` / `get_hdencode_miss_resolution`,
`backend/api/routes/rss.py`).

## The problem

`get_hdencode_rss_readiness()` is a hard gate: `POST /rss/mode` refuses to switch
`hdencode_discovery_mode` to `rss_primary` unless `readiness["ready"]` is true
(`api/routes/rss.py:282`). One of its blocking conditions is `misses_undetermined > 0`.

Live state (2026-08-11, read-only copy of prod `crawler.db`):

- `successful_cycles: 287` (bar: 20), `observed_days: 17.1` (bar: 7), `normal_feeds_healthy: true`
- `misses_never_acquired: 0`, `worst_acquisition_lag_hours: 4.06`, `request_reduction_pct: 83.9`
- **blocking:** `misses_undetermined: 8`, `misses_not_yet_assessable: 9`, `unattributed_listing_candidates`

All 8 undetermined are one show — **Gun Stories** S04/S06/S07/S08/S10/S11/S12/S14, `web-dl`
"archived" back-catalog uploads. They appeared on the HDEncode *listing* as `missing_season`,
then left the listing before RSS was ever observed carrying them.

## Root cause — why "undetermined" is permanent

`classify_miss_resolution()` (hdencode_shadow.py) resolves a listing-only miss by scanning valid
later cycles for the URL:

- URL later in `feed_only` or `duplicate_urls` → **acquired** (RSS carried it).
- URL later in `listing_only` (still listed) but never in the feed → **never_acquired** — a REAL,
  demonstrable RSS coverage gap.
- URL never seen again on either side → **undetermined** — "left the listing without ever appearing
  in the feed, so neither acquisition nor loss can be proven."

`undetermined` is **terminal**: once a URL has paged off the listing, no future cycle produces a
`listing_only`/`feed_only`/`duplicate` observation of it, so it can never reclassify. Bulk archive
re-posts (old seasons dumped on the listing and quickly paged away, never in the feed, that the
operator never wanted) land here and **block promotion forever**, regardless of how well RSS
performs on real releases.

## Design constraints (the gate's hard-won invariants — do NOT regress these)

The readiness gate has a documented history of fail-open bugs (comments cite "two HIGH findings").
Any fix must preserve:

1. **`never_acquired` blocks unconditionally, no deadline** (2026-08-07 decision). A release still on
   the listing that RSS never carried is a real gap; time must not forgive it.
2. **`not_yet_assessable` blocks** (2026-08-07 reversal). Because shadow comparison is only recorded
   while `discovery_mode == rss_shadow`, promoting stops producing the observations a pending row
   needs — the gate must not open on evidence its own promoted mode destroys.
3. **No auto-vanish.** "The honest way to pass is a frozen cohort … not to make a live unresolved
   row vanish." (summarise_miss_resolutions docstring.)
4. **No string-matching on release names / JSON** to classify (an explicitly-removed anti-pattern).

So the fix targets ONLY the `undetermined` bucket, and must be an explicit, auditable, reversible
operator decision — not an automatic or heuristic reclassification.

## Proposed fix — operator acknowledgement of undetermined misses

Follow the existing `dismissed_items` precedent (database.py:448 — the app already lets an operator
permanently hide releases from future scans, with a `dismissed_at` audit column and a cache kept in
sync by mutators).

1. **New state:** an `hdencode_shadow_miss_ack` table keyed by `canonical_url`, with
   `acknowledged_at`, `reason`, and the `state_at_ack` (must be `undetermined` at ack time — the ack
   is rejected if the row is anything else, so a `never_acquired` gap can never be acknowledged away).
2. **Readiness change, surgical:** in the readiness computation, an `undetermined` row whose URL is
   acknowledged is counted into a new `misses_undetermined_acknowledged` field and EXCLUDED from the
   `misses_undetermined` that feeds `reasons`. `never_acquired` and `not_yet_assessable` are
   untouched. The `reasons` list gains nothing new; it only stops listing `miss_resolution_undetermined`
   when every undetermined row is acknowledged.
3. **Transparency:** `readiness` exposes both `misses_undetermined` (active/blocking) and
   `misses_undetermined_acknowledged`, and `GET /rss/status` surfaces the acknowledged URLs so the
   operator can always see exactly what was set aside and re-open it.
4. **Routes:** `POST /rss/misses/acknowledge` (body: url + reason) and
   `POST /rss/misses/unacknowledge` (url). Acknowledge validates the URL is currently `undetermined`.
5. **Reversibility:** un-acknowledge restores the block immediately; the audit row is retained
   (soft, not deleted) so history is preserved.

### Why this is safe against the fail-open history

- It cannot forgive a `never_acquired` (the ack is refused unless the row is `undetermined` at ack
  time — a real gap is never dismissible).
- It is not automatic and not time-based — a human explicitly decides per URL, logged with a reason.
  This is the "frozen cohort resolved by an operator" shape, not "a live row vanishes."
- It does not touch the classifier or use name/JSON heuristics; classification is unchanged. Only the
  *gate's treatment of an already-terminal `undetermined`* changes.
- It is fully visible in `readiness` output, so promoting on an acknowledged set is an informed,
  auditable operator choice, not a hidden bypass.

## Alternatives considered (and why not)

- **Bounded age-out of undetermined (e.g., >90 days).** Rejected as the primary mechanism: it is
  exactly the automatic-time-heals shape the subsystem rejected for `never_acquired`, and a
  *systematic* RSS gap (a whole category RSS never carries) would age out silently. Could be revisited
  later as a secondary hygiene valve ONLY if paired with a clustering guard, but it should not gate
  promotion on its own.
- **Relevance re-check (is the release still wanted?).** Rejected: "want-ness" for an arbitrary paged
  release is fuzzy and would re-introduce heuristic classification; and the miss was already recorded
  because it matched a relevant state, so re-deriving relevance later is guesswork.
- **Pattern/prefix dismiss ("ignore all Gun Stories").** Rejected as the default granularity:
  over-dismisses future real gaps under the same title. Per-URL ack is explicit; a pattern helper
  could sit on top later if the operator burden proves high.

## Open questions for the reviewer

1. Is per-URL operator acknowledgement the right shape, or is a frozen-cohort admission-cutoff (only
   count undetermined admitted before cutoff X; require the cohort to be all-acknowledged) safer?
2. Should acknowledgement require the row to still be `undetermined` at ack time (proposed), and what
   should happen if a later cycle somehow reclassifies an acknowledged URL (can it, given terminal-ness)?
3. Does exposing acknowledged URLs in `GET /rss/status` plus the `reasons` change give enough
   transparency that promoting on an acknowledged set is clearly an operator decision, not a bypass?
4. Is there any path by which an acknowledged `undetermined` could mask a `never_acquired` (e.g., a URL
   that was undetermined at ack time but represents content RSS systematically drops)? If so, how to
   detect the systematic case.

## Scope / testing plan (for the implementation round)

- DB: new `hdencode_shadow_miss_ack` table + `acknowledge/unacknowledge/get_acknowledged` methods;
  readiness excludes acknowledged undetermined and reports the split.
- Routes: acknowledge/unacknowledge with validation (reject non-undetermined URLs).
- Tests: ack of an undetermined URL clears exactly that block and nothing else; ack of a
  `never_acquired` URL is REFUSED; unack restores the block; readiness `ready` flips only when all
  undetermined are acknowledged AND every other gate already passes; the acknowledged split is
  reported. Mutation-verify each (e.g., removing the "must be undetermined" guard must let a
  never_acquired ack succeed and fail the refusal test).
