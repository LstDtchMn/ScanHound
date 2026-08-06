# Peer review request — RSS miss accounting, Round 3

**Round:** 3 (response to REQUEST CHANGES at `9bba55a`)
**Branch:** `agent/rss-miss-accounting`
**Base:** `main` @ `d909b44`
**Date:** 2026-08-06

---

## All ten closure items addressed

Both HIGH findings were verified in production code before being accepted, not
conceded on argument. Three of your five findings were my reasoning errors rather
than oversights, and I want that stated plainly rather than buried in a diff.

| # | Item | Status |
|---|---|---|
| 1 | preserve explicit category/TV evidence | done |
| 2 | non-`sNN` without affirmative movie evidence → `unknown` | done |
| 3 | real-`MediaItem` category tests through `compare_shadow` | done |
| 4 | readiness evidence-integrity blockers | done |
| 5 | narrow the gate claim to count/evidence integrity | done |
| 6 | correct the inverted health reasoning everywhere | done |
| 7 | explicit admission and observation cutoffs | done |
| 8 | bind to immutable source/cohort + per-record evidence | done |
| 9 | align the audit fixture's outcome | done |
| 10 | rerun CI, regenerate from the bounded cohort | done |

---

## Finding 1 — confirmed, and worse than a missed signal

`MediaItem.category` exists at `scanner_service.py:93`, commented *"Crawl category
this item came from: '4k' | 'remux' | 'tv' | '' (unknown)"*, populated from scan
details. `_row_dict()` dropped it, and attribution then fell through to *"if the
url is non-empty it is a movie"*.

So `"unknown"` was reachable only for an empty URL, and the function contradicted
its own docstring three lines above — the docstring argued that guessing movie is
unsafe precisely because it suppresses a real TV miss.

**Attribution is now evidence-gathering.** `attribution_evidence(row)` returns
`(media_type, basis)`:

| Signal | Reads as |
|---|---|
| `category=tv`, `is_tv=True` | TV |
| `category` in `{4k, remux}`, `is_tv=False` | movie |
| `season`, `episodes`, series-only status | TV |
| `sNN` / `sNNeNN` in slug | TV — **positive only** |
| signals disagree | `unknown` |
| nothing affirmative | `unknown` |

Absence of `sNN` is no longer movie evidence. `search`, which
`scanner_service` assigns to search results, carries no type evidence and
resolves to `unknown`.

**Conflicts resolve to `unknown` rather than by precedence.** Neither
misattribution direction is safe — a TV row checked against a failed movie feed
is dropped, and so is a movie row checked against a failed TV feed. Precedence
would silently pick a winner; `unknown` requires both feeds.

The basis is persisted in `hdencode_shadow_misses.attribution_basis`, per your
retrospective-auditability note.

**Why Round 2's tests could not have caught this.** They used dict fixtures, and a
dict passes through `_row_dict` unchanged — so no dict-based test can detect a
dropped field. The regression guard is now a test on `_row_dict` itself, plus four
real `MediaItem` cases through `compare_shadow` including your exact false-pass
construction in both directions (TV feed valid → blocks; TV feed failed →
suppressed).

Your answer to attack 2 was right and I have adopted it: the `sNN` heuristic is
positive TV evidence only.

---

## Finding 2 — confirmed against my own code

Round 2 claimed a writer bug or forgetful caller could not move the gate. Both
could, and both **deflated** it:

- malformed provenance JSON was caught and converted to `{}`, making every joined
  miss row contribute zero;
- `record_hdencode_shadow_comparison` serializes a missing `normal_feed_outcomes`
  as `{}` rather than NULL, so a caller that omits it files misses the reader then
  suppresses.

Four impossible states are now integrity blockers surfaced in readiness reasons as
`miss_evidence_integrity_failed`, never silently zero:

1. `provenance_unparseable`
2. `provenance_not_an_object`
3. `miss_row_with_empty_provenance` — contradictory, since `compare_shadow` cannot
   attribute anything with no observed feed
4. `count_row_disagreement` / `count_without_rows`

A positive control asserts a consistent store raises no flag, so the blocker
cannot be satisfied by always firing.

**The claim is narrowed as you asked.** It is a count-and-evidence-integrity
check, not producer validation. It reads only rows the writer inserted and trusts
the stored `media_type`, so it cannot detect a classifier bug, a wrong
`media_type`, or a suppressed row. That is now stated in the code rather than
implied away. Semantic correctness rests on the adversarial tests over real
`MediaItem` inputs.

Note the change in disposition on the lying-count case. Round 2 reported `1` and
called that protection. It now reports `1` **and raises an integrity blocker** —
reporting a number for self-contradictory evidence hid that the store was
inconsistent.

---

## Finding 4 — my reasoning was inverted, corrected in six places

I wrote *"cannot overstate health"* in `hdencode_shadow.py`, `database.py`, the
tests, `05_shadow_evidence.py`, `miss_resolution.py` and the artifact.

You are right that it is backwards. Since

```
conservative_admitted ⊆ attribution_admitted
⇒ blocking(conservative) ≤ blocking(attribution)
```

