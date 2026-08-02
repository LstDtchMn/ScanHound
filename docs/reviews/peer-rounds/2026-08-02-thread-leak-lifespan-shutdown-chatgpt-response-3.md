# ScanHound Peer Round 3 — Attribution Design and Sequencing Review

**Date:** 2026-08-02  
**Repository:** `LstDtchMn/ScanHound`  
**Branch:** `claude/nice-meitner-2b717b`  
**Head reviewed:** `252ec6439f5138a2e6e3080a6f02f1bf0d225b8d`  
**Base:** `7cc5275b4a518cb6986f34e26e1c0e9c98175b7c`  
**Review package:** `docs/reviews/peer-rounds/2026-08-02-thread-leak-lifespan-shutdown-for-chatgpt.md`

## Evidence boundary

The pushed branch was inspected directly through the private GitHub repository.

The branch is two commits ahead of the stated base and changes only:

- the three peer-round Markdown documents;
- `tests/tools/probe.py`;
- `tests/tools/netwatch.py`.

There are no production-code changes on this branch. The reported 296-test probe runs and their timing/count results are author-reported local evidence; there is no branch CI and the full suite was not run in this scope.

The following current production paths were inspected at the reviewed head:

- `backend/api/routes/scanner.py`
- `backend/api/routes/scheduler.py`
- `backend/scanner_service.py`
- `backend/background_scanner.py`
- `backend/metadata_enricher.py`
- `backend/api/dependencies.py`
- `ui/controllers/scanner_controller.py`
- `tests/tools/probe.py`
- `tests/tools/netwatch.py`

The two distinct SHAs for the closed flake fix are accepted as branch-specific applications of the same patch:

- `6d067e2` on `agent/rename-safety-gate`;
- `9b059c5` on `agent/hybrid-sweep-implementation`.

This round does not reopen that fix.

---

# Executive verdict

## Q1 — attribution design

**Proceed, but add several fields and widen the coverage before implementation.**

The proposed context is worthwhile, but an implementation limited to `/scan/start`, `_run_scan`, and the asyncio default executor would still be unable to prove ownership of event 2.

The context must cover:

1. every origin that calls `ScannerService.run_scan`;
2. the asyncio listing executor;
3. the explicit detail-processing `ThreadPoolExecutor`;
4. the metadata-enrichment `ThreadPoolExecutor`;
5. acceptance-time and execution-time lifecycle identity;
6. publication attempts, not only execution milestones.

The first implementation should be **observational only**. It must not yet capture a strong scanner reference or change `_run_scan` to use the accepted scanner, because doing so would alter the ownership race being measured.

## Q2 — guard or production attribution first

**The no-egress guard should be the first code commit. The attribution context should be the first production-code commit.**

Keep the existing probe as a record-and-block diagnostic instrument. Harden `netwatch.py` into a fail-capable test gate and demonstrate:

- red on the current leaking baseline;
- green after the ownership fix;
- red again under an injected-egress mutation.

Do not merge a permanently red mainline. Preserve the red-before/green-after evidence as separate commits or recorded commands in one review branch.

## Q3 — sequencing after the other session disappeared

**Yes, the sequencing recommendation becomes simpler and stronger.**

There is no longer a reason to delay or coordinate around an in-flight lifespan implementation. After the guard and attribution pass:

1. fix foreground `_run_scan` ownership and its executor lifecycle;
2. prove the known foreground event is gone;
3. use event-2 attribution to scope the remaining lifespan cleanup;
4. then implement the generic lifespan worker contract.

The disappearance of the parallel session removes the coordination gate. It does not remove the need to keep attribution and behavior-changing fixes in separate commits.

---

# 1. What the attribution context must contain

A useful minimum shape is:

```python
@dataclass
class ScanOperationContext:
    scan_uuid: str

    # Where this scan came from.
    origin: str
    parent_operation: str | None

    # Snapshot at acceptance/call creation.
    accepted_at_ns: int
    accepted_lifespan_generation: int
    accepted_scanner_id: int | None

    # Snapshot when the worker actually begins.
    entered_at_ns: int | None
    entered_lifespan_generation: int | None
    entered_scanner_id: int | None

    # Thread-safe, bounded diagnostic sink.
    trace: ScanTrace
```

Recommended `origin` values include:

```text
api_manual
api_scheduler
background_periodic
background_manual
qt_scan_worker
direct_test
unknown
```

`parent_operation` is useful when a background cycle launches one source scan or when a scheduler action launches a foreground scan.

