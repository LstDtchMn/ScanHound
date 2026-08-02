# ScanHound Peer Round Review — Thread Leak, Unexpected Egress, and Shutdown Ownership

**Review date:** 2026-08-02  
**Repository:** `LstDtchMn/ScanHound`  
**Code baseline inspected:** `main` / `7cc5275`  
**Closed flake fix inspected:** `6d067e2`  
**Scope:** The original cancellation-test flake is treated as closed. This review addresses the thread leak and the proposed shutdown/egress design only.

## Evidence boundary

I inspected the committed code behind the described mechanism, including:

- `backend/api/routes/scanner.py`
- `backend/api/main.py`
- `backend/api/dependencies.py`
- `backend/scanner_service.py`
- `backend/background_scanner.py`
- `backend/download_queue.py`
- `backend/detail_scraper.py`
- `backend/hdencode_transport.py`
- `tests/test_api_routes.py`
- `tests/test_feature_pack_integration.py`
- `tests/conftest.py`
- commit `6d067e2`

I did not execute the uncommitted `probe.py` or `netwatch.py`, so the numerical observations are treated as measured evidence supplied with the review request. The code-level ownership and shutdown mechanisms were independently inspected.

---

# Executive verdict

The diagnosis is substantially correct, but the design choice is not actually **join versus cooperative cancellation**.

The correct shape is:

> **Cooperative cancellation first, followed by a bounded join that verifies ownership has ended.**

A join without cancellation can stall on sleeping or blocked workers. Cancellation without a join leaves the exact current defect: shutdown returns while application-owned threads remain alive.

The known egress mechanism is sufficient to explain the historical flake. Two construction events per session do **not** imply that more events must exist. However, “explains a ~20% rate” is still only a compatibility claim, not a measured probability model.

The five leaked `_run_scan` threads versus two network events are reconcilable from the code:

1. each test resets `_scan_state` to `idle` without joining the prior scan;
2. each `client` fixture creates a new `ScannerService` with a new per-instance scan lock;
3. each orphaned `_run_scan` receives the same mutable module-level `registry`, not an immutable scanner snapshot;
4. depending on scheduling, it can observe the old scanner, `None`, or a later test’s scanner;
5. many threads can therefore be alive at fixture teardown while only a subset reach HTTP-client construction.

The proposed sequence should be changed. The egress guard should come first, but `_run_scan` ownership should be fixed before, or as part of the same ownership primitive as, the remaining lifespan workers. Fixing lifespan-owned loops first may remove some noise, but it does not address the proven request-owned scan leak or its nested `asyncio_0` executor.

---

# A. Is two construction events per session consistent with a 2-in-10 flake?

## Verdict

**Yes. It is statistically and mechanically consistent. It is not yet a measured explanation of the rate.**

The historical result was 2 failures in 10 runs, an observed rate of 20%. With only ten runs, the exact 95% binomial confidence interval is approximately **2.5% to 55.6%**. The rate estimate is therefore very weak.

The current confirmation set did not reproduce the flake in four runs. That does not conflict with a true 20% probability:

```text
P(no failure in 4 runs | p = 0.20) = 0.8^4 = 0.4096
```

A four-run non-hit is about a 41% event under that model.

More importantly, the two events are not uniformly distributed across 296 tests:

- the first repeatedly lands in `test_scan_start`;
- the second drifts among a smaller region of later tests;
- the original patched assertion is active for only one test;
- thread scheduling, network setup, fixture teardown, and startup work synchronize the event timing to test execution rather than distributing it uniformly.

If only the wandering second event can collide with the cancellation test, a 20% flake merely requires that this event overlap that test’s patch window in roughly one run out of five. It does not require additional construction events.

If both events were eligible and independent, a 20% session-level failure rate would correspond to an approximately 10.6% collision chance per event:

```text
1 - (1 - q)^2 = 0.20
q ≈ 0.106
```

The evidence suggests the first event is not eligible because it consistently occurs earlier, leaving one wandering event as the likely mechanism.

## What can and cannot be claimed

Supported:

> The observed foreign construction events are sufficient to cause the flake, and the observed count is compatible with 2 failures in 10 runs.

Not supported:

> The two events have been shown quantitatively to produce a stable 20% failure probability.

## A better measurement that requires no real external traffic

No record-and-allow run is needed to test the flake-rate model. The assertion failed on `create_scraper()` construction, not on successful HTTP completion.