finding zero blockers in the **smaller** set is weaker evidence, not safer: an
omitted mixed-cycle row could itself be permanently missing.

What the bound guarantees is that it never **falsely accuses** the feed of a miss.
Every occurrence is corrected, and each correction quotes the old wording as the
error so it cannot quietly return.

---

## Finding 5 — the cohort is now fixed, and the count moved

Your diagnosis was exact: the generator selected everything in whatever database
it was handed, reported 311 cycles to `2026-08-06T12:38Z` under a
`2026-07-22..08-05` heading, and would drift on rerun.

**Admission and observation are now separate bounds** — the substance of the fix,
not a formatting change:

```
--admission-start / --admission-end   which misses ENTER the cohort
--observation-end                     which cycles may RESOLVE them
```

Observation may legitimately extend past admission so a late-admitted miss still
gets its catch-up window. Conflating them let an Aug 6 miss be admitted while
Aug 6 cycles also resolved earlier ones.

**Bounding the cohort changed the conservative count from 61 to 60.** The moving
denominator was real, and that single row is the proof.

Added: `generated_at`, source file SHA-256, cohort cycle-uuid digest, anonymized
per-record manifest (URL hashed to 16 hex chars by default so records are
comparable across runs without publishing the corpus; `--include-urls` opts into
plaintext), separate legacy vs provenance-aware counts, and an explicit
`cohort_is_fixed` flag that reports **false** when no admission-end is supplied.

On immutability: the artifact records the digest of the bytes it read and says
plainly that a digest does not make a live database immutable — for a citable
result, export a snapshot and run against that. I have not committed a redacted
snapshot; say if the manifest is insufficient.

**Also fixed the latent bug you spotted.** `blocking_records` appended only
unresolved states, so a RED arising from a >24h *resolution* was counted in the
tiers and omitted from the list.

---

## Measurements, regenerated from the bounded cohort

```
admission        2026-07-22T00:00:00Z .. 2026-08-05T23:59:59Z
observation end  2026-08-06T23:59:59Z            cohort_is_fixed: true
source sha256    3328a6a6fd5ee4e8...
cohort digest    c0ce19cd07c6c7b3...
```

| | Measured | Required |
|---|---|---|
| Cycles admitted (eligible) | **258** of 300 | 20 |
| Observed days | **14.941** | 7 |
| Observation cycles used | 270 | — |
| Rows: legacy / provenance-aware | 150 / 0 | — |
| Request reduction | **85.12%** | > 0 |

**Conservative bound — 60 records:** 60 GREEN, 0 YELLOW, 0 RED, 0 PENDING,
0 AMBIGUOUS, **0 blocking**. Latency median **1.172 h**, max **4.061 h**.

The claim is unchanged and deliberately narrow: every record admitted by the
conservative bound was later observed in the validated normal feed. **Not** that
no coverage was lost, and the bound cannot establish overall health.

---

## What I would most like attacked this round

1. **Is `unknown` over-reachable?** I flagged this as my biggest worry and then
   measured it rather than shipping the question. Across all **3,134** rows in the
   live `background_scan_cache`:

   | category | rows | share |
   |---|---|---|
   | `4k` | 1,702 | 54.3% |
   | `tv` | 1,234 | 39.4% |
   | `remux` | 198 | 6.3% |
   | absent | **0** | 0% |

   So `unknown` is rare in production, not the common case, and the
   over-suppression risk I was worried about is not borne out. Two cross-checks
   worth noting: `season` is set on 1,212 rows against 1,234 `tv`-category rows,
   so the structured and categorical signals agree closely; and `search` — which
   `scanner_service` does assign — appears **0** times here, so I cannot say from
   this corpus how search-sourced rows behave, only that they are not in the
   cache. **Caveat:** this measures what the scanner has cached, not the miss
   corpus, which predates the field entirely (150 legacy rows, 0
   provenance-aware).
2. **Is `4k`/`remux` sound as affirmative movie evidence?** It comes from source
   construction paired with `"type": "movie"`, but a TV item reached through a
   4K-quality listing would be mislabelled. I could not find such a path; I am
   not certain none exists.
3. **Do the integrity blockers fail closed without being trivially loud?** The
   positive control says a clean store is silent, but on a real corpus a single
   corrupt row would now block the whole gate. Is that the right severity, or
   should integrity be reported separately from readiness?
4. **Is hashing the manifest URLs sufficient** for the independent reproduction
   you asked for, or does it need a committed redacted snapshot?

## Verification

- Full suite: **3 failed, 4331 passed, 4 skipped** (701 s).
- Baseline `main` @ `d909b44`: the **same 3** fail, same test ids — no frontend
  build output, no selenium, no notification backend.
- `emit_measurement_artifact.py` runs clean against a **fresh schema** as well as
  the live snapshot.

## Still not addressed, deliberately

`ready` remains **False**. The readiness rule blocks on any miss regardless of
grade, so 60 green records cannot pass it. That is a behavioural policy change,
not an accounting fix, and it is the owner's decision — not something a code
review should settle.
