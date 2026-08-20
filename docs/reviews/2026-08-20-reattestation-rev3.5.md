# Completion contract — evidence at a frozen head

**Date:** 2026-08-20 · **Author:** Claude · **Amends:** rev3.2
**Supersedes:** rev3.3 and rev3.4, both of which went stale
**Head:** `7a50443` on `agent/hybrid-sweep-rebased` · **Base:** `main @ 344027a`, **0 behind**

## Why this is the third attempt

rev3.3 bound itself to `fc7760c` "0 behind main". #89 landed and it was two
behind. rev3.4 bound itself to `c62774d`; I then committed the Q2 work and it
was stale within the hour.

**Both went stale for the same reason: I kept committing after binding them.**
An attestation is a claim about a head, and a head that moves makes the claim
false no matter how carefully the evidence was gathered.

So this document ends with a commitment rather than only a table: **no further
commits to this branch until a verifier has passed over it.** If something must
change, this document is superseded again and says so — it is not quietly
inherited.

It is also named for what it is. Under Rule 2 a builder cannot attest their own
work, and I built the merge repairs. This is **evidence gathered at a frozen
head**, which is the input a verifier needs, not the attestation itself.

## Every row's named evidence, at `7a50443`

| Row | Evidence named by the contract | Result |
|---|---|---|
| R-1 | `test_detail_hydration_composition.py` + `test_feed_upsert_authority.py` | **33 passed** |
| R-3 | `scripts/r3_differential_harness.py` | **71 cases / 40 identical / 31 divergences · exit 0** |
| R-4 | `test_completed_row_feed_authority.py` | **12 passed** |
| R-5 | `test_listing_membership_authority.py` | **14 passed** |
| R-1 / R-6 | `tests/tools/mutation_check.py` | **10/10 DISCRIMINATES · 0 survived · exit 0** |
| bundle | `qualification/scripts/selftest.py` | **ALL SELFTESTS PASSED** |
| bundle | `SHA256SUMS` | **0 mismatches** |

Full suite: **35 failed / 5839 passed / 4 skipped.** Identical failure set to
clean `main` (32 `test_network.py`, plus `test_source_hdencode`,
`test_notifications`, `test_hdencode_off_switch`) — all pre-existing and
network-dependent. **+622 passing over `main`. Zero net new failures.**

R-3 in full, since it is the row that was previously substituted rather than
run:

```text
old=c17152976  new=7a504431b  cases=71  identical=40  differing=31
every divergence matches the committed expected file
exit 0
```

`old` is the parent of the first R-3 delegation commit — the last
pre-unification DetailScraper on this branch. Counts match rev3.2's original
claim exactly.

## Corrected canonical binding for R-1

rev3.2 reads *"`test_detail_hydration_composition.py` 28/28"*. That file has
never held 28 tests (history 11 → 14 → 14 → 19; exactly 14 at the bound SHA,
no parametrisation). The 28 was a **combined run** with
`test_feed_upsert_authority.py`. Naming one of two files made the citation
un-re-runnable, and it led me to nearly record that nine tests had been lost in
the merge.

```text
tests/test_detail_hydration_composition.py   19
tests/test_feed_upsert_authority.py          14
                                             --  33 at 7a50443
```

## Round-11 verifier findings, dispositions

| | |
|---|---|
| **I1** guard precedence | **UNRESOLVED — needs the verifier's ruling, not more code** |
| **I2** inconclusive cycles usable as evidence | **FIXED** — rejected in the shared predicate; mutation kills 5 |
| **T1** over-broad rationale | **FIXED** — narrowed to effect, not meaning |
| **Q2** mirror accounting | **FIXED** — row-validated, in its own SQL |
| **Q2** reconciliation | **FIXED** — every gate field enforced; a missing delta blocks |

### I1 — why it is not fixed, and why I stopped trying

The verifier asked that `disjoint_identity_sets` outrank `relevant_miss`,
because a broken join manufactures the very `listing_only` rows the miss is
derived from. The reasoning is sound. Both implementations overruled one of the
two branches:

```text
disjoint outranks miss      -> breaks main's TestOutcomeLabel
                               rss 1, listing 2 missing, zero overlap
                               expects relevant_miss

minimum-identity threshold  -> breaks the sweep's scenario_09_canonical_variants
                               rss 1, listing 1 in_library, zero overlap
                               expects disjoint_identity_sets
```

Both fixtures are tiny with zero overlap, so **size cannot separate them**, and
the only signal that does is whether misses exist — which is exactly what the
existing gate already tests. At that size, *"the identity join is broken"* and
*"RSS has not got these yet"* are the same observation.

Reverted to the status quo both branches ship. Recorded in
`tests/test_round11_guard_precedence.py`, which states current behaviour and
names the test that must flip. **I reported this fixed once; that was wrong.**

## Still open, stated plainly

- **R-2b** — post-deploy by design.
- **R-7 onward** — Jesse-gated: sign-off, merge, pinned digest, deploy, ~30h
  bootstrap, 7-day window, grading, decision.
- **Reason-code enum** for media-kind refusals — suggested by the verifier, not
  built. My claim that all refusal causes are logged was overstated: conflict
  and client-mismatch are, a missing row or unrecognised category is not.
- **Grab-time resolver measurement** — the verifier is right that 53/664 is not
  the user-impact number. Not measured.

## The commitment

This branch does not move again until a verifier has passed over `7a50443`.
Both previous attestations were invalidated by my own subsequent commits, and a
third would make the document worthless rather than merely stale.
