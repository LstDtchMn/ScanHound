# A5 — predeclared qualification thresholds

**Status: PROPOSED, awaiting Jesse's approval. Not yet binding.**
**Written 2026-08-01, before any qualification window has been started.**

Plan rev 2.1 §A5 requires these to exist **before** the window opens, because
"without these written first, the analysis can describe data but cannot produce
a disciplined verdict." ChatGPT's §7.2 covers most of the list; two items on it
were never given numbers, and those are the point of this document.

Each threshold below is marked **MEASURED** (derived from observed data) or
**CHOSEN** (a judgement with no measurement behind it). That distinction is
here because an invented constant already had to be disabled once this session
after being presented as if it were calibrated.

---

## The two that were missing

### T1 — Accepted acquisition latency

> How late may a release be and still count as acquired?

| statistic | threshold | basis |
|---|---|---|
| median (p50) | **≤ 2 h** | MEASURED — observed median was 1.02 h across 99 acquisitions |
| 95th percentile | **≤ 6 h** | MEASURED — 6 h is the existing GREEN band in design §8 |
| maximum | **≤ 24 h** | MEASURED — beyond 24 h is RED, already a hard fail |

Measured **from `first_normal_at`**, never from a catch-up or sweep
observation. A release reached only by catch-up has not been acquired by the
feed under test.

**Rationale.** These are not new bars; they restate what the gate already
enforces, at percentiles the measured data already met. If the fresh window
cannot meet a standard the void window met, that is a real regression rather
than a stretch goal.

### T2 — Acceptable classification / hydration failure rate

> A URL arriving is not the point. The candidate must reach the same actionable
> decision the listing path produced (§A4).

**Threshold: ZERO actionable candidates left unresolved at window closure.**

* a transient failure that later succeeds inside the window does **not** count
  against this;
* a candidate that ends the window unable to be identity-parsed, classified,
  hydrated, or compared against the library **blocks the pass**;
* `ambiguous_identity` and `processing_failed` already block in the gate, so
  this makes the existing behaviour an explicit promise rather than an
  implementation detail.

**CHOSEN, and deliberately strict.** I considered a small tolerance (e.g. ≤1%)
and rejected it: with roughly 200 cycles' worth of candidates, 1% is a couple of
releases silently written off, and no one would investigate them. Zero-at-
closure is enforceable and leaves transient noise unpunished. **If this proves
impractical in the window, the right response is to relax it deliberately and
record why — not to discover the tolerance after seeing the data.**

---

## The rest, restated so the whole bar is in one place

| # | Threshold | Source |
|---|---|---|
| T3 | ≥ 7 calendar days of evidence, all from the corrected build | ChatGPT §7.2 |
| T4 | ≥ 20 valid comparison cycles | ChatGPT §7.2 |
| T5 | Zero relevant misses **inside the window** | §A5 + §7.2 |
| T6 | Zero PENDING and zero AMBIGUOUS at closure | §A5 |
| T7 | No negative coverage margin at ordinary cadence — or a catch-up that provably closes it **within the same window** | §A5 |
| T8 | ≥ 1 restart recovery and ≥ 1 missed-poll/catch-up recovery, both demonstrated | §A5 + §7.2 |
| T9 | Application readiness reconciliation available and agreeing for **every** counted cycle | §A5 ("no pass when reconciliation is unavailable") |
| T10 | Measured request reduction ≥ 50 %, target 70 % | design §10 |
| T11 | Healthy required feeds; every required source interval `current` | design §10 |
| T12 | No database integrity failure or other mandatory stop | §7.2 |
| T13 | Auto-grab and auto-rename OFF throughout | standing gate |

**T7 note.** `tv_all` already showed one negative-margin cycle at −0.12 h in the
void window. That is the single threshold most likely to fail on merit, and it
should fail rather than be waived quietly.

---

## What is NOT covered, and must not be claimed

* **Volume-anomaly detection does not operate.** `expected_typical` has no
  producer, so the check is disabled. No threshold here depends on it, and the
  qualification report must not imply the protection exists.
* **A4 suitability is asserted by T2, not yet demonstrated.** Nothing has proven
  end-to-end that an RSS-discovered candidate reaches the same actionable
  decision as a listing-discovered one. T2 makes that a pass condition; it does
  not make it tested. If you want it tested before the window, that is
  additional work and should be said now.

## Changing these

Once the window opens, changing a threshold invalidates the window in the same
way moving the boundary does. If one of these turns out to be wrong, the honest
move is to close the window, record why, fix the threshold, and start a new one.

---

## For approval

The two decisions that are genuinely yours:

1. **T1** — is "typically within 2 hours, always within 24" the standard you
   want for how quickly a new release must be noticed?
2. **T2** — should a single release the app cannot fully process block the
   whole pass, or would you rather allow a small number and review them by
   hand?

Everything else restates bars already agreed in the design or by ChatGPT.


## Population identity (added 2026-08-03, inventory §5.4)

The Phase A acquisition population is the set of **Form-A post identities**
(`canonicalize_hdencode_post_url`, version `hdencode-post-v1`) in
`hdencode_candidates`. The miss ledger and policy-exclusion store key on
**Form B** (`canonicalize_listing_url`, `listing-v1`). Every instrument that
joins the two MUST use the named bridge `post_to_listing_identity` (or the
producer-consistent membership already inside `compare_shadow` /
`miss_resolution.py`) and state its denominators. No threshold may be graded
from a join that has not declared its bridge.
