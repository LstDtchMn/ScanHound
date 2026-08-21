# Evidence — commands and results

Throwaway containers from `scanhound:latest`, tree copied in (never the 9p bind
mount), plus `docs/` and `scripts/`. Host/container md5 parity asserted before
every run; after every mutation the mutated file's md5 was checked.

## 1. Targeted suites

```text
tests/test_round16_coverage_evaluator.py          16 passed
tests/test_round16_traversal_emission.py           5 passed
tests/test_round16_low_findings.py                 7 passed
tests/test_round16_journal_durability.py           4 passed
tests/test_round16_alias_and_ordering.py           6 passed
tests/test_round15_restart_durability.py           5 passed
tests/test_round14_listing_claim_ledger.py        26 passed
tests/test_round13_parser_health_coverage.py       4 passed
tests/test_round13_hold_withdraws_identity.py      7 passed
tests/test_round12_attestation_authority.py       13 passed
tests/test_media_kind_is_server_owned.py          30 passed
tests/test_background_scanner.py                  25 passed
                                                  -----------
                                                 148 passed
```

## 2. Mutation — the coverage evaluator

The one named critical in the review comes first.

```text
mutation                                        killed   what it proves

frontier = min(observed posted_date)                 8   THE named requirement.
  fails all three sticky tests, including the
  reviewer's own page-bottom counterexample
deepest anchor accepted uncorroborated               7   the sticky-at-end defence
duplicate_in_run ignored                             1   S8.4
equal timestamps accepted as crossing                1   S8.5
unusable pages traversed anyway                      2   S8.1 / S8.2
```

Under the `min()` mutant, specifically:

```text
FAILED ...::test_a_pinned_old_post_at_the_bottom_of_page_one
FAILED ...::test_an_algorithm_using_min_would_disagree_with_us
FAILED ...::test_a_sticky_in_the_middle_refuses_outright
```

**The positive control matters as much.** Nearly every assertion in this file says
"refuse", so an evaluator that refused everything would satisfy them all while
making attestation impossible forever. `test_a_clean_multi_page_monotonic_traversal_proves_coverage`
is the one that fails if the evaluator becomes unconditionally strict.

## 3. Fixtures, as the review specified them

```text
old sticky post on page 1                  present, and the critical case
repeated URL across pages                  present
equal-minute timestamps                    present
unknown-date policy exclusion              present
HTTP-200 unparseable page                  present
page error before the frontier             present
valid multi-page monotonic traversal       present, as the positive control
```

## 4. End-to-end: the crawler really emits it

The contract and evaluator could both be perfect and worthless if nothing
produced the input, which has been the recurring failure in this work. So
`test_round16_traversal_emission.py` drives the real `_crawl_pages()` and feeds
what it produced into the real evaluator:

```text
a real crawl yields a usable frontier          passed
listing order preserved, positions ascending   passed
an unparseable page is recorded unrecognised   passed
a repeat across pages is FLAGGED, not dropped  passed
the report carries no coverage conclusion      passed
```

## 5. Full suite, like-for-like

```text
                              failed   passed   skipped   duration
main control (origin/main)         1     5320         4   804s
this branch (89706b7)              1     5420         4   832s
```

Identical single pre-existing failure both sides:

```text
FAILED tests/test_dv_settings.py::test_all_frontend_editable_settings_keys_are_in_model
```

**+100 passing, zero net new failures.**

## 6. Live deployment

Running the ROUND-14 code, deployed dark this morning. Nothing in this package is
deployed.

```text
08:05   180 claims / 173 releases
13:09   188 / 181
14:18   190 / 183
15:18   195 / 188

movie-vs-tv conflicts     0   across all 188 releases
posted_date_changed       0   but see below
category_attested         0
downloads.media_kind   NULL   on all 684 rows
errors                    0   in 7 hours
```

**The `posted_date_changed = 0` here is NOT yet meaningful.** The running
container predates the L15-1 fix, so it still has the version of the enrichment
pass that could never raise the flag. A meaningful zero needs the current code
deployed; this one still means only "no change detected by a path that cannot
detect one".

Growth is slow by design: early-stop means a cycle only picks up genuinely new
releases.
