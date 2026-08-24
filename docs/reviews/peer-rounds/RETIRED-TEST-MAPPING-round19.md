# Retired-test mapping — the two round-19 identity suites

**Retired at:** `1f77a1d` (2026-08-24)
**Files:** `tests/test_round19_one_arm_identity.py` (24 tests),
`tests/test_round19_arm_key_migration.py` (19 tests) — 43 tests, 606 lines.

## Two retractions

### 1. The original claim

In the round-21 package I wrote:

> Every intent of those files that still holds is carried forward.

**That claim was false and is withdrawn.** Peer review checked it against the
actual diff — which is what I had asked for — and found four intents with no
destination. One of them (R21-10a) was a live safety regression, not a
bookkeeping gap: the replacement test was *named*
`test_BOTH_raw_variants_survive_as_aliases` while asserting `len(aliases) >= 1`,
and the production code had stopped persisting the second raw href at all.
Because `consume_cross_crawl_conflicts()` revokes by enumerating
`listing_claim_aliases`, a variant missing there is a download row that keeps
its media kind after the release has been contradicted — the exact M15-2 hole
the alias table was introduced to close.

The failure was not a lack of mutation testing. Every individual replacement
test discriminated against the bug it targeted. The failure was **contract
inventory**: no one asked, per retired assertion, "what still enforces this?"
This table is that question, asked late. Future retirements do it first.

### 2. The table's own A entries

Round 22 checked this table the way I asked it to, and found **three entries
marked A whose named destinations did not exercise the same production path**.
The implementations were correct by composition, but "correct by composition"
is not what **A** promises, and a mapping that overstates its own legend is the
original failure with a table in front of it.

All three are now direct regressions rather than reclassifications --
`TestTheThreeOverstatedMappingEntries` -- because composition arguments are
exactly what went wrong the first time. The counts below are unchanged: the
dispositions were right, only the destinations were overstated.

## Legend

| | |
|---|---|
| **A** | a surviving test exercises the same production path |
| **B** | a replacement test is strictly stronger |
| **C** | the behaviour is intentionally obsolete; the rule that superseded it is named |
| **LOST** | found by review to have no destination — and what was done about it |

---

## `test_round19_one_arm_identity.py`

### TestTheShippedSourcesDoNotMerge

| Retired test | | Destination |
|---|---|---|
| `test_every_shipped_feed_has_its_own_arm_key` | **B** | `TestTheDeclaredArmsMatchTheProducer::test_no_declared_arm_is_unproducible` + `test_every_emitted_feed_resolves_to_a_declared_arm`. Stronger: checks BOTH directions against the real `_build_sources()`, where the old test compared a hand-copied descriptor list. |
| `test_the_two_ddlbase_remux_feeds_are_distinct` | **A** | `TestTheDeclaredArmsMatchTheProducer::test_the_two_ddlbase_remux_feeds_are_distinct` |
| `test_they_were_the_same_under_the_legacy_shape` | **A** | `test_they_were_one_key_under_the_legacy_shape` |
| `test_the_live_hdencode_keys_are_unchanged_in_source_and_category` | **C** | The assertion was that `arm_key` starts with `legacy_key + ":"`. Round 20 made ids **opaque** precisely so nothing may infer structure from them; `TestArmIdIsOpaque` enforces the opposite rule. Legacy resolution is covered by `supersedes` instead. |

### TestTheRegistryRefusesToMergeFeeds

| Retired test | | Destination |
|---|---|---|
| `test_a_genuine_collision_raises_rather_than_merging` | **B** | `TestTheRegistryRefusesToMergeFeeds` now has **four** refusal tests, not one: duplicate id, one request under two names, a legacy key claimed by two arms, and a legacy key equal to a live id. Only the first existed before. |
| `test_the_shipped_set_builds_cleanly` | **A** | `test_the_shipped_set_builds_cleanly` |
| `test_an_identical_repeat_is_not_a_collision` | **A** | `test_an_identical_repeat_is_not_a_collision` |

### TestLegacyKeysMigrateOnlyWhenTheAnswerIsKNOWN

