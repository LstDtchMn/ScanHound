# RSS hybrid with the listing canary — design revision 2 (2026-09-06)

Status: DESIGN, no code. Revision of `09-rss-hybrid-canary-slice-design.md` after the peer review of 2026-09-05 ("design direction accepted, request changes before code", findings RHC-1 to RHC-15). Every finding was checked against the code before being accepted; the dispositions are in §9. Base for the eventual slice: `main` @ `0a2751d` plus #108. Nothing here promotes anything.

## 1. What changed from revision 1, in one paragraph

Activation and runtime are now two evaluations of one authority module (RHC-1): full shadow qualification is required to *enter* provisional primary and is not a runtime conjunct, so a pending miss after promotion is observable but does not revoke. The promotion route evaluates a *prospective* promotion record before persisting it (RHC-2), and the record carries a `canary_contract_hash` over the safety-critical settings (RHC-3). Promotion persists mode and record together, verified by re-reading the file, because `save_config` swallows write errors (RHC-4). Revocation reconciles whenever the *requested* mode is primary, including at startup, and every revocation trigger is also an authority blocker, so a failed persistence still leaves effective shadow (RHC-5). Listing visibility is measured from listing-side membership recorded per cycle with page index, and the first promotion requires a virtual-canary replay over a dense shadow window rather than an estimate; the RSS-rate formula is a cross-check only (RHC-6, RHC-7). One membership table replaces the catches table (RHC-8). Gap detection gets its own post-promotion query with the miss's first sighting (RHC-10). Canary scheduling tracks attempts and backoff separately from success (RHC-11); a blocked crawl never pauses the clock (RHC-12). Request cost is measured over wall-clock from all requests (RHC-13). Qualification requires continuity with a maximum gap, and a fresh epoch starts now that the app runs again (RHC-14). In-app enforcement never depends on the collector or Gotify; an end-to-end alert test is an operational GO item (RHC-15).

Two additions from my own verification while checking the findings:

- **`poll_cycle` gates on raw readiness in primary.** `backend/hdencode_rss_service.py:104-120` returns `primary_not_ready` and polls nothing when `mode == "rss_primary"` and `readiness["ready"]` is false, and `fallback_qualified` (:167-175) requires the same. Under RHC-1 those two checks must consult the authority's *runtime* evaluation, or the RSS poll itself would stop the moment a pending miss appears after promotion, which is the exact failure RHC-1 removes from the authority.
- **The listing crawl early-stops by design.** The background scan calls `run_scan(..., early_stop=True)` (`background_scanner.py:682`) and the crawler stops at the first page with no new URLs (`scanner_service.py:1067`). Dense membership for the replay (RHC-7) therefore needs a qualification-only switch that crawls the configured depth without early stop for the HDEncode sources: about 9 requests per cycle instead of the measured mean of 4.34, for the qualification window only, and only in shadow. The virtual canaries' cost is projected from *their* depth, not from the dense crawl's.

## 2. Evidence schema (build step 1)

