# Evidence — commands and results

Throwaway containers from `scanhound:latest`, tree copied in (never the 9p bind
mount), plus `docs/` and `scripts/`. Host/container md5 parity asserted before
every run; after every mutation the mutated file was checked **in the container**
to confirm the mutation landed.

## 1. Targeted suites

```text
tests/test_round19_one_arm_identity.py            29 passed   NEW
tests/test_round19_arm_key_migration.py           19 passed   NEW
tests/test_round18_arm_scope_and_snapshot.py      22 passed   NEW
tests/test_round16_coverage_evaluator.py          24 passed
tests/test_round16_traversal_emission.py           9 passed
tests/test_round16_alias_and_ordering.py           6 passed
tests/test_round16_low_findings.py                 7 passed
tests/test_round16_journal_durability.py           4 passed
tests/test_round15_restart_durability.py           5 passed
tests/test_round14_listing_claim_ledger.py        26 passed
tests/test_round13_parser_health_coverage.py       4 passed
tests/test_round12_attestation_authority.py       13 passed
tests/test_media_kind_is_server_owned.py          30 passed
tests/test_background_scanner.py                  25 passed
                                                  -----------
                                                 223 passed
```

70 new tests this round, across three files.

## 2. Mutation — every fix reverted to the shape you found

Each mutation reverts ONE fix. A fix no test can distinguish from its own
absence is not a fix, it is a comment.

### Round-18 findings

```text
mutation                                        result   target
M18-1  contract keyed on source again           KILLED   3 failed, 2 passed
M18-2  one duplicate set for the whole crawl    KILLED   2 failed, 1 passed
M18-3a page sealed before enumeration           KILLED   4 failed
M18-3b positions checked for uniqueness only    KILLED   3 failed, 1 passed
M18-4  caller dictionary retained by reference  KILLED   4 failed, 2 passed
```

### Gate item 1 — the unified identity

```text
ledger ignores the stamped key                  KILLED   1 failed, 1 passed
producer stops stamping the key                 KILLED   3 failed
claim dedup keyed on category again             KILLED   1 failed, 2 passed
registry merges a collision silently            KILLED   1 failed, 2 passed
resolve_legacy guesses when ambiguous           KILLED   2 failed, 3 passed
arm key drops the endpoint                      KILLED   8 failed, 16 passed
```

### The migration — the part that touches live rows

```text
merge clobbers instead of summing               KILLED   1 failed, 3 passed
a date change seen by one row is dropped        KILLED   1 failed, 3 passed
alias history is left behind                    KILLED   1 failed, 1 passed
KNOWN_ARMS loses a shipped feed                 KILLED   1 failed, 2 passed
unresolved keys are not logged                  KILLED   1 failed, 5 passed
```

**16 of 16 killed.**

### The passing counts are the point

Every mutation above left tests **passing** as well as failing. That is
deliberate. Nearly every assertion in this work says "refuse", so a
universally-refusing implementation would satisfy them all while making
attestation impossible forever. Each class carries its opposite:

```text
the contracted arm IS authoritative, sibling and parser-mismatch are not
a clean multi-page monotonic traversal MUST prove coverage
a complete page IS sealed usable, with all its sightings
a genuine repeat within ONE arm is still flagged
the snapshot still reflects the values at capture
the shipped descriptor set builds a registry cleanly
one unresolvable legacy key does not block the resolvable ones
```

The one mutation with no surviving positives — `producer stops stamping the
key`, 3 failed and 0 passed — is a whole-class kill, and the class only has
three tests.

## 3. Fixtures from the round-18 gate

```text
two arms sharing a release, each first sighting eligible     present, PRODUCER
the inversion shape, later arm REFUSES                       present, PRODUCER
two feeds of one category, both claims kept                  present, PRODUCER
exception mid-enumeration, partial page unusable             present, PRODUCER
positions [1, 3] and [2, 1]                                  present
caller mutates dates and unstable after capture              present
contract on one arm, sibling and parser-mismatch checked     present
partial vs complete registry on an ambiguous key             present
both key shapes present, merged not clobbered                present
```

## 4. Full suite, like-for-like

Both runs executed **concurrently in this session**, so durations are inflated
by CPU contention and are not comparable to earlier rounds. Pass and fail counts
are unaffected.

```text
                              failed   passed   skipped   duration
main control (origin/main)         1     5320         4   807s
this branch (1aff26f)              1     5502         4   913s
```

Identical single pre-existing failure on both sides:

```text
FAILED tests/test_dv_settings.py::test_all_frontend_editable_settings_keys_are_in_model
```

**+182 passing, zero net new failures.**

`sh-main` was verified to contain **no** `backend/coverage.py` and **no**
`backend/arms.py` before running.

An earlier branch run was **discarded**: I copied new backend files into the
container while it was mid-run. Recorded because a discarded run and a run that
never happened are not the same thing.

## 5. Live deployment

Running the ROUND-14 code, dark since 08:05 local. Nothing from rounds 16
through 19 is deployed.

```text
08:05   180 claims / 173 releases
13:09   188 / 181
15:18   195 / 188
16:0x   198 / 190
later   209 / 201

distinct arm_keys         3   hdencode:tv 78, :4k 69, :remux 62
movie-vs-tv conflicts     0   across all 201 releases
category_attested         0   of 4285 background_scan_cache payloads
downloads.media_kind   NULL   on all 684 rows
posted_date_changed       0
```

Growth is slow by design: early-stop means a cycle only picks up genuinely new
releases.

**Figures that are still not meaningful, and should not be read as evidence:**

- `posted_date_changed = 0` — the running container predates the L15-1 fix, so
  it has the enrichment pass that could never raise the flag.
- `movie-vs-tv conflicts = 0` — a recent-window measurement over ~201 of the
  newest releases. Round 15 measured the frontier depth of a 3-page crawl at
  under a day for the TV arm, so the back catalogue is not sampled at all and
  no rate for it can be inferred.
- `listing_claim_aliases` — the table does not exist in the live database. It
  is a round-15 table and the deployed code is round-14. The alias-rekeying half
  of the migration therefore has no live rows to act on until the new code
  ships.

### One measurement I got wrong while preparing this package

I queried `category_attested` as a column on `downloads`. It is not a column —
it is a key inside the `background_scan_cache.data` JSON payload. The query
raised `no such column` rather than returning zero.

The round-18 figure of `0` is correct and is reproduced above from the right
place. Recorded because the distinction matters: "measured none" and "measured
nothing" look identical in a report, and a query that errors is the only reason
I noticed rather than writing down a zero I had not actually measured.