| Retired test | | Destination |
|---|---|---|
| `test_every_live_key_resolves_deterministically` | **A** | `TestAttribution::test_apply_attributes_every_live_key` |
| `test_an_ambiguous_legacy_key_is_reported_not_guessed` | **B** | `TestAmbiguityIsNeverResolvedByGuessing::test_the_ambiguous_key_is_quarantined` — now also asserts a durable audited snapshot and a reason, not just a log line. |
| `test_a_key_for_a_feed_that_no_longer_exists_is_unresolved` | **A** | `test_a_key_for_a_feed_that_no_longer_exists_is_unresolved` |
| `test_a_key_that_is_already_modern_is_left_entirely_alone` | **A** | `TestAttribution::test_applying_twice_changes_nothing` |
| `test_a_mixed_ledger_migrates_the_knowable_part` | **A** | `test_the_knowable_rows_still_move_alongside` |

### TestTheProducerStampsTheKeyTheTraversalReports

| Retired test | | Destination |
|---|---|---|
| `test_the_claim_carries_an_arm_key` | **B** | `TestTheProducerStampsWhatTheTraversalReports::test_every_claim_carries_a_full_revision` — asserts all three components, not just a name. |
| `test_it_is_the_SAME_key_the_traversal_reports` | **A** | `test_the_claim_names_what_the_traversal_names` |
| `test_the_key_is_three_parts_not_the_legacy_two` | **C** | Asserted `arm_key.count(":") == 2`. Round 20 made ids opaque and colon-free; the superseding rule is `TestArmIdIsOpaque` plus `test_the_stamped_id_is_the_DECLARED_one`. |

### TestTheLedgerStoresTheStampedKey

| Retired test | | Destination |
|---|---|---|
| `test_a_stamped_claim_is_stored_under_its_own_key` | **B** | `TestTheWriterStoresTheRevision::test_a_stamped_claim_is_stored_under_its_revision` and `test_two_revisions_of_one_arm_do_not_collide`. |
| `test_an_unstamped_claim_still_records_rather_than_dropping` | **LOST → restored** | R21-10c. The replacement supplied `arm_key="arm.hdencode.tv-packs"` and only omitted the versions, so it tested *missing versions*, never the actual no-arm path. Now `TestTheArmIdNamespaceIsGuarded` parametrises **five** cases including "nothing stamped at all", and asserts the row is recorded UNATTRIBUTED with `arm_id` NULL. |

### TestEndpointSlug

| Retired test | | Destination |
|---|---|---|
| `test_slugs` | **C** | `endpoint_slug()` no longer exists. Round 20 derives identity from a hashed request definition rather than a URL path segment, so there is no slug to normalise. |
| `test_case_and_whitespace_are_normalised` | **C** | Same. Normalisation now lives in `RequestDefinition.canonical()`, covered by `test_the_normaliser_version_is_inside_the_digest`. |

### TestArmSpecIsInert

| Retired test | | Destination |
|---|---|---|
| `test_a_spec_has_no_authority_field` | **A** | `TestArmSpecIsInert::test_a_spec_has_no_authority_field` |
| `test_specs_are_frozen` | **A** | `TestArmSpecIsInert::test_specs_are_frozen` |

### TestTwoFeedsOfOneCategoryBothKeepTheirClaim

| Retired test | | Destination |
|---|---|---|
| `test_both_feeds_record_their_own_claim` | **LOST → restored** | R21-10b. `TestTwoFeedsOfOneCategoryBothKeepTheirClaim` is rebuilt as a real two-feed crawl, and now also asserts the CONSUMER: `test_both_claims_survive_into_the_ledger`. |
| `test_the_traversal_reports_two_arms_for_them` | **LOST → restored** | `test_the_traversal_reports_two_arms` |
| `test_a_genuine_repeat_within_one_feed_is_still_collapsed` | **LOST → restored** | `test_a_genuine_repeat_WITHIN_one_feed_is_still_collapsed` |

---

## `test_round19_arm_key_migration.py`

### TestTheLiveLedgerShape

| Retired test | | Destination |
|---|---|---|
| `test_all_three_live_keys_move` | **A** | `TestAttribution::test_apply_attributes_every_live_key` |
| `test_running_it_twice_changes_nothing` | **A** | `TestAttribution::test_applying_twice_changes_nothing` |
| `test_an_empty_ledger_is_a_no_op` | **A** | `TestTheThreeOverstatedMappingEntries::test_an_empty_ledger_migration_is_a_no_op`. **Corrected in round 22:** previously pointed at a lifecycle test that calls a *different method*, plus dry-run tests that all use a POPULATED ledger -- nothing exercised the empty branch and no counter was asserted zero. |
| `test_no_row_is_lost` | **B** | `TestTheShapeMigration::test_every_row_survives_untouched` compares per-row tuples, and `rebuild_equivalence_failure()` now enforces the same thing in PRODUCTION with seven dedicated tests. |

