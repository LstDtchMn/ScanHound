# Round 19 review package — M18-1 through M18-4 closed

**Self-contained.** The full diff travels with the package.

## Identity and state — stated once, here

```text
repository    LstDtchMn/ScanHound
branch        fix/round12-attestation-authority
code head     1aff26f949a54466c322a38b53798389ee4831fd
base          6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
working tree  clean

DEPLOYED      YES, DARK, since 2026-08-21 08:05 local.
              The RUNNING container is the ROUND-14 code.
              Nothing from rounds 16 through 19 is deployed.
```

Live, read-only from the running container, re-measured for this package:

```text
listing_claims          209 claims / 201 releases
distinct arm_keys         3   hdencode:tv 78, :4k 69, :remux 62
movie-vs-tv conflicts     0
category_attested         0   of 4285 background_scan_cache payloads
downloads.media_kind   NULL   on all 684 rows
posted_date_changed       0
```

Two notes on how those are measured, because I got one wrong while preparing
this package and would rather say so than quietly fix it:

- **`category_attested` is not a column.** It is a key inside the
  `background_scan_cache.data` JSON payload. The round-18 package reported `0`
  and that figure is correct, but a query treating it as a column errors rather
  than returning zero — which is the difference between "measured none" and
  "measured nothing".
- **`listing_claim_aliases` does not exist in the live database.** It is a
  round-15 table and the deployed container is round-14 code. Expected, not a
  fault, but it means the alias-rekeying half of this round's migration has no
  live rows to act on until the new code ships.

## Contents

| File | What it is |
|---|---|
| `01-request.md` | **Start here.** Each M18 finding, the gate item by item, and four questions. |
| `02-code-changes.patch` | Complete diff of `backend/` and `tests/` against `main`. |
| `03-evidence.md` | Commands, mutation results, suite figures, live data. |
| `04-provenance.md` | SHAs, blob hashes, container identity, what is NOT covered. |

## Round-18 dispositions

```text
M18-1  three arm identities        CLOSED   backend/arms.py is the single
                                            definition; contracts keyed
                                            (arm_key, parser_version); the
                                            209 live ledger rows migrated
M18-2  duplicates global to crawl  CLOSED   scoped per arm; both producer
                                            regressions you specified
M18-3  page sealed before reading  CLOSED   sealed after enumeration; a
                                            mid-page failure seals the
                                            PARTIAL page marked unusable
M18-3  positions sorted            CLOSED   requires exactly 1..N in
                                            EMITTED order
M18-4  evidence mutable            CLOSED   frozen snapshot that COPIES;
                                            date and raw from one read
```

## The headline

**The DDLBase remux merge you flagged is shipped, not hypothetical.** Two
distinct feeds, both `movie`/`remux`, both keyed `ddlbase:remux` in the ledger —
so whichever listed a release second had its claim discarded as a repeat of the
first. The ledger's entire purpose is to keep what each arm said before releases
age off the listing.

## What I most want challenged

The migration of the 209 live rows. It declines to guess, it merges rather than
clobbers, and it refuses to run against a partial registry — but it writes to
Jesse's live data during what has so far been a strictly read-only dark
deployment. `01-request.md` §4 makes the case both ways and does not assume you
will agree with the call I made.
