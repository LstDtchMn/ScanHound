# PR 1 evidence — RSS canary part 1 (2026-09-06)

Draft PR #116 `feat/rss-canary-authority-and-evidence` @ `6a5a4e9`, stacked on #108 (`fix/r7b-refuse-blind-rss-primary` @ `2e91de0`). Implements the reviewer's PR-1 list from the design closed as PASS on 2026-09-06 (`11-...-rev3.md`).

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

618 tests pass across eleven focused files on this branch. Only focused files were run: this worktree does not carry the suite's trash isolation, and `C:\.scanhound-trash` stayed absent throughout.

Four tests asserted that shadow readiness blocks the RUNTIME, which is exactly what the review changed. None was deleted or weakened. Each keeps the claim it existed for and now asserts readiness at activation, with a comment naming the change and the reason:

| test | now asserts |
|---|---|
| `test_route_and_runtime_consult_the_same_function` → `test_each_consumer_asks_the_authority_its_own_question` | each consumer follows its OWN question and carries no local rule |
| `test_readiness_is_still_a_blocker_on_its_own` → `test_readiness_gates_activation_but_never_the_runtime` | readiness blocks activation, and is absent from the runtime blockers |
| `test_primary_service_refuses_before_shadow_gate` | the effective mode is still shadow and readiness blocks activation |
| `test_a_not_ready_primary_runs_as_shadow_not_as_primary` | same, at the integration level |

New tests cover the promotion write: a promotion is persisted and verified before it is visible; a promotion that cannot be saved changes nothing and answers 503; leaving primary drops the record so returning must be fresh.

## 5. Mutants, each on a whole-tree copy with a green control

Every blocker that needs no canary evidence is pinned by a test, and each test is shown to fail when its protection is removed. The blockers that read canary evidence (`canary_stale`, `gap_proven`, `coverage_unassessable`, `systematic_gap`, `overlap_lost_twice`, `visibility_margin_lost`) are represented here by the single explicit `canary_evidence_unavailable` and are pinned in PR 2, where their data exists.

| mutant | result | killed by |
|---|---|---|
| drop the `CHECK` on `mode` | KILLED | `test_mode_column_rejects_a_third_value` |
| invert the nearest-page predicate | KILLED | both `TestNearestPageSemantics` tests |
| restore the exclusive upper bound in `sum_requests` | KILLED | `test_kinds_are_recorded_exclusively_not_doubly` |
| strict writer delegates back to `save_config` | KILLED | six of the seven persistence tests, including the live-memory one |
| remove the retention floor | KILLED | `test_retention_below_the_evidence_horizon_blocks_activation` |
| switch `CANARY_IMPLEMENTED` on | KILLED | three tests, including `test_the_canary_flag_is_an_absolute_blocker_on_both_questions` |
| classify a missing promotion record as neither suspension nor revocation | KILLED | the classification test and the hand-written-primary test |
| stop checking the contract hash at runtime | KILLED | `test_retention_is_inside_the_contract_so_lowering_it_revokes` |
| treat a database failure as durable | KILLED | `test_a_database_that_cannot_answer_suspends_and_keeps_the_promotion` |

**One of these tests was vacuous and the mutant caught it.** The first retention test built its input from `MIN_RETENTION_DAYS` itself, so setting the floor to zero moved the input with the constant: the test stayed green while the protection was gone. It now pins the policy separately (the floor must be at least 30 days) and uses fixed values either side of it, and the same mutant kills it.

## 6. A real defect found while building

`sum_requests` first used an exclusive upper bound with `until` defaulting to now. Two calls a few bytecodes apart can produce the same timestamp string, so the newest event was silently dropped from its own "as of now" query. The bound is inclusive, with the reason recorded at the method.

## 7. CI

`577d99c`: green on Python 3.11 (24m45s and 26m54s across the two runs), Python 3.12 (12m28s), frontend, and the dv-scripts checks. CI VERIFIED at that commit; the blocker tests added afterwards at `8fa6601` are running.

## 8. Process, and one specification error

Two Sonnet lanes implemented the evidence layer and the config writer from written specifications; the supervisor wrote the authority module and the route, reviewed both lanes at first hand, ran the mutants, migrated the four tests and committed. The specification for the evidence lane cited `record_reveal_observation` and `_query_dicts_strict` as conventions to copy; neither exists on this branch, because HDE-4 sits on the #109 → #113 stack. The lane reported that rather than inventing them, and used the real closest analogues.

## 9. The five review findings, closed at `6a5a4e9`

Each was checked against the code before being accepted; all five were real, and two would have become live the moment part 2 supplied canary evidence.

| finding | what was actually wrong | what changed |
|---|---|---|
| R1 | `persist_config_snapshot` verified AFTER `os.replace`, which is the commit, so a failed verification raised while the candidate was already the config on disk, and the route's 503 said nothing had changed | the staged file is uniquely named, written, fsynced, re-opened, parsed and checked against `must_contain` **before** the replace, which is the sole commit point; the sensitive-key read is strict in this method (an unreadable existing config raises rather than proceeding with candidate blanks), while `save_config` keeps its fail-soft behaviour |
| R2 | `poll_cycle` took the effective mode from the runtime authority and then re-asked raw readiness anyway, early-returning `primary_not_ready` and refusing to qualify the listing fallback | readiness removed from both control points and kept as diagnostic payload; two inverse tests pin that an authorized primary keeps polling and can still qualify fallback with readiness false |
| R3 | `list_listing_membership` folded "cannot read" into "no rows", which makes suspension on unevaluable evidence impossible | tri-state: `None` unavailable, `[]` healthy and empty, rows otherwise, implemented locally rather than borrowed from HDE-4's helper, which is not on this branch |
| R4 | the route asked about the live config and built the promotion record afterwards, so activation's contract-hash check was dead code | the record and candidate are built first and that exact record is qualified, so the thing qualified is the thing persisted |
| R5 | `coverage_canary_not_implemented` was in neither severity set and fell through the state logic | classified as a suspension; an unclassified blocker is now treated as a revocation so a future omission fails closed; the classification test derives its set from the module instead of a hand-written list |

Also hardened, as recommended: the ledger constrains `kind` and refuses a negative count at the schema, so malformed evidence cannot vanish from a safety total if the writer is ever bypassed.

**Mutants for the fixes**, each on a whole-tree copy with a green control:

| mutant | killed by |
|---|---|
| verify after the replace, as before the review | the test asserting the prior state survives on disk after a mismatch |
| the strict sensitive read goes fail-soft again | the unreadable-existing-config test |
| membership turns unreadable into empty | all three tri-state tests |
| the ledger's kind constraint always true | the bad-kind integrity test |
| no constraint on the request count | the negative-count test |
| readiness gates the poll again | both inverse poll tests |
| readiness gates the fallback again | the fallback inverse test |
| activation asked without the record | the qualified-equals-persisted test |
| the canary blocker classified nowhere | the classification test |

**One mutant was rerun rather than counted.** Deleting the ledger's `CHECK` line produced invalid SQL and 22 collection errors, which would have been a kill for the wrong reason. The valid form, making the constraint always true, is killed by the test that inserts a bad kind.

## 10. Not in this PR

The membership PRODUCER (the reviewer's mandatory constraint I-1: write the sighting before the crawler's global `seen_post_urls` dedup at `backend/scanner_service.py:1004`, with the source key taken from the source being traversed) is PR 2, along with the dense qualification crawl, the replay over actually sampled membership, the canary scheduler, the four coverage states, overlap and churn, the systematic-gap check, requested-primary reconciliation, status and the estimator.
