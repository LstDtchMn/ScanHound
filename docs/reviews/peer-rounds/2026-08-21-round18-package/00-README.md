# Round 18 review package — M17-1 through M17-4 closed

**Self-contained.** The full diff travels with the package.

## Identity and state — stated once, here

```text
repository    LstDtchMn/ScanHound
branch        fix/round12-attestation-authority
code head     4c0f1de712bc0c2e7a454d6e8cea17d25d06b31a
base          6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
working tree  clean

DEPLOYED      YES, DARK, since 2026-08-21 08:05 local.
              The RUNNING container is the ROUND-14 code.
              Nothing from rounds 16, 17 or 18 is deployed.
```

Live, read-only from the running container:

```text
listing_claims        198 claims / 190 releases
movie-vs-tv conflicts   0
category_attested       0
downloads.media_kind    NULL on all 684 rows
errors                  0
```

## Contents

| File | What it is |
|---|---|
| `01-request.md` | **Start here.** Each M17 finding, the gate item by item, and the two things still open. |
| `02-code-changes.patch` | Complete diff of `backend/` and `tests/` against `main`. |
| `03-evidence.md` | Commands, mutation results, suite figures, live data. |
| `04-provenance.md` | SHAs, blob hashes, container identity, what is NOT covered. |

## Round-17 dispositions

```text
M17-1  corroboration unsound        ADDRESSED  frontier is now telemetry, not
                                               authority -- ORDERING_CONTRACTS
                                               is empty and enforced in code
M17-1  raw-alias variant            CLOSED     duplicate_in_run is canonical
M17-2  evaluator bridges gaps       CLOSED     validates continuity; producer
                                               now emits every attempted page
M17-3  "any arm of each type"       CLOSED     universal, by explicit arm key
M17-4  arm identity merges feeds    CLOSED     arm_key includes the endpoint
```

## The two things still open, both wanting your shape

**A versioned required-arm policy.** `covers_release()` takes the required keys
explicitly, which closes the existential bug — but nothing DERIVES that set from
a target's claimed type. Which arms can contradict a given release is a domain
judgement, and a wrong answer produces a confidently wrong negative.

**Persistence.** Your schema is detailed enough to build from. Before I do, one
thing in it worries me: the evaluator currently receives a MUTABLE `dates` map
and `unstable` set, so a persisted proof would reference values that can change
underneath it. That is the part most likely to be got subtly wrong.

## The headline

**I wrote a false claim in a docstring and you disproved it with two terminal
outliers.** The fix is not a better comment — the limitation is now enforced in
code, and a test documents the defeat rather than hiding it.