Run the subset repeatedly with egress blocked and record:

- monotonic timestamp of each `create_scraper()` call;
- thread name and stack;
- current pytest node;
- start/end timestamps of the cancellation test’s monkeypatch window;
- scan operation ID and lifespan generation, once added.

Then calculate empirical overlap directly. This answers whether the observed events explain the flake without sending any request to `hdencode.org`.

---

# B. Why do five `_run_scan` threads leak while only two perform network work?

## Verdict

The apparent contradiction is explained by two independent test-isolation breaks and one mutable-ownership bug.

## 1. The tests forcibly reset the gate without stopping its owner

`tests/test_api_routes.py` has an autouse fixture that resets `_scan_state["state"]` to `idle` before every test. It does not join or cancel `_scan_thread`.

Therefore:

1. test A starts a scan and receives HTTP 200;
2. its scan thread remains alive;
3. test B’s fixture changes the shared state back to `idle`;
4. test B starts another scan and also receives HTTP 200.

The route-level 409 guard is not being disproved. The test fixture is manually erasing the evidence that the prior scan is running.

## 2. The scan lock is per `ScannerService`, but every test creates a new service

The `client` fixture creates a new application lifespan per test. Startup constructs a new `ScannerService`, whose `_scan_slot` is an instance-level lock.

A leaked scan using the previous fixture’s scanner does not hold the next fixture’s scanner lock. The next request can therefore pass both:

- `_scan_state == idle`, because the fixture reset it;
- `new_scanner.scan_in_progress == False`, because it is a different object.

That reconciles all five HTTP 200 responses.

## 3. `_run_scan` receives a mutable global registry, not its owned scanner

`scan_start()` starts:

```python
threading.Thread(target=_run_scan, args=(reg, req), daemon=True)
```

`reg` is the reused module-level `ServiceRegistry`.

Inside the new thread, `_run_scan()` later executes:

```python
scanner = reg.scanner
```

This lookup is not performed at request acceptance. It occurs whenever the new OS thread gets scheduled.

Between those moments, lifespan teardown/startup can:

- clear `reg._scanner_service`;
- set it to `None`;
- construct and publish the next fixture’s scanner.

An orphan can therefore observe:

1. the scanner from the test that created it;
2. no scanner, causing immediate return;
3. the scanner belonging to a later test.

This also explains why foreign work appears inside unrelated tests.

## 4. “Leaked at teardown” does not mean “continues scraping indefinitely”

The thread-leak probe records threads that are alive at teardown. Some can terminate moments later because:

- `reg.scanner` becomes `None`;
- a DB or service was closed;
- an exception occurs before client construction;
- a scan-slot acquisition fails;
- source construction yields no work;
- teardown or another scan changes shared stop state.

Broad exception handling then converts several of those paths into silent or logged failure rather than a test failure.

Five threads alive at teardown and only two later reaching transport construction are therefore compatible.

## 5. There is an additional executor-ownership layer

`ScannerService.run_scan()` creates a fresh asyncio event loop. Listing fetches use:

```python
await loop.run_in_executor(None, _fetch_page)
```

Those default-executor workers are named `asyncio_0`, which matches the probe.

The outer `_run_scan` thread is not the thread making the socket call. Joining only `_run_scan` does not fully define ownership unless the scan’s event loop also shuts down its default executor and all submitted work.

The current code closes the loop but does not explicitly await `loop.shutdown_default_executor()`. A blocking executor call can therefore outlive the logical scan loop.

## Required instrumentation to close the remaining uncertainty

Before relying on thread names, assign every foreground scan:

- `scan_uuid`;
- `lifespan_generation`;
- captured `id(scanner)`;
- creating test/request identifier in test instrumentation.

Record milestones:

```text
accepted
thread_started
scanner_snapshotted
slot_acquired
run_scan_entered
listing_executor_submitted
transport_constructed
slot_released
thread_finished
```

Give the executor a scan-specific `thread_name_prefix`, or wrap submitted functions with the scan ID. Multiple event loops all calling their first worker `asyncio_0` cannot establish which foreground or background scan owns the call.

---

# C. Join threads or make them cooperatively cancellable?

## Verdict

**Use both. Cooperative stop is the mechanism; bounded join is the proof.**

Candidate (i) alone is insufficient.  
Candidate (ii) alone is also insufficient.

## Required worker contract

Every application-created worker should have:

