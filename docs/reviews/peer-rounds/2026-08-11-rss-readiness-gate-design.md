# Decision record + design — RSS promotion via a coverage-canary hybrid

**Repository:** `LstDtchMn/ScanHound`
**Status:** DECISION + design outline (no build yet). Date: 2026-08-11.
**Supersedes:** the per-URL-acknowledgement proposal previously in this file (see "Rejected"),
which BOTH peer reviews rejected.

## Decision

RSS is fully built and running as a shadow evaluator (mode `rss_shadow`). It performs well
(287 cycles / 17 days, `never_acquired = 0`, worst lag 4.06h, ~84% fewer discovery requests if
promoted), but the promotion path is blocked and the safe way to promote is NOT the original
per-URL-ack design.

**Chosen direction — a coverage-canary hybrid ("D"), agreed by both peer reviews:**

> Promote RSS to acquisition-primary under an explicit *provisional* state, while retaining a
> reduced-frequency listing scrape purely as an independent **coverage canary**, with **automatic
> reversion** to `rss_shadow` on any demonstrated `never_acquired` or a systematic-gap guard firing.

The hybrid's whole point: **you never have to prove RSS is complete.** RSS drives the fast path; the
canary catches whatever RSS misses and preserves the miss-detection evidence that pure `rss_primary`
would destroy. This makes the 8 "undetermined" blockers, the acknowledgement mechanism, and the
frozen-cohort-as-gate all unnecessary.

**Timing / go-no-go:**
- **NO-GO today.** The ~9 `not_yet_assessable` rows are genuine fresh uncertainty; let them resolve.
- **GO** on the hybrid once the only remaining blockers are the 8 Gun Stories historical unknowns —
  AND only after the hybrid is built and reviewed.
- **NO-GO** on today's blind `rss_primary` (option B) — it throws away the coverage alarm for the last
  slice of the saving.
- **Not** indefinite shadow (option A alone) — that gives unknowable historical cases more weight than
  17 days of clean evidence warrant.

This is a build for a *convenience* payoff (request reduction + faster pickups), not a capability gap
— listing mode acquires everything today with zero misses. Build it when it is actionable (pending
rows cleared) and the efficiency is wanted.

## Why the original per-URL-ack design was rejected (both peers)

1. **`undetermined` is not structurally terminal.** `classify_miss_resolution` recomputes each row
   against later cycles, so a re-listed URL can reclassify. Acknowledgement must never outrank the
   live classifier; current state is authority.
2. **Per-URL ack can hide a systematic category gap.** If RSS silently drops an entire population,
   every member pages off as `undetermined` and NONE becomes `never_acquired` (that needs a later
   `listing_only` sighting they never get). Acknowledging them all yields `ready=true` while RSS
   carries 0% of that category — a new fail-open at the population level.
3. **It conflates operator risk-acceptance with empirical readiness.** `ready=true, reasons=[]` must
   not mean the same thing for "all evidence resolved" and "operator waived unresolved evidence."

The hybrid sidesteps all three: it does not try to declare empirical completeness at all.

## The design

### 1. Provisional primary mode (`rss_primary` + canary)
- RSS is the acquisition source (the fast path), as today's `rss_primary`.
- A **scheduled** limited listing scrape runs independently of RSS health (unlike the existing
  `fallback_qualified` fallback, which fires ONLY on `coverage_uncertain` and requires
  `readiness["ready"]` — a transient-error fallback, not a canary). This scrape:
  - runs `compare_shadow` and writes the SAME shadow evidence while RSS is primary (today that only
    happens in `rss_shadow` — this is the core change that keeps the instrument alive after promotion);
  - acts as a fallback acquisition path for anything the canary finds that RSS missed.

### 2. Canary interval — the load-bearing parameter (ChatGPT's key refinement)
A canary only protects you if a release stays visible on the listing **longer than the canary
interval**. Gun Stories proves items page off fast; if a category appears and vanishes between two
sweeps, RSS misses it and the canary never sees it.

> **Invariant: canary interval < credible listing-visibility window.**

The visibility window is **measurable from existing shadow data** — how many consecutive valid cycles
a URL persists in `listing_only` before disappearing. Derive the window empirically (e.g. the low
percentile of observed residence), set the canary interval safely below it, start faster (every few
hours) and relax after measuring. If no such window can be established, a fixed cadence is only
occasional sampling, not a safety net — and that must be surfaced, not assumed.

### 3. The 8 Gun Stories = a documented, operator-accepted historical cohort
Freeze them as explicitly accepted historical uncertainty. They are **not** relabeled `acquired` and
**not** "proven safe" — empirical readiness keeps telling the truth. The risk acceptance covers the
frozen historical unknowns only, not future blindness (the canary handles the future).

