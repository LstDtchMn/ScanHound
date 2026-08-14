# Coverage-canary build plan (kickoff 2026-08-14, Jesse's go)

Builds the agreed design in `2026-08-11-rss-readiness-gate-design.md` (this
directory, pinned from PR #61). Constraints carried forward verbatim:
**do NOT build per-URL ack** (rejected by both peers); watermark-based
variable-depth sweep, not fixed page-1 (an incomplete sweep is a FAILURE, not
coverage); automatic reversion to listing mode on canary failure; canary health
is part of the safety claim.

## What 8/6-8/14 evidence settles

- **The canary is not optional.** 20 RED misses, category-shaped (TV/docu the
  tag feeds never carry), growing ~2/day. Promotion without the canary loses
  exactly these. The design's premise is now measured, not estimated.
- **Canary-interval invariant** (interval < listing-visibility window): the
  RED cohort had listing residence of days (57-98h observed ages), and the
  qualification data holds per-cycle listing observations to measure the
  RESIDENCE PERCENTILES the design asks for. Task 0 below computes them
  before any cadence is chosen. Start conservative (design: "every few
  hours"), then widen with measured p05 residence.

## Build order

0. **Measure listing residence** from hdencode_shadow_cycles/misses (script in
   scripts/analysis/, output committed as evidence). Sets the canary interval.
1. **Sweep engine**: watermark-based variable-depth listing crawl -- crawl
   until crossing the last SUCCESSFUL watermark; page 1 fast-path when quiet;
   incomplete sweep => sweep FAILED (never "no misses").
2. **Canary mode wiring**: `rss_primary` + scheduled canary in
   background_scanner; canary finds a release RSS lacks => acquire via the
   listing path AND record a canary_catch row (the miss evidence pure
   rss_primary would destroy).
3. **Automatic reversion**: canary stale/failed beyond its interval, or a
   canary_catch above threshold => revert to listing mode, alert (through the
   deduped stop-alert path), require operator re-arm.
4. **Gate rewrite**: promotion gate consumes canary evidence; the 8 Gun
   Stories cohort documented as operator-accepted historical unknowns.
5. Qualification collector: new mode awareness (rss_primary+canary is a
   VALID observed mode; anything else stops).

Each numbered step is its own reviewable PR; ChatGPT relay after each per
Jesse's standing review preference.