## Do not rely on `id(scanner)` alone

Use the tuple:

```text
(lifespan_generation, id(scanner))
```

at acceptance and entry.

The generation prevents a recycled Python object ID from looking like the same owner across lifespans.

Record both of these independently:

```text
accepted_owner = (accepted_generation, accepted_scanner_id)
entered_owner  = (entered_generation, entered_scanner_id)
```

A mismatch is the evidence being sought.

## Capture generation at both times

The current proposal mentions one `lifespan_generation`. That is insufficient for the race being investigated.

Record:

- generation when the request or scan invocation is accepted;
- generation when `_run_scan` begins;
- generation immediately before any result publication.

The registry is mutable and reused. The thread can cross a lifespan boundary before it executes.

## Do not retain the accepted scanner in the diagnostic phase

The first attribution pass should store identity only:

- generation;
- object ID;
- optional weak reference;
- type/name.

Do **not** put a strong `scanner` reference in the diagnostic context yet.

A strong reference would keep the accepted scanner alive and could change the very inert/old/new-scanner distribution being measured. It would also be the beginning of the ownership fix rather than neutral instrumentation.

After the attribution run, the production fix should intentionally replace identity-only capture with an immutable operation object holding the accepted scanner reference.

---

# 2. Instrument every scan origin

The current code has more than one route into `ScannerService.run_scan`.

## FastAPI manual scan

`POST /scan/start`:

- sets the module-global state;
- starts `_run_scan`;
- passes the mutable registry;
- `_run_scan` later dereferences `reg.scanner`.

This is the proven foreground leak.

## FastAPI scheduler trigger

`POST /scheduler/trigger` starts `_run_scan` independently and does not retain a thread handle.

It must create the same operation context with `origin="api_scheduler"`.

## Background scanner

`BackgroundScanner._scan_source()` calls `self._reg.scanner.run_scan()` directly.

Event 2 remains unproven specifically because the current thread name cannot distinguish this path from a foreground scan's executor. The background path must receive or create a context with a distinct origin and parent background-cycle ID.

## Qt/Desktop scan worker

`ui/controllers/scanner_controller.py::ScanWorker.run()` also calls `ScannerService.run_scan()`.

This path is not implicated in the FastAPI TestClient flake, but the `ScannerService` API is framework-agnostic. Adding a mandatory FastAPI-specific context would break or distort this caller.

Use an optional context argument:

```python
def run_scan(..., operation_context: ScanOperationContext | None = None):
```

If absent, create a context with `origin="direct"` or allow the caller to supply `origin="qt_scan_worker"`.

## Direct tests and scripts

The default/fallback context ensures direct `run_scan()` calls remain observable without forcing every existing test double to understand FastAPI lifecycle state.

---

# 3. Instrument all executor domains

A single scan-specific prefix on the asyncio default executor is not enough.

## A. Listing executor

The listing crawl uses:

```python
await loop.run_in_executor(None, _fetch_page)
```

This is the source of the ambiguous `asyncio_0` name.

Create and retain a dedicated default executor for the scan:

```python
listing_executor = ThreadPoolExecutor(
    thread_name_prefix=f"scan-{short_uuid}-listing"
)
loop.set_default_executor(listing_executor)
```

Record:

```text
listing_executor_created
listing_submitted
listing_started
transport_constructed
listing_finished
listing_executor_shutdown_started
listing_executor_shutdown_finished
```

## B. Detail-processing executor

`ScannerService._process_posts()` creates a separate explicit `ThreadPoolExecutor`.

Those workers can reach `detail_scraper` and construct transports. Give this pool its own prefix:

```text
scan-<uuid>-detail
```

Pass the operation context explicitly into the worker closure and record detail milestones.

## C. Metadata-enrichment executor

`MetadataEnricher.enrich()` creates another explicit `ThreadPoolExecutor`.

Its network work is TMDB/OMDb/IMDb/RT rather than the observed HDEncode construction, but it is still part of the scan's external-I/O ownership and can survive or delay shutdown.

Give it a prefix such as:

```text
scan-<uuid>-metadata
```

Either pass the operation context to `enrich()` or pass a neutral trace callback. Do not make `metadata_enricher.py` depend on FastAPI registry types.

## Context propagation must be explicit

Do not rely only on `contextvars`.

`run_in_executor()` and explicit `ThreadPoolExecutor.submit()` do not provide the required ownership propagation automatically in every supported Python path.

Wrap submitted functions or pass the context directly:

