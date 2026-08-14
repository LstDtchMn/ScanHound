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
