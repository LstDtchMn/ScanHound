# RSS qualification: replace the absolute miss gate with a recovery criterion

**Date:** 2026-07-27
**Revision:** 2 — revised against peer review of `c9214d7`. Four required changes applied.
**Status:** design, for peer review. No code changed. Nothing enabled.
**Branch:** `agent/rss-miss-recovery-criterion` (off the parked Stage 0 branch)
**Decision owner:** Jesse.

---

## Revision 2 — what changed and why

Peer review accepted the concept and requested changes. All four are applied.
One claim in revision 1 was **wrong** and is retracted outright.

| # | Finding | Disposition |
|---|---|---|
| 1 | **BLOCKER.** Absence from `feed_only` is not evidence of RSS absence — a URL held by *both* sources lands in `duplicate`, which stores no URLs. | **Accepted. Claim retracted.** See [The retracted claim](#the-retracted-claim). A narrower positive signal is substituted. |
| 2 | "Artifact" must mean structural cycle ineligibility, not merely `rss_requests = 0`. | **Accepted.** Definition replaced. Measured: identical result on current data (6 = 6); adopted anyway for correctness. |
| 3 | The 6-hour rule conflates "never recovered" with "recovered after six hours". | **Accepted.** Six states, counted and exposed separately. |
| 4 | After promotion, shadow comparisons **stop** — this is a one-time gate, not ongoing detection. | **Accepted and confirmed in code.** Stated plainly below. |
| — | Unexplained 28 vs 29. | **Reconciled — no discrepancy existed.** See [Count reconciliation](#count-reconciliation). |

---

## The retracted claim

Revision 1 asserted:

> "Critically, a permanent loss **would** be detectable: it presents as
> 'flagged missing once, then never observed in the feed'."

**That is false, and it was the load-bearing claim.** Verified at
[backend/hdencode_shadow.py:66](../../backend/hdencode_shadow.py):

```python
duplicate    = rss & listing_urls     # count only — URLs are NOT stored
feed_only    = rss - listing_urls     # URLs stored
listing_only = listing_urls - rss     # URLs stored
```

A release the feed acquires *while it is still on the listings page* lands in
`duplicate`. Only `duplicate_count` is persisted. Such a release would never
appear in `feed_only`, and revision 1 would have misfiled it as a permanent
loss. Confirmed against the database: `details_json` carries exactly
`feed_only` and `listing_only` as URL lists and every other field as a count.
The full RSS set is not recoverable from stored telemetry.

### What *is* positively provable from existing telemetry

Review treated both directions as unprovable. One direction is provable, and it
is the one the criterion needs. Because `listing_only = listing − rss`:

| observation | inference | strength |
|---|---|---|
| URL in `listing_only` at cycle N | RSS **does not** have it at N | **positive proof of absence** |
| URL in `feed_only` at cycle N | RSS **does** have it at N | **positive proof of presence** |
| URL in neither | `duplicate` (both hold it) **or** both dropped it | **ambiguous** |

A miss's life is therefore covered by positive observations for as long as it
remains in `listing_only`, and resolves positively the moment it appears in
`feed_only`. The ambiguous state is reached only when a URL vanishes from both
sets — which is what `evidence_gap` now names. This narrows the gap; it does
not close it, and this spec no longer claims otherwise.

### Bearing on the historical verdict

The weak branch **never fired**. All 23 misses recorded by structurally
eligible cycles were positively observed arriving in `feed_only`. No conclusion
in this document rests on absence of evidence.

The telemetry addition below is **required for the rule to be sound in
general**, but the current promotion evidence does not depend on it.

---

## Count reconciliation

Review flagged 28 vs 29 as unexplained. Measured directly:

```
readiness summary  SUM(relevant_miss_count) : 29
miss-table rows                             : 29
distinct canonical_url                      : 29
cycles where column != row count            : 0
```

**No discrepancy exists.** Both paths agree at 29. The "28" was a different
quantity — misses positively confirmed arriving *as of the earlier
measurement*, with one then too new to have had its next observation. That
record has since resolved. The two numbers were never in conflict; revision 1
reported both without labelling which was which. Corrected.

---

## The problem

`get_hdencode_rss_readiness` ([backend/database.py:2109](../../backend/database.py))
fails qualification on **any** relevant miss ever recorded:

```python
if summary["relevant_misses"]>0: reasons.append("relevant_misses_detected")
```

The count is unbounded in time by design — [database.py:2061](../../backend/database.py)
documents it as a mandatory stop condition. Current value: **29**. It can only
grow, so **the gate can never pass** and `rss_primary` is unreachable.

That is the rule working as written. The question is whether it still expresses
what we want, now that we know what a "miss" looks like in practice.

### Why it over-fires

The rule predates any shadow data, when a "miss" was assumed to mean *the feed
does not carry this release*. Measurement shows it means *the feed had not
carried it yet at the moment we looked*. The listings page and the feed update
on different schedules; median divergence is about one polling interval.

An absolute gate on that quantity does not measure feed reliability. It
measures whether two sources were ever momentarily out of step, which they
always will be.

---

## Evidence

Measured 2026-07-27 against the production database, read-only, under the
**revised** definitions in this document. Not carried over from prior summaries.

**Cycles:** 100 total — 94 structurally eligible, 6 ineligible.

| state | count |
|---|---|
| `recovered_on_time` (≤ 6.0 h) | **23** |
| `recovered_late` (> 6.0 h) | 0 |
| `pending` (too new to judge) | 0 |
| `unrecovered` | 0 |
| `evidence_gap` | 0 |
| `artifact` (ineligible recording cycle) | 6 |
| **total** | **29** |

| latency | value |
|---|---|
| min / median / max | 1.0 h / 1.2 h / **4.1 h** |
| exceeding 6 h | **0** |
| exceeding 24 h | **0** |

**Blocking under the proposed gate: 0.**

Every miss recorded by an eligible cycle was positively observed arriving in
the feed, worst case 4.1 hours. Nothing was permanently lost, and that
conclusion rests entirely on positive confirmation.

---

## Proposed criterion

Replace the absolute test with a **recovery** test over six explicit states.

### Cycle eligibility (finding 2)

A cycle is eligible to record a miss only if **all** hold:

```
outcome in ("success", "relevant_miss")
AND normal_feeds_complete = 1
AND rss_requests     > 0
AND listing_requests > 0
```

Revision 1 excluded only `rss_requests = 0`. That was too narrow: it admits
cycles where a feed failed, or the listing scrape failed, or the outcome was an
error — all of which produce a miss set that is not trustworthy.

Review is also correct that `rss_requests = 0` does not mean no comparison
occurred: the service still supplies `candidate_urls` from persisted feed
membership, so the comparison runs against stale RSS state rather than not at
all. "A comparison that never happened" was inaccurate framing and is withdrawn.

Measured impact on current data: **6 excluded either way.** Adopted for
correctness, not for effect.

### The six states (finding 3)

Evaluated per miss, walking forward through eligible cycles only:

| state | definition | blocks? |
|---|---|---|
| `recovered_on_time` | seen in `feed_only` within `RECOVERY_HOURS` | no |
| `recovered_late` | seen in `feed_only`, but after `RECOVERY_HOURS` | **yes** |
| `pending` | age < `RECOVERY_HOURS`, not yet observed either way | **yes** |
| `unrecovered` | positively confirmed absent for ≥ `RECOVERY_HOURS` | **yes** |
| `evidence_gap` | vanished from both sets before `RECOVERY_HOURS`, never seen in `feed_only` | **yes** |
| `artifact` | recording cycle structurally ineligible | excluded |

```
qualification fails if
    recovered_late + pending + unrecovered + evidence_gap > 0
```

Revision 1 collapsed the middle four into "unrecovered / undetermined", which
conflated a release that arrived at 7 hours with one that never arrived. Both
block, but they are different failures and are now counted and reported
separately.

### Thresholds

`RECOVERY_HOURS = 6.0`, **fixed**, config key
`hdencode_rss_max_miss_recovery_hours`.

Deliberately *not* derived from observed cadence: a degrading feed would widen
its own cadence and so relax its own threshold. A fixed SLO cannot be gamed by
the thing it measures. 6.0 h is 46% above the worst case ever observed (4.1 h).

`hdencode_rss_min_recovery_observations = 3` — a miss must have at least three
eligible cycles after it before `recovered_on_time` is trusted, so a single
lucky observation cannot clear the gate.

### Undetermined never ages into passing

An unresolved miss is classified `unrecovered` or `evidence_gap` — both
blocking. It never becomes `recovered` through the passage of time. A blocking
`evidence_gap` older than 24 hours escalates as a distinct reason
(`evidence_gap_stale`) rather than sitting indefinitely.

---

## Finding 4: this is a one-time gate, not ongoing detection

**Confirmed in code, and it materially changes what promotion buys.**

[backend/background_scanner.py:449](../../backend/background_scanner.py) gates
the comparison on `discovery_mode == "rss_shadow"` exactly. Under
`rss_primary`, [line 400](../../backend/background_scanner.py) skips the
listing scan entirely (`continue`), so there is no listing set to compare
against — and line 449 would not record a comparison even if there were.

Consequences, stated plainly:

- After promotion, **no further shadow comparisons are recorded.**
- The criterion therefore **cannot detect a silent miss that begins after
  promotion.** It certifies the feed's behaviour up to the moment of promotion
  and nothing after.
- Even the `fallback_qualified` path — which does re-run a one-page listing
  scan — records no comparison, because the mode check is exact.

This is not an argument against promotion. It is a limit that must be written
down, because revision 1 implied the stop condition "survives", and after
promotion it does not survive; it stops being evaluated.

**Options, for the decision owner:**

1. Accept it — promotion is a one-time certification, and monitoring reverts to
   whatever non-shadow signals exist.
2. Add a low-frequency audit — periodically run a listing scan under
   `rss_primary` purely to record a comparison. Costs a small number of extra
   requests; restores ongoing detection.

Option 2 is a separate change and is **not** part of this spec.

---

## Required telemetry (finding 1)

Record the full RSS candidate set per cycle in `details_json` as `rss_urls`.

Without it, "in neither set" stays permanently ambiguous between *both sources
hold it* and *both dropped it*, and `evidence_gap` cannot be resolved even in
principle. With it, the full RSS set is reconstructable and every state becomes
positively determinable.

Review classes this as a prerequisite. Recorded as such, with one qualification
already stated above: **no conclusion in this document currently depends on
it**, because all 23 eligible misses resolved by positive `feed_only`
observation. It is required for the rule to be sound going forward — and, given
finding 4, it is only meaningful at all if a post-promotion audit (option 2) is
also adopted.

Historical rows lack the field. Cycles predating the change must classify as
`evidence_gap` rather than being retroactively assumed clean — except where a
positive `feed_only` sighting already exists, which remains valid evidence.

---

## What is deliberately NOT changed

- The other five qualification conditions — cycle count, observation days, feed
  health, request-reduction, restart recovery — untouched.
- The fail-closed structure in
  [hdencode_rss_service.py:104](../../backend/hdencode_rss_service.py) and
  [background_scanner.py:400](../../backend/background_scanner.py) stays exactly
  as is. If qualification fails, `rss_primary` refuses to run rather than
  degrading silently.
- No change to shadow comparison logic, feed fetching, or grabbing.
- Auto-rename remains paused. Unrelated, and unaffected.

---

## Implementation sequence

Ordered per review. **`rss_primary` is not enabled at any point in steps 1–5.**

| step | change | risk |
|---|---|---|
| 1 | Cycle-eligibility correction only (structural, per finding 2) | low — correctness fix, stands alone |
| 2 | Read-only recount + reconciliation table | none — no behaviour change |
| 3 | Full `rss_urls` telemetry in `details_json` | low — additive write |
| 4 | Six-state recovery helper + unit tests | none — not yet wired to the gate |
| 5 | Read-only live evaluation; report all six counts | none — reported, not enforced |
| 6 | Wire to the gate; promotion **only** if `recovered_late`, `pending`, `unrecovered`, `evidence_gap` are all 0 | gated on step 5 evidence |

Step 1 is independently correct and can land alone even if the rest is rejected.

---

## Risks

1. **This is a criterion change made after seeing the results.** Stated plainly
   because it is the main objection. The defence is that the measurement answers
   a question the original rule was guessing at, and the new rule is strictly
   more specific — it still fails on the thing the old rule was protecting
   against. It is a narrowing to "misses that never resolve matter", not a
   loosening to "no misses matter".
2. **Sparse detection.** A release is typically visible as missing for a small
   number of cycles before it resolves or leaves the listings page. Positive
   `listing_only` observations cover that interval, but the `rss_urls`
   telemetry in step 3 is what makes coverage complete.
3. **Threshold selection.** 6.0 h is chosen from a 5.4-day sample with a 4.1 h
   maximum. A longer observation could surface a slower case. Configurable, so
   it can be tightened without a code change.
4. **One-time certification.** Per finding 4 — the gate says nothing about
   behaviour after promotion.
5. **Deployment cost.** One container recreate. Should be batched with the
   pending model repoint (TL-010) rather than spent alone.

---

## Verification before enabling

1. Unit tests for the helper, one per state: recovered on time; recovered late;
   pending; unrecovered; evidence gap; artifact excluded.
2. Run the criterion read-only against the live database and confirm the six
   counts match the evidence table above.
3. Confirm the other five conditions still evaluate identically.
4. Only then set `hdencode_discovery_mode = rss_primary`, and confirm the first
   cycle actually runs rather than skipping with `primary_not_ready`.
5. Confirm listing fallback still engages when RSS coverage is uncertain.

---

## Open question for the decision owner

Given finding 4 — that promotion ends shadow comparison — the choice is between
promoting on a one-time certification, or first adding a post-promotion audit so
that miss detection continues. That decision belongs to Jesse and is not made
here.
