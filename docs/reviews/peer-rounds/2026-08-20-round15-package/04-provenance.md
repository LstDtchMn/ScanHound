# Provenance

## SHAs

```text
repository   LstDtchMn/ScanHound
branch       fix/round12-attestation-authority

code head    ef2fb188342350507eeb649f533f3b197fc031e2
base         6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
```

`ef2fb18` is the last commit touching `backend/` or `tests/`. Any later commit on
the branch adds documentation only — this package. Stated explicitly because an
earlier round bound an attestation to a head and then kept committing,
invalidating the document twice.

## Commits since main, oldest first

```text
90535e7   R12  earn the clean attestation, and revoke as one transaction
64815c5   R12  fault-injection regression around the real revocation sequence
90ea479   doc  round 13 request
2c89c0c   doc  round 13 verification figures
2eb5d7f   doc  round 13 as a self-contained package
0a6a600   R13  a pending revocation withholds authority at the CONSUMER
75c6b0c   R13  the hold must withdraw the WHOLE identity; parsed-arm coverage
393eaf9   doc  round 14 package
9245c24   R14  keep the listing claims, before the releases age off
0c0f5d1   R14  report the unknowns as a measured class
f4ce610   doc  round 14 package figures
ef2fb18   R15  independent journal, cross-crawl revocation, ledger shape
          ---- code ends here ----
```

Three commits fix defects introduced by earlier commits in this same branch:

```text
75c6b0c  fixes the mask inversion introduced by 0a6a600
ef2fb18  fixes the circular restart recovery in 0a6a600 / 75c6b0c
ef2fb18  removes a duplicate listing_claim_summary introduced while reshaping
```

`02-code-changes.patch` is against `main`, so it shows the net result rather than
the intermediate mistakes. Those are described in `01-request.md`, because in
this branch they have been the most instructive part of each round.

## Blob hashes at the code head

```text
backend/database.py                              f8b6ee3b56b8
backend/scanner_service.py                       355bb82ca871
backend/background_scanner.py                    efaaa996ab16
tests/test_round15_restart_durability.py         c0bc8ec7c9e2
tests/test_round14_listing_claim_ledger.py       bdfa4d1666c2
```

## New surface added by this branch

All in `DatabaseManager` unless noted:

```text
authority hold      hold_media_kind, release_media_kind_hold,
                    is_media_kind_held, held_media_kinds
revocation          record_classification_conflicts_and_retract_kinds,
                    _mark_category_conflicts, reconcile_unrevoked_conflicts
journal (round 15)  _revocation_journal_path, _journal_append,
                    read_pending_revocations, _journal_compact
claim ledger        record_listing_claims, backfill_listing_claim_posted_dates,
                    get_listing_claims, listing_claim_summary
narrowing           consume_cross_crawl_conflicts
reporting           media_kind_coverage_summary
crawler             crawl_attestation_verdict (backend/background_scanner.py)
```

## Test-container identity

The same pair used since round 13, provisioned identically from
`scanhound:latest` with `backend/`, `tests/`, `pytest.ini`, `main.py`, `docs/`
and `scripts/` copied in, and `pytest`, `pytest-asyncio`, `httpx` installed in
both.

`docs/` and `scripts/` matter: without them the suite reports ~74 failures
because `test_version_labeler` reads `docs/kometa/version_badges.yml` and
`test_verification_hold` reaches into `scripts/`. Diagnosed in round 13 by
reproducing the same 74 on the `main` control.

## What is NOT covered

- **No CI attestation.** Builder-side runs, described so they can be re-run.
- **Nothing is deployed**, verified directly against the running container.
- **The contiguous-frontier coverage proof is not built.** `min(observed
  posted_date)` is accepted as rejected.
- **The revocation journal has not been exercised against a real disk failure**,
  only an injected one. Its behaviour under a full/read-only volume is reasoned,
  not measured.
- **Corpus measurements** come from the LIVE database, which predates this
  branch. They describe the data, not this code.
- **`main` has not moved** since the round-13 control was measured.
