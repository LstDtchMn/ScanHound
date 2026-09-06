# RSS hybrid with the listing canary, as one slice — design for critique (2026-09-05)

Status: DESIGN, no code. Written for the peer reviewer's critique before anything is built. Base: `main` @ `0a2751d` plus #108 (`backend/rss_primary_authority.py`, the single authority that refuses `rss_primary` until the canary exists). Decision record it implements: `docs/reviews/peer-rounds/2026-08-11-rss-readiness-gate-design.md` (both peers converged on a coverage-canary hybrid, "D"). Nothing here promotes anything; promotion stays owner-enabled.

## 0. Two facts found while gathering inputs (they change what the slice can rely on)

1. **The production container has been stopped since 2026-08-31 11:51 UTC** (`docker inspect`: exit 143, SIGTERM, after one hour of running). The RSS shadow has recorded nothing since (last shadow cycle 2026-08-31 05:48 UTC). Every readiness figure, and the "days since the last never-acquired release" clock, is frozen there. The slice cannot be qualified against live evidence until the app runs again.
2. **The shadow-evidence collector has failed every run since 2026-08-31 16:57 UTC** with `sqlite3.OperationalError: unable to open database file`. Cause: it opens the database read-only on a read-only volume mount; while the app ran, the WAL shared-memory file existed; after the clean stop it was removed, and a read-only open of a WAL database cannot create it. It will recover when the app runs; a copy-then-open (or `immutable=1` on a quiescent file) would make it independent of the app. **Its Gotify alert delivery has failed since 2026-08-18** (last "notify: sent" 2026-08-18 22:57 UTC; every push since fails inside `urlopen`; the Gotify container is healthy; the token line is present in the source it reads). So no stop-condition alert has reached the owner for 18 days, and no "collection failed" alert for 5. Both are reported to the owner alongside this design; neither was changed.

## 1. What the slice is

**Canary-protected provisional primary.** RSS is the acquisition source; a scheduled listing scrape (the canary) keeps running at a reduced cadence, independent of RSS health, writes the same shadow comparison rows as `rss_shadow` does today, and feeds anything it finds that RSS lacked into the same acquisition path listing mode uses. The authority in `rss_primary_authority.py` grows the real prerequisites. Auto-demotion to `rss_shadow` is armed. A pre-hybrid persisted `rss_primary` never becomes effective without a fresh, explicit promotion.

Explicit signals, every one of them surfaced on `GET /rss/status` and logged:

