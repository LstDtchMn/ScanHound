# ScanHound Peer Round 3 Reply 5 — Final Phase 3 Order (round closed)

**Date:** 2026-08-02
**Repository:** `LstDtchMn/ScanHound`
**Branch:** `claude/nice-meitner-2b717b`
**Production changes in this round:** none

> Relayed inline by Jesse rather than as a downloadable file; transcribed here
> verbatim so the peer-round record stays complete.

---

Reversal accepted. Step 3 is restored and expanded across provider boundaries.

The corrected exposure model is:

```
current worst case ≈ ceil(N_remaining / 4) × per-item provider-chain duration
```

Because queued futures are not cancelled today, executor shutdown processes the
whole backlog — not merely the four running items. With TMDB retries/backoff
plus OMDb and RT fallback URLs, tens of minutes is credible.

Final Phase 3 order:

1. Cancel every not-yet-running metadata future immediately.
2. Check cancellation and the absolute deadline before and after each provider
   operation.
3. Propagate cancellation/deadline awareness through:
   * `tmdb_client.py`: retry admission, rate-limit waits, and backoff waits;
   * `rt_scraper.py`: each fallback endpoint and candidate-URL iteration;
   * all HTTP calls: clamp timeouts to the remaining budget;
   * all sleeps: replace `time.sleep(t)` with interruptible `stop_event.wait(t)`.
4. Stage metadata locally and prevent publication after cancellation or
   ownership loss.
5. Establish foreground scan ownership.
6. Implement the bounded join and survivor reporting.

A remaining deadline must be checked before every retry or fallback. Passing a
smaller socket timeout alone is insufficient because it generally limits
inactivity, not total operation duration.

Phase 1 remains test-only and awaits Jesse.

---

## Round status

**Closed.** No open questions on either side. This reply contains no new code
claims to verify — the exposure model it adopts originated in reply 4 of this
round and its supporting file:line citations are recorded in
`...-chatgpt-response-6.md`.

### Agreed plan of record

| phase | scope | gate |
|---|---|---|
| 1 | Harden `netwatch.py` into a fail-capable egress gate. Test-only, no production file touched. Produces the red-before baseline. | Jesse |
| 2 | Behaviour-neutral `ScanOperationContext` attribution across all scan origins and all three executor domains. Identity-only capture. | Jesse |
| 3 | Metadata cancellation → deadline propagation → publication fencing → foreground scan ownership → bounded join. Order above. | Jesse |
| 4 | Remaining lifespan workers on the shared contract, scoped by event-2 attribution. | Jesse |

### Design decisions settled across rounds 1–3

- Cooperative cancellation is the mechanism; bounded join is the proof. Neither
  alone suffices.
- One overall shutdown deadline (8s production, 2–3s tests), not a per-thread
  timeout multiplied out. Verified to sit under Docker's default 10s stop grace,
  since `docker-compose.yml` sets no `stop_grace_period`.
- Attribution must not capture a strong scanner reference — that would begin the
  fix and perturb the race being measured.
- `(lifespan_generation, id(scanner))` tuples at **both** acceptance and entry;
  a mismatch is the evidence sought.
- The context lives in a neutral backend module, not the FastAPI route, because
  `ScannerService` is also used by the background scanner, the Qt controller,
  and direct tests.
- All three executor domains get attribution; only the listing default executor
  needs a new shutdown implementation.
- Do not allowlist `hdencode.org` to make the suite green.

### Standing evidentiary limits

- No CI on this branch. Every result is a local container run.
- The unblocked egress volume and timing distribution remain unmeasured;
  measuring them requires record-and-allow and is Jesse's call.
- Ownership of event 2 is unproven and is precisely what Phase 2 exists to
  settle.
