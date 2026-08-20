# Completion contract — re-attestation at the merged head

**Date:** 2026-08-20 · **Author:** Claude · **Supersedes nothing.**
**Amends:** `2026-08-06-completion-contract-rev3.2.md`
**Head re-attested:** `fc7760c` on `agent/hybrid-sweep-rebased`
**Base:** `main @ 3013556`. **0 behind.**

## Why this exists

Rev 3.2 bound every evidence row to SHAs on a branch that was then **0 behind
`main`**. By 2026-08-19 it was **347 behind**, and the merge that closed that gap
touched `database.py`, `hdencode_shadow.py` and `scanner_service.py` — the files
those rows are about.

Under the contract's own **Rule 1** (evidence names EXECUTED behaviour) every
🔨 row's attestation went stale the moment the code moved. A green suite is not
an attestation; this document re-runs the named evidence and binds it to the
current head.

## THE LIMIT — read before treating any row as closed

**Rule 2 says the verifier is never solely the builder.** I built the merge
repairs. Everything below is therefore a **builder-side re-run**, which is
necessary and *not* sufficient.

**No row changes status here.** R-1, R-3, R-4, R-5 and R-6 remain 🔨. What has
changed is that a verifier now has current, reproducible commands instead of
SHAs whose code has moved 347 commits.

## Suite state at `fc7760c`

```text
35 failed / 5818 passed / 4 skipped
```

Identical failure set to clean `main` (35 failed / 5217 passed): 32
`test_network.py`, plus `test_source_hdencode`, `test_notifications`,
`test_hdencode_off_switch` — all pre-existing and network-dependent.

**+601 passing tests over `main`.** Zero net new failures.

## Re-run evidence

| Row | Evidence, as named in rev 3.2 | Result at `fc7760c` |
|---|---|---|
| R-1 | emission suite | **33 passed** (see the citation correction below) |
| R-1 / R-6 | `tests/tools/mutation_check.py` — rev 3.2 says 9/9, exit 0 | **10/10 DISCRIMINATES, 0 survived, exit 0** |
| R-3 | grammar | `tests/test_release_grammar.py` **79 passed** |
| R-4 | sparse-detail discrimination, rev 3.2 says 7 tests | `tests/test_completed_row_feed_authority.py` **12 passed** |
| R-5 | listing composition / final consumer | `tests/test_listing_membership_authority.py` **14 passed** |

## A CITATION CORRECTION, and it cost an hour

Rev 3.2 R-1 reads:

> emission suite `tests/test_detail_hydration_composition.py` 28/28 at
> `b4707b766c788652a3c19e16ad55e63d39a0910b`

**That file has never contained 28 tests.** Its history on this branch is
11 → 14 → 14 → 19, and at the bound SHA it collects exactly **14**, with no
parametrisation and no case-multiplying loops.

The 28 was a **combined run of two files**. `b4707b7` also touched
`tests/test_feed_upsert_authority.py`, which held 14 at that commit. Verified by
checking out `b4707b7` and collecting both:

```text
tests/test_detail_hydration_composition.py + tests/test_feed_upsert_authority.py
    28 tests collected
```

The evidence was real. **The citation was not re-runnable**, which is what
Rule 3 exists to prevent — and it names only one of the two files, so anyone
re-attesting it (me, today) runs the named file, sees 19, and concludes nine
tests were lost in the merge. I nearly recorded exactly that.

**Corrected binding for R-1's emission evidence:**

```text
tests/test_detail_hydration_composition.py   19 tests
tests/test_feed_upsert_authority.py          14 tests
                                             -- 33 passed at fc7760c
```

## Regressions found and repaired since rev 3.2

All were **main-side tests the sweep had never run**, because they do not exist
on `8f5686b`:

1. `05_shadow_evidence.py` — a second PRODUCTION implementation of readiness
   that the collector reads. Its schema pin was reverted 9 → 8 and its
   attribution filter lost, so the mirror could never be ready and would drive
   a priority-8 mandatory stop every six hours.
2. The qualification window scoped **one query in six**. Everything the gate
   reads was still computed over every row ever recorded.
3. Three new `compare_shadow` outcomes were added without teaching
   `get_hdencode_miss_resolution` about them, so guarded cycles stopped
   blocking — the "right and unreachable" failure that file's own comment
   already records from 2026-08-07.
4. Cached films could not resolve a media type, because nothing proves MOVIE
   from a title. Measured on 400 real rows: 375 became unroutable. ~3,800 of
   4,073 live rows would have read "Type unresolved — review" on deploy.

## Still open, unchanged by this document

- **R-2b** is post-deploy by design.
- **R-7 onward** is the Jesse-gated sequence: sign-off, merge, pinned digest,
  deploy, ~30h bootstrap, 7-day window, grading, decision.
- **App/mirror miss accounting diverges** when a cycle's stored count disagrees
  with its rows. The app validates against rows; the mirror sums the count. They
  agree on every consistent cycle. Recorded in the parity fixture, unsettled.