### TestAmbiguityIsNeverResolvedByGuessing

| Retired test | | Destination |
|---|---|---|
| `test_the_ddlbase_remux_rows_stay_where_they_are` | **B** | `test_the_quarantined_row_stays_in_the_ledger` — also asserts it stays UNATTRIBUTED rather than wearing a phantom arm id. |
| `test_it_is_logged_not_silently_skipped` | **B** | `test_the_ambiguous_key_is_quarantined` asserts a durable audited row with a reason, which a log line is not. |
| `test_a_feed_that_no_longer_exists_stays` | **A** | `TestTheThreeOverstatedMappingEntries::test_a_row_for_a_vanished_feed_SURVIVES_migration`. **Corrected in round 22:** previously pointed at a resolver-only test that proves classification and never put such a row through the migration -- which is where the observation could actually be lost. |
| `test_the_knowable_rows_still_move_alongside` | **A** | same name |
| `test_a_partial_registry_cannot_be_used_to_resolve_it` | **A** | `test_a_partial_registry_cannot_resolve_it` |
| `test_the_migration_refuses_to_run_without_a_registry` | **A** | `TestAttribution::test_it_refuses_to_run_without_a_registry` |

### TestBothShapesPresentAreMergedNotClobbered

| Retired test | | Destination |
|---|---|---|
| `test_the_two_rows_become_one` | **A** | `TestCollisionsAreMergedNotClobbered::test_the_two_rows_become_one` |
| `test_the_sightings_are_summed_not_replaced` | **A** | `test_the_sightings_are_summed` |
| `test_the_earliest_first_seen_survives` | **A** | `test_the_span_is_unioned` |
| `test_a_date_change_seen_by_either_row_survives` | **B** | Four tests now, including `test_the_posted_date_is_merged_not_dropped` and `test_the_LEGACY_date_wins_when_the_legacy_row_was_seen_later`, which is the axis the original could not distinguish. |

### TestAliasHistoryMovesWithTheClaim

| Retired test | | Destination |
|---|---|---|
| `test_aliases_are_rekeyed` | **LOST → restored** | R21-10d. Nothing put an alias through semantic attribution, and that gap is exactly what let **R21-11** through: a colliding alias hit the composite key and aborted the whole migration. `TestAliasHistoryMovesWithTheClaim` is rebuilt with four tests, including `test_the_colliding_histories_are_MERGED_not_discarded`. |
| `test_an_unresolvable_arms_aliases_stay_put` | **A** | `TestTheThreeOverstatedMappingEntries::test_an_unresolvable_arms_aliases_stay_attached`, plus its quarantine-snapshot and narrowing-consumer companions. **Corrected in round 22:** previously pointed at a test that creates a NEW unattributed claim; it never put a preexisting legacy alias through semantic migration. |

### TestTheStaticTableMatchesWhatTheProducerEmits

| Retired test | | Destination |
|---|---|---|
| `test_every_emitted_feed_is_in_the_table` | **A** | `test_every_emitted_feed_resolves_to_a_declared_arm` |
| `test_the_table_has_no_feeds_the_producer_cannot_emit` | **A** | `test_no_declared_arm_is_unproducible` |
| `test_the_table_itself_builds_a_registry` | **A** | `test_the_shipped_set_builds_cleanly` |

---

## Summary

| Disposition | Count |
|---|---|
| **A** — same path still exercised | 22 |
| **B** — strictly stronger replacement | 10 |
| **C** — intentionally obsolete, superseding rule named | 5 |
| **LOST** — no destination, found by review, now restored | 6 |
| **Total retired** | 43 |

Two of the six losses were live defects rather than coverage gaps: R21-10a
(raw aliases dropped before persistence) and, via the R21-10d gap, R21-11
(alias collision aborting the migration). Both are fixed with behavioural
regressions, and R21-10a has a mutation check confirming the old behaviour is
killed by the new tests.
