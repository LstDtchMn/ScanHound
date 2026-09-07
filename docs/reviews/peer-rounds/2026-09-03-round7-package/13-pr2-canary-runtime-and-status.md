# PR 2 evidence — RSS canary part 2 (2026-09-07)

Branch `feat/rss-canary-authority-and-evidence`, stacked on #108 (`fix/r7b-refuse-blind-rss-primary` @ `2e91de0`). Part 1 was reviewed and closed PASS on 2026-09-06 (`12-pr1-canary-authority-evidence.md`); everything below is on top of that, and `CANARY_IMPLEMENTED` is now `True`, so primary is reachable for the first time.

Design of record: `11-rss-hybrid-canary-slice-design-rev3.md`, closed PASS.

## 1. What part 2 builds

| area | what it does |
|---|---|
| the canary crawl | under `rss_primary` the HDEncode listing keeps running at contract depth on its own schedule, **with early stop disabled** — the crawler normally stops at the first page with nothing new, which is right for discovery and wrong for evidence, since the canary's claim is "these pages were observed" |
| one crawl, two jobs | when the RSS poll degrades AND the canary is due, a single crawl at canary depth serves both; it strictly covers the one-page fallback, and the fallback flag is kept so the cycle still reports that it acquired through the listing |
| membership producer | every sighting is recorded per source, page and rank **before** the crawler's global `seen_post_urls` dedup, so a release listed under two categories produces two rows |
| `rss_canary_policy` | pure, no I/O: sampled-cycle selection, replay, overlap, churn, coverage classification, systematic gap |
| grading | success / incomplete / overlap_lost / error, with the reason. Only a success moves `last_success_at`, so a source that keeps failing ages into `canary_stale` and the authority revokes rather than calling itself protected |
| evidence → authority | staleness, the four coverage states, overlap loss and the systematic-gap check are read from the canary's own evidence and classified as suspensions or revocations |
| demotion | a revocation persists `rss_shadow`, deletes the promotion record and records why; returning to primary requires a fresh explicit promotion |
| status surface | per-source canary health and the request ledger's raw totals, published on `GET /rss/status` under `promotion` |

## 2. The defect I would most like a second opinion on

**Two key spaces met and did not match, and nothing failed.**

The contract names canary sources by listing category — `"4k"`, `"remux"`, `"tv"` — because that is what an operator configures and how `hdencode_listing_canary_feed_map` is keyed. The crawler names each listing arm `"<source>:<category>"`, so what reaches `hdencode_listing_membership` and `hdencode_canary_state` is `"hdencode:4k"`. Every consumer looked the configured name up verbatim:

- `_canary_health` — the canary read as never having run, however well it was running;
- `_canary_is_due` — the schedule was never found, so the canary was due on **every** cycle. The hybrid would have spent more requests than the listing-only mode it replaces;
- `canary_evidence` — membership was never found, so the evidence stayed permanently too thin to judge and a promoted system could never clear its own suspension.

Every one of those is fail-closed, so nothing unsafe would have run — but the feature could not have worked, and the request-cost claim the whole hybrid rests on would have been inverted in production.

**Why the suite did not catch it.** Every test used one spelling on both sides of the boundary. `test_canary_grading.py` configured `["hdencode:4k"]` and asserted against `"hdencode:4k"`; the primary-mode tests built their fake scheduling state from `contract_inputs(...)["hdencode_listing_canary_sources"]`, i.e. from the same list the consumer read. Both agreed with themselves.

The fix is one function, `canary_source_key`, used by both sides. The tests for it deliberately put the **configured** spelling in and assert against the **crawler's**, and two of them take the producer's spelling from the producer rather than restating it: one reads the `source_key` expression out of `ScannerService._crawl_pages`, and one calls the real `_build_sources` and asserts every configured canary source resolves to a listing arm that actually gets crawled.

