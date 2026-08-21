# Round 17 review package — the coverage contract and evaluator

**Self-contained.** The full diff travels with the package.

## Identity and state — stated once, here

```text
repository    LstDtchMn/ScanHound
branch        fix/round12-attestation-authority
code head     89706b77987e4c2bfac0a083321a3fbdd103c483
base          6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
working tree  clean

DEPLOYED      YES, DARK, since 2026-08-21 08:05 local.
              The RUNNING container is the round-14 code.
              Nothing in this package is deployed.
```

Live state, read-only from the running container:

```text
listing_claims        195 claims / 188 releases   (7h, steady)
movie-vs-tv conflicts   0
posted_date_changed     0
category_attested       0
downloads.media_kind    NULL on all 684 rows
errors                  0
```

## Contents

| File | What it is |
|---|---|
| `01-request.md` | **Start here.** What was built, the flaw your example caught, and three things I want challenged. |
| `02-code-changes.patch` | Complete diff of `backend/` and `tests/` against `main`. |
| `03-evidence.md` | Commands, mutation results, suite figures, live data. |
| `04-provenance.md` | SHAs, blob hashes, container identity, what is NOT covered. |

## What changed since round 16

```text
039a06e  L15-1 and L15-2 closed          (all round-15 findings now closed)
ceadfcd  the traversal contract + evaluator
89706b7  the crawler emits the report; the evaluator consumes it
```

## The headline

**Your sticky counterexample caught a real flaw in my first design.** I refused on
listing-order inversions, which handles a pinned post in the middle of a page —
but yours sits at the bottom, where the dates still descend and no inversion
fires. That version would have adopted January 2024 as the frontier: `min(posted
date)` by another route, the exact thing you rejected.

Fixed by requiring corroboration. The mutation implementing `min()` now kills 8
tests, including all three sticky tests.

## Still true of everything here

Nothing writes `category_attested`. No caller sets `attest_coverage=True`. The
evaluator has no database handle and a test asserts it.
