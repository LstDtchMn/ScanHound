# Scan-failure visibility and the listing-URL ledger

**Date:** 2026-07-27
**Status:** design. No code changed. Nothing enabled.
**Branch:** `agent/scan-failure-visibility` (off `main` @ `9d1a99c`)
**Decision owner:** Jesse.
**Supersedes the scope of:** task #184 ("show scan failures in the UI"), which is
now the smallest slice of a larger correctness problem.

---

## The defect

Measured on six consecutive live cycles, 2026-07-27:

```
Skipped 850 previously scanned URLs
Found 128 posts, processing details...
Processing complete: 2 items created from 128 posts
```

Items kept: 4, 2, 2, 3, 1, 2 out of ~128. A **~98% discard rate**, stable across
cycles — the signature of a fixed set failing repeatedly, not of varying content.

### It is self-reinforcing

Early-stop fires only when a listing page yields zero *new* posts
([scanner_service.py:743](../../backend/scanner_service.py)). A post that fails
detail processing never becomes a `MediaItem`, so it is never cached, so it
counts as new again next cycle, so the page never looks fully-seen, so the crawl
continues deeper and re-fetches it. **The failures cause the over-crawling that
re-fetches the failures.**

Confirmed in the logs: 4K Movies never logs `reached previously-cached content`
and runs ~60 s, while Remux Movies and TV Packs both early-stop at page 1–2 in
3–4 s. `background_scan_pages = 30`.

### What it is not

Ruled out with evidence, so the investigation does not repeat these:

| hypothesis | evidence against |
|---|---|
| coordinator denying requests | `source_health` = `healthy`, `consecutive_failures` = 0, `cooldown_until` = `None`, last success seconds ago. `_CLASS_LIMITS["detail"] = 3` is concurrency, not a quota. |
| HTTP/Cloudflare failure | the coordinator degrades health on non-200 via `observe_http_status`; health is clean, so fetches return 200. |
| cached-URL skip hiding posts | a brand-new post is by definition not in the cache. The skip set destroys *audit completeness*, not new-arrival detection. |

**The fetches succeed and the results are discarded during parsing or item
construction.** Both paths are invisible at the container's INFO level:
[scanner_service.py:818](../../backend/scanner_service.py) (`if not details:
return None`) and [scanner_service.py:823](../../backend/scanner_service.py)
(`except Exception: logger.debug(...)`).

**Absence of error lines in the log is not evidence of success here.** The
logging is suppressed, not the errors. This document exists partly because that
inference was drawn once already and was wrong.

### Cost and blast radius

- ~126 discarded detail fetches × 16.6 cycles/day ≈ **2,100 requests/day** wasted.
  Larger than the ~475/day the parked `rss_primary` promotion would have saved.
- The surviving 1–4 items are what
  [background_scanner.py:463](../../backend/background_scanner.py) passes as
  `listing_items` to `compare_shadow`. **This invalidated the seven-day RSS
  certification** — see the parked criterion spec at `65c54cd`.
- `background_scan_enabled` is **`False`**. The crawl runs only because
  `rss_shadow` forces it ([background_scanner.py:143](../../backend/background_scanner.py)).
  So HDEncode discovery currently runs at 1–4 items/cycle through a path the
  operator has switched off.

---

## Design

Peer-reviewed 2026-07-27. The central correction, which rejects this author's
first proposal:

> **A URL stops counting as "new" once it has been *observed* on a listing page —
> not once it successfully parses.**

The rejected alternative was a negative cache keyed on failure count. That
conflates "we could not parse this" with "we have handled this," and would mask a
parser regression behind a suppression list. Keying on observation breaks the
loop after the *first* sighting while failure state stays separate and visible.

### Three independent states

Never collapse these into "did this URL become a cached MediaItem?":

```
listing_state : observed | no_longer_observed
detail_state  : unattempted | retryable_failure | quarantined_failure | succeeded
retry_state   : due | deferred_until | manual_hold
```

A URL may legitimately be *observed on the listing*, *not successfully parsed*,
*temporarily backed off*, and *still visible as an unresolved failure*.

### Listing-URL ledger

Written **as soon as a URL is seen on a listing page, before any detail work**:

```
canonical_url, source, category, first_seen_at, last_seen_at,
detail_state, consecutive_failures, total_failures,
last_failure_code, last_failure_at, next_retry_at, last_success_at,
parser_version, content_fingerprint
```

Early-stop consults observation, not success.

### Quarantine governs retries, not discovery