1. an explicit owner;
2. a retained handle;
3. a stop event;
4. a captured lifespan generation;
5. captured service references, rather than late reads from the mutable registry;
6. finite I/O timeouts;
7. ownership checks before external I/O and before publishing state;
8. a bounded shutdown join.

Long sleeps must become interruptible:

```python
if stop_event.wait(timeout=30):
    return
```

not:

```python
time.sleep(30)
```

This specifically fixes `poster-backfill` without making shutdown wait up to 30 seconds.

## Shutdown sequence

Use one overall deadline, not a full timeout multiplied by every thread:

1. stop accepting new scans/background work;
2. set registry and per-worker stop events;
3. set `scanner.stop_scan_flag`;
4. wake sleeping workers;
5. cancel queued executor futures where possible;
6. join/await every owned worker against the remaining common deadline;
7. log every survivor with thread name, owner generation, operation ID, and last milestone;
8. clear registry references only after cancellation has been issued and stale workers are unable to publish to the next generation.

### Suggested deadline

- **Production:** 8 seconds total.
- **Tests:** 2–3 seconds total, configurable.

Eight seconds stays below a common ten-second container stop grace period while allowing loops that already poll at 0.5–2 seconds to exit normally.

This is not permission for operations to take eight seconds routinely. Once sleeps are interruptible, ordinary shutdown should complete in well under one second. The remainder is a bounded allowance for in-flight I/O and service cleanup.

## Which threads should be awaited?

Await every application-owned thread:

- maintenance;
- notification loop;
- download queue;
- download queue watchdog;
- JDownloader results poller;
- background scanner;
- poster backfill;
- foreground `_run_scan`;
- any service-owned executor.

Do not manually join the AnyIO/TestClient portal thread. It is framework-owned and should disappear when the lifespan and its application-owned work terminate. If it survives, treat that as evidence that application work still holds the portal open.

Do not find executor threads by enumerating all threads and joining names. Shut down the executor through the object that owns it.

## What may be abandoned?

No application thread should be deliberately abandoned in the normal path.

After the hard deadline, a daemon thread may be left for process termination only if:

- it cannot publish into the current/new lifespan;
- it cannot write through captured closed services;
- its network operation has a finite timeout;
- the survivor is logged as a shutdown defect.

Python cannot safely kill an arbitrary thread. The safety design therefore cannot rely on force termination.

## `_run_scan` must become lifespan-bounded

The current single module-global `_scan_thread` is not enough:

- it is overwritten by later starts;
- earlier leaked thread handles become unreachable;
- it is outside `_teardown_services`;
- it is not generation-scoped;
- it owns an event loop and nested executor work.

Use an active-task collection keyed by scan UUID/generation, or a registry-owned foreground scan manager. Shutdown must signal and join every active scan, not only the last value assigned to `_scan_thread`.

## Assessment of the stated falsifiable prediction

The prediction should be narrowed.

Current wording:

> Fixing only the eight lifespan threads will not remove these two events.

The defensible prediction is:

> Fixing only lifespan-owned workers will not remove any transport construction proven to originate from foreground `_run_scan` work.

At least one event is likely to persist because the first repeatedly appears in `test_scan_start`. The second event may come from either another leaked foreground scan or a lifespan-owned background scanner using the same scanner pipeline. Since the socket thread is merely named `asyncio_0`, the evidence does not yet prove both events have the same owner.

Therefore, “both events persist” is stronger than the current attribution supports.

---

# D. Sequencing and the no-egress guard

## Verdict

The guard should be built first, but the production-fix order should prioritize the proven foreground ownership defect.

## Recommended sequence

### Step 1 — preserve the measured baseline

Already done:

- construction count;
- blocked egress count;
- thread identity;
- landing drift;
- repeated confirmation runs.

Commit the diagnostic probes before modifying production code.

### Step 2 — install a fail-reliable no-egress guard

Yes, this is worth implementing before any production change.

The guard must do two separate things:

1. block non-allowlisted network access;
2. record the attempt in a thread-safe ledger that pytest checks outside the worker.

Raising `OSError` may still be used to stop the request. It simply cannot be the only failure signal because application code catches it.

A robust structure is:

- patch `socket.getaddrinfo`, `socket.create_connection`, `socket.socket.connect`, and `connect_ex` as needed;
- allow loopback, Unix sockets, and explicit test-local endpoints;
- record current test, thread, destination, monotonic time, and stack;
- raise to block the call;
- fail the current test in an autouse fixture finalizer when possible;
- also fail the session at terminal summary for attempts occurring between tests or after teardown.