I would like this challenged. The resolution rule is "an unqualified name means the HDEncode listing", which is true today because the canary is an HDEncode mechanism, and a qualified name passes through so another listing could be named later. The alternative was to change the contract default to fully qualified keys, which I rejected because the feed map is keyed by category and the contract hash would have changed.

## 3. Silence recorded as silence

Only sources that produced rows were graded. A configured source that returned nothing — disabled in `background_scan_categories`, renamed, or simply failing — had **no attempt recorded at all**: its last outcome still read `success` from hours earlier while it was observing nothing, and its `next_attempt_at` never moved, so it stayed permanently due. Every configured source is graded now. An empty result from a crawl that finished is an `error` with reason `no_membership_recorded`; a crawl that never finished still reports `listing_incomplete`, because those are different faults and only one is about the source.

## 4. Three status fields that had become wrong

`canary_last_success`, `canary_age_seconds` and `canary_interval_seconds` were hard-coded `None` from #108, whose comment said they stay empty until the canary exists. It exists now, so `None` had stopped meaning "not built yet" and started asserting the canary has never succeeded — to any consumer, indistinguishable from a dead one. The same three were returned by the **promotion route**, which is what an owner reads when deciding whether to promote.

They now summarise the **worst** configured source: one source succeeding does not make the listing observed, so a scalar reporting the freshest would overstate the protection. Unreadable, unconfigured, or any source that has never succeeded all read `None` — all three are "we cannot say this is protected", which is the safe direction for a scalar to be wrong in. Per-source truth is in `promotion.canary.sources`, which also publishes the resolved `source_key` so an operator can match what they configured to the row the crawler writes.

## 5. What the cost block says, and does not

It publishes the ledger's per-kind counts and the two thresholds (floor 0.50, target 0.70) and **no reduction percentage**. A percentage needs a baseline for what listing-only would have cost, which cannot be observed after promotion because the listing crawls stopped happening. That projection belongs to the replay over the dense shadow evidence. Printing a number here that looked measured would be worse than printing none.

`available: false` means the ledger could not be read. An unreadable ledger does not publish zero: zero is a measurement nobody made.

**The window was the dishonest part, and is fixed.** The ledger records the mode each batch was spent in, but `sum_requests` aggregates by kind across every mode. A flat seven-day trailing window on a system promoted two days ago therefore added five days of *shadow* spending to a figure labelled as the hybrid's. The window now starts at the promotion whenever the promotion falls inside it, and the block publishes `since` and `scope` (`since_promotion` / `trailing_window`) so the reader knows which regime the numbers describe. Averaging two regimes silently is how a cost claim stops meaning anything.

## 5a. One example is not an extent

`canary_evidence` appended the offending URL *inside* the "this blocker is not already recorded" guard, so `detail["gap_proven"]` could only ever hold a single entry however many releases were missed — one gap and fifty read identically on the status page. Findings are now counted exactly (`gap_proven_count`, `unassessable_count`) and the example list is capped at 20, because the block is published on an endpoint the UI polls. The count is what an owner needs to judge severity; the examples are what they need in order to go and look.

## 6. Tests

- Focused: 157 tests across the canary files, the wider authority and primary-mode suites, the RSS routes and the API lifecycle.
- Whole suite, on a whole-tree copy carrying #110's trash isolation: **5,633 passed, 5 skipped, 15m36s**. `C:\.scanhound-trash` absent before and after.
- The status surface was additionally run against a **real `DatabaseManager`** — real schema, real `record_canary_attempt`, real ledger rows — configured with the contract's own spelling, to check the whole path rather than a double that answers whatever it was written to answer.

Migrated with the reason recorded in each: the two `_canary_not_due` / `_canary_due` helpers in `test_hdencode_rss_primary.py`, which keyed their fixture state by the configured name and so agreed with the bug.

## 7. Mutants — 26 in this pass, on whole-tree copies, each with a green control

