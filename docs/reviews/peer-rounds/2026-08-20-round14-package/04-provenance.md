# Provenance

## SHAs

```text
repository   LstDtchMn/ScanHound
branch       fix/round12-attestation-authority

code head    75c6b0c1d9a6ed6bd26f7ca2cd00df6413025115
base         6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
```

`75c6b0c` is the last commit touching `backend/` or `tests/`. Any later commit on
the branch adds documentation only — this package. Review `75c6b0c` for code.

This distinction is stated explicitly because an earlier round bound an
attestation to a head and then kept committing, invalidating the document twice.

## Commits, oldest first

```text
90535e7   Round 12: earn the clean attestation, and revoke as one transaction
64815c5   Round 12: fault-injection regression around the real revocation sequence
90ea479   Round 13 request (documentation)
2c89c0c   Round 13 verification figures (documentation)
2eb5d7f   Round 13 as a self-contained package (documentation)
0a6a600   Round 13 M13-1: withhold authority at the consumer
75c6b0c   Round 13: withdraw the WHOLE identity; coverage needs a parsed arm
          ---- code ends here ----
```

Note `0a6a600` and `75c6b0c` are both in scope: the second fixes a regression the
first introduced. The diff in `02-code-changes.patch` is against `main`, so it
shows the net result, not the intermediate mistake. The mistake is described in
`01-request.md` because it is the most instructive thing in the round.

## Blob hashes at the code head

```text
backend/database.py                                64153056b496
backend/scanner_service.py                         b9f113b19deb
backend/background_scanner.py                      618f4a91f3d1
backend/app_service.py                             a2ae21515747
backend/api/routes/scanner.py                      d012239691db
tests/test_round13_hold_withdraws_identity.py      65b475f40924
tests/test_round13_revocation_failclosed.py        f9dd7de6567a
tests/test_round13_parser_health_coverage.py       067590c89fda
```

## Test-container identity

The same pair used for round 13, provisioned identically from `scanhound:latest`
with `backend/`, `tests/`, `pytest.ini`, `main.py`, `docs/` and `scripts/` copied
in, and `pytest`, `pytest-asyncio`, `httpx` installed in both.

`docs/` and `scripts/` matter: without them the suite reports ~74 failures
because `test_version_labeler` reads `docs/kometa/version_badges.yml` and
`test_verification_hold` reaches into `scripts/`. That artifact was diagnosed in
round 13 by reproducing the same 74 on the `main` control.

## What is NOT covered

- **No CI attestation.** The connector exposed no status contexts for this SHA in
  round 13; these are builder-side runs, described so they can be re-run.
- **Nothing is deployed**, verified directly against the running container.
- **The coverage/watermark model is not built.** Evidence and questions only.
- **The corpus measurement in `03-evidence.md` §4** is from the LIVE database in
  the running container, which predates this branch. It describes the data, not
  this code.
