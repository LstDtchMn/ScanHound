# RSS hybrid with the listing canary — design revision 3 (2026-09-06)

Status: DESIGN, no code. Revision of `10-...-rev2.md` after the peer review of 2026-09-06 (conditional accept, R2-1 to R2-6). Every finding was verified against the code before being accepted; all six are accepted, one is extended by a defect of the same shape the reviewer had not cited. Base for the slice: `main` @ `0a2751d` plus #108. Nothing here promotes anything.

## 1. R2-1 — a strict candidate writer, because the current one cannot be used safely

Verified: `AppService.save_config` (`backend/app_service.py:1145-1190`) takes no argument and serialises `self.config`; its sensitive-key preservation writes into **live memory** (`self.config[key] = disk_val`, :1167); and it ends with `except (IOError, OSError) as e: logger.error(...)` (:1189-1190), returning nothing. A caller cannot persist a candidate without first mutating live state, and cannot learn that the write failed.

So the slice adds one primitive beside it, and does not change the existing method:

```
persist_config_snapshot(candidate: dict) -> dict      # the verified on-disk config
```

Under `self._config_lock`: deep-copy the candidate; apply the same sensitive-key preservation **into that copy** by reading the file, never into `self.config`; write to a temporary file opened `0o600`, `flush`, `fsync`, `os.replace`; re-open the written file and confirm it parses and that the fields the caller named are present with the values it asked for; return that verified dict. Any failure raises. The promotion route calls it with `{**config, hdencode_discovery_mode: "rss_primary", hdencode_rss_primary_promotion: {...}}`, and only on a returned result does it assign into the live config and answer 200; otherwise 503, and the effective mode stays shadow because the in-memory value never changed. Demotion uses the same primitive.

Tests: a failure injected at each of `os.replace`, `fsync`, and the verify read must leave `reg.config["hdencode_discovery_mode"]` untouched and `evaluate_runtime` unauthorized; a mutant that swaps `persist_config_snapshot` back to `save_config` must be killed by the assertion that live config is unchanged after a failed write.

## 2. R2-2 — the rev-2 `gap_proven` was the resolver's `undetermined`

Verified in `classify_miss_resolution` (`backend/hdencode_shadow.py:566-603`): a URL is `acquired` when a later valid cycle carries it in `feed_only ∪ duplicate_urls`; it is `never_acquired` **only** when a later valid cycle still shows it in `listing_only` (`last_missing is not None`); and a URL that simply leaves the listing with no affirmative RSS carriage is `undetermined`, "neither acquisition nor loss can be proven". Revision 2's `gap_proven` described exactly that last shape. The reviewer is right, and adopting the wrong label would have put an unprovable claim into the demotion record.

Post-promotion classification therefore has four states, using the resolver's own vocabulary:

| state | condition | effect on provisional primary |
|---|---|---|
| `acquired` | a later valid cycle carries the URL in `feed_only ∪ duplicate_urls` | none |
| `pending` | no later complete, source-valid canary exists yet | none (measuring the clock is not a gap) |
| `gap_proven` | a later **complete, source-valid** canary observes the URL again within protected depth while RSS still lacks it | revoke |
| `coverage_unassessable` | it left protected membership without affirmative RSS carriage and without later evidence able to decide | revoke, **not** labelled proven |

Both revoking states fail closed; they are counted, surfaced and recorded separately, and `last_demotion.reason` carries which one fired. The distinction is the whole point: "we proved a loss" and "we can no longer tell" are different claims about the same evidence.

## 3. R2-3 — replay actual sampled membership, never interpolate

Revision 2 treated a URL as caught when a virtual sample time fell between its first and last observation within depth. That false-passes discontinuous membership: present at `t0`, absent at `t1`, present at `t2` would score a `t1` sample as a catch although that canary would have seen nothing.

The replay now works on concrete cycles: choose the dense cycles a canary at cadence C would actually have sampled (the first dense cycle at or after each due time); for each, take only the membership rows recorded **in that cycle** with `page_index <= D`; union those sampled memberships; compare that union against the dense population observed within depth over the epoch. A URL in the population and not in the union is a miss, and any miss makes the cell `interval_unsafe`. Residence, where reported, is published as observed segments and the gaps between them, never as a first-to-last span.

## 4. R2-4 — poll-only rows stay out of the comparison table, and a second reason to keep them out

Verified, both halves of the finding and one more:

- `hdencode_shadow_cycles` declares eleven comparison columns `NOT NULL` (`database.py:1118-1144`): feed completeness, both request counts, four comparison counts, the miss count, the reduction percentage, the outcome, and `details_json`. Poll-only rows with NULL comparison fields cannot be inserted without rebuilding the table.
- `get_hdencode_shadow_summary` ends with an unfiltered `SELECT * FROM hdencode_shadow_cycles ORDER BY completed_at DESC LIMIT 1` (`database.py:2655-2658`). A poll-only row would become the apparent latest comparison cycle. (The eligibility-filtered aggregate above it would have excluded such a row; this one has no filter at all.)
- **Not cited by the reviewer, same shape:** the miss-resolution loader reads `SELECT ... FROM hdencode_shadow_cycles WHERE details_json IS NOT NULL ORDER BY completed_at` (`database.py:2748-2753`) and treats every row as an observation cycle. Since `details_json` is `NOT NULL DEFAULT '{}'`, poll-only rows would enter the resolver's cycle list as cycles that observed nothing, exactly where an empty membership can only do harm.