```python
executor.submit(run_with_scan_context, context, process_post, post)
```

Thread names are evidence aids, not the ownership mechanism.

---

# 4. Add publication milestones

The current proposed milestone list is execution-heavy. Add state-publication boundaries.

At minimum:

```text
accepted
thread_started
entry_owner_snapshotted
slot_attempted
slot_acquired
run_scan_entered
event_loop_created

listing_executor_created
listing_submitted
listing_transport_constructed
listing_finished

detail_executor_created
detail_submitted
detail_transport_constructed
detail_finished

metadata_executor_created
metadata_submitted
metadata_finished

stop_requested
results_ready

publish_last_scan_items_attempted
publish_websocket_attempted
publish_config_attempted
publish_notification_attempted
publish_autograb_attempted

executor_shutdown_started
executor_shutdown_finished
slot_released
thread_finished
```

For each publication milestone, record:

- operation UUID;
- current generation;
- accepted generation;
- thread name/ID;
- whether the operation still owns the lifespan;
- whether it is still the active foreground operation.

This will prove not only who made event 2, but whether stale scans attempt to mutate shared state after rollover.

---

# 5. Trace implementation requirements

The trace must be:

- thread-safe;
- bounded;
- based on `time.monotonic_ns()`;
- sequence-numbered;
- free of credentials;
- optionally enabled or injectable;
- available to the probe without making the probe depend on thread-name parsing.

A record shape such as this is sufficient:

```python
@dataclass(frozen=True)
class ScanTraceEvent:
    sequence: int
    monotonic_ns: int
    scan_uuid: str
    stage: str
    origin: str
    thread_name: str
    thread_ident: int | None
    thread_native_id: int | None
    lifespan_generation: int | None
    scanner_id: int | None
    executor_kind: str | None
    source_kind: str | None
```

Do not store full release URLs, query strings, tokens, or request headers in the general trace.

A short host/source label is enough for this ownership question.

---

# 6. Where the context should live

Do not define the reusable context in `backend/api/routes/scanner.py`.

`ScannerService` is explicitly framework-agnostic and is used by:

- FastAPI routes;
- the background scanner;
- the Qt controller;
- direct tests/scripts.

Put the context in either:

```text
backend/scan_context.py
```

or a similarly neutral backend module.

The FastAPI route may construct the context with registry-generation data, but the scan engine should depend only on the neutral context interface.

---

# 7. No-egress guard sequencing

## First code commit: make the guard fail reliably

The current `probe.py` and `netwatch.py`:

- patch DNS/connect;
- record the active test and thread;
- raise `OSError`;
- print a summary.

They do not currently turn swallowed egress into a failed pytest run.

The hardened guard needs:

1. a thread-safe attempt ledger;
2. loopback and explicitly declared local-fixture allowlisting;
3. a nonzero pytest outcome even when the application swallows the socket exception;
4. detection of events between tests and after test teardown;
5. a self-test or injected mutation proving it fails.

A practical split:

- `probe.py`: diagnostic, blocks and records but may allow the suite result to stay green;
- `netwatch.py`: enforcement, blocks, records, and forces a failing pytest exit when any unauthorized attempt occurs.

## Attribution wording

The guard should distinguish:

```text
observed_during_test
originating_operation
```

The active pytest node is where the leaked thread happened to run, not necessarily the test that created it.

Once the scan context exists, report both.

## Red-before/green-after

Recommended history:

1. **test-only guard commit**
   - optional plugin or explicit command;
   - current baseline produces a nonzero result;
   - injected egress test proves enforcement.

2. **attribution commit**
   - production instrumentation only;
   - still red under the enforcement guard;
   - blocked probe identifies event owners.

3. **foreground ownership/executor fix**
   - guard becomes green for foreground-origin events.

4. **remaining lifespan fix**
   - guard and thread-leak gate fully green.

Do not add `hdencode.org` to the allowlist.

Because this branch has no CI, the red/green evidence is local and must be recorded with exact commands and exit codes.

---

# 8. First production change

After the guard commit, the attribution context is the correct first production-code change.

Keep it behavior-neutral:

- do not replace the late registry dereference yet;
- do not capture a strong scanner reference yet;
- do not suppress publication yet;
- do not join or cancel threads yet;
- do not combine the context with the lifespan implementation.

The goal of that commit is one falsifiable output:

> Every blocked `create_scraper` and socket attempt can be assigned to one scan UUID, one origin, one accepted owner, one entry owner, and one executor stage.

