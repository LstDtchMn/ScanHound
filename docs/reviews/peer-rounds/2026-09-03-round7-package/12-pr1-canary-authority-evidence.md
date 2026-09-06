# PR 1 evidence — RSS canary part 1 (2026-09-06)

Draft PR #116 `feat/rss-canary-authority-and-evidence` @ `577d99c`, stacked on #108 (`fix/r7b-refuse-blind-rss-primary` @ `2e91de0`). Implements the reviewer's PR-1 list from the design closed as PASS on 2026-09-06 (`11-...-rev3.md`).

## 1. There is no path to primary

`CANARY_IMPLEMENTED = False`, so both evaluations always carry `coverage_canary_not_implemented`. No route and no runtime path in this change can authorize a promotion. The canary, the replay, the coverage states, overlap and churn are PR 2.

## 2. What was built

| area | what it does |
|---|---|
| activation vs runtime | one module, two questions. Activation (`POST /rss/mode`) still demands shadow readiness; runtime (scanner, status, `effective_discovery_mode`) deliberately does not |
| suspension vs revocation | `database_unavailable` and any unevaluable read suspend and KEEP the promotion record; a durable safety finding deletes it. Every revocation trigger derives from evidence or configuration, never process memory, so each re-derives after a restart |
| contract | `canary_contract_hash` over the canary's sources and feed mapping, depth, interval, max age, membership retention, the auto-demotion policy and the code versions. Retention has a floor (`retention_below_evidence_horizon`) |
| prospective promotion | the route builds the record and passes it to the check; a check demanding an existing record could never pass the first legitimate promotion |
| fail-closed write | `persist_config_snapshot` (deep copy, sensitive keys into the copy, temp + fsync + replace, re-read and verify, raise) and `commit_config_in_place` (mutates the SHARED dict; `backend/api/main.py:113` aliases the registry's config to the module's). A promotion that cannot be written durably is never visible in memory; the route answers 503 |
| membership | `hdencode_listing_membership` with page index and rank; a URL sighted twice in one cycle keeps the CLOSEST sighting, by conditional upsert, so a deeper duplicate cannot make the replay believe it sat outside the protected depth |
| request cost | its own ledger, with mutually exclusive kinds and a stable row id, instead of rows in the comparison table |
| comparison table | `mode` added, CHECK-constrained to `rss_shadow` and `rss_primary_canary` |

## 3. Why request cost is not in `hdencode_shadow_cycles`

Three consumers verified in the code, any one of which a cost-only row would have corrupted:

- eleven comparison columns are `NOT NULL` (`backend/database.py:1118-1144`), so such a row cannot be inserted without rebuilding the table;
- `get_hdencode_shadow_summary` ends with an unfiltered `SELECT * ... ORDER BY completed_at DESC LIMIT 1` (`:2655-2658`), so it would become the apparent latest comparison;
- the miss-resolution loader reads `... WHERE details_json IS NOT NULL ORDER BY completed_at` (`:2748-2753`) and treats every row as an observation cycle, and `details_json` is `NOT NULL DEFAULT '{}'`.

The third was not cited by the review; it was found while verifying the first two.

## 4. Tests, and the four that were migrated

596 tests pass across eleven focused files on this branch. Only focused files were run: this worktree does not carry the suite's trash isolation, and `C:\.scanhound-trash` stayed absent throughout.

Four tests asserted that shadow readiness blocks the RUNTIME, which is exactly what the review changed. None was deleted or weakened. Each keeps the claim it existed for and now asserts readiness at activation, with a comment naming the change and the reason:

| test | now asserts |
|---|---|
| `test_route_and_runtime_consult_the_same_function` → `test_each_consumer_asks_the_authority_its_own_question` | each consumer follows its OWN question and carries no local rule |
| `test_readiness_is_still_a_blocker_on_its_own` → `test_readiness_gates_activation_but_never_the_runtime` | readiness blocks activation, and is absent from the runtime blockers |
| `test_primary_service_refuses_before_shadow_gate` | the effective mode is still shadow and readiness blocks activation |
| `test_a_not_ready_primary_runs_as_shadow_not_as_primary` | same, at the integration level |

New tests cover the promotion write: a promotion is persisted and verified before it is visible; a promotion that cannot be saved changes nothing and answers 503; leaving primary drops the record so returning must be fresh.

## 5. Mutants, each on a whole-tree copy with a green control

| mutant | result | killed by |
|---|---|---|
| drop the `CHECK` on `mode` | KILLED | `test_mode_column_rejects_a_third_value` |
| invert the nearest-page predicate | KILLED | both `TestNearestPageSemantics` tests |
| restore the exclusive upper bound in `sum_requests` | KILLED | `test_kinds_are_recorded_exclusively_not_doubly` |
| strict writer delegates back to `save_config` | KILLED | six of the seven persistence tests, including the live-memory one |

## 6. A real defect found while building

`sum_requests` first used an exclusive upper bound with `until` defaulting to now. Two calls a few bytecodes apart can produce the same timestamp string, so the newest event was silently dropped from its own "as of now" query. The bound is inclusive, with the reason recorded at the method.

## 7. CI

`577d99c`: green on Python 3.11 (24m45s and 26m54s across the two runs), Python 3.12 (12m28s), frontend, and the dv-scripts checks. CI VERIFIED.

## 8. Process, and one specification error

Two Sonnet lanes implemented the evidence layer and the config writer from written specifications; the supervisor wrote the authority module and the route, reviewed both lanes at first hand, ran the mutants, migrated the four tests and committed. The specification for the evidence lane cited `record_reveal_observation` and `_query_dicts_strict` as conventions to copy; neither exists on this branch, because HDE-4 sits on the #109 → #113 stack. The lane reported that rather than inventing them, and used the real closest analogues.

## 9. Not in this PR

The membership PRODUCER (the reviewer's mandatory constraint I-1: write the sighting before the crawler's global `seen_post_urls` dedup at `backend/scanner_service.py:1004`, with the source key taken from the source being traversed) is PR 2, along with the dense qualification crawl, the replay over actually sampled membership, the canary scheduler, the four coverage states, overlap and churn, the systematic-gap check, requested-primary reconciliation, status and the estimator.
