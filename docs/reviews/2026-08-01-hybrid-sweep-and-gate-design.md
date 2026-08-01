# Hybrid sweep + lag-aware gate — design rev 2.1

**Date:** 2026-08-01 · **Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
**Status: implementation authorised** (ChatGPT round 6, conditional on this
revision). RSS-primary promotion and auto-grab remain blocked.

Rev 2.1 applies twelve exact requirements. Four touched the analysis scripts and
are committed at `3b9b4a3`; the remaining eight are below.

---

## 0. Corrected measurements (round 6)

Horizon is `observation − oldest`, **not** `newest − oldest`. Observation happens
at or after the newest publication, so horizon ≥ span and every previously
published margin **understated** the real headroom.

| Feed | min coverage horizon | min margin | negative cycles | recon exact |
|---|---|---|---|---|
| `movies_all` | 5.44 h | **+3.37 h** | 0 | 100.0% |
| `tv_all` | 2.82 h | **−0.12 h** | **1** | 98.8% |

`tv_all` dips negative by **seven minutes on one cycle**. The two imperfectly
reconstructed cycles do **not** drive that minimum — excluding them gives the
identical −0.12 h.

Latency is measured from `first_normal_at`. Result unchanged, because
first-normal-feed membership equals global acquisition for all 99 — now
demonstrated rather than assumed.

---

## 1. Sweep completion — CONJUNCTIVE

Ordinary completion requires **all** of:

```
complete =
    timestamp_target_crossed
AND source_known_frontier_crossed
AND clean_older_page_observed
AND all_discoveries_durably_persisted
AND parser_structure_valid
AND no unresolved request/page failure
```

Stopping when *any one* signal fires recreates silent undercoverage. On a quiet
source page 1 may satisfy all three primary conditions — that is correct and a
minimum page count would add cost, not evidence.

**An unexpectedly empty or structurally changed page is a parser failure**, never
"no unseen identities".

**The deeper audit is a separate, less-frequent control**, not a fourth stop
signal. It deliberately traverses beyond the normal target to detect late
insertion and backdating.

---

## 2. Known frontier = per-source listing ledger

The frontier must come from a durable ledger of what *this listing source* has
been observed to contain. A URL known only via RSS, or via another source, proves
nothing about traversal depth on this one.

Key: `(source_key, canonical_url)`

Fields: first/last listing observation · displayed publication time · first/last
page index seen · raw and canonical URL · **canonicaliser version** · title and
status snapshot · sweep session UUID · persistence status.

Candidate persistence proves the *product* retained a discovery. The source
ledger proves *listing traversal history*. Not interchangeable.

---

## 3. One logical session across continuation

`coverage_through` is set from the **original logical sweep-session start `S`**,
preserved across every continuation, restart, lease reacquisition and authorised
deep catch-up. It is **not** reset when the page cap is reached or the process
dies.

Persist one `sweep_session_uuid`: original start · source · prior target ·
overlap · continuation frontier · lease owner and expiry · attempt count ·
terminal status.

```
session_started_at = S
stop target        = prior coverage_through − overlap
on success         → coverage_through = S     (atomic commit)
on failure         → coverage_through unchanged
```

---

## 4. Concurrency, leases and replay safety

- **one active session per source**; no overlapping sweeps for a source
- durable **lease** with owner and expiry
- **idempotent** resume after restart — replay-safe page and URL handling
- **page number is not a stable anchor**; resume from a timestamp/URL frontier
  with overlap, because pages shift as new items are published
- page cap advances **continuation state only**, never `coverage_through`
- terminal success **atomically** commits the watermark
- a backlog larger than the cap must be recoverable across attempts

---

## 5. Bootstrap = 30 hours

Derived, not chosen: **24 h RED boundary + 6 h initial overlap = 30 h.**

Applies to every required listing source. Fail **incomplete** if the page cap is
hit first. **The clean qualification clock starts only after every source
completes bootstrap successfully.**

48 h is defensible as extra-conservative; 30 h is the first value derived from
the declared bands.

---

## 6. Overdue stays overdue during recovery

```
due_at     = coverage_through + 6 h
overdue_at = due_at + 1 h
```

**Do not suppress `overdue` because a sweep or continuation is running.** Expose
compound states — `running_overdue`, `incomplete_overdue` — so an in-progress
recovery cannot mask staleness.

Measure run and continuation duration during qualification, then revisit the
grace only if evidence supports it.

---

## 7. Listing-volume measurement — blocks promotion, not coding

Implementation proceeds with **15 pages as a provisional per-attempt circuit
breaker**, durable continuation, and fail-closed health.

**Before promotion**, measure per source: usable page capacity · rolling volume ·
order inversions · displayed-date lag · pages per logical session · cap hits ·
continuation frequency · audit findings.

The previously cited 134-item burst remains **withdrawn** — it was measured from
RSS candidates, not listing pages.

---

## 8. State models

**RSS acquisition** (measures the FEED): `pending` <6 h · `green` normal-feed
≤6 h · `yellow` 6–24 h · `red` >24 h · `ambiguous`

**Identity coverage** (measures the PRODUCT): `covered_by_rss` ·
`covered_by_sweep` · `rss_red_covered_by_sweep` · `ambiguous_identity` ·
`processing_failed`

**Source interval** (replaces per-identity `pending_sweep`): `current` · `due` ·
`running` · `incomplete` · `overdue` · `degraded` · `unknown`, plus the compound
overdue states above.

A RED RSS item recovered by a complete sweep is an **RSS-health metric, not an
uncovered release**.

---

## 9. TV 30-minute experiment — predeclared outcomes

Approved as a qualification experiment. **Success is not "margin becomes +0.25"**
— that was a counterfactual on the same observed body. Predeclared:

- normal-feed lag stays within 6 h
- successful-observation gaps shrink
- no meaningful rate-limit or coordinator contention
- request floor still met
- no increase in failures
- horizon and saturation measured directly after instrumentation

Failure or restart requires **adaptive immediate recovery**, not waiting out the
next tick.

---

## 10. Promotion requirements

Every required source interval `current` · no `incomplete`/`overdue` sweep · all
discoveries durably in the candidate pipeline · no `ambiguous_identity` · no
persistence failure · **no watermark advance after partial persistence** ·
restart recovery proven · one induced missed poll recovered · one induced
incomplete sweep recovered · reconciliation **fail-closed** · request floor met ·
listing-volume evidence exists · auto-grab **off**.

**Request cost:** baseline = matched listing-only operation; count all HDEncode
requests including retries and continuations; **minimum 50% reduction**, target
70%; no increase in missed actionable discoveries; no rate-limit breach. Below
50% fails promotion regardless of coverage.

---

## 11. Implementation order

1. Schema: source listing ledger, sweep sessions, per-source watermark + lease
2. Sweep engine: conjunctive completion, continuation, atomic commit
3. Interval health + compound overdue states
4. Lag-aware gate over the three state models
5. `#191` RSS full-disc symmetry
6. Parser structural-failure detection (scraper layer, not documentary-specific)
7. Collector networking + fail-closed reconciliation
8. Tests: one-cycle lag · 6–24 h yellow · >24 h RED recovered by sweep · burst
   spilling to page 2 · missed sweep · task delay · parser returns unexpected
   empty · restart mid-sweep · canonical variants · normal vs catch-up
   acquisition · stale readiness endpoint
9. Deploy, then bootstrap all sources, then start the clean window