Three survived on the first run. All three were real gaps, and all three are now killed. (Earlier part-2 commits carried their own mutant runs for the policy module, the canary state table, the membership producer and grading; the five demotion mutants below were run with that commit and are repeated here for completeness.)

| mutant | result | killed by |
|---|---|---|
| reconcile only while primary is in effect | KILLED | `test_it_runs_on_the_requested_mode_not_the_effective_one` |
| a suspension demotes too | KILLED | `test_a_suspension_changes_nothing_durable` |
| commit even when the write failed | KILLED | `test_a_write_that_fails_reports_it_and_changes_nothing` |
| do not check the record actually went | KILLED | `test_a_writer_that_keeps_the_record_is_not_called_a_demotion` |
| thin evidence revokes again | KILLED | the evidence-insufficient suspension tests |
| an unreadable canary state renders as "no canaries" | KILLED | `test_unreadable_state_is_published_as_unavailable_not_as_no_canaries` |
| a canary that never ran does not read stale | KILLED | `test_a_canary_that_never_ran_is_stale_and_says_so_separately` |
| an unreadable ledger publishes zero spend | KILLED | `test_request_cost_reports_unavailable_rather_than_zero` |
| the scalar age takes the freshest source | KILLED | `test_the_scalar_health_fields_describe_the_worst_source` |
| a source that never succeeded does not veto the scalar | KILLED | `test_one_healthy_source_does_not_speak_for_a_source_that_never_ran` |
| the promotion route keeps the #108 placeholder | KILLED | `test_the_promotion_route_sees_the_same_canary_the_status_page_does` |
| **an unclassified blocker is harmless** | **SURVIVED → now killed** | `test_a_blocker_nobody_classified_revokes_rather_than_passing_through` |
| a hand-written primary needs no promotion record | KILLED | three tests incl. `test_a_hand_written_primary_without_a_record_is_refused_not_migrated` |
| changing the contract under a promotion is fine | KILLED | `test_retention_is_inside_the_contract_so_lowering_it_revokes` |
| primary runs with auto-demotion disarmed | KILLED | `test_disarming_auto_demotion_refuses_primary_rather_than_running_unprotected` |
| a database outage is a durable finding | KILLED | `test_a_database_that_cannot_answer_suspends_and_keeps_the_promotion` |
| the canary's own findings never reach the verdict | KILLED | five tests across evidence and reconciliation |
| a suspension outranks a revocation | KILLED | three tests |
| **the pre-fix behaviour: the configured name IS the key** | KILLED | four boundary tests |
| the status surface resolves, the evidence does not | **SURVIVED → now killed** | `test_the_evidence_reads_the_membership_the_crawler_wrote` |
| the health surface looks up the unresolved name | KILLED | `test_a_canary_recorded_by_the_crawler_is_found_by_the_status_surface` |
| the scheduler looks up the unresolved name | KILLED | `test_the_scheduler_finds_the_state_the_crawler_wrote` |
| **grade only the sources that produced rows** | **SURVIVED → now killed** | `test_every_configured_source_records_an_attempt_not_only_the_ones_that_spoke` |
| a source that recorded nothing is graded a success | KILLED | `test_a_source_that_recorded_nothing_is_a_failure_with_a_reason` |
| a flat trailing cost window, mixing shadow spend in | KILLED | `test_the_cost_window_starts_at_the_promotion_not_seven_days_back` |
| the promotion window wins even when it is older | KILLED | `test_an_older_promotion_falls_back_to_the_trailing_window` |
| an unparseable promotion time is labelled `since_promotion` | KILLED | `test_an_unparseable_promotion_time_does_not_mislabel_the_window` |
| only the first coverage finding is recorded | KILLED | `test_every_coverage_finding_is_counted_and_a_few_are_listed` |
| the example list is unbounded | KILLED | same |
| the oldest success is picked by sorting the timestamp STRINGS | KILLED | `test_the_timestamp_and_the_age_always_describe_the_same_source` |
| the two scalars are reduced independently | KILLED | same, plus `test_the_scalar_health_fields_describe_the_worst_source` |