The reported test should be described as the test **during which the egress was observed**, not necessarily the test that originated the leaked worker.

### Should Step 2 land alone?

**Yes as an independent commit on the working branch. No as a permanently red commit on `main`.**

A good reviewable history is:

1. guard commit, demonstrated to fail against the baseline leak;
2. lifecycle/ownership fix commit, making the same guard pass;
3. mutation or injected-egress test proving the guard still fails when real egress is reintroduced.

The guard and fix can merge through one PR while retaining separate commits.

Do not add `hdencode.org` to an allowlist merely to keep CI green. That would encode the defect.

### Step 3 — fix `_run_scan` and executor ownership

This should move ahead of the generic lifespan cleanup because:

- it is the directly demonstrated route leak;
- it can perform real external I/O;
- it corrupts global `_scan_state` across fixtures;
- the single `_scan_thread` handle loses earlier owners;
- the nested `asyncio_0` executor is part of the leak.

The other session’s lifespan-join work can continue, but it should not be considered closure of this issue.

### Step 4 — migrate all remaining lifespan workers to the same contract

Replace ad hoc daemon creation with one ownership primitive or thread group:

- register handle;
- stop event;
- generation;
- bounded join;
- survivor reporting.

### Step 5 — prove closure

Run:

- the thread-leak plugin;
- the no-egress guard;
- the original 296-test subset repeatedly;
- the full suite;
- a mutation that removes one stop/join and proves the leak test fails;
- a mutation that injects external socket access and proves the egress guard fails.

---

# Additional findings

## The current test fixture masks a production invariant

Resetting `_scan_state` to idle while a scan owner remains alive is not neutral cleanup. It manufactures a state the production route is designed not to permit.

The fixture should first stop and join active scan tasks, then reset result/state fields. Test cleanup should exercise the ownership contract rather than overwrite it.

## Shared registry generation is useful but inconsistently applied

`ServiceRegistry` already has:

- `begin_lifespan()`;
- `owns_lifespan()`;
- `request_shutdown()`.

`BackgroundScanner` uses generation checks. Foreground `_run_scan`, `poster-backfill`, and some other workers do not consistently capture and enforce that ownership. The correct fix can extend the existing model rather than inventing an unrelated cancellation system.

## Production relevance is real but bounded

In a full process/container exit, daemon threads are eventually killed. The leak is still production-relevant because graceful shutdown currently:

- closes databases/services while workers can still use them;
- cannot prove in-flight work has stopped;
- can lose or misattribute state;
- behaves incorrectly under repeated app lifespans, hot reloads, tests, or in-process restart;
- may abandon external requests during shutdown.

This is not evidence of continuous scraping after process exit, and the withdrawn claim should remain withdrawn.

---

# Final answers

## A

Two construction events per session are compatible with a 2-in-10 flake. No additional hidden event is mathematically required. The mechanism is sufficient; the 20% rate is not measured. Use timestamp-overlap instrumentation under blocked egress to quantify it.

## B

The five/ two gap is reconciled by fixture state resets, per-lifespan scanner instances, and late dereferencing of a mutable global registry. Some leaked threads see the old scanner, some `None`, and some a later scanner. Add scan UUID/generation/executor attribution to prove the exact path of each event.

## C

Use cooperative cancellation **and** bounded joins. Await all application-owned workers, including `_run_scan`; shut executors down through their owner; do not manually join the AnyIO portal. Use a common 8-second production shutdown deadline and a shorter test deadline. A pure join patch is incomplete.

## D

The guard-first sequence is correct, but fix `_run_scan` ownership before or alongside the lifespan worker cleanup. Land the guard as a separate branch commit demonstrated red-before/green-after, not as a permanently failing mainline commit.

---

# Disposition

**Design approval:** conditional.

The work should proceed with these requirements:

1. no-egress guard records out-of-band and fails pytest even when the app swallows the socket exception;
2. foreground scans become explicitly lifespan-bounded and generation-scoped;
3. the nested event-loop executor is owned and shut down;
4. all background workers become cooperatively stoppable and bounded-joinable;
5. one overall shutdown deadline is used;
6. tests stop resetting live scan state without first ending its owner;
7. completion requires both zero leaked application threads and zero unexpected network attempts.