Retry policy, all configurable and tuned after measurement: retry on first
failure; back off exponentially; quarantine after 2–3 consecutive failures with
the *same* bounded reason code *and* an unchanged content fingerprint; cap the
retry delay near 24 h. **No negative entry is ever permanent.**

Retries become immediately due when the parser version changes, the content
fingerprint changes, the operator asks, a scheduled canary selects the URL, or
the failure classification changes.

An **independent retry queue** is mandatory: once failed URLs count as known,
the listing crawl will no longer rediscover them, so retries must be scheduled
from `next_retry_at`. Without this, the ledger *becomes* the regression-hiding
suppression list it was designed to avoid.

### Bounded reason codes

Persisted per discard — never free-form DEBUG text:

```
detail_empty            detail_parse_exception
missing_required_title  missing_required_url
invalid_metadata        media_item_exception
traffic_cancelled       source_blocked
```

### Per-stage counters

Persisted per source and category:

```
listing_pages_requested   listing_pages_succeeded
listing_urls_seen         listing_urls_new        listing_urls_skipped_cached
detail_attempted          detail_succeeded
detail_returned_none      detail_exception
media_item_created        media_item_construction_failed
cancelled                 blocked                 early_stopped
```

Plus `detail_retryable`, `detail_quarantined`, `detail_failed_this_cycle`,
`oldest_quarantined_age`, failure counts by reason and by source, and the detail
success ratio.

### Source-level circuit breaker

Per-URL quarantine is insufficient when most URLs fail at once. 126 failures in
one cycle is one systemic regression, not 126 unrelated events.

Trigger on: detail-success ratio below a floor; absolute unexplained failures
above a threshold; one failure reason dominating a large cohort; or a success
count implausibly small against attempts. On trigger: mark HDEncode detail
processing degraded, stop further detail work for the cycle, persist the reason
distribution, log at WARNING, notify via Gotify, and **keep the URLs retryable
rather than suppressing them**.

**A cycle yielding 1–4 items from ~128 attempts must never be published as an
ordinary successful scan.** That it currently is, is the whole defect.

### Quarantine must never count as success

Quarantined or retryable URLs are not successful cached releases, not completed
discovery, not valid shadow-comparison evidence, and not a healthy detail result.

---

## Relationship to the parked RSS work

This ledger is the foundation for the Phase 1 / Phase 2 model adopted in the
parked criterion spec (`65c54cd`):

- **Phase 1 — membership.** Compute `listing_only` / `feed_only` / `duplicate`
  from full URL membership sets captured before detail processing. Never derive
  the listing side from surviving `MediaItem`s.
- **Phase 2 — classification.** Only `listing_only` URLs need relevance
  classification. Cheaper than detail-fetching every duplicate, and more correct.
  Any unclassified `listing_only` URL makes the cycle's evidence incomplete.

One ledger fixes both the repeated-fetch waste and the invalid coupling between
RSS evidence and detail-parse success.

---

## Implementation order

Peer-reviewed ordering, retained as given:

1. Durable per-stage counters and bounded reason codes.
2. **One DEBUG cycle; establish the failure distribution.** *(Jesse's action —
   `debug_mode` is read at startup, [app_service.py:423](../../backend/app_service.py),
   so it needs a restart.)*
3. Listing-URL ledger, populated before detail processing.
4. Early-stop switches to observed-URL membership.
5. Independent retry/backoff queue.
6. Source-level degradation threshold and circuit breaker.
7. Fix the dominant parser or construction defect.
8. Verify failed URLs retry and recover after a parser change.
9. Refactor the shadow comparison to use membership.
10. Start a fresh RSS qualification epoch only after all of the above.

Steps 1 and 2 are independent; step 2 costs nothing and may materially shrink
steps 3–7 if the distribution shows a single dominant cause.

---

## Constraint on any future RSS-primary transition

There is currently **no listing-discovery mode that works independently of
shadow qualification**. `background_scan_enabled` is `False`, and the scan runs
only as a side effect of `rss_shadow`. Disabling shadow mode would therefore stop
HDEncode discovery rather than reverting to listing discovery.

A future degraded latch cannot claim a "safe listing fallback" until such a mode
exists and has been verified. Recorded here because the parked criterion spec's
fallback options assumed one was available.

---

## Open items

- Quarantine threshold and backoff cap — defaults proposed above, tune after
  step 2.
- Whether the circuit breaker halts the cycle or only marks it degraded.
- Whether the ledger supersedes `background_scan_cache` or sits beside it.
