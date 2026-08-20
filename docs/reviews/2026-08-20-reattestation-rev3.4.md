# Completion contract — re-attestation at a FROZEN head

**Date:** 2026-08-20 · **Author:** Claude · **Amends:** rev3.2, supersedes rev3.3
**Frozen head:** `c62774d` on `agent/hybrid-sweep-rebased`
**Base:** `main @ 344027a` — **0 behind**, verified after merging #89 in.

## Why rev3.3 was not enough

Two reasons, both raised by the verifier and both correct.

**The head moved.** rev3.3 bound itself to `fc7760c` with "0 behind main", and
that expired the moment #89 landed on `main`. An attestation against a head the
branch has since left is not evidence about the branch.

**R-3 was substituted, not run.** rev3.3 offered
`tests/test_release_grammar.py` — 79 passed — for a row whose exit criterion
names a differential harness comparing the OLD parser against the new tree over
a fixed corpus. Those are not the same artifact, and the verifier said so. The
79 tests can be entirely good and still not prove the property R-3 asserts.

## R-3 — now actually run

```text
python scripts/r3_differential_harness.py --new c62774d

old=c17152976  new=c62774d4b  cases=71  identical=40  differing=31
every divergence matches the committed expected file
exit 0
```

`old` is `c1715297`, the parent of the first R-3 delegation commit — the last
pre-unification DetailScraper on this branch. The expected-divergence corpus is
`docs/reviews/evidence/r3-expected-divergences.json`, committed. The harness
builds disposable git worktrees and runs each side in subprocess isolation, so
it does not depend on the invoking checkout's importable state.

Case counts match rev3.2's original claim exactly: 71 / 40 / 31.

## Every row's named evidence, at `c62774d`

| Row | Evidence | Result |
|---|---|---|
| R-1 | `test_detail_hydration_composition.py` + `test_feed_upsert_authority.py` | **33 passed** |
| R-3 | `scripts/r3_differential_harness.py` | **71 cases / 40 identical / 31 divergences, exit 0** |
| R-4 | `test_completed_row_feed_authority.py` | **12 passed** |
| R-5 | `test_listing_membership_authority.py` | **14 passed** |
| R-1 / R-6 | `tests/tools/mutation_check.py` | **10/10 DISCRIMINATES, 0 survived, exit 0** |
| bundle | `qualification/scripts/selftest.py` | **ALL SELFTESTS PASSED** |

## Corrected canonical binding for R-1

rev3.2 reads:

> emission suite `tests/test_detail_hydration_composition.py` 28/28 at `b4707b76…`

That file has never held 28 tests — its history is 11 → 14 → 14 → 19, and at
the bound SHA it collects exactly 14 with no parametrisation. The 28 was a
**combined run** with `test_feed_upsert_authority.py` (14 at that commit),
verified by checking out `b4707b7` and collecting both.

The evidence was real; the citation named one of two files and was therefore
not re-runnable. **This correction belongs in the canonical contract row, not
only in an amendment**, as the verifier noted.

```text
tests/test_detail_hydration_composition.py   19
tests/test_feed_upsert_authority.py          14
                                             --  33 at c62774d
```

## THE LIMIT — unchanged, and it still binds

**Rule 2: the verifier is never solely the builder.** I built the merge repairs,
so everything above is a builder-side execution at a frozen head. It is
necessary and not sufficient.

**No row status changes in this document.** What has changed is that a verifier
now has a head that is not moving, and a complete set of named evidence that
actually runs — including the R-3 harness that was missing.

The verifier's independent GitHub-hosted run at the `fc7760c` tree already
found R-1/R-4/R-5/R-6 non-vacuous by design and by execution. Those dispositions
were explicitly historical. They need re-issuing against `c62774d`.

## Known open, carried forward honestly

**I1 is UNRESOLVED, not fixed.** I previously reported it fixed; that was wrong.
The verifier asked that `disjoint_identity_sets` outrank `relevant_miss`. Both
implementations I tried overruled one of the two branches:

```text
disjoint outranks miss    -> breaks main's TestOutcomeLabel
                             rss 1, listing 2 missing, zero overlap -> relevant_miss

minimum-identity threshold -> breaks the sweep's test_scenario_09_canonical_variants
                             rss 1, listing 1 in_library, zero overlap -> disjoint
```

Both fixtures are tiny with zero overlap, so size cannot separate them, and the
only signal that does is whether misses exist — which is what the existing gate
already tests. At that size "the join is broken" and "RSS has not got these yet"
are the same observation. Reverted to the status quo both branches ship, and
recorded in `tests/test_round11_guard_precedence.py` with the test named that
must flip if the ruling goes the other way. **This needs the verifier's decision,
not mine.**

**I2 is fixed.** Guard cycles are rejected in the shared evidence predicate, so
both the miss resolver and the candidate state machine inherit it. Mutation:
removing the rejection kills 5 tests.

**Q2 not started.** The mirror still sums the stored miss count while the app
validates against rows, and `reconciliation_blockers()` still enforces only
`ready_matches` rather than the gate-field deltas it computes.

**Still Jesse-gated:** R-7 sign-off onward — merge, pinned digest, deploy, ~30h
bootstrap, 7-day window, grading, decision.
