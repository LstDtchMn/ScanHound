# Provenance — exact identity of what is under review

## SHAs

```text
repository   LstDtchMn/ScanHound
branch       fix/round12-attestation-authority

code head    64815c52fab412acbb79056ab02c9e4b6a02e33a
base         6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
```

**Code head vs branch head.** `64815c5` is the last commit touching `backend/` or
`tests/`. Every later commit on the branch adds documentation only — this
package. Review `64815c5` for code; the branch head exists so the package travels
with it.

This distinction is stated because a previous round bound an attestation to a
head and then kept committing, which invalidated the document twice.

## Commits, oldest first

```text
90535e7   Round 12: earn the clean attestation, and revoke as one transaction
64815c5   Round 12: fault-injection regression around the real revocation sequence
          ---- code ends here; documentation only below ----
90ea479   Round 13 request: the round-12 remediation, verification figures pending
2c89c0c   Round 13: verification figures, measured like-for-like against origin/main
```

## Blob hashes at the code head

Independent of the diff, so a file can be verified directly.

```text
backend/background_scanner.py                    54a1916d6ca8
backend/database.py                              a946f4731d31
backend/scanner_service.py                       e6b08ac4ea26
backend/api/routes/scanner.py                    d012239691db
tests/test_round12_attestation_authority.py      4e9ad94898cd
```

## Test-container identity

Both suite runs used containers built the same way, in the same session, from
`scanhound:latest`, with the tree copied in (never the 9p bind mount) plus
`docs/` and `scripts/`. `pytest`, `pytest-asyncio` and `httpx` installed
identically in both.

The only intended difference was the code tree, asserted by md5 on the file
carrying most of the change:

```text
backend/background_scanner.py
  branch container   b9e5184af6fb2ad64bc8203187b1a165
  main container     49f09c813b284208f2960245f94855da
```

Host/container parity was asserted before each run, because a stale container
tree has previously invented failures in this project and produced a wrong
conclusion.

## What is NOT covered by this package

- **No CI attestation.** I cannot vouch for a CI run at this SHA; the figures are
  builder-side runs, described exactly so they can be re-run.
- **No deployment.** Nothing here is deployed, verified directly against the
  running container (`03-evidence.md`, section 6).
- **The dedicated coverage/backfill model is not built**, which is the open
  question this round asks. See `01-request.md`.
