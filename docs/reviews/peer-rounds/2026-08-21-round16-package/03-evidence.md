# Evidence — commands and results

Throwaway containers from `scanhound:latest`, tree copied in (never the 9p bind
mount), plus `docs/` and `scripts/`. Host/container md5 parity asserted before
every run; after every mutation the mutated file's md5 was checked to confirm the
mutation landed.

## 1. Targeted suites

```text
tests/test_round16_journal_durability.py           4 passed
tests/test_round16_alias_and_ordering.py           6 passed
tests/test_round15_restart_durability.py           5 passed
tests/test_round14_listing_claim_ledger.py        26 passed
tests/test_round13_hold_withdraws_identity.py      7 passed
tests/test_round13_revocation_failclosed.py        7 passed
tests/test_round12_attestation_authority.py       13 passed
tests/test_media_kind_is_server_owned.py          30 passed
tests/test_background_scanner.py                  25 passed
                                                  -----------
                                                 123 passed
```

## 2. Mutation results — nine applied, nine killed

```text
mutation                                        killed   finding

M15-1, the journal
torn lines skipped as harmless                       1   1B
unclosed sessions ignored                            1   1A
unreadable journal treated as empty                  1   1B

M15-2, raw aliases
consumer reads listing_claims again                  1   the aliases are lost

M15-3, ordering
consume gated on _claims again                       1   quiet-cycle case
enrichment moved ahead of the consumer in one try    1   your case

earlier rounds, re-run and still killed
mask media_kind only (the fail-open inversion)       3
hold taken AFTER the erase                           2
award arm coverage on entry again                    3
```

Each of the six new mutants is killed by **exactly one** test, which is the
result I wanted: a mutant killed by many tests usually means the tests overlap
rather than that they discriminate.

**Positive controls, one per class**, because nearly every assertion in this work
says "no identity" or "interlock", and a mechanism that always interlocks would
satisfy them all while destroying the feature:

```text
a clean shutdown must NOT interlock
aliases must NOT fragment the canonical identity
an unconditional consumer must NOT revoke an uncontradicted release
```

## 3. Full suite, like-for-like

```text
                              failed   passed   skipped   duration
main control (origin/main)         1     5320         4   804s
this branch (6869886)              1     5392         4   808s
```

Identical single pre-existing failure both sides:

```text
FAILED tests/test_dv_settings.py::test_all_frontend_editable_settings_keys_are_in_model
```

**+72 passing, zero net new failures.** `main` has not moved since the round-13
control was measured, and the same container pair was used throughout.

## 4. Live data from the running deployment

The container running in production is the **round-14 code**, deployed dark this
morning. Nothing in this package is deployed yet.

```text
08:05   deployed, first cycle    180 claims / 173 releases
11:03                            185 / 178
11:58                            186 / 179
13:09                            188 / 181

movie-vs-tv conflicts              0   across all 181 releases
category_attested                  0
downloads.media_kind            NULL   on all 684 rows
errors                             0   in 6 hours
```

Growth is slow by design: early-stop means a cycle only picks up genuinely new
releases, so this accumulates at roughly the rate the site publishes.

**Still a recent-window measurement.** 181 releases is the newest slice, not the
4,251-row cache. Combined with the frontier depth measured in round 15 — three
pages of the TV arm reaching back **less than a day** — the back catalogue is not
being sampled here at all, and no conflict rate for it can be inferred from this.

## 5. Deploy script

Verified by execution, not by reading:

```text
PowerShell 5.1 parses it clean
the reviewed-head pin passes:
    "OK backend/ and tests/ match the reviewed head ef2fb18..."
the dirty-tree gate correctly refused to run while the fix itself was
    uncommitted
```

Two defects fixed BEFORE this round, both introduced by rewrites made after the
round-14 review: `${tmpInContainer}?mode=ro` (PowerShell swallowed `?mode` into
the variable name, so verification opened a new empty database and blamed the
real one) and a readiness poll using `wget`, which is not in the image — so a
SUCCESSFUL deploy reported failure and printed rollback instructions.

That pattern is the reason S10 of round 15 was worth doing: the script has been
rewritten after every review that touched it, and each rewrite was unreviewed.
