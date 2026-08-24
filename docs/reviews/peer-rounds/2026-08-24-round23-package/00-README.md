# Round 23 — ScanHound peer review package

**Date:** 2026-08-24
**Branch:** `fix/round12-attestation-authority`
**Code head:** `46be010`
**Reviewed against:** the Round-22 exact-head review of `c4d9dc0`
**Diff enclosed:** `c4d9dc0..46be010` — 15 files, 9632 lines

| File | What it is |
|---|---|
| `00-README.md` | this file |
| `01-request.md` | what to attack |
| `02-code-changes.patch` | the complete diff since the head you reviewed |
| `03-evidence.md` | every number, and how it was measured |
| `04-provenance.md` | deployed vs. branch-only |
| `05-retired-test-mapping.md` | now carrying a **second** retraction |

## Every Round-22 finding

| Finding | Status | How |
|---|---|---|
| **R22-1** required arm id vs required active revision | **FIXED** | your third option: `active_revisions_for()` resolves at the policy layer, `covers_release()` takes exact revisions, `coverage.py` stays registry-free |
| **R22-2** intermediate rebuild discards revision + alias provenance | **FIXED** | attributed rows keep their exact recorded revision; alias history migrated, orphans refuse the migration; the guard now compares revisions |
| **R22-3** arm-shaped vs declared-arm attribution | **FIXED** | `is_declared_arm_id()` in the writer; lifecycle reports `undeclared_arm` |
| **R22-4** alias lookup silently drops aliases | **FIXED** | raises and rolls back; tested by injecting the miss |
| **R22-5** unattributed type overwrite erases narrowing evidence | **FIXED** | `listing_type` joins the unattributed identity; attribution refuses a contradictory type |
| **R21-4** lifecycle report had no consumer | **FIXED** | `/health` reports the per-state counts |
| **R21-12** source negative control was vacuous | **FIXED** | same-pagination mirror; mutation-proved |
| **R21-10** three overstated **A** entries | **FIXED** | restored as direct regressions, mapping corrected |
| **R21-5** intermediate-shape half | **FIXED** | folded into R22-2 |
| Three stale comments | **FIXED** | including the one that said FILL widens authority |

## The two you pressed hardest

**R22-2.** You were right, and the part worth stating plainly is that the
equivalence guard I added in round 21 to catch exactly this class of loss
**could not see it**. Its projection compared `COALESCE(legacy_arm_key, arm_id)`
and never the revision columns, so a rebuild that demoted an attributed row was
equivalent *by construction*. A guard that cannot fail on the case it guards
supplies confidence without evidence. The projection now carries the revision on
both sides, and asserts NULL for a source shape that has none — so a rebuild
that *invents* an attribution is detectable too.

**R22-5.** Fixed as you suggested — `listing_type` in the unattributed identity —
plus the follow-through you anticipated: attribution refuses to absorb a
contradictory type rather than colliding on the attributed index or picking a
winner. Tested through the **real** `consume_cross_crawl_conflicts()` with a
live download whose authority must actually be withdrawn, which is also the
consumer-level evidence you asked for on R21-10a.

## Status

- **Not deployed.** `backend/arms.py` is still absent from the running image.
- **The live database has never been written to.**
- **Live apply remains gated.**