Only after that measurement should the behavior-changing ownership fix land.

---

# 9. Sequencing now that the parallel session is gone

The prior coordination caution no longer applies.

The recommended sequence is now:

## Phase 1 — test containment

1. Harden `netwatch.py`.
2. Prove current subset fails because of unauthorized egress.
3. Prove an injected egress mutation fails.
4. Preserve `probe.py` as the non-failing diagnostic mode.

## Phase 2 — ownership attribution

1. Implement the neutral scan context.
2. Cover manual, scheduler, background, Qt/direct origins.
3. Prefix listing, detail, and metadata executors.
4. Add publication milestones.
5. Run the blocked probe repeatedly.
6. Identify event 1 and event 2 by scan UUID/origin.

## Phase 3 — foreground fix

This work is unopposed and should proceed next regardless of event 2:

1. replace the single module-global `_scan_thread` with a reachable active-operation owner;
2. capture the accepted scanner strongly in the operation object;
3. make stop/cancellation operation-scoped;
4. own the event loop and executors;
5. shut executors down;
6. bounded-join the outer scan operation;
7. reject stale-generation publication;
8. make the test fixture stop/join rather than overwrite live state.

Event 1 is already sufficient to justify this fix.

## Phase 4 — remaining lifespan cleanup

Use event-2 attribution to determine which additional owner must be fixed first.

Then apply the shared worker contract to all remaining lifespan-created workers:

- cooperative cancellation;
- interruptible waits;
- retained handles;
- common shutdown deadline;
- bounded joins;
- stale-generation publication checks;
- named survivor reporting.

There is no longer a need for Jesse to choose between competing implementation sessions.

Jesse still remains the gate for merging behavior-changing production work.

---

# 10. Minimum tests for the attribution commit

Before treating the attribution instrument as trustworthy, add tests for:

## Acceptance versus entry ownership

Force the worker to pause before `_run_scan` dereferences the registry, advance the lifespan, and replace the scanner.

Assert the trace records:

```text
accepted_generation != entered_generation
accepted_scanner_id != entered_scanner_id
```

Do not depend on timing luck.

## Same-owner normal path

A normal scan should record matching accepted and entered owner tuples.

## Origin coverage

Assert distinct origins for:

- `/scan/start`;
- `/scheduler/trigger`;
- `BackgroundScanner._scan_source`;
- direct/Qt-style `run_scan`.

## Executor attribution

Assert scan-specific names or context records for:

- listing;
- detail;
- metadata.

## Context propagation

A worker event must carry the same scan UUID as its parent scan.

## Bounded trace

A long scan or repeated milestones must not grow an unbounded process-global list.

## No semantic behavior change

The attribution-only commit must leave existing scan results, route status, and stop behavior unchanged.

---

# 11. What evidence is required after implementation

Re-run the exact blocked subset three or more times and report, per event:

```text
scan_uuid
origin
accepted owner tuple
entered owner tuple
executor kind
milestone
active pytest node
destination host/port
```

The next review should answer:

1. Is event 1 foreground manual, as expected?
2. Is event 2 foreground, scheduler, or background?
3. Did any scan cross lifespan generation?
4. Did any executor continue after its outer scan thread finished?
5. Did any stale scan attempt publication?
6. Did the guard return nonzero despite broad application exception handling?

A full suite is not required to answer ownership, but it will be required before merging the eventual production fix.

---

# Final answers

## Question 1

Yes, the context is worth implementing after these additions:

- origin and parent operation;
- acceptance and entry generation;
- acceptance and entry scanner identity;
- explicit coverage of manual, scheduler, background, Qt/direct calls;
- listing, detail, and metadata executor attribution;
- publication milestones;
- explicit thread-context propagation;
- bounded, thread-safe monotonic trace;
- identity-only capture during the diagnostic phase.

## Question 2

The no-egress guard should land first as the test-only red-before/green-after instrument.

The attribution context should then be the first production change.

Do not combine attribution with the ownership fix, because that would change the race before measuring it.

## Question 3

Yes, sequencing changes now that the other session produced no commits and is gone.

After guard and attribution, `_run_scan` ownership is the unopposed first production fix. Generic lifespan cleanup follows, informed by event-2 attribution.

---

# Disposition

**Approved to proceed with implementation, subject to the design additions in this review.**

This is not approval of a production patch because none exists on the reviewed branch.

No further pasted parts are needed for this round; the private GitHub connector provided the package, probe code, and current production files directly.