| signal | meaning | effect |
|---|---|---|
| `canary_success` | a canary crawl completed (the crawler's own complete verdict), the comparison row was written, the overlap check passed | refreshes `canary.last_success` |
| `canary_incomplete` | the crawl did not complete (error, cancellation, block, early termination) | does not refresh `last_success`; counts toward staleness |
| `canary_overlap_lost` | the canary saw none of the URLs the previous canary saw (see §4) | treated as `canary_incomplete` and recorded as `coverage_gap_possible` |
| `canary_catch` | a URL the canary found on the listing that RSS had not carried | recorded per URL with the cycle id; acquisition proceeds through the listing path; the miss classifier decides later whether RSS ever carried it |
| `canary_stale` | `now - last_success > max_age` | **demotion** |
| `gap_proven` | the miss classifier reports a new `never_acquired` since promotion | **demotion** |
| `systematic_gap` | one canary source has listing evidence over the last N canaries and RSS acquisitions of zero in the same window | **demotion** |
| `promotion_not_fresh` | `rss_primary` is persisted without a valid promotion record | primary refused; runs as `rss_shadow` (today's behaviour) |
| `visibility_window_unknown` | the interval-versus-window rule cannot be evaluated for a canary source | primary refused |

No silent fallback: the existing transient fallback (`fallback_qualified`, fires only on a failed RSS poll) stays, but every firing is counted and surfaced as `rss_fallback_used`; it never substitutes for the canary and never refreshes `last_success`.

## 2. Where it lives (the open implementation questions from the decision record, answered)

- **Scheduler:** `BackgroundScanner.scan_once` (`backend/background_scanner.py`). Today, in `rss_primary`, the HDEncode listing scan is skipped outright unless the transient fallback qualified (lines ~400-413), and `compare_shadow` runs only when `discovery_mode == "rss_shadow"` (~447-452). The slice replaces the skip with: *in effective `rss_primary`, scan HDEncode when the canary is due, at the canary depth, and run `compare_shadow` for it exactly as in shadow mode.* Due = `now - last_success >= interval` **or** no canary has run since promotion (the first canary runs on the first cycle after promotion, so promotion never starts blind).
- **Depth and cadence (config, deploy-only like the other `hdencode_rss_*` keys):** `hdencode_listing_canary_minutes` (default 360), `hdencode_listing_canary_pages` (default: the same depth as `background_scan_pages`, 3; the saving comes from cadence, not depth, and a shallower canary must pass the window rule in §4 on its own), `hdencode_listing_canary_max_age_minutes` (default 2 × interval).
- **Comparison rows:** `hdencode_shadow_cycles` gets the same row shape with `mode = "rss_primary_canary"` (new column, default `"rss_shadow"` for existing rows), so `get_hdencode_shadow_summary`, `get_hdencode_rss_readiness` and `classify_miss_resolution` keep reading one table and readiness keeps being computed after promotion. This is the core change the decision record asked for: the instrument stays alive after promotion.
- **Catches:** a small table `hdencode_canary_catches (url, cycle_uuid, source, observed_at)` written for every `listing_only` URL a canary produces. It is observation only; acquisition of those items happens through the ordinary listing scan path (the canary *is* a listing scan), which is already the path that fills results and candidates in listing mode. Nothing is retried or re-queued by the canary itself.
- **The authority** (`rss_primary_authority.py`): `CANARY_IMPLEMENTED = True` when the slice lands, and `evaluate_rss_primary_authority` returns blockers from this set: `shadow_readiness_not_met` (unchanged), `promotion_not_fresh` (§3), `canary_interval_not_below_visibility_window` and `visibility_window_unknown` (§4), `canary_stale` (§5), `auto_demotion_not_armed` (§5), `database_unavailable`. `provisional` is `True` only when authorized; the `canary` block carries `interval_seconds`, `pages`, `last_success`, `age_seconds`, `last_outcome`, `visibility_window_hours` per source and `interval_ok`.

## 3. The fresh-promotion requirement (from #108's forward note)

`POST /rss/mode {mode: rss_primary}`, when the authority authorizes it, persists **two** things: `hdencode_discovery_mode: rss_primary` and `hdencode_rss_primary_promotion: {at, canary_version, by: "operator"}`. The authority treats the mode as effective only when the promotion record exists and its `canary_version` equals the module constant `CANARY_VERSION` (1 for this slice). A persisted `rss_primary` written before the hybrid has no record, so it carries `promotion_not_fresh` and keeps running as `rss_shadow` with the requested/effective split #108 already publishes. No migration rewrites the persisted mode (the round-7c agreement); the missing record *is* the refusal. Demotion (§5) deletes the record, so re-promotion is always a fresh, explicit act.

## 4. The load-bearing parameter: canary interval versus listing visibility

The decision record's invariant is *canary interval < the listing's credible visibility window*. Two findings while trying to measure that window from existing shadow data:

1. **The stored shadow data cannot measure it.** Each cycle's `details_json` keeps `listing_only` and `feed_only` URL lists but only a *count* for the intersection (the grader in the evidence directory documents the same limit). A URL RSS catches up on leaves `listing_only` within about an hour, so "residence in `listing_only`" measures RSS lag, not how long a post stays on the listing. In the last 500 cycles only 8 URLs ever appear in `listing_only` and 2 have a bounded residence, both under one cycle. The window must be measured another way.
2. **It is derivable from source-owned inputs that are already recorded.** Visibility at depth D pages ≈ D × posts-per-page ÷ posting-rate. From a read-only copy of the production database (2026-09-05): the site publishes a median **211** distinct RSS-seen posts per day (p90 **460**, max **511**, 26 days to 2026-08-31); the crawled categories run at roughly **14/day** for 4K movies (`movies_2160p` feed, 31 days) and **7/day** for remux; the TV-packs listing has no feed of its own. The deepest crawls return about **233** posts across the nine pages of the three HDEncode sources, about **25 posts per page**. So a 3-page canary of the 4K-movie listing sees about 75 posts, which at 14/day is a **5-day** window and at a p90 burst (about 2.2× the median) about **2.3 days**; a 1-page canary sees 25 posts, a **1.7-day** window normally and under **1 day** in a burst. The Gun Stories cohort paged off fast during a bulk archive upload; bursts are the case the rule exists for.

Proposal, two layers:

- **Promotion-time rule (the authority):** for every canary source, `interval ≤ window(p90 daily rate over the last 14 days, canary pages, observed posts per page) / 2`. The rate comes from `hdencode_candidate_feeds.first_seen_at` for the feed mapped to that source (4K movies → `movies_2160p`, remux → `movies_remux`); for a source with no feed (TV packs) the rate comes from the listing itself: new URLs first cached per day for that source in `background_cache`. If either input is missing for any source, the blocker `visibility_window_unknown` refuses primary, as the decision record requires ("a fixed cadence with no established window is sampling, not a safety net, and that must be surfaced").
- **Runtime guard (every canary, the one that does not depend on an estimate):** the canary must see at least one URL the previous complete canary saw (**overlap ≥ 1**). Zero overlap means posts may have paged off between two canaries unseen; the crawl is recorded as `canary_overlap_lost` and counts as incomplete. Two consecutive overlap losses exceed `max_age` and demote. This converts the interval-versus-window relationship from an estimate into an observation made on every run.

## 5. Automatic reversion, and canary health as part of the safety claim

Evaluated once per scan cycle in effective `rss_primary`, after the RSS poll and any canary:

- `gap_proven`: `get_hdencode_miss_resolution()` reports a `never_acquired` URL whose first sighting is after the promotion record's `at`. The classifier is already lag-aware (a URL still on the listing is pending, not a gap; a URL RSS carried later is acquired), so one listing sighting that beat RSS does not demote, as the record requires.
- `canary_stale`: `now - last_success > max_age`. Before the first canary after promotion, the last complete shadow listing crawl counts as the baseline, so promotion straight from a healthy shadow is not stale at t=0.
- `systematic_gap`: stratum = the canary source (a listing category, source-owned provenance, never a title heuristic). Over the last N complete canaries (N = 4), the source produced listing evidence (> 0 URLs) and RSS carried none of its URLs (zero of that source's URLs in `feed_only ∪ duplicates` across those cycles). Coarse by design; a source whose coverage cannot be claimed refuses to claim it.
- A coordinator block (a Turnstile hold or cooldown) that stops the canary crawl is `canary_incomplete`; if it persists past `max_age` the result is demotion. That is deliberate: while the listing cannot be observed, "canary-protected" is not a claim the system can make.

Demotion persists `hdencode_discovery_mode: rss_shadow`, deletes the promotion record, writes `hdencode_rss_last_demotion: {at, reason, evidence}`, logs at WARNING, and surfaces on `/rss/status.promotion.last_demotion`. There is no automatic re-promotion. `auto_demotion_armed` is a prerequisite of authorization (a config key that defaults to true; setting it false refuses primary rather than running unprotected).

## 6. What is not in the slice

- **#94 `agent/hybrid-sweep-rebased`** (143 files, four new tables, sweep sessions with watermarks, a qualification-window model; one HIGH merge blocker, RN-5). Its `backend/sweep/gate.py` three-state model (`RssAcquisition` / `IdentityCoverage` / interval health) is the right vocabulary and its `completion.py` rule ("an unexpected page is a failure, never nothing new") is adopted here as the crawler's complete verdict. The slice does **not** merge #94 or its tables. Question for the reviewer: import the two pure modules (`gate.py`, `health.py`, about 450 lines with their tests) into the slice for vocabulary, or keep #94 whole as the evidence record and let this slice stand alone?
- Auto-grab enablement (`hdencode_rss_auto_grab_enabled`) keeps its own gate; the slice changes nothing there.
- The twelve grandfathered never-acquired releases stay a documented cohort, never relabelled.
- Promotion itself: after the slice merges and deploys, the owner promotes by `POST /rss/mode`; the authority decides. With the app stopped since 2026-08-31 the 14-day no-gap condition is not advancing.

## 7. Tests the slice must ship (each guard shown to fail)

1. Authority: every blocker in §2 produced by exactly one condition, with mutants that remove each condition killed by its test; a pre-hybrid persisted `rss_primary` (no promotion record) → `promotion_not_fresh`, effective `rss_shadow`.
2. Scheduler: in effective primary the canary runs when due and not when not due; it runs when the RSS poll failed; the first cycle after promotion runs it; the comparison row is written with `mode = rss_primary_canary`; readiness computed from mixed shadow/canary rows equals readiness from shadow rows alone for the same data.
3. Completion: an incomplete crawl does not refresh `last_success`; overlap-lost is recorded and counts as incomplete.
4. Demotion: each of the three triggers demotes exactly once, persists the mode, deletes the promotion record, writes the demotion record; none of them fires on a lag-only miss.
5. Window rule: the estimate per source; a missing input → `visibility_window_unknown`; an interval above half the window → the blocker.
6. Status: every field in §2 present with the vocabulary above; `provisional` true only when authorized.
7. Request cost, measured not asserted: a simulation over the last 500 real cycles (RSS 1.62 and listing 4.34 requests per cycle on average, cycles about 72 minutes apart) gives, for a 3-page canary every 6 h, about a 50% reduction; every 12 h about 57%; a 1-page canary every 6 h about 63%. The 2026-08-19 figures (2.07 and 10.17 per cycle over 499 cycles, mostly full-depth crawls) give 66% and 75% for the 3-page cases. #94's floor is 0.50 and its target 0.70; the target is reachable only at 12 h or shallow depth, which is exactly the trade the window rule polices. The number to decide on is the one measured after the slice runs in shadow for a week.

## 8. Order of work, if the reviewer accepts the shape

1. Restore the app and the collector (owner decisions, §0).
2. Slice PR, stacked on #108, in this order: promotion record and authority prerequisites (with `CANARY_IMPLEMENTED` still `False` until step 3 lands, so nothing opens early); scheduler canary + comparison rows + catches table; demotion; status; the window estimator; tests and mutants at each step.
3. A shadow-mode week with the canary code deployed but primary refused, to measure the request cost, the overlap guard and the window estimates on live data.
4. Owner promotion by `POST /rss/mode`, provisional, canary-protected. Never by editing the config file.

## 9. Questions for the reviewer

1. The fresh-promotion mechanism (§3): a promotion record with a `canary_version`, versus normalising a pre-hybrid persisted `rss_primary` to `rss_shadow` in a migration. The record keeps #108's requested/effective split intact; the migration would rewrite persisted state. Preference?
2. The window rule (§4): is the estimate (rate × depth) plus the runtime overlap guard sufficient, or should the authority also require a measured minimum residence from a new per-URL listing-membership record before the first promotion?
3. The systematic-gap stratum (§5): the listing source/category. Coarse enough to be safe, or too coarse to ever fire?
4. Demotion on a blocked canary after `max_age` (§5): correct, or should a coordinator hold pause the staleness clock?
5. #94 (§6): import its pure modules, or leave it whole as the record?
6. The request-cost target (§7): hold #94's 0.70 target, or accept the floor (0.50) for the provisional state and measure?
