# Evidence — commands and results

All test runs are in throwaway containers from `scanhound:latest` with the tree
copied in (never the 9p bind mount), plus `docs/` and `scripts/`. Host/container
md5 parity was asserted before each run.

## 1. Targeted suites

```text
tests/test_round14_listing_claim_ledger.py        15 passed
tests/test_round13_hold_withdraws_identity.py      7 passed
tests/test_round13_revocation_failclosed.py        7 passed
tests/test_round13_parser_health_coverage.py       4 passed
tests/test_round12_attestation_authority.py       13 passed
tests/test_media_kind_is_server_owned.py          30 passed
tests/test_rescan_preserves_classification.py     12 passed
tests/test_background_scanner.py                  25 passed
                                                  -----------
                                                 113 passed
```

Plus the identity suites, which the broader `season` mask could have disturbed:

```text
test_source_identity_unified.py, test_package_link_provenance.py,
test_hdencode_identity_promotion.py, test_hdencode_year_provenance.py
                                                  38 passed
```

## 2. Mutation results

Applied from a copied script file and verified by md5 before each run. (A
heredoc through `docker exec` without `-i` applies nothing and exits 0, which
makes a mutant look like it survived — that cost a wrong conclusion earlier in
this work.)

```text
mutation                                        killed   what it proves
mask media_kind only (my first attempt)              3   the inversion is detected
  decisive assertion: assert 'tv_season' == 'unknown'
consumer mask removed entirely                       2   the mask is load-bearing
hold taken AFTER the erase                           2   ordering, not just outcome
reconciliation made a no-op                          2   restart recovery is real
mask applied to EVERY row                            4   over-strict is detected
award arm coverage on entry again                    3   parser health is enforced

round 14 -- the claim ledger and the coverage report
first_seen_at overwritten on re-sighting             1   age info is preserved
sightings never increments                           1
order_key overwritten instead of preserved           1   see the note below
claims recorded only by an attesting crawl           1   the ledger is NOT gated
conflict branch never fires in the summary           2   class precedence
```

**One mutant initially SURVIVED, and it was the useful one.** Removing the
`COALESCE` in `record_listing_claims()` changed nothing: every existing test went
through `backfill_listing_claim_order_keys()`, whose `WHERE order_key IS NULL`
already prevents an overwrite, so the `ON CONFLICT` clause was never reached. The
line was real but unexercised. Two tests were added for it and the mutant now
dies. Recorded because a surviving mutant that is quietly fixed afterwards looks
identical to one that never survived.

The last two are the anti-vacuity controls. Every negative assertion in this work
says "no identity" or "not covered", so withdrawing everything — or never
recording coverage — would satisfy them all while destroying the feature.

## 3. Full suite, like-for-like

```text
                              failed   passed   skipped   duration
main control (origin/main)         1     5320         4   804s
this branch (0c0f5d1)              1     5366         4   828s
```

Identical single pre-existing failure on both sides:

```text
FAILED tests/test_dv_settings.py::test_all_frontend_editable_settings_keys_are_in_model
```

**+46 passing, zero net new failures.** `main` has not moved since the round-13
control was measured, and the same container pair was used.

## 4. The corpus measurement behind the watermark discussion

Run against the LIVE database in the running container, read-only:

```python
for (d,) in c.execute("SELECT data FROM background_scan_cache LIMIT 4000"):
    j = json.loads(d); pd = j.get("posted_date")
```

```text
cached rows sampled     4000
with a posted_date      4000   (100%)
samples                 "June 29, 2026 at 11:38 PM"
                        "June 29, 2026 at 11:07 PM"
                        "June 29, 2026 at 10:42 PM"
```

Where it comes from, established by reading the code rather than inferring:

```text
backend/detail_scraper.py:373-405   posted_date is parsed here -- the DETAIL page
backend/scanner_service.py          _select_posts() returns ANCHOR ELEMENTS only;
                                    href and link text. No date, no id, no
                                    ordering token is taken from a listing page.
```

So the order key exists and is durable for the whole corpus, but the listing side
carries none — which is the awkward half, because the releases that most need
attesting are the ones the crawl skips and therefore never detail-fetches.

## 5. The population a conflict could exist in, and why it is unmeasured

Live database, read-only:

```text
cached rows              4195
  category 4k            2101     movie arm
  category tv            1816     tv arm
  category remux          278     movie arm
rows flagged conflicted     0
```

Two things worth drawing out.

**The conflict-eligible population is large.** 2,379 rows came from a movie arm
and 1,816 from the TV arm, so movie-vs-TV disagreement is not a corner case by
volume — it is the shape of the corpus.

**Zero conflicts have ever been recorded**, because the deployed code has no
conflict detection; it predates all of this work. So the obvious question — *how
often do two listings actually disagree?* — has never been answerable here, and I
could not answer it for round 12 either when I wanted to size the risk.

**And it cannot be answered retrospectively.** Each cached row stores exactly ONE
category. Whether that release ALSO appeared under another arm was never written
down: `url_type_claim` held it for the duration of a single crawl and then the
dict went out of scope. That is precisely the evidence `listing_claims` now
captures, and precisely why capturing it could not wait for the coverage-model
ruling — every crawl that ran without it destroyed another cycle's worth.

## 6. Deployment state

```bash
docker exec scanhound sh -c "grep -c 'media_kind' /app/backend/database.py"
```

```text
0        # and 0 for crawl_attestation_verdict
```

Nothing from rounds 10-14 is deployed. Round 13 permitted a dark deployment of
this branch; that decision is the owner's and has not been taken.