- `hdencode_shadow_cycles.mode` TEXT NOT NULL DEFAULT `'rss_shadow'`: `rss_shadow` (today's comparison rows), `rss_primary_canary` (a canary comparison), `rss_primary_poll` (a primary cycle with an RSS poll and no canary: `listing_requests = 0`, comparison fields NULL, excluded from readiness by the existing eligibility filter, counted for request cost).
- `hdencode_listing_membership (cycle_uuid, source_key, canonical_url, observed_at, page_index, rank_on_page, rss_present)`, primary key `(cycle_uuid, source_key, canonical_url)`, index on `(source_key, canonical_url, observed_at)`. Written for every URL the listing crawl sees, in every mode (shadow dense crawl, primary canary), with `rss_present` = the URL is in the current RSS URL set at observation time. Supports catches (`rss_present = 0`), overlap, direct residence measurement at a given depth, the virtual-canary replay, the per-source systematic-gap check, the runtime churn guard and re-grading. Retention: rows older than `hdencode_listing_membership_retention_days` (default 90) are deleted by the existing maintenance loop; the parameter is documented, not hidden.
- `hdencode_shadow_misses` gains nothing; the post-promotion gap query (§6) reads the membership table and the feed sets instead of the legacy resolver rows, which do not carry the miss's first sighting (`summarise_miss_resolutions` emits url/state/hours/detail only; the timestamp is joined from the cycle and dropped).
- Config keys (deploy-only, like the other `hdencode_rss_*` keys): `hdencode_listing_canary_minutes` (360), `hdencode_listing_canary_pages` (3), `hdencode_listing_canary_max_age_minutes` (2 × interval), `hdencode_listing_membership_full_depth` (false; true only during qualification, in shadow), `hdencode_rss_auto_demotion_enabled` (true), `hdencode_rss_primary_promotion` (the record, written only by the route or by demotion), `hdencode_rss_last_demotion`, `hdencode_rss_qualification_epoch_started_at`.

## 3. The authority: one module, two evaluations (build step 2)

`backend/rss_primary_authority.py` keeps its one entry point per consumer and gains a second question.

**Activation** (`evaluate_activation(config, db, proposed_record)`), asked only by `POST /rss/mode` with a *prospective* record it builds from the current contract. Blockers, every one a separate predicate with its own test:

```text
shadow_readiness_not_met            get_hdencode_rss_readiness()["ready"] is false (unchanged rule, incl. pending)
qualification_window_incomplete     fewer than 14 consecutive observed clean days in the current epoch (§7)
visibility_window_unknown           no replay result for a canary source at the contract's depth/cadence
interval_unsafe                     the replay missed any URL, or interval > window(listing churn, p90)/2, for any source
canary_not_recent                   no complete dense listing crawl within max_age (the baseline the first canary inherits)
auto_demotion_not_armed             hdencode_rss_auto_demotion_enabled is not true
contract_hash_mismatch              the prospective record's hash differs from the hash of the live settings
database_unavailable                any read above failed (unknown is a blocker)
```

If none holds, the route persists `hdencode_discovery_mode = rss_primary` and the record `{at, by: "operator", canary_version: 1, canary_contract_hash}` **in one save**, then re-reads the config file and confirms both values before it updates the in-memory config or answers 200. `AppService.save_config` (`app_service.py:1189-1190`) catches `IOError`/`OSError` and only logs, so success cannot be inferred from its return; the route verifies by reading back. A failed or unverifiable save answers 503, changes nothing in memory, and the effective mode stays shadow.

**Runtime** (`evaluate_runtime(config, db)`), asked by the scanner every cycle, by `poll_cycle`, by the fallback decision and by `/rss/status`. Blockers:

```text
promotion_record_missing            requested primary with no record (a pre-hybrid persisted value)
promotion_contract_changed          record hash != hash of the live settings
canary_stale                        now - last_success > max_age
visibility_margin_lost              the runtime churn guard fired (§5)
overlap_lost_twice                  two consecutive complete canaries with zero overlap (§5)
gap_proven                          the post-promotion gap query returns a row (§6)
systematic_gap                      a canary source with listing evidence and zero RSS presence over the last 4 canaries (§6)
auto_demotion_not_armed
database_unavailable
```

`shadow_readiness_not_met` is deliberately absent at runtime (RHC-1): after promotion the canary keeps producing the comparisons a pending row needs, so pending is observable, not a revocation. The `canary_contract_hash` covers exactly: the canary source set and their feed mapping, pages, interval, max age, the estimator and policy versions, `CANARY_VERSION`, and the auto-demotion policy version; it excludes measurements and unrelated config.

**Consumers.** `effective_discovery_mode` returns `rss_primary` only when runtime authorization holds; `poll_cycle` replaces its raw-readiness check with the runtime evaluation; `fallback_qualified` likewise; `/rss/status.promotion` publishes both evaluations (§8).

## 4. The canary scheduler in primary (build step 4), and the virtual scheduler in shadow (build step 3)

Per scan cycle in effective primary: RSS poll as today; then, if the canary is due, the HDEncode sources are scanned through the ordinary `_scan_source` path at the contract depth (no early stop for the canary), the membership rows are written, `compare_shadow` runs and writes a `rss_primary_canary` row, and the items enter the same cache/results/candidate path as in listing mode, which is the fallback acquisition for anything RSS lacked. Nothing is retried by the canary itself.

Scheduling state, persisted per source: `last_attempt_at`, `next_attempt_at`, `last_success_at`, `last_outcome`, `last_reason`, `consecutive_failures`, `consecutive_overlap_losses`. Due = `now >= next_attempt_at`. After a complete canary: `last_success_at = now`, `next_attempt_at = now + interval`, failures reset. After an incomplete canary (crawl error, cancellation, coordinator block, overlap lost): `next_attempt_at = now + min(interval, 15 min × 2^consecutive_failures)`, `last_success_at` untouched, reason recorded. Only `last_success_at` refreshes protection (RHC-11, RHC-12). Completion is the crawler's own complete verdict (`_last_crawl_termination == "complete"`), never the absence of an exception.

In shadow, with `hdencode_listing_membership_full_depth = true`, every cycle's dense crawl writes membership; the **virtual scheduler** marks which cycles a canary at the contract cadence *would* have run (the first cycle at or after each due time) and records, per virtual canary, its listing requests at the contract depth. No duplicate network calls (RHC-7, RHC-13).

## 5. Visibility: measured before, guarded during

**Before the first promotion (the replay, RHC-7).** Over the qualification epoch's dense membership, for each canary source and each URL: the interval during which it was within the contract depth (first to last cycle with `page_index ≤ pages`). For the contract cadence, the virtual canary sample times; the URL is *caught* if any sample falls inside its interval. `interval_unsafe` if any URL over the epoch was not caught, per source. The replay is also run for a small grid (1/2/3 pages × 3/6/12 h) and published on `/rss/status` so the owner sees the trade before choosing; the contract's cell must be clean. Cross-check (the estimate, listing-side): window ≈ pages × observed posts-per-page ÷ p90 daily arrivals *on that listing* (new URLs per day in membership for that source, never the RSS-seen rate: a population RSS omits also vanishes from an RSS-derived rate, which lengthens the estimated window exactly when protection is needed, RHC-6). `interval ≤ window / 2` per source.

**During primary (runtime, no estimate needed).** Two guards on each complete canary, per source: (a) **overlap**: at least one URL the previous complete canary saw within depth is seen again; zero overlap increments `consecutive_overlap_losses`; two in a row → `overlap_lost_twice`. Overlap is a negative signal only, never proof of coverage. (b) **churn**: the number of URLs *new since the previous complete canary* must be at most half the window capacity (`pages × posts-per-page / 2`); more means posts are churning faster than the interval can observe → `visibility_margin_lost`. Both are recorded on the cycle row and surfaced.

## 6. Gaps, and reversion (build step 5)

- **`gap_proven`**: a dedicated query over membership and feed sets: a URL first observed on a canary source *after* `promotion.at`, with `rss_present = 0` at every observation, not present in any later cycle's `feed_only ∪ duplicates`, and absent from the latest complete canary of its source (it left the listing without RSS ever carrying it). A URL still listed is pending, not a gap; a URL RSS carried later is acquired. Evidence problems (unparseable rows) make the query report `unassessable`, which is a blocker, not a pass.
- **`systematic_gap`**: for each canary source, over the last 4 complete canaries: membership rows exist and none has `rss_present = 1` and none of those URLs appears in the RSS URL sets of those cycles. Per-URL source provenance comes from the membership table; aggregate feed-only/duplicate totals are never used as source evidence (RHC-9). Insufficient provenance → `unassessable` → blocker.
- **Reconciliation, every cycle and at startup, whenever the requested mode is `rss_primary`** (RHC-5): if any hard trigger holds (`gap_proven`, `systematic_gap`, `canary_stale`, `overlap_lost_twice`, `visibility_margin_lost`, `promotion_contract_changed`, `auto_demotion_not_armed`, `database_unavailable`), persist `hdencode_discovery_mode = rss_shadow`, delete the promotion record, write `hdencode_rss_last_demotion = {at, reason, evidence}`, log at WARNING. If the save fails, the effective mode is still shadow because the trigger is itself a runtime blocker; the reconciliation retries next cycle. Never auto-repromote.

## 7. Qualification continuity (RHC-14)

A qualification epoch starts when the membership recording is live and the app has run continuously; `hdencode_rss_qualification_epoch_started_at` is written by the operator route (`POST /rss/qualification/start`) or by the deploy runbook, never implicitly. `qualification_window_incomplete` unless the epoch spans 14 consecutive observed clean days: eligible comparison cycles with no gap between consecutive eligible cycles longer than 6 h (about five missed cycles at the observed spacing), no `never_acquired` first sighted in the epoch, and the epoch's dense membership present for the replay. A gap longer than the maximum resets the clock to the first eligible cycle after it and is surfaced with its cause. The 2026-08-31 to 09-06 outage therefore counts for nothing; the readiness rows from before it remain evidence for the shadow rule but not for continuity.

## 8. Status (build step 6)

`GET /rss/status.promotion` publishes: `requested_mode`, `effective_mode`, `provisional`, `activation: {eligible, blockers}` (evaluated with a prospective record from the live contract, so the owner sees what a promotion would face), `runtime: {authorized, blockers}`, `promotion_record` (without the hash's inputs), `contract_hash_current`, `canary: {per source: last_attempt_at, next_attempt_at, last_success_at, age_seconds, last_outcome, last_reason, consecutive_failures, consecutive_overlap_losses, new_urls_last_canary, window_capacity}`, `visibility: {per source: replay result per grid cell, churn p90, estimated window, interval_ok}`, `qualification: {epoch_started_at, consecutive_clean_days, max_gap_hours, complete}`, `request_cost: {window_days, rss_requests, listing_requests, canary_retries, fallback_requests, reduction_vs_listing_baseline, floor_ok, target_ok}`, `last_demotion`, `fallback_uses_24h`.

## 9. Peer findings, verified and dispositioned

| id | finding | verified against | disposition |
|---|---|---|---|
| RHC-1 | readiness as a runtime conjunct revokes on ordinary lag | `database.py:3019-3020` (pending blocks readiness); the scanner consults the authority per cycle | **accepted**; §3. Plus `poll_cycle`/`fallback_qualified` moved to the runtime evaluation (§1) |
| RHC-2 | the route evaluates before persisting, so "no record" is circular | `routes/rss.py:287-302` on #108 | **accepted**; prospective record, §3 |
| RHC-3 | bind promotion to a contract hash | design | **accepted**; fields listed in §3 |
| RHC-4 | fail-closed persistence for both transitions | `app_service.py:1189-1190`: `save_config` swallows write errors and returns nothing | **accepted and strengthened**: verify by read-back; 503 on failure; failure-injection tests for both transitions |
| RHC-5 | revocation must run on requested primary | design (rev 1 said "in effective primary") | **accepted**; §6 reconciliation incl. startup; triggers double as blockers |
| RHC-6 | the RSS-seen rate is not a visibility authority | reasoning checked: an omitted population lengthens the estimate | **accepted**; listing-side membership with page index; RSS rate demoted to nothing (the cross-check uses listing arrivals) |
| RHC-7 | estimate + overlap insufficient; replay virtual canaries over dense shadow | `background_scanner.py:682`, `scanner_service.py:1067`: the crawl early-stops, so dense membership needs a qualification-only full-depth switch (~9 vs 4.34 requests per cycle) | **accepted**, with the cost stated; §4-5 |
| RHC-8 | one normalized membership table | design | **accepted**; §2, retention parameter added |
| RHC-9 | source/category stratum, source-scoped evidence, fail closed | design | **accepted**; §6 |
| RHC-10 | resolver rows lack the miss's first sighting | `hdencode_shadow.py:660-661` (rows: url, media_type, state, hours, detail); `hdencode_shadow_misses` has no timestamp column (`database.py:1147-1158`) | **accepted**; dedicated post-promotion query on membership, §6 |
| RHC-11 | attempts/backoff separate from success; explicit overlap counter | design | **accepted**; §4-5 |
| RHC-12 | never pause the clock for a block | design | **accepted** (unchanged) |
| RHC-13 | request cost over wall clock, all requests; 0.50 floor, 0.70 target, safety cadence outranks | `get_hdencode_shadow_summary` counts eligible comparison rows only | **accepted**; `rss_primary_poll` rows carry the non-canary cycles; virtual scheduling projects cost in shadow; §8 |
| RHC-14 | continuity after the outage | `database.py:2975-2980`: `observed_days` is last-minus-first | **accepted**; epoch + 6 h maximum gap, §7; the 14 consecutive days start with the new epoch |
| RHC-15 | enforcement in-app; alert test as operational GO | the outage itself | **accepted**; the end-to-end alert test is an owner item before promotion, never an enforcement path |

Answers to the six questions of revision 1 are as the reviewer gave them: promotion record (not migration); empirical listing-side evidence before the first promotion; source/category stratum; no clock pause; no wholesale import of #94 (a small `rss_canary_policy.py` owned by the slice carries the pure rules: pending is not a gap, ambiguity fails closed, incomplete work does not clear staleness, 0.50 floor / 0.70 target); 0.50 hard floor, 0.70 target, safety cadence first.

## 10. Order of work (the reviewer's ten steps, adopted)

1. Evidence schema: cycle `mode`, membership table with page index and rank, retention parameter.
2. Authority state model: activation vs runtime, prospective record, contract hash, the blocker set, `poll_cycle`/fallback on the runtime evaluation; `CANARY_IMPLEMENTED` stays `False` so nothing opens.
3. Shadow dense crawl switch and the virtual scheduler: membership, replay grid, cost projection; no duplicate requests.
4. Primary canary scheduler: due/attempt/success/backoff, comparison rows, catches through the ordinary path.
5. Revocation: triggers as blockers, requested-primary reconciliation at startup and per cycle, persistence-failure tests.
6. Status.
7. Listing-side estimator and the churn guard.
8. Mutation and discrimination tests on whole-tree copies; each blocker shown to fail.
9. Fresh live qualification epoch with continuity enforced; the end-to-end alert test (owner).
10. Owner promotion through the route only.

Steps 1 and 2 are one PR stacked on #108; 3 to 7 are the second; 8 accompanies both. Each PR is reviewed before the next is built.
