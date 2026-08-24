# Round 24 — ScanHound peer review package

**Date:** 2026-08-24
**Branch:** `fix/round12-attestation-authority`
**Code head:** `2004bd2`
**Reviewed against:** the Round-23 exact-head review of `46be010`
**Diff enclosed:** `46be010..2004bd2`, code and mapping only — 5 files, 1874 lines

| File | What it is |
|---|---|
| `00-README.md` | this file |
| `01-request.md` | what to attack, including where I did not comply |
| `02-code-changes.patch` | the complete code diff since the head you reviewed |
| `03-evidence.md` | every number, and how it was measured |
| `04-provenance.md` | deployed vs. branch-only |
| `05-retired-test-mapping.md` | now carrying a **third** correction |

## Every Round-23 finding

| Finding | Status | How |
|---|---|---|
| **R23-3** guard coupled to the migration | **FIXED** | replaced by `validate_shape_migration()`, given only two table names; two independent mutants |
| **R22-3** reopened — never-declared namespaces | **FIXED** | the validator owns the rule; `arm.unregistered.*` and `arm.unscheduled.search` with a full revision are REFUSED |
| **R23-1** revision does not version semantics | **FIXED, your Option B not Option A** | see `01-request.md` §1 — this is the one I want argued |
| **R23-2** quarantine identity not widened | **FIXED** | `listing_type` in both quarantine keys, rows preserved on rebuild |
| **R21-10** fourth overstated **A** | **FIXED** | restored as a direct regression; mapping corrected |
| **R21-10a** two-alias consumer regression | **FIXED** | the actual test, through `consume_cross_crawl_conflicts()` |
| Half revisions | **REFUSED** | the validator owns that too |
| Lifecycle log spam on `/health` poll | **FIXED** | reported on state change, keyed on content |
| Stale identity comments | **FIXED** | both, and the note now says to check call sites rather than the comment |

## Two defects I found in my own work while doing this

Recorded here rather than buried, because they are the same failure mode as
everything else this round.

**The first version of the new validator would have refused every migration.**
It compared plain tuples against `sqlite3.Row` objects, because the production
connection sets `row_factory` and the unit fixtures do not. What makes it the
same trap: **both mutants still "raised"**, so it looked like a working guard.
They were raising for the wrong reason. There are now tests that run the
validator on a production-shaped connection.

**A migration defect was being classified as database corruption.** `init_db()`
catches `sqlite3.DatabaseError` and quarantines the file — renaming the ledger
to `<db>.corrupt.<timestamp>` and creating a fresh empty one.
`sqlite3.IntegrityError` is a subclass. So a constraint violation caused by a
bug in the rebuild was filed as a damaged file: the data survived under the
quarantine name, but the app came up with an EMPTY ledger and reported success.
Observed directly while mutating the migration. Verified on the real 266-row
copy: before, quarantine plus fresh DB; after, refused with all 266 rows
intact.

## Status

- **Not deployed.** `backend/arms.py` is still absent from the running image.
- **The live database has never been written to.**
- **Live apply remains gated.**
