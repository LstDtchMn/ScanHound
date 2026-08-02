# ScanHound Peer Round 3 Reply 3 — Metadata Cancellation Ordering

**Date:** 2026-08-02
**Repository:** `LstDtchMn/ScanHound`
**Branch:** `claude/nice-meitner-2b717b`
**Production changes in this round:** none

> Relayed inline by Jesse rather than as a downloadable file; transcribed here
> verbatim so the peer-round record stays complete.

---

Severity addition accepted. Metadata cancellation is a prerequisite, not
parallel cleanup.

One precision: the wait is not bounded by the application shutdown deadline.
Even if individual HTTP calls have transport timeouts, their retries and
sequential providers can greatly exceed an eight-second shutdown contract.

Corrected Phase 3 order:

1. Cancel queued metadata futures.
2. Add stop/deadline checks inside `fetch_metadata()`, including before and
   after each provider call.
3. Make retry delays and request timeouts deadline-aware.
4. Prevent cancelled or stale workers from publishing partial metadata.
5. Establish foreground scan ownership.
6. Add the bounded join and survivor reporting.

A stop check only at `fetch_metadata()` entry is insufficient: up to four
already-running workers remain uncancellable by `Future.cancel()`. They must
cooperate between external operations, and their publication must be fenced
after ownership is lost.

Phase 1 remains test-only and awaits Jesse; the corrected metadata prerequisite
belongs before the Phase 3/4 shutdown deadline work.

---

## Claude's verification of this reply (2026-08-02, at `d0fb99e`)

The precision point is correct, and the code allows it to be quantified.

`fetch_metadata()` (`backend/metadata_enricher.py:142`) issues a **sequential
provider chain**, not a single call:

| # | call | timeout |
|---|---|---|
| 1 | `tmdb.find(imdb_id)` | 10s (`TmdbClient(api_key, timeout=10)`, `:140`) |
| 2 | `tmdb.search(title, ...)` | 10s |
| 3 | `tmdb.search(base_title, ...)` — conditional re-search | 10s |
| 4 | `tmdb.external_ids(...)` or `tmdb.details(...)` | 10s |
| 5 | `requests.get("https://www.omdbapi.com/", timeout=10)` (`:223`) | 10s |
| 6 | RT scrape | — |

Worst case is therefore **~50–60s of sequential network wait inside a single
worker**, with no stop check between steps. With `max_workers=4` all running at
stop time, `shutdown(wait=True)` can block for that full duration. An 8s
shutdown contract cannot survive it.

**One correction to the mechanism.** No retry or backoff loop exists in this
path — `grep` for `max_retries`, `retries`, `time.sleep`, `backoff` across
`backend/metadata_enricher.py` returns nothing. What produces the long wall
time is the **provider fallback chain** plus the one conditional base-title
re-search at step 3, not retry delays.

Consequence for the corrected order: step 3 ("make retry delays and request
timeouts deadline-aware") has no retry delays to target in this module. The
effective work is a deadline check **between providers**, which is step 2's
scope, plus passing a shrinking remaining-deadline into each call's `timeout=`
so the chain cannot outlive the contract. Step 3 should be re-scoped to the
latter rather than dropped.
