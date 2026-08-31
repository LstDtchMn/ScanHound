# Completion contract — re-attestation after review round 4 (R4-94-1)

**Date:** 2026-08-28 · **Author:** Claude · **Supersedes:** rev3.6
**Base:** `main @ 0a2751d`, **0 behind**

## Why rev3.6's frozen head moved

rev3.6 closed with "no further commits to this branch until the R-7 sign-off.
If anything must change, this document is superseded and says so." Round 4
returned a HIGH finding (**R4-94-1**) against `/scan/rescan-item` plus one
accepted test-coverage regression. Fixing a reported defect is the named
exception; this document is the "says so".

One commit sits on top of `9ed50d6`.

## R4-94-1 — the carried verdict did not reach the deciding field

The rescan route recovered the cached positive TV verdict into the legacy
`is_tv` field (main's round-11 `rescan_classification`, still correct) and then
computed the authoritative `media_type` **without** it. The matcher selects a
library from `media_type`, not `is_tv`, so a preserved decision could sit in the
field nothing reads while the deciding field had been re-derived from strictly
less evidence — and the route persists that object back into
`background_scan_cache`, so the re-derivation became the next carried verdict.

Fixed by extracting **one** cache-evidence reconstruction
(`cached_type_evidence` / `cached_verdict_evidence` / `cached_media_type` /
`resolve_rescan_media_type`, all in `backend/scanner_service.py` beside
`resolve_listing_media_type`) out of `_media_item_from_dict`, and having the
rescan route combine it with the single thing a rescan re-observes: the fresh
detail filename. No third copy of the rule exists. The commit message carries
the authority table and the full reasoning.

**Not "old always wins":** a stored *provisional* verdict re-enters at ROUTE, so
a freshly-parsed season token still overrules it — the round-13 case, still
pinned by the pre-existing
`test_rescan_item_resolves_media_type_through_the_one_composition`.

### The single behaviour change the R-7 sign-off should know about

Rescanning a row that records a `category_conflict` now returns
`media_type='ambiguous'` where it returned `'movie'`. The conflicted route is
suppressed — exactly as `_media_item_from_dict` already suppressed it for the
same row. Before this commit the two readers of one cached row disagreed:
`rematch_cache` refused it, a rescan resolved it to a film. They now agree, in
the refusing direction. No other outcome moves for a row with no other
evidence.

## Restored pin

`tests/test_scanner_carries_is_tv.py::TestItSurvivesTheCache` is back,
retargeted from `is_tv` to `media_type`, with a fixture whose title/category
heuristics **disagree** with the stored verdict (neutral title, `4k` route, no
season) so the pin proves the verdict is CARRIED rather than coincidentally
re-derived. A companion control asserts the same row without the stored verdict
does resolve `'movie'`. The reviewer's two other accepted drops stay dropped.
The zero-signal REFUSE behaviour is kept and re-pinned.

## What was RUN today, at this head

Environment: host Windows, Python 3.12.9, pytest 9.0.2 — same environment and
same session as rev3.6's runs.

| Row | Evidence | Result today | rev3.6 said |
|---|---|---|---|
| R-3 | `scripts/r3_differential_harness.py` | **`old=c17152976 new=9ed50d667 cases=71 identical=40 differing=31` · every divergence matches the committed expected file · exit 0** | same counts at `3f4d000` |
| R-1/R-6 | `tests/tools/mutation_check.py` | **10/10 DISCRIMINATE · 0 survived · exit 0** | 10/10 |
| **R4-94-1** | `tests/tools/r4_94_1_mutation_check.py` (new) | **13 mutants · 0 survived · baseline and restored both 268 passed** | n/a |
| bundle | `docs/feature-pack-review/qualification/scripts/selftest.py` | **ALL SELFTESTS PASSED** | pass |
| bundle | `SHA256SUMS` | **14 files, 0 mismatches** | 0 mismatches |

The worktree was verified clean (`git status` showing only this change set)
after both mutation harnesses and before the full suite, so the suite ran
against exactly this tree.

Full suite: **1 failed / 6085 passed / 5 skipped (19:52).** The one failure is
`test_dv_host_scan.py::test_post_rows_direct_success_delivers_key` — the same
ordering/concurrency flake rev3.6 recorded at `3f4d000`; it passes in isolation
(1/1) and with its whole file (37/37) at this head, re-checked today.

**Delta over rev3.6: +19 passing (17 new regression tests, 2 restored pins),
zero new failures.**

## R4-94-1 mutation results

Each mutant reintroduces one defect, runs the five affected test files, and is
then reverted. Line-numbered rather than string-keyed: a snippet that matches
nothing reports a healthy test as "survived", and every edit here prints the
line it replaced so a drifted number is visible in the log.

| Mutant | Result |
|---|---|
| baseline | 268 passed |
| M1 restore the original defect (route composes from category + fresh detail only) | **5 failed** ← the finding itself |
| M2 drop `cached-is-tv` evidence | 7 failed |
| M3 drop `cached-season` evidence | 3 failed |
| M4 drop `cached-category` evidence | 4 failed |
| M5 do not carry a stored verdict | 1 failed ← the restored pin |
| M6 stored verdict always DETAIL authority | 1 failed |
| M7a stored `'ambiguous'` counts as MOVIE | 2 failed |
| M7b stored `'ambiguous'` counts as TV | 2 failed |
| M8 conflict no longer suppresses the route | 1 failed |
| M9 drop the fresh detail evidence | 3 failed |
| M10 answer TV unconditionally | 8 failed |
| M11 answer MOVIE unconditionally | 17 failed |
| M12 route does not persist the verdict | 10 failed |
| M13 drop the listing-title fallback | 1 failed |
| restored | 268 passed |

**0 survivors.** Two assertions did not kill their mutant on the first run and
were rewritten until they did: the `'ambiguous'`-is-not-evidence pin needed a
fixture where admitting it as evidence actually changes the answer, and M1's
own first attempt shifted a line number and produced a syntax error instead of
the defect — caught by the printed `was:` lines, not by the pass/fail counts.

## What is INHERITED from rev3.6 (NOT re-run)

- **R-1, R-4, R-5 rows** — unchanged files, unchanged results; not re-run
  individually today because the full suite covers them.
- **R-3 reference corpus and expected-divergence file** — committed
  definitions; today's run re-executed the harness against them.
- **Live measurements**, **the review-round history**, **R-2b / reason-code
  enum / grab-time resolver measurement** (still open), and **I1 (guard
  precedence, deliberately unresolved)** — all exactly as rev3.6 states.

Merged-is-not-deployed still applies: nothing here says anything about the
running container.

## The commitment, renewed

No further commits to this branch until the R-7 sign-off. If anything must
change, this document is superseded and says so.
