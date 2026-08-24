# Round 21 — ScanHound peer review package

**Date:** 2026-08-24
**Branch:** `fix/round12-attestation-authority`
**Commit under review:** `1f77a1d` — "M19-1/M19-2: evidence identity is a revision, not a name"
**Parent:** `7720a75` (the Round-20 PLAN package you reviewed)

This package is self-contained. The full diff travels with it as
`02-code-changes.patch`; you do not need repository access.

| File | What it is |
|---|---|
| `00-README.md` | this file |
| `01-request.md` | what to attack, and the specific claims I want challenged |
| `02-code-changes.patch` | the complete commit, 10 files, 3103 lines |
| `03-evidence.md` | every number in this package, and how it was measured |
| `04-provenance.md` | what is deployed vs. what is only on the branch |

## What changed in one paragraph

Round 19 identified a listing feed ("arm") by a stable name. You established
that a name cannot be the durable identity of *evidence*: two request
definitions can be published under one arm deliberately, and keying the ledger
on the name lets the second refresh the first's rows and merge their sightings
and dates, after which nothing can untangle which definition made which claim.
Identity is now the triple `(arm_id, request_definition_version,
parser_version)`. The ledger migration to that shape is split into a mechanical
half that runs automatically and a judgement half that is gated, audited, and
dry-run by default.

## Status

- **Not deployed.** The running container is unchanged.
- **The live database has never been touched.** Every measurement in
  `03-evidence.md` was taken against a `VACUUM INTO` copy.
- **Live apply remains gated** behind the items that were open at Round 20.

## What is NOT in this commit, deliberately

Coverage proofs do not yet *consume* the revision. `is_active_revision()`
exists and is tested, but `coverage.py` still reasons about `arm_key` as an
opaque string label of its own. That is the next unit of work, and I would
rather you attack this foundation before it has dependents.
