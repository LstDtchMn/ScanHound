# ScanHound Peer Round 3 Reply — Executor Scope Clarification

**Date:** 2026-08-02  
**Repository:** `LstDtchMn/ScanHound`  
**Branch:** `claude/nice-meitner-2b717b`  
**Head verified:** `b3175d6cfc436d1eccc9a316fba791b401f76f87`  
**Base:** `7cc5275b4a518cb6986f34e26e1c0e9c98175b7c`  
**Production changes in this round:** none

## Verdict

**Scope reduction accepted.**

The Round 3 wording should be corrected to distinguish:

1. **attribution coverage**, which must include listing, detail, and metadata workers; from
2. **executor-lifecycle repair**, which is required only for the listing path's asyncio default executor.

The detail and metadata pools are already explicitly owned by their enclosing call frames:

```python
with ThreadPoolExecutor(...) as executor:
    ...
```

On every ordinary or exceptional exit from those `with` blocks, `ThreadPoolExecutor.__exit__()` invokes executor shutdown and waits for its workers.

They appear in the leak probe because the enclosing scan has not reached the end of the `with` block—not because those executors are ownerless or lack shutdown code.

---

# Corrected Phase 2 scope

Instrument all three worker domains:

| Domain | Instrument? | New shutdown implementation? |
|---|---:|---:|
| Listing: `loop.run_in_executor(None, ...)` | Yes | **Yes, in the later fix phase** |
| Detail: `_process_posts()` context-managed pool | Yes | No |
| Metadata: `MetadataEnricher.enrich()` context-managed pool | Yes | No |

For all three, add:

- scan UUID;
- origin and parent operation;
- accepted and entered `(generation, scanner_id)` tuples;
- executor-kind milestone;
- explicit context propagation into submitted callables;
- scan-specific thread-name prefix where practical;
- transport-construction attribution;
- completion/cancellation milestones.

For the diagnostic phase, do not change detail or metadata executor lifetime semantics.

---

# Important qualification

Although the detail and metadata executors are already owned and shut down, their current cancellation behavior is not equally strong.

## Detail executor

`ScannerService._process_posts()`:

- uses a context-managed pool;
- checks `stop_scan_flag` in each worker;
- passes a stop callback into detail scraping;
- attempts to cancel queued futures once the coordinator observes the stop flag.

Running futures still have to return before the context manager exits, which is expected because Python cannot forcibly kill worker threads.

No separate executor-owner implementation is needed.

## Metadata executor

`MetadataEnricher.enrich()`:

- uses a context-managed pool;
- checks `stop_flag_fn` only while consuming completed futures;
- submits the whole item set up front;
- does not cancel queued futures when stopping;
- may therefore continue processing submitted items while the context manager waits for all work to end.

That is not an executor leak. It is a **cooperative scan-termination gap**.

Phase 3 should consider:

- checking the stop callback inside `fetch_metadata`;
- canceling futures that have not started;
- avoiding new publication after cancellation;
- ensuring every network operation has finite timeouts.

Those changes belong to scan termination, not to a second metadata-executor shutdown framework.

---

# Listing executor

The listing path remains materially different:

```python
await loop.run_in_executor(None, _fetch_page)
```

The work is submitted to the event loop's default executor. The current scan creates and closes a fresh event loop without explicitly awaiting completion of that executor.

This is the only executor domain that needs an executor-lifecycle fix.

The later ownership fix should:

1. create or retain the listing executor explicitly;
2. give it a scan-specific prefix;
3. propagate the operation context into `_fetch_page`;
4. request cancellation of queued work where possible;
5. await or shut down the executor as part of scan completion;
6. bind that wait to the eventual application-wide shutdown deadline;
7. support every declared Python version.

---

# Correction to Round 3 wording

Replace any implication that all three pools need new shutdown implementations with:

> All three executor domains must be instrumented because transport construction may occur in any of them. Only the listing path's asyncio default executor lacks explicit completion ownership. The detail and metadata pools are already context-managed; their remaining issue is whether the enclosing scan can cooperatively reach the end of those contexts promptly.

The original recommendation to assign distinct prefixes remains useful:

```text
scan-<uuid>-listing
scan-<uuid>-detail
scan-<uuid>-metadata
```

For detail and metadata, the prefix is attribution only. It is not a new ownership mechanism.

---

# Sequencing remains unchanged

## Phase 1 — test-only guard

Harden `netwatch.py` so swallowed egress still produces a nonzero pytest result.

This remains the correct first code change.

## Phase 2 — behavior-neutral attribution

Implement the neutral `ScanOperationContext` and instrument:

- manual FastAPI scans;
- scheduler-triggered scans;
- periodic/manual background scans;
- Qt scans;
- direct callers;
- listing, detail, and metadata worker domains;
- publication attempts.

Keep identity-only capture. Do not yet retain the accepted scanner strongly or fix the late registry dereference.

## Phase 3 — foreground ownership and termination

Fix:

- `_run_scan` ownership;
- captured scanner ownership;
- operation-scoped cancellation;
- stale-generation publication;
- listing executor completion;
- prompt exit from detail/metadata work through cooperative cancellation.

Do not build redundant executor managers around the existing detail and metadata context managers.

## Phase 4 — remaining lifespan workers

Use event-2 attribution to identify and fix any remaining owner.

---

# Final answer

I agree with the scope reduction.

**Phase 2 should instrument three domains but plan only one new executor-shutdown implementation: the listing default executor.**

For detail and metadata, the relevant later work is making the enclosing scan terminate promptly and safely so their existing context managers can complete. Metadata cancellation is weaker than detail cancellation and should be addressed in Phase 3, but it is not a separate leak-ownership defect.

The production phases remain gated on Jesse's authorization, as stated.
