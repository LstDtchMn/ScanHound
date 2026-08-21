# Evidence — commands and results

Throwaway containers from `scanhound:latest`, tree copied in (never the 9p bind
mount), plus `docs/` and `scripts/`. Host/container md5 parity asserted before
every run; after every mutation the mutated file's md5 was checked to confirm the
mutation actually landed.

## 1. Targeted suites

```text
tests/test_round15_restart_durability.py           5 passed
tests/test_round14_listing_claim_ledger.py        26 passed
tests/test_round13_hold_withdraws_identity.py      7 passed
tests/test_round13_revocation_failclosed.py        7 passed
tests/test_round13_parser_health_coverage.py       4 passed
tests/test_round12_attestation_authority.py       13 passed
tests/test_media_kind_is_server_owned.py          30 passed
tests/test_background_scanner.py                  25 passed
                                                  -----------
                                                 117 passed
```

## 2. Mutation results

```text
mutation                                        killed   what it proves

round 15 -- restart durability
recovery ignores the independent journal             1   THE M14-1 CASE
  fails with: "startup found no record of the interrupted revocation,
               so the stale authority is about to be served again"
journal failure no longer disables authority         1   the interlock is real

round 14 -- cross-crawl revocation
revoke on ANY claim, not only contradictory ones     3   over-strict detected
consumer never revokes                               3   under-strict detected

round 14 -- the ledger
first_seen_at overwritten on re-sighting             1
sightings never increments                           1
order_key overwritten instead of preserved           1   (see note)
claims recorded only by an attesting crawl           1   the ledger is NOT gated
conflict branch never fires in the summary           2

round 13 -- the hold
mask media_kind only (my first attempt)              3   the inversion
  decisive: assert 'tv_season' == 'unknown'
consumer mask removed entirely                       2
hold taken AFTER the erase                           2
reconciliation made a no-op                          2
mask applied to EVERY row                            4   over-strict detected
award arm coverage on entry again                    3   parser health
```

**The over-strict rows are the anti-vacuity controls.** Nearly every negative
assertion in this work says "no identity" or "not covered", so withdrawing
everything — or never recording coverage — would satisfy them all while
destroying the feature. Each has a paired positive control.

**A note on the `order_key` mutant.** It initially SURVIVED: every test went
through the backfill, whose `WHERE ... IS NULL` already prevents an overwrite, so
the `ON CONFLICT` clause was never reached. The line was real but unexercised.
That is what revealed the date-change question as a SEMANTIC decision, which is
now `posted_date_changed` rather than a silent coalesce. Recorded because a
survivor that gets quietly fixed looks identical to one that never survived.

## 3. Full suite, like-for-like

```text
                              failed   passed   skipped   duration
main control (origin/main)         1     5320         4   804s
this branch (ef2fb18)              1     5382         4   816s
```

Identical single pre-existing failure on both sides:

```text
FAILED tests/test_dv_settings.py::test_all_frontend_editable_settings_keys_are_in_model
```

**+62 passing, zero net new failures.** `main` has not moved since the round-13
control was measured, and the same container pair was used. Parity for this run:

```text
backend/database.py   container ab64f631...   host ab64f631...
```

## 4. Corpus measurements (live database, read-only)

```text
cached rows              4195
  category 4k            2101     movie arm
  category tv            1816     tv arm
  category remux          278     movie arm
rows flagged conflicted     0

posted_date present      4000 / 4000 sampled   (100%)
samples                  "June 29, 2026 at 11:38 PM"
```

Both populations are large, so movie-vs-TV disagreement is the shape of the
corpus rather than a corner case. **No conflict rate should be inferred from the
2379/1816 split** — it establishes only that both populations exist.

Zero recorded conflicts says nothing about the true disagreement rate: the
deployed code never persisted a second claim, and `url_type_claim` held it only
for the life of one crawl. That is why the question cannot be answered
retrospectively, and why the ledger could not wait for the coverage ruling.

Where `posted_date` comes from, established by reading rather than inferring:

```text
backend/detail_scraper.py     parsed here -- the DETAIL page
backend/scanner_service.py    _select_posts() returns ANCHOR ELEMENTS only.
                              No date, no id, no ordering token is taken from
                              a listing page.
```

## 5. Deployment state

```bash
docker exec scanhound sh -c "grep -c 'media_kind' /app/backend/database.py"
```

```text
0
```

Nothing from rounds 10-15 is deployed. Dark deployment remains approved by round
14; that decision is the owner's and has not been taken.
