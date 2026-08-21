# Round 16 review package — M15-1/2/3 and D15-1/2/3 closed

**Self-contained.** The full diff travels with the package.

## Identity and state — stated ONCE, here

```text
repository    LstDtchMn/ScanHound
branch        fix/round12-attestation-authority
code head     039a06e (all round-15 findings closed)
base          6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
working tree  clean

DEPLOYED      YES, DARK, since 2026-08-21 08:05 local.
              The RUNNING container is the round-14 code (ef2fb18 lineage).
              Everything in THIS package is committed but NOT yet deployed.
```

That last line is the distinction round 15's S11 caught me blurring. There are
two different heads in play and they are not the same:

```text
running in production   the image built at deploy time this morning
under review here       039a06e, which contains today's remediation
```

Live state as of this package (read-only, from the running container):

```text
listing_claims        188 claims / 181 releases (3h stable)
movie-vs-tv conflicts   0
category_attested       0
downloads.media_kind    NULL on all 684 rows
errors                  0
```

## Contents

| File | What it is |
|---|---|
| `01-request.md` | **Start here.** What was fixed, what is still open, and the one question I want challenged. |
| `02-code-changes.patch` | Complete diff of `backend/` and `tests/` against `main`. |
| `03-evidence.md` | Commands, mutation results, suite figures, live data. |
| `04-provenance.md` | SHAs, blob hashes, container identity, what is NOT covered. |

## Round-15 dispositions

```text
M15-1  journal fails closed        CLOSED  session markers; residual named in 01
M15-2  raw aliases                 CLOSED  listing_claim_aliases, seeded from live
M15-3  safety before enrichment    CLOSED  plus a second cause you did not name
L15-1  posted_date_changed         CLOSED  now compared through the real route
L15-2  consumer idempotence        CLOSED  narrows to outstanding work only
D15-1  rollback tags wrong image   CLOSED
D15-2  no fresh before-snapshot    CLOSED
D15-3  dark checks not enforced    CLOSED
S11    package consistency         CLOSED  fixed at source, see above
coverage evaluator                 NOT STARTED, architecture ruling accepted
```

## The one question

The **residual in M15-1**: the journal now detects its own past failure, but only
when it was writable long enough to record `SESSION_OPEN`. Is a filesystem
journal simply the wrong instrument for "storage was gone from the start", or is
that case outside what any in-process mechanism can cover — in which case the
honest answer is the ERROR log and an operator, not more machinery?
