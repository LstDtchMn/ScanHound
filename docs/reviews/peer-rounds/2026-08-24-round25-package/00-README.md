# Round 25 — ScanHound peer review package

**Date:** 2026-08-24
**Branch:** `fix/round12-attestation-authority`
**Code head:** `e26c2f7`
**Reviewed against:** the Round-24 exact-head review of `2004bd2`
**Diff enclosed:** `2004bd2..e26c2f7`, code and mapping only — 5 files, 1088 lines

| File | What it is |
|---|---|
| `00-README.md` | this file |
| `01-request.md` | what to attack, and one thing I found and did NOT fix |
| `02-code-changes.patch` | the complete code diff since the head you reviewed |
| `03-evidence.md` | every number, and how it was measured |
| `04-provenance.md` | deployed vs. branch-only |
| `05-retired-test-mapping.md` | now carrying corrections through round 25 |

## Every Round-24 finding

All six were **reproduced against the running code before being accepted**, and
all six now fail to reproduce.

| Finding | Status | How |
|---|---|---|
| **R24-1** corruption classifier too broad | **FIXED, globally** | quarantine requires positive evidence; `IntegrityError` caught first and re-raised |
| **R23-1a** pin is drift detection, not immutability | **FIXED** | `arm_semantic_history` — durable evidence a code edit cannot rewrite |
| **R23-1b** writer looser than the migration | **FIXED** | `semantic_mismatch()` is now the admission rule for both |
| **R23-1c** lifecycle counts contradicting evidence | **FIXED** | compares `listing_type` too |
| **R23-2** alias audit blanked a recoverable type | **FIXED** | recovered by join; genuinely unknowable stays `''` |
| **R24-2** fingerprint and resolver disagree | **FIXED** | all semantic fields normalised in one place |
| **R21-10** fifth and sixth overstated **A** | **FIXED** | one reclassified **B**, one given a direct regression |

## The one that mattered most

**R24-1.** Measured on a file that passes `PRAGMA integrity_check`:

```
DATABASE CORRUPTION DETECTED ... UNIQUE constraint failed: hdencode_candidates.guid
Renamed corrupt DB to ….corrupt.1787599727. Creating fresh DB.
init_db did NOT raise
rows left at the original path: 0 (was 2)
```

A healthy database, replaced by an empty one, with startup reporting success.
Narrowed **globally** as you recommended rather than per-statement, because
requiring every future schema author to remember "this statement can fail in a
way that is not corruption" is exactly the distributed negative obligation that
has failed repeatedly here.

Live exposure, measured: **not armed today** — the index exists and there are
0 duplicate guids among 7,205 rows. But six unique indexes are built at startup
over tables that may already hold data.

## Your central criticism, accepted

> A pin stored beside the declaration is independent data only while the two
> disagree. If both are updated together, it becomes a second copy of the same
> current belief.

That is the same lesson as the round-23 migration oracle, and you were right
that I had reproduced it. `arm_semantic_history` is the part a code edit cannot
rewrite: once a database has seen an arm's meaning, changing it under the same
id refuses to start against that database.

## Status

- **Not deployed.** `backend/arms.py` is still absent from the running image.
- **The live database has never been written to.**
- **Live apply remains gated.**
