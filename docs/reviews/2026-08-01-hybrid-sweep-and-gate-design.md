# Hybrid sweep + lag-aware gate — design rev 2

**Date:** 2026-08-01 · **Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
**Closeout steps 4–6, revision 2.** Design only — no code, nothing deployed.
**Rev 1 superseded**: it used per-identity `pending_sweep` (structurally
impossible), a 1-hour overlap (unjustified), a page cap with no continuation
(unrecoverable), and sized the sweep with a burst figure measured from the wrong
dataset.

---

## 1. Withdrawn from rev 1

| Rev 1 claim | Status |
|---|---|
| 134-item burst sizes the sweep | **Withdrawn.** Measured from `hdencode_candidates` — the RSS-ingested union. The sweep traverses *listing pages*. Wrong population. |
| `pending_sweep` per identity | **Withdrawn.** An upstream omission is unknown until the sweep finds it, so no identity row exists to mark pending. |
| 1-hour overlap | **Withdrawn.** Judgement, not measurement. Start at one full sweep interval. |
| Page cap = incomplete, retry | **Insufficient.** Without continuation, a backlog larger than the cap restarts at page 1 forever. |

The watermark architecture survives — it never depended on the burst number,
which is exactly its advantage over a fixed depth.

**Also corrected in the measurement scripts** (commit `573f3de`): failed polls no
longer reset the observation gap; the reconstruction is validated against
recorded entry counts; `outage_headroom` renamed to
`minimum_observed_feed_horizon_h`; missing attribution now exits nonzero.

---

## 2. Measured basis

| Feed | min depth | min margin | negative cycles | reconstruction exact |
|---|---|---|---|---|
| `movies_all` | 4.81 h | +2.74 h | 0 | 100.0% |
| `tv_all` | **1.29 h** | **−0.95 h** | **2** | 98.8% |

Saturation: 100% of changed HTTP-200 bodies held 50 persisted candidates
(`movies_all` 171/171, `tv_all` 172/172).

`tv_all` is the binding constraint: shallowest, the only feed with negative
margin, and it carries 63% of misses.

---

## 3. Sweep — watermark, variable depth

**Cadence:** every 6 hours (Jesse).

### 3.1 Per-source durable state

Each listing source (4K Movies, Remux Movies, TV Packs) owns independent state:
`coverage_through`, prior successful sweep id, last successful start/completion,
oldest item reached, overlap used, continuation state, health.

One source failing degrades **overall hybrid health** but must not force healthy
sources to re-crawl forever.

### 3.2 Boundary — defined by sweep START, not newest item

For a source sweep started at `S`:

```
stop target   = prior coverage_through − overlap
on success    → coverage_through = S
```

Deriving the boundary from the newest item seen would let a quiet period ratchet
coverage forward without evidence. `S` is what we can prove we looked at.

Advance **only** when the prior target was crossed, every discovered identity was
**durably persisted**, and the parser completed without structural uncertainty.
Advance after durable candidate persistence — **not** after hydration or action.

### 3.3 Overlap = one full sweep interval (6 h) initially

Rev 1's 1 hour was invented. Six hours is deliberately generous; reduce only once
measured disorder supports it.

**Measure during qualification**, per source: `listing first_observed_at −
displayed publication time`, and page-order inversions. Choose the overlap from
the observed tail plus clock skew.

**Stated limitation:** no finite overlap is safe against arbitrary backdating.
Monitor that assumption rather than assume it away.

### 3.4 Four stop signals, not one

1. cross the timestamp boundary minus overlap
2. cross a durable known-URL frontier
3. one complete older page containing no unseen identities
4. periodic deeper audit sweep for late insertions

### 3.5 Page cap = circuit breaker WITH continuation

Cap 15 pages per **attempt**, never a total recovery ceiling.

