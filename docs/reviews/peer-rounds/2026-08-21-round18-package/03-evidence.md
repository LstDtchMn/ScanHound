# Evidence — commands and results

Throwaway containers from `scanhound:latest`, tree copied in (never the 9p bind
mount), plus `docs/` and `scripts/`. Host/container md5 parity asserted before
every run; after every mutation the mutated file was checked to confirm the
mutation landed.

## 1. Targeted suites

```text
tests/test_round16_coverage_evaluator.py          24 passed
tests/test_round16_traversal_emission.py           9 passed
tests/test_round16_low_findings.py                 7 passed
tests/test_round16_journal_durability.py           4 passed
tests/test_round16_alias_and_ordering.py           6 passed
tests/test_round15_restart_durability.py           5 passed
tests/test_round14_listing_claim_ledger.py        26 passed
tests/test_round13_parser_health_coverage.py       4 passed
tests/test_round12_attestation_authority.py       13 passed
tests/test_media_kind_is_server_owned.py          30 passed
tests/test_background_scanner.py                  25 passed
                                                  -----------
                                                 153 passed
```

## 2. Mutation — the four M17 findings

```text
mutation                                        killed   finding

duplicate_in_run keyed on raw href again             2   M17-1, raw-alias variant
page continuity validation removed                   3   M17-2
only the first required arm is checked               3   M17-3
every proof marked authoritative                     3   M17-1, the telemetry gate
failed pages emit no observation                     1   M17-2, required change 1
```

### One of these initially SURVIVED, and that is the useful part

Reverting `duplicate_in_run` to raw-href keying left the ENTIRE evaluator suite
green. `TestOneCanonicalPostUnderTwoRawAliases` builds its `Sighting`s by hand
with `duplicate_in_run=True`, so it proves the evaluator HONOURS the flag and
proves nothing about whether the crawler ever SETS it.

That is the producer-versus-component gap, appearing inside the fix for a finding
about exactly that. A producer test now drives a real crawl with one canonical
release under two cosmetic raw hrefs, and the revert fails with:

```text
AssertionError: the aliased repeat corroborated the old terminal post,
manufacturing years of coverage from one page
```

Reported rather than quietly fixed, because a survivor that gets patched
afterwards looks identical to one that never survived.

### Positive controls

Nearly every assertion in this work says "refuse", so a universally-refusing
implementation would satisfy them all while making attestation impossible
forever. Each class carries its opposite:

```text
a clean multi-page monotonic traversal MUST prove coverage
all required arms crossing MUST pass
both raw variants MUST still be OBSERVED, only the second flagged
```

## 3. Fixtures from the round-17 gate

```text
two distinct terminal old posts                    present, documents the defeat
one canonical terminal post under raw aliases      present, at the PRODUCER
a real missing page (exception on page 2)          present, end to end
one failed same-type arm                           present
required arm absent entirely                       present
empty required set                                 present, refuses
```

## 4. Full suite, like-for-like

```text
                              failed   passed   skipped   duration
main control (origin/main)         1     5320         4   804s
this branch (4c0f1de)              1     5432         4   856s
```

Identical single pre-existing failure both sides:

```text
FAILED tests/test_dv_settings.py::test_all_frontend_editable_settings_keys_are_in_model
```

**+112 passing, zero net new failures.**

## 5. Live deployment

Running the ROUND-14 code, dark since 08:05 local. Nothing from rounds 16, 17 or
18 is deployed.

```text
08:05   180 claims / 173 releases
13:09   188 / 181
15:18   195 / 188
16:0x   198 / 190

movie-vs-tv conflicts     0   across all 190 releases
category_attested         0
downloads.media_kind   NULL   on all 684 rows
errors                    0
```

Growth is slow by design: early-stop means a cycle only picks up genuinely new
releases.

**Two figures here are still not meaningful, and should not be read as evidence:**

- `posted_date_changed = 0` — the running container predates the L15-1 fix, so it
  has the enrichment pass that could never raise the flag.
- `movie-vs-tv conflicts = 0` — a recent-window measurement over ~190 of the
  newest releases. Round 15 measured the frontier depth of a 3-page crawl at
  under a day for the TV arm, so the back catalogue is not being sampled at all
  and no rate for it can be inferred.