The last two are a hazard this repo has hit before: ISO timestamp strings only sort chronologically while every one of them carries the same offset shape, which a value that has been through the database cannot be promised. Both scalars are now taken from one entry chosen by **parsed age**, so the timestamp and the age can never describe different sources. The test stores the older stamp in a `+09:00` offset, whose local clock reads later, so a string sort picks the wrong one.

### The survivor worth reading

`revocations = revocations + unclassified` — the escalation that treats a blocker in neither the suspension nor the revocation set as a revocation. Deleting it left the whole suite green.

The reason is instructive: `test_every_blocker_is_classified_as_exactly_one_of_suspension_or_revocation` proves every **current** blocker is classified, which is exactly why no test ever reaches that branch. The protection against somebody adding a blocker later and forgetting to classify it had never been executed. The new test sends a blocker through the evidence path under a name the module does not know, as a future one would.

## 8. Known gaps, stated rather than discovered

1. **The frontend does not render any of this.** `promotion` has been published since #108 and `frontend/src/routes/rss/+page.svelte` reads none of it. Worse, the page's mode selector shows `status.mode`, which is the **requested** mode — so the UI can display "RSS primary" while the runtime is running shadow and the reason sits unread two keys away. I did not fix this in part 2: I cannot run the frontend build here, and an unverified Svelte change is the kind of work I would object to in a review. It is the obvious next slice.
2. **No live observation.** Nothing here has run against production. Every claim above is CI VERIFIED or HOST VERIFIED on a real SQLite database, never LIVE OBSERVED.
3. **The replay's cost projection is not wired to the status surface**, deliberately — see §5.
4. **`evaluate_rss_primary_authority` has no production caller left.** Its docstring claimed "it is what `POST /rss/mode` asks" and "#108's route and tests use it"; PR 1 moved the route onto `evaluate_activation` and left the sentence behind — the stale-comment class HDE-5 spent a round removing. I corrected the docstring rather than deleting the function: it is #108's published response shape, this branch is stacked on #108, and deleting it here would settle a question belonging to that PR. Its canary block is filled for real now rather than left as the placeholder, so a caller that comes back is not answered with a permanent "never succeeded".
5. **A canary source that is never crawled promotes anyway, then demotes twelve hours later.** `background_scan_categories` can be narrowed to a subset (e.g. `["4k"]`); the contract's canary sources default to `4k, remux, tv`. Nothing at activation compares the two, so an operator can promote into a configuration guaranteed to go stale and auto-demote once `canary_max_age` passes. It is fail-safe — the demotion is the correct outcome — and it is now visible before promotion, because the status block shows `has_run: false` for the uncrawled sources. I did not add an activation blocker for it because the check needs config the authority does not currently read, and adding it to the contract changes the hash. See question 5.

## 9. Questions for the reviewer

1. The `canary_source_key` resolution rule (§2) — is "unqualified means HDEncode" the right boundary, or should the contract default become fully qualified and the feed map re-keyed?
2. Grading a silent source as `error` rather than `blocked` — `blocked` exists in the outcome set and is currently unused. Is `error` the honest label for "the crawl finished and this source produced nothing"?
3. The worst-source scalar (§4): is collapsing three sources into one timestamp worth doing at all, given the per-source block sits beside it? The alternative is to drop the three legacy keys and force consumers onto `canary.sources`.
4. Anything in §8.1 that should block part 2 rather than follow it.
5. §8.5 — should activation refuse a promotion whose canary sources are not all being crawled, or is "it demotes itself in twelve hours, and the status page says so first" the right amount of protection?
6. §8.4 — keep `evaluate_rss_primary_authority` as #108's published shape, or delete it (and its tests) once #108 lands?