On cap, persist continuation state — target boundary, oldest timestamp reached,
anchor URL / crawl-session ledger, attempt count — then schedule a bounded
continuation resuming with overlap. Escalate after a predeclared attempt count.
Operator-authorised deep catch-up permitted.

Without this, a backlog exceeding the cap can never be cleared.

### 3.6 Bootstrap

No prior watermark: crawl each source to **at least 24 h before qualification
start**, persist every identity, fail **incomplete** if the cap is hit first, set
`coverage_through = S` only on success.

---

## 4. TV polls more often — a qualification experiment

**30 minutes for TV**, movies unchanged (~1.2 h). A *controlled qualification
parameter*, not a proven configuration.

Rev 1's claim that halving cadence moves the worst margin from −0.95 h to +0.25 h
is a **counterfactual on the same observed body**, not an expected production
result. Labelled as such.

Measure: 200/304 ratio, acquisition-lag distribution, successful-observation gap,
minimum observed feed horizon, denials and rate limits, coordinator contention,
request cost.

Failure or restart requires **adaptive immediate recovery** — not waiting out the
next 30-minute tick.

**Still open:** if HDEncode regenerates feeds on a timer rather than on write,
faster polling only sees shallower bodies and gains nothing.

---

## 5. Three state models

### 5.1 RSS acquisition state — measures the FEED

`pending` (<6 h) · `green` (normal-feed acquisition ≤6 h) · `yellow` (6–24 h) ·
`red` (no normal-feed acquisition after 24 h) · `ambiguous`

Latency measured from **first normal-feed membership**, not global candidate
`first_seen_at`, because the gate is defined around normal-feed acquisition.

### 5.2 Identity coverage state — measures the PRODUCT

`covered_by_rss` · `covered_by_sweep` · `rss_red_covered_by_sweep` ·
`ambiguous_identity` · `processing_failed`

A RED RSS item recovered by a complete sweep is an **RSS-health metric, not an
uncovered release**. That distinction is the point of the hybrid.

### 5.3 Source interval state — replaces `pending_sweep`

Per source, not per identity, because an undiscovered release has no row:

`current` · `due` · `running` · `incomplete` · `overdue` · `degraded` · `unknown`

```
due_at     = last successful coverage_through + 6 h
overdue_at = due_at + 1 h execution grace
```

The 7-hour boundary is **aggregate source freshness**, not a per-identity timer.

---

## 6. Promotion requirements

- every required source interval `current`; no `incomplete` or `overdue` sweep
- all sweep discoveries durably entered into the candidate pipeline
- no `ambiguous_identity`, no persistence failure
- **no watermark advance after partial persistence**
- restart recovery proven
- one induced missed poll recovered
- one induced incomplete sweep recovered
- application/database reconciliation **fail-closed**
- request-cost floor met (below)
- auto-grab remains **off**

---

## 7. Request cost — floor predeclared, number measured

Deferring the *measured* number is correct; deferring the *acceptance rule* was
not. Predeclared now:

- **baseline:** matched listing-only operation over the same window
- **counted:** all HDEncode HTTP requests including retries and sweep continuations
- **minimum:** ≥50% fewer requests than baseline
- **target:** 70% reduction
- **hard conditions:** no increase in missed actionable discoveries; no
  coordinator or rate-limit breach

Falling below 50% fails promotion regardless of coverage.

---

## 8. Open questions

1. Is `coverage_through = S` right, or should it be the start of the *oldest page
   fetched* to be conservative under long crawls?
2. Stop signal 3 requires "one complete older page with no unseen identities" — on
   a quiet source that is page 1 every time. Sufficient, or does it need a
   minimum page floor?
3. Should the 24-hour bootstrap be longer for TV given `tv_all`'s 1.29 h horizon?
4. Is 7-hour `overdue_at` right when a failed attempt plus continuation could
   plausibly exceed 1 h of grace?
5. The listing-page burst still needs measuring from listing evidence. Block
   implementation, or run alongside?
