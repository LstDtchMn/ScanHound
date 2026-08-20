# Provenance

## SHAs

```text
repository   LstDtchMn/ScanHound
branch       fix/round12-attestation-authority

code head    0c0f5d12b48535991ec1b31ee58e7d3834210e44
base         6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
```

`0c0f5d1` is the last commit touching `backend/` or `tests/`. Any later commit on
the branch adds documentation only — this package.

Stated explicitly because an earlier round bound an attestation to a head and then
kept committing, invalidating the document twice.

## Commits since main, oldest first

```text
90535e7   R12  earn the clean attestation, and revoke as one transaction
64815c5   R12  fault-injection regression around the real revocation sequence
90ea479   doc  round 13 request
2c89c0c   doc  round 13 verification figures
2eb5d7f   doc  round 13 as a self-contained package
0a6a600   R13  a pending revocation withholds authority at the CONSUMER
75c6b0c   R13  the hold must withdraw the WHOLE identity; coverage needs a parsed arm
393eaf9   doc  round 14 package
9245c24   R14  keep the listing claims, before the releases age off
0c0f5d1   R14  report the unknowns as a measured class
          ---- code ends here ----
```

`0a6a600` and `75c6b0c` are both in scope: the second fixes a regression the first
introduced. `02-code-changes.patch` is against `main`, so it shows the net result
rather than the intermediate mistake — the mistake is described in `01-request.md`
because it is the most instructive thing in the round.

## Blob hashes at the code head

```text
backend/database.py                              9c890a5a390f
backend/scanner_service.py                       355bb82ca871
backend/background_scanner.py                    ddcdf3de0c8a
backend/app_service.py                           a2ae21515747
tests/test_round14_listing_claim_ledger.py       903482048bff
```

## Test-container identity

The same pair used for rounds 13 and 14, provisioned identically from
`scanhound:latest` with `backend/`, `tests/`, `pytest.ini`, `main.py`, `docs/`
and `scripts/` copied in, and `pytest`, `pytest-asyncio`, `httpx` installed in
both.

`docs/` and `scripts/` matter: without them the suite reports ~74 failures because
`test_version_labeler` reads `docs/kometa/version_badges.yml` and
`test_verification_hold` reaches into `scripts/`. Diagnosed in round 13 by
reproducing the same 74 on the `main` control.

Host/container md5 parity was asserted before every run, and after every mutation
the mutated file's md5 was checked to confirm the mutation actually landed.

## What is NOT covered

- **No CI attestation.** Builder-side runs, described so they can be re-run.
- **Nothing is deployed**, verified directly against the running container.
- **The coverage/watermark model is not built.** Evidence and questions only; the
  claim LEDGER is built, and it authorizes nothing.
- **The corpus measurements** in `03-evidence.md` come from the LIVE database in
  the running container, which predates this branch. They describe the data, not
  this code.
- **`main` has not moved** since the round-13 control was measured, so the control
  figure is the one measured then.
