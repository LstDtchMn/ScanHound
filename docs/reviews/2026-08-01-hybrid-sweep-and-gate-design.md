# Hybrid sweep + lag-aware gate — design

**Date:** 2026-08-01 · **Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
**Closeout steps 4–6.** Design only — no code written, nothing deployed.
**Supersedes** the fixed page-1 sweep, which the burst measurement disproved.

---

## 1. Why the previous design failed

| Claim I published | Measured truth |
|---|---|
| `tv_all` depth 9.8 h | **min 1.29 h** (p5 4.18, p50 10.86) |
| margin +4.0 h, both safe | **min −0.95 h — two cycles negative** |
| outage headroom 9.8 h | **1.29 h** |
| page 1 sufficient at 6 h | busiest 6 h published **134 releases**; page 1 holds ~30 |

The 9.8 h was one current snapshot standing in for a history. Page 1 is short by
more than 4× against the observed peak — and a fixed page count fails *silently*,
which is the same shape as the full-disc bug.

`movies_all` is materially healthier: min depth 4.81 h, min margin +2.74 h, zero
negative cycles. **`tv_all` is the binding constraint** and carries 63% of misses.

---

## 2. Watermark-based variable-depth sweep

**Cadence:** every 6 hours (Jesse, unchanged — the cadence survived, the depth
rule did not).

Per listing source (4K Movies, Remux Movies, TV Packs):

1. Read `last_successful_sweep_boundary` — the newest publication time proven
   covered by a **completed** sweep.
2. Crawl from page 1 forward.
3. Stop when the crawler reaches items older than that boundary **minus a
   safety overlap** (proposed: 1 h, to absorb out-of-order and backdated posts).
4. Process every actionable item found.
5. **Advance the watermark only after every required source crossed its
   boundary.** Partial success advances nothing.
6. On page cap, parser error, transport error or cancellation: mark the sweep
   **incomplete**, keep the old watermark, alert, retry. Do **not** count it as
   coverage.
7. Record: pages crawled, oldest publication reached, boundary crossed y/n,
   discoveries per source, RSS-missing items recovered, request count, duration,
   failure reason.

**Page cap is a circuit breaker, not a completion criterion.** Hitting it means
*incomplete*, never *done*. Proposed cap 15 pages (~450 items, 3.4× the observed
peak) purely to bound a runaway.

**Cost profile:** page 1 only during quiet periods — which is almost always,
since the crawl averages 3.5 new items per cycle. Depth is spent only when
actually needed.

### Why this is required rather than preferable

A fixed page count is sized to the busiest window *observed in one 9.6-day
sample*. The watermark is sized to **what actually happened since the last
success**, so a burst, a late start, or a skipped run all self-correct. Nothing
has to predict the peak.

---

## 3. TV polls more often than movies

**Jesse's decision, from the measurement.** `tv_all` is ~4× shallower than
`movies_all` at the tail (1.29 h vs 4.81 h minimum) and produced the only two
negative-margin cycles. Treating both feeds identically ignores a measured
difference.

Proposed: **TV every 30 minutes, movies unchanged at ~1.2 h.**

Rationale: halving the TV gap moves the worst observed margin from −0.95 h to
roughly +0.25 h — the two negative cycles become non-negative. It does not create
headroom for a long outage; that is the sweep's job.

**Open — needs measurement, not assumption:** whether a 30-minute TV poll changes
`tv_all`'s observed depth. If HDEncode regenerates on a timer, depth is
independent of our polling. If it regenerates on write, more frequent polling
sees shallower bodies and gains less than expected.

---

## 4. Two state models, deliberately separate

Collapsing these was the original gate's mistake.

### 4.1 RSS acquisition state — measures the FEED

| State | Meaning |
|---|---|
| `pending` | listing saw it; accepted latency not yet elapsed |
| `green` | acquired by a **normal** feed within 6 h |
| `yellow` | acquired 6–24 h |
| `red` | no normal-feed acquisition after 24 h |
| `ambiguous` | evidence cannot establish identity or acquisition |

### 4.2 Hybrid coverage state — measures the PRODUCT

| State | Meaning |
|---|---|
| `covered_by_rss` | RSS acquired it inside the band |
| `covered_by_sweep` | the listing sweep recovered it |
| `pending_sweep` | RSS missed it; next sweep not yet run |
| `unresolved` | neither path has it and both have had their chance |
| `ambiguous` | cannot be established |

**A RED RSS item recovered by a complete sweep is an RSS-health metric, not an
uncovered release.** That distinction is the whole point of the hybrid: it lets
RSS be imperfect without the product losing anything.

Persist per identity: normalised URL, first/last miss time, miss cycle UUID,
first acquiring feed, first acquisition time, resolution latency, terminal
classification, **evidence version and canonicaliser version**.

---

## 5. Promotion requirements

- minimum clean cycles and wall-clock duration
- **zero hybrid `unresolved`**
- zero hybrid `ambiguous`
- zero expired `pending_sweep`
- all normal feeds healthy and fresh
- every sweep in the window crossed its watermark
- restart recovery proven
- **one induced outage or skipped poll, recovered**
- request reduction still positive **after sweep cost**
- application/database reconciliation **fail-closed**
- auto-grab remains **off**

Yellow is surfaced, never silently counted as green.

---

## 6. Request cost — the honest position

RSS-primary claimed ~89% request reduction. The sweep spends some back.

Measured inputs: 172 normal-feed polls per feed over 9.6 days (~18/day each);
the listing crawl early-stops at page 1 on most cycles.

**I have not computed the net figure**, because it depends on the watermark's
real behaviour — how often a sweep needs more than page 1 — and that cannot be
derived from a window in which no sweep ran. It must be **measured during the
qualification window**, not estimated now.

Stating it as unknown rather than producing a plausible number, because a
plausible number here would be exactly the kind of unverified figure that has
gone wrong three times this session.

---

## 7. What I want attacked

1. **Is the 1-hour safety overlap right?** Chosen by judgement, not measurement.
   Are backdated or out-of-order posts common enough on HDEncode to need more?
2. **The 30-minute TV poll may gain nothing** if depth is independent of polling
   frequency. Is there a way to establish that from retained evidence, or does it
   need an experiment?
3. **Is a 15-page circuit breaker sane** given the observed 134-item peak, or does
   it risk masking a genuine runaway?
4. **`pending_sweep` expiry** — I have not proposed a duration. Should it be the
   sweep interval plus one, or absolute?
5. **Is deferring the request-cost number defensible**, or should promotion
   require a predeclared floor even if measured later?
