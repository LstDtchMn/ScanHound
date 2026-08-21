# Provenance

## SHAs

```text
repository   LstDtchMn/ScanHound
branch       fix/round12-attestation-authority

code head    6869886173e1d027ea237901baeb6bc8022b1aa8
base         6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
```

`6869886` is the last commit touching `backend/` or `tests/`.

**The branch is NOT documentation-only above the code head.** Round 15's S11
caught me stating that when it was no longer true. The branch also carries
`scripts/morning-deploy.ps1`, an operational script that has been changed several
times since round 14 and which round 15 reviewed separately. Any commit after
`6869886` is documentation or that script — never backend or test code.

## Commits since main, oldest first

```text
90535e7   R12  earn the clean attestation, and revoke as one transaction
64815c5   R12  fault-injection regression around the real revocation sequence
0a6a600   R13  a pending revocation withholds authority at the CONSUMER
75c6b0c   R13  the hold must withdraw the WHOLE identity; parsed-arm coverage
9245c24   R14  keep the listing claims, before the releases age off
0c0f5d1   R14  report the unknowns as a measured class
ef2fb18   R15  independent journal, cross-crawl revocation, ledger shape
a8f6116   ops  morning-deploy.ps1 (adversarially reviewed before handover)
4edb44b   ops  fix: PowerShell swallowed ?mode into the variable name
d951d67   ops  fix: the image has no wget, so the health gate never passed
7663729   ops  fix D15-1/D15-2/D15-3 from the round-15 review
7d7dfd2   R16  raw aliases (M15-2) and safety before enrichment (M15-3)
6869886   R16  the journal fails closed when it cannot be trusted (M15-1)
          ---- backend/tests code ends here ----
```

Commits that fix defects introduced by earlier commits **on this same branch**,
listed because the net diff against `main` hides all of them:

```text
75c6b0c  fixes the mask inversion introduced by 0a6a600
ef2fb18  fixes the circular restart recovery in 0a6a600 / 75c6b0c
ef2fb18  removes a duplicate listing_claim_summary created while reshaping
7d7dfd2  fixes the same-arm alias loss introduced by 9245c24
6869886  fixes the journal fallback introduced by ef2fb18
4edb44b  fixes an interpolation bug introduced by a8f6116
d951d67  fixes a readiness gate introduced by a8f6116
```

Seven of thirteen commits repair earlier work on this branch. Four of those seven
repair something introduced by a rewrite made **after** a review had passed over
the previous version.

## Blob hashes at the code head

```text
backend/database.py                            3d8bcca1c510
backend/background_scanner.py                  4b6830618b2c
scripts/morning-deploy.ps1                     50fb161edcda
tests/test_round16_journal_durability.py       55aacd333e65
tests/test_round16_alias_and_ordering.py       29bcd2efb79e
```

## Deployment

The running container was built from the **round-14 code** and deployed dark at
08:05 local on 2026-08-21. Nothing in this package is deployed. Live figures in
`03-evidence.md` S4 describe that container, not this code head.

Rollback image `scanhound:rollback-20260821-080443` and a verified 57MB host
backup at `C:\DockerData\scanhound-backups\` both exist from that deploy.

## Test-container identity

The same pair used since round 13, provisioned identically from
`scanhound:latest` with `backend/`, `tests/`, `pytest.ini`, `main.py`, `docs/`
and `scripts/` copied in, plus `pytest`, `pytest-asyncio` and `httpx`.

`docs/` and `scripts/` matter: without them the suite reports ~74 failures,
diagnosed in round 13 by reproducing the same 74 on the `main` control.

## What is NOT covered

- **No CI attestation.** Builder-side runs, described so they can be re-run.
- **The coverage evaluator is not started.** Architecture ruling accepted.
- **L15-1 and L15-2 are not addressed.**
- **The M15-1 residual**: a journal unwritable from process start leaves no
  trace. Named in `01-request.md` and is the question for this round.
- **The revocation journal has not been exercised against a real disk failure**,
  only injected ones. Behaviour on a full or read-only volume is reasoned, not
  measured.
