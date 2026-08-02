# ScanHound Peer Round 3 Reply 4 — Revised Phase 3 Order and Provider Audit

**Date:** 2026-08-02
**Repository:** `LstDtchMn/ScanHound`
**Branch:** `claude/nice-meitner-2b717b`
**Production changes in this round:** none

> Relayed inline by Jesse rather than as a downloadable file; transcribed here
> verbatim so the peer-round record stays complete.

---

Mechanism correction accepted. Remove the retry/backoff claim from
`metadata_enricher.py`.

The quantified shutdown exposure is approximately 50–60 seconds wall-clock, not
four times that: the four workers run concurrently, while each worker executes
its provider chain sequentially.

Revised Phase 3 order:

1. Cancel queued metadata futures.
2. Check stop/deadline before and after every provider boundary.
3. Pass the shrinking remaining deadline into every outbound provider
   operation; do not begin another call when no budget remains.
4. Stage metadata locally and block publication after cancellation or ownership
   loss.
5. Establish foreground scan ownership.
6. Implement the bounded join and survivor reporting.

One caveat: a library's ordinary request timeout may bound socket inactivity
rather than total chain duration. `TmdbClient` and the RT scraper therefore need
a focused audit to determine whether they perform internal retries or multiple
requests. Until then, no retry-loop claim should be made beyond the verified
fallback chain in `metadata_enricher.py`.

Phase 1 remains test-only and awaits Jesse.

---

## Claude's audit of the caveat (2026-08-02, at `9726d75`)

The requested audit was run. **It overturns the mechanism correction from the
previous round, and both parties' 50–60s figure is too low.**

### `TmdbClient` does perform internal retries with sleeping backoff

`backend/tmdb_client.py` — the module docstring says so at `:3` ("rate-limited,
retrying HTTP access"), and the implementation confirms it:

| line | finding |
|---|---|
| `:25` | `max_retries: int = 2` default |
| `:53` | `for attempt in range(self.max_retries + 1)` → **3 attempts per call** |
| `:63` | `time.sleep(1.0 * (attempt + 1))` on HTTP failure |
| `:73-74` | same backoff on `requests.ConnectionError` / `requests.Timeout` |
| `:41` | `_rate_limit()` sleeps to honour the rate limit, additionally |

Worst case for **one** `TmdbClient` call is therefore
`3 × 10s timeout + 1s + 2s backoff ≈ 33s`, plus rate-limit sleep — not 10s.

The chain in `fetch_metadata()` makes up to **four** TmdbClient calls (`find`,
`search`, conditional base-title re-search, `external_ids`/`details`), so the
TMDB portion alone reaches **~132s**.

### The RT scraper is itself a multi-request fallback chain

`backend/rt_scraper.py`:

| line | finding |
|---|---|
| `:140` | `_build_rt_urls()` returns **multiple** candidate URLs |
| `:195` | `_scrape_rt_direct` loops them, `scraper.get(url, timeout=10)` each |
| `:266` | fallback endpoint `napi/search` |
| `:309` | fallback endpoint `api/private/v2.0/search` |
| `:345` | fallback `/search` page |

Several sequential 10s requests per item, not one.

### Revised exposure

Per item, worst case is on the order of **~3 minutes**, not 50–60s:
~132s TMDB + ~10s OMDb + RT's own multi-request chain.

And this compounds with a point not yet stated in either direction: **before
step 1 lands, `shutdown(wait=True)` drains the entire remaining queue, not just
the four running workers.** `enrich()` breaks out of the `as_completed` loop
without cancelling, so every already-submitted future still runs. Current
exposure is therefore `ceil(N_remaining / 4) × ~3min`, which for a scan with a
substantial backlog is minutes to tens of minutes — not a fixed 50–60s.

The 50–60s figure is only correct *after* step 1 lands and *if* the provider
chain were a single 10s call per leg. Neither holds today.

### `time.sleep()` is not interruptible

The backoff at `tmdb_client.py:63,73` and the rate-limit sleep at `:41` are
plain `time.sleep()`. A stop/deadline check placed *between* providers (step 2)
cannot interrupt a worker already parked inside one, nor one blocked in a 10s
socket read. Step 2 bounds only where checks are *reachable*; it does not bound
worst-case latency by itself.

### Correction to the previous round

The previous round's mechanism correction was **scoped to
`metadata_enricher.py`** and explicitly flagged `tmdb_client.py` and the RT
scraper as unaudited. That scoping was stated, but the consequence was still
that step 3 was recorded as having no target. It does have a target — one module
down.

**Step 3 should be restored**, retargeted from `metadata_enricher.py` to
`tmdb_client.py` (deadline-aware `max_retries`, backoff, and rate-limit sleeps)
and `rt_scraper.py` (deadline-aware fallback-URL iteration). The confirmed
`requests` semantics also matter here: `timeout=` bounds connect and read
inactivity, not total call duration, so a slow-drip response can exceed it even
without retries.
