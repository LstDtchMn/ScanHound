# RSS qualification: replace the absolute miss gate with a recovery criterion

**Date:** 2026-07-27
**Status:** design, for peer review. No code changed. Nothing enabled.
**Branch:** `agent/rss-miss-recovery-criterion` (off the parked Stage 0 branch)
**Decision owner:** Jesse — decision made 2026-07-27 on the evidence below.

---

## The problem

`get_hdencode_rss_readiness` ([backend/database.py:2109](../../backend/database.py))
fails qualification on **any** relevant miss ever recorded:

```python
if summary["relevant_misses"]>0: reasons.append("relevant_misses_detected")
```

The count is deliberately unbounded in time — the comment at
[database.py:2061](../../backend/database.py) states this is intentional, a
mandatory stop condition. Current value: **28**. That number can only grow, so
**the gate can never pass**, and `rss_primary` can never be reached.

That is not a bug. It is the rule doing exactly what it was written to do. The
question is whether the rule still expresses what we actually want, now that we
know what a "miss" looks like in practice.

## What the evidence shows

All figures measured 2026-07-27 against the production database, not taken from
prior summaries.

| measure | value |
|---|---|
| recorded relevant misses | 29 |
| from cycles that never fetched the feed (artifacts) | 6 |
| genuine misses | 23 |
| **positively observed arriving in the feed later** | **28 of 29** |
| never acquired | 1 — recorded 54 minutes before measurement |
| catch-up time: min / median / max | 1.0 h / 1.2 h / **4.1 h** |
| misses taking > 6 h | **0** |
| misses taking > 24 h | **0** |

**Nothing was permanently lost.** Every miss older than one polling interval was
confirmed present in the feed afterwards. The single outstanding item is younger
than one cycle and has not yet had its next observation.

Critically, a permanent loss **would** be detectable: it presents as "flagged
missing once, then never observed in the feed". Exactly one record has that
shape and it is too new to interpret. The confidence therefore comes from
*positive confirmation of arrival*, not from absence of evidence.

### Why the original rule over-fires

The rule was written before any shadow data existed, when a "miss" was assumed
to mean *the feed does not carry this release*. Measurement shows it actually
means *the feed had not carried it yet at the moment we looked* — the listings
page and the feed simply update on different schedules. Median divergence: about
one polling interval.

An absolute gate on that quantity does not measure feed reliability. It measures
whether the two sources were ever momentarily out of step, which they always
will be.

## Proposed criterion

Replace the absolute test with a **recovery** test. Fail qualification only on a
miss the feed did not acquire within a bounded window.

```
unrecovered miss  := recorded as listing-only, AND not subsequently observed
                     in feed-only within RECOVERY_HOURS
qualification fails if  unrecovered_misses > 0
```

Default `RECOVERY_HOURS = 6.0`, i.e. **46% above the worst case ever observed**
(4.1 h). Configurable as `hdencode_rss_max_miss_recovery_hours`.

Three properties this preserves:

1. **It still fails closed.** A genuine permanent miss never appears in
   feed-only, so it can never satisfy the recovery test at any threshold. The
   stop condition survives for the case it was written to catch.
2. **A too-new miss cannot pass.** Anything recorded less than
   `RECOVERY_HOURS` ago and not yet observed is *undetermined*, not recovered —
   it blocks qualification until it resolves either way. Waiting is the only way
   to clear it, so the gate cannot be raced by timing.
3. **The artifact misses stop counting.** Misses recorded by cycles that never
   fetched the feed (`rss_requests = 0`) are excluded — they describe a
   comparison that never happened. 6 of the current 29. This is a correctness
   fix independent of the threshold change and should land even if the rest is
   rejected.

## What is deliberately NOT changed

- The other five qualification conditions — cycle count, observation days, feed
  health, request-reduction, restart recovery — are untouched.
- The fail-closed structure in
  [hdencode_rss_service.py:104](../../backend/hdencode_rss_service.py) and
  [background_scanner.py:400](../../backend/background_scanner.py) stays exactly
  as is. If qualification fails, `rss_primary` still refuses to run rather than
  degrading silently.
- No change to shadow comparison, feed fetching, or grabbing.

## Implementation sketch

**One function**, `get_hdencode_rss_readiness`, plus a helper.

1. New helper `count_unrecovered_misses(recovery_hours)` in `DatabaseManager`:
   walk `hdencode_shadow_misses`, join to its recording cycle, skip records whose
   recording cycle has `rss_requests = 0`, then scan forward through usable
   cycles for the URL in `feed_only`. Classify each as recovered /
   unrecovered / undetermined.
2. Replace the line 2109 condition with `unrecovered > 0` → `relevant_misses_unrecovered`,
   and add `undetermined > 0` → `relevant_misses_undetermined`.
3. Return all three counts alongside the existing `relevant_misses` total, so
   the evidence collector keeps reporting the raw number and the change is
   visible rather than hidden.
4. Config key with a default, validated `> 0`.

Cost estimate: ~60 lines including the helper. **No schema change** — the
`details_json` per cycle already carries `feed_only`, which is what the existing
`miss_resolution.py` grader walks.

## Risks

1. **This is a criterion change made after seeing the results.** Stated plainly
   because it is the main objection. The defence is that the measurement answers
   a question the original rule was guessing at, and the new rule is strictly
   more specific — it still fails on the thing the old rule was protecting
   against. It is not a loosening to "no misses matter"; it is a narrowing to
   "misses that never resolve matter".
2. **One-shot detection.** A release is typically observable as missing for
   exactly one cycle before it either resolves or leaves the listings page. The
   detector gets a single look per release. It worked 28 times out of 28, but
   the design does not give a second chance. Recording a per-cycle `feed_urls`
   set would fix this properly and is worth doing later; it is not required for
   this change, which only reinterprets data already captured.
3. **Threshold selection.** 6.0 h is chosen from a 5.4-day sample with a 4.1 h
   maximum. A longer observation could surface a slower case. The threshold is
   configurable so it can be tightened without a code change.
4. **Deployment cost.** One container recreate. Should be batched with the
   pending model repoint (TL-010) rather than spent alone.

## Verification before enabling

1. Unit tests for the helper: recovered inside window; unrecovered beyond
   window; undetermined because too new; artifact cycle excluded; miss with no
   subsequent usable cycle.
2. Run the new criterion read-only against the live database and confirm it
   reports 0 unrecovered, 1 undetermined, and therefore still **not ready**
   until that item resolves.
3. Confirm the other five conditions still evaluate identically.
4. Only then set `hdencode_discovery_mode = rss_primary`, and confirm the first
   cycle actually runs rather than skipping with `primary_not_ready`.
5. Confirm listing fallback still engages when RSS coverage is uncertain.

## Open questions for review

1. Is 6.0 h the right threshold, or should it be tied to observed cycle cadence
   (e.g. 3× median interval) so it adapts if polling changes?
2. Should `undetermined` block qualification, as proposed, or should a
   sufficiently old undetermined record be treated as unrecovered? Proposed
   behaviour blocks until resolution, which is stricter but can stall on a
   release that permanently leaves the listings page.
3. The artifact exclusion is a correctness fix on its own. Should it ship
   separately and first, so the threshold change is evaluated against a clean
   count of 23 rather than 29?