### 4. Automatic reversion
Demote `rss_primary` → `rss_shadow` on:
- a *proven* `never_acquired` (a real gap), or
- a systematic-gap guard firing (a stratum with listing evidence > 0, RSS acquisitions = 0),
but **not** merely because one listing sighting beat RSS (RSS may legitimately lag hours).

### 5. Canary health is part of the safety claim
A stale/broken canary = degraded protection. Do not run for long labeled "canary-protected" if the
canary has not successfully observed the listing recently; surface it and treat it as a demotion
signal.

## Open implementation questions (for the build round)
- Where the scheduled canary lives (background_scanner) and how its cadence is configured/derived.
- The exact `compare_shadow`-in-`rss_primary` wiring (today gated on `discovery_mode == rss_shadow`).
- The systematic-gap guard's stratum definition — using ONLY source-owned structured provenance
  (category / media_type / feed attribution), never title/name/URL/JSON heuristics; and what to do
  when the provenance is too coarse to make a category claim (conservative: cannot claim coverage).
- The provisional-state surfacing in `GET /rss/status` so promotion is visibly a provisional,
  canary-protected, operator-accepted state — not "empirically ready".

## Peer trail
- Claude design (per-URL ack) → ChatGPT review: request-changes (systematic-gap fail-open).
- Claude go/no-go: A now / hybrid if efficiency matters, grounded in the code (existing fallback is
  transient-error-only, readiness-gated).
- ChatGPT go/no-go: "D" (the hybrid), NO-GO today, canary-interval-< -visibility-window, auto-demote,
  document (not relabel) the 8; redirect PR #61 off per-URL-ack toward retained observability.
- Both peers converged on the hybrid; this record is that convergence.

---

# Gate restated — 2026-08-19 (re-measured at 585 cycles)

The original go/no-go was written against 287 cycles / 17 days. The shadow has
now run **585 cycles over 28 days** (2026-07-22 → 2026-08-19). Re-measuring
changed the decision, and corrected a mistake in how the first pass counted.

## The counting mistake

The headline "misses" figure conflates two different things. Separating them:

| | count |
|---|---|
| Total recorded misses | **201** |
| RSS caught up on a later cycle — **lag, not a gap** | **189** |
| **Never acquired by RSS at all** | **12** |

Lag distribution for the 189: **median 1.1h, p90 2.1h, max 15.2h.** Cycles run
~67 min apart, so the typical "miss" is one cycle of delay.

A rate quoted from the 201 describes how often RSS is *briefly behind the
listing*, not how often it *loses something*. Those want opposite responses:
lag is what the reduced-frequency canary is for, whereas a permanent gap is the
thing that must block promotion.

## What the 12 actually are

```text
2026-07-30   pallichattambi-2026-2160p-sonyliv-web-dl
2026-08-07   gun-stories-s04 … s14   (8 releases, the documented cohort)
2026-08-10   a-man-on-the-beach-1956-2160p-uhd-bluray   (x2, two release groups)
2026-08-10   x-the-unknown-1956-ar-1-66-2160p-uhd-bluray
```

Two things stand out.

**Nothing has been permanently missed since 2026-08-10 — nine days clean.**

**Eleven of the twelve are back catalogue.** The Gun Stories cohort is marked
`archived`; the 2026-08-10 group are 1956 films posted as UHD Blu-ray rips. Only
Pallichattambi is a current release. The feed carries what is newly posted; a
bulk archive upload does not reliably reach it.

That is a *shaped* gap, not a random one, and it is the strongest argument the
shadow has produced for the hybrid: the canary is not insurance against RSS
being unreliable in general, it covers a specific and explicable blind spot.

## The gate, restated

The original condition — *"GO once the only remaining blockers are the 8 Gun
Stories historical unknowns"* — cannot be evaluated as written, because it names
a fixed cohort while the population keeps growing. Four more have appeared since
it was written, and by its own terms that is a permanent NO-GO even though all
four are nine or more days old.

Replace it with a condition that stays meaningful as evidence accumulates:

**GO when all three hold:**

1. **No new `never_acquired` release for 14 consecutive days.** Measured as it
   is here: a missed URL that appears in no later cycle's `feed_only` or
   `duplicate_urls`. Today this stands at **9 days**.
2. **Canary interval < the listing's real visibility window.** Unchanged from
   the original design, and still the load-bearing constraint.
3. **The hybrid is built and reviewed.** Also unchanged. Nothing here promotes
   anything on its own.

**Automatic reversion to `rss_shadow`** on any single proven `never_acquired`,
which is the original design's reversion trigger and is not weakened by this.

The known 12 are grandfathered as an accepted historical cohort — the same
treatment the original gave the 8 — and are never relabelled acquired or safe.

## Efficiency, re-measured

The original quoted ~84% fewer discovery requests. Measured across the 499
cycles that completed normally: **64.3%** (1,034 RSS requests against 5,073
listing requests). Still a large saving, but the smaller number is the one to
decide on.