So `hdencode_shadow_cycles` keeps its meaning — one row per real comparison — and gains `mode` only to distinguish `rss_shadow` from `rss_primary_canary`. Request cost moves to its own ledger:

```
hdencode_request_ledger (at, mode, kind, source_key, requests)
    kind: rss_poll | canary | canary_retry | fallback
```

Cost is summed from the ledger over wall-clock, which is also what makes RHC-13 answerable after promotion, when canary rows are sparse and RSS polls are not.

## 5. R2-5 — retention is part of the safety contract

`hdencode_listing_membership_retention_days` decides how much evidence exists for the replay, the gap states, the systematic-gap check and any re-grading, so it joins the `canary_contract_hash` inputs. Activation additionally refuses with `retention_below_evidence_horizon` when the value is under the horizon the other settings need: the qualification epoch (14 days) plus the replay window plus a margin, in practice ≥ 30 days against the 90-day default. Lowering it after promotion changes the hash, which raises `promotion_contract_changed`, which revokes. The default is ample; the point is that weakening it later cannot be silent.

## 6. R2-6 — suspension and revocation are different states

Revision 2 made `database_unavailable` a hard demotion trigger and argued a failed save was safe because the trigger keeps the mode shadow. That holds only within the process: after a restart with a healthy database, the on-disk primary plus its surviving record would authorize primary again, which contradicts both "hard demotion" and "never auto-repromote".

- **Suspension** (temporary; the promotion record is kept): `database_unavailable`, and any evidence read that cannot be evaluated. The effective mode becomes shadow immediately and `/rss/status.promotion.state` reads `runtime_suspended` with the reason. Nothing is persisted, because nothing durable has been decided.
- **Revocation** (durable; the record is deleted): `gap_proven`, `coverage_unassessable`, `systematic_gap`, `canary_stale`, `overlap_lost_twice`, `visibility_margin_lost`, `promotion_contract_changed`, `auto_demotion_not_armed`. State reads `runtime_revoked`.

A database outage that outlives the canary's `max_age` becomes `canary_stale`, which is durable, so a long outage still ends in revocation without any need to persist during the outage itself. Every revocation trigger is derived from evidence or configuration rather than process memory, so each one re-derives after a restart: if the persistence of a revocation fails, the next cycle in the new process reaches the same verdict and tries again. `runtime_suspended` never deletes a record, and no path re-promotes.

## 7. Unchanged from revision 2 (the reviewer's keep list)

One authority with the activation/runtime split; raw readiness only at activation; `poll_cycle` and the fallback decision on the runtime evaluation; the prospective promotion record; the contract hash; requested-primary revocation reconciliation at startup and each cycle; source-scoped membership with page index and rank; a real primary canary at contract depth with no early stop; overlap and churn as negative runtime guards; the source/category systematic-gap stratum; attempts and backoff separate from `last_success`; no staleness pause for a block or cooldown; the explicit 14-day continuity epoch with a 6-hour maximum gap; a 0.50 request-reduction floor and 0.70 target with safety cadence first; #94 not imported (a small `rss_canary_policy.py` carries the pure rules); enforcement in-app, independent of the collector and Gotify.

## 8. Build order (the reviewer's gate)

**PR 1** — membership schema with page index and rank plus its retention parameter; `mode` on the comparison table for the two real comparison modes only; the separate request ledger; `persist_config_snapshot`; the activation/runtime authority with the full blocker set; the prospective promotion record; the contract hash including retention; the suspension/revocation vocabulary; `CANARY_IMPLEMENTED = False` throughout, so nothing can open.

**PR 2** — the dense shadow crawl switch; the virtual replay over actually sampled membership; the primary canary at contract depth with no early stop; the scheduler with attempts and backoff; the four post-promotion gap states; overlap and churn; the systematic-gap check; requested-primary revocation; status; the listing-side estimator; mutants for every blocker, each shown to fail.

Then, in order: the operational path (#102 with #101) deployed and proven by the owner; a fresh 14-day qualification epoch; the end-to-end alert test; and promotion through the route only.

## 9. Operational note

The owner has chosen to ship #102 and #101. A single verified script prepares and proves that path (merge, rebuild, confirm the running app reports `GUARD_VERSION >= 1`, replace the pinned task, then run the task once and fail unless the container survives). PR #101's description had claimed `mount-nas-shares.ps1` was not modified while the branch changes it by 282 lines; it has been corrected, since the owner's merge record would otherwise have understated exactly the change being shipped. Until that path is live the qualification epoch cannot start, which blocks promotion but not the coding of PR 1.
