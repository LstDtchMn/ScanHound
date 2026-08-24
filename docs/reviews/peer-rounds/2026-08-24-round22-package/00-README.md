# Round 22 — ScanHound peer review package

**Date:** 2026-08-24
**Branch:** `fix/round12-attestation-authority`
**Code head:** `c4d9dc0`
**Reviewed against:** the Round-21 exact-head re-review of `1f77a1d`
**Diff enclosed:** `1f77a1d..c4d9dc0` — 15 files, 7021 lines

| File | What it is |
|---|---|
| `00-README.md` | this file |
| `01-request.md` | what to attack, including the one place I did not comply |
| `02-code-changes.patch` | the complete diff since the head you reviewed |
| `03-evidence.md` | every number, and how it was measured |
| `04-provenance.md` | deployed vs. branch-only |
| `05-retired-test-mapping.md` | the R21-10 deliverable, and the retraction |

## Disposition of every Round-21 finding

| Finding | Status | Where |
|---|---|---|
| R21-1 quarantine representation | **FIXED** | explicit `attribution_state`, CHECK-enforced |
| R21-2 NULL sentinel | **WITHDRAWN by you** | EMPTY was `''` and NOT NULL |
| R21-3a claim/alias revision drift | **FIXED structurally** | aliases reference `claim_id`; disagreement is unrepresentable |
| R21-3b date authority split | **FIXED, with one departure** | see `01-request.md` §1 |
| R21-4 parser lifecycle | **FIXED** | `revision_lifecycle_summary()` |
| R21-5 content-preservation guard | **FIXED** | `rebuild_equivalence_failure()`, 7 tests, mutation-proved |
| R21-6 colon-bearing `arm_id` | **FIXED** | `is_arm_id()` guard, 5 parametrised cases |
| R21-7 truncated identity | **FIXED** | full digest; the value is no longer an `arm_id` at all |
| R21-8 duplicated pagination | **FIXED** | crawler calls `build_page_url()`; golden vectors + request capture + static guard |
| R21-9 transaction granularity | **CLOSED by you** | — |
| R21-10a raw aliases dropped | **FIXED** | live safety regression; mutation-proved |
| R21-10b two-feed regression | **RESTORED** | real crawl, plus a ledger-level assertion |
| R21-10c no-arm fallback | **RESTORED** | 5 parametrised cases |
| R21-10d alias migration coverage | **RESTORED** | 4 tests — the gap that let R21-11 through |
| R21-11 alias collision aborts | **FIXED** | merged: earliest first, latest last, summed sightings |
| R21-12 descriptor semantics | **FIXED** | source/category/type validated; 3 negative + 1 positive control |
| R21-13 revision absent from proofs | **FIXED** | reaches `Arm`, `CoverageProof`, contract key; mutation-proved |

## The two you called highest-value

Both are done, and both had a second defect behind them.

**Propagate the revision through the proof path.** `request_definition_version`
now reaches traversal `Arm` and `CoverageProof`, `ORDERING_CONTRACTS` is keyed
on the full triple, and `covers_release()` refuses rather than choosing when one
`arm_id` appears under two revisions in a run — a case a dict keyed on `arm_id`
would have silently resolved by keeping whichever came last.

**Separate claim dedupe from raw-alias capture.** Done, and the test that had
codified the wrong behaviour is replaced. Worth stating plainly: that test was
*named* `test_BOTH_raw_variants_survive_as_aliases` while asserting
`len(aliases) >= 1`. Your diagnosis — contract inventory, not mutation coverage
— is right, and a name that overstates its body is a second way the same
failure hides.

## Status

- **Not deployed.** The running container is unchanged and does not contain
  `backend/arms.py` at all.
- **The live database has never been written to.** Every measurement used a
  `VACUUM INTO` copy.
- **Live apply remains gated.**
