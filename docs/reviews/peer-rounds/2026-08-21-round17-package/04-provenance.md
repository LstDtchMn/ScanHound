# Provenance

## SHAs

```text
repository   LstDtchMn/ScanHound
branch       fix/round12-attestation-authority

code head    89706b77987e4c2bfac0a083321a3fbdd103c483
base         6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
```

`89706b7` is the last commit touching `backend/` or `tests/`. Later commits are
this package or `scripts/morning-deploy.ps1` — never backend or test code.

## Commits since main, oldest first

```text
90535e7   R12  earn the clean attestation, and revoke as one transaction
64815c5   R12  fault-injection regression around the real revocation sequence
0a6a600   R13  a pending revocation withholds authority at the CONSUMER
75c6b0c   R13  the hold must withdraw the WHOLE identity; parsed-arm coverage
9245c24   R14  keep the listing claims, before the releases age off
0c0f5d1   R14  report the unknowns as a measured class
ef2fb18   R15  independent journal, cross-crawl revocation, ledger shape
a8f6116   ops  morning-deploy.ps1
4edb44b   ops  fix: PowerShell swallowed ?mode into the variable name
d951d67   ops  fix: the image has no wget, so the health gate never passed
7663729   ops  fix D15-1/D15-2/D15-3
7d7dfd2   R16  raw aliases (M15-2) and safety before enrichment (M15-3)
6869886   R16  the journal fails closed when it cannot be trusted (M15-1)
039a06e   R16  close L15-1 and L15-2
ceadfcd   R17  the traversal contract and the coverage evaluator
89706b7   R17  the crawler emits the report; the evaluator consumes it
          ---- backend/tests code ends here ----
```

Commits repairing defects introduced by earlier commits **on this same branch**,
listed because the net diff against `main` hides every one:

```text
75c6b0c  fixes the mask inversion introduced by 0a6a600
ef2fb18  fixes the circular restart recovery in 0a6a600 / 75c6b0c
ef2fb18  removes a duplicate listing_claim_summary created while reshaping
7d7dfd2  fixes the same-arm alias loss introduced by 9245c24
6869886  fixes the journal fallback introduced by ef2fb18
4edb44b  fixes an interpolation bug introduced by a8f6116
d951d67  fixes a readiness gate introduced by a8f6116
```

Seven of sixteen. Four of those seven repair something introduced by a rewrite
made **after** a review had passed over the previous version.

Two further defects were caught before they were ever committed, so they do not
appear above; both are described in `01-request.md` because the near-miss is more
useful than the fix:

- the sticky-at-page-bottom flaw in the first evaluator design, caught against
  the reviewer's own counterexample
- a substring-matched edit that would have raised `NameError` on every
  `ScannerService` construction, caught by reading the line numbers the edit
  reported rather than trusting that "syntax OK" meant correct

## Blob hashes at the code head

```text
backend/coverage.py                              d4478cd855b0
backend/scanner_service.py                       88e50de55e55
tests/test_round16_coverage_evaluator.py         018ad9d2c175
tests/test_round16_traversal_emission.py         10c6e50d09df
```

## Test-container identity

The same pair used since round 13, provisioned identically from
`scanhound:latest` with `backend/`, `tests/`, `pytest.ini`, `main.py`, `docs/`
and `scripts/` copied in, plus `pytest`, `pytest-asyncio` and `httpx`.

`docs/` and `scripts/` matter: without them the suite reports ~74 failures,
diagnosed in round 13 by reproducing the same 74 on the `main` control.

## What is NOT covered

- **No CI attestation.** Builder-side runs, described so they can be re-run.
- **Proofs are not persisted.** The evaluator returns objects; nothing stores them.
- **No caller sets `attest_coverage=True`**, and nothing writes `category_attested`.
- **The corroboration rule rests on an assumption** — that a listing is
  monotonically non-increasing in publication time. Stated in `01-request.md` as
  the first thing I want challenged.
- **An empty-but-valid listing is treated as `unrecognised`**, because the two are
  indistinguishable from the crawler. Deliberate, and it means a source that ends
  mid-page cannot prove coverage of its own tail.
- **The live figures describe the ROUND-14 container**, not this code head. In
  particular the live `posted_date_changed = 0` is not yet meaningful: that
  container predates the L15-1 fix, so it still runs the enrichment pass that
  could never raise the flag.
