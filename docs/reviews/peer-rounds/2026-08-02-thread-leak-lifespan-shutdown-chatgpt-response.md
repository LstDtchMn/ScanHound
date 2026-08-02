# ScanHound Peer Review — Lifespan Shutdown Thread Ownership

**Review date:** 2026-08-02  
**Repository:** `LstDtchMn/ScanHound`  
**Branch:** `claude/nostalgic-brattain-946f4f`  
**Branch head:** `4e606ab5ffd2d4e6327d3acf8bb8301ade1a15bf`  
**Code commit reviewed:** `b25b330f886575a3ffa0bc8f0cb496cea6e36218`  
**Base:** `7cc5275b4a518cb6986f34e26e1c0e9c98175b7c`  
**Review range:** `7cc5275..b25b330`  
**Review package:** `docs/reviews/peer-rounds/2026-08-02-thread-leak-lifespan-shutdown-for-chatgpt.md`

## Evidence boundary

The private repository, committed code, tests, review package, and branch metadata were inspected directly.

The branch head differs from the code commit only by deleting the superseded root handoff and adding the peer-round package. The implementation verdict therefore applies to `b25b330`.

The reported Linux-container suite result is accepted as author-reported local evidence:

```text
4206 passed, 4 skipped, 13 warnings
THREADLEAK: none
PYTEST_EXIT=0
```

There is no status/check result attached to the agent-branch commit, and the repository workflow does not run on pushes to this branch. I did not independently execute the suite.

---

# Verdict

## CHANGES REQUIRED BEFORE MERGE

The implementation fixes the ordinary TestClient leak path and the successful full-suite result is meaningful. The following design defects remain, however:

1. shutdown has no single application-wide deadline and can substantially exceed Docker's stop grace;
2. a timed-out worker can still publish through shared globals or the next lifespan;
3. `begin_lifespan()` discards prior thread handles and clears the shared stop event;
4. spawning is not closed when shutdown begins;
5. the lock held across `Thread.start()` can defeat the claimed join budget;
6. the executor cleanup silently degrades on supported Python 3.11;
7. metadata shutdown claims resumable pause semantics even for the non-durable legacy job;
8. several key failure paths are not exercised by the new tests.

These are not objections to bounded joins as a strategy. They are gaps in making the bound and ownership contract true under failure.

---

# Priority findings

## P0 — Q5: one application-wide shutdown deadline is required

The current five-second registry budget is only one component of a serial shutdown sequence.

Current possible waits include:

| Owner | Current bounded waits |
|---|---:|
| Download queue worker + watchdog | 5s + 5s |
| Background scanner | 2s |
| Notification executor + loop thread | 3s + 2s |
| Registry-owned loose workers | 5s |
| AppService scheduler + maintenance | 3s + 3s |
| **Total before other cleanup** | **up to 28s** |

That excludes:

- watchlist close;
- notification-manager shutdown;
- AppService shutdown hooks;
- database close;
- runtime-lock release;
- FastAPI/Uvicorn shutdown overhead.

The review package's estimate of approximately 23 seconds omits the notification bridge's possible five seconds.

`docker-compose.yml` does not set `stop_grace_period`. Docker's documented Linux default is ten seconds before force termination.

### Required design

Create one absolute monotonic deadline at the beginning of teardown:

```python
deadline = time.monotonic() + APP_SHUTDOWN_BUDGET_SECONDS
```

Every stop/join function receives either that deadline or:

```python
remaining = max(0.0, deadline - time.monotonic())
```

No service may start its own fresh full timeout.

Recommended structure:

```text
Phase 1: atomically close admission to new work
Phase 2: signal every worker/service
Phase 3: join every producer using the remaining shared deadline
Phase 4: close dependent services and DB using the remaining time
Phase 5: report every survivor and incomplete close
```

A practical worker-wait budget is approximately six to seven seconds, leaving time inside the ten-second container grace for framework shutdown and durable cleanup.

It is also reasonable to set an explicit Compose `stop_grace_period` longer than ten seconds, such as 15–20 seconds, but that is additional margin—not a substitute for one bounded application deadline.

### Ordering defect tied to Q5

`NotificationBridge.shutdown()` currently runs before registry-owned scan/rename workers are joined. A scan finishing during that interval can still call notification and WebSocket publication paths after the notification loop has been stopped.

Signal all workers first, then join producer work, then close the services they publish through.

Likewise, AppService's scheduler and maintenance threads are not signaled until `backend.shutdown()`, which happens after the registry-owned join. The scheduler therefore remains capable of triggering work during teardown.

AppService needs separate:

```text
request_stop()
join_until(deadline)
close_resources()
```

semantics rather than one late `shutdown()` that both signals and waits.

---

## P0 — bounded abandonment does not yet prevent stale publication

A bounded join is safe only when a survivor cannot mutate the completed or next lifespan.

That property is not currently enforced consistently.

For example, `_run_scan()` can, after an uninterruptible call returns:

- replace `_last_scan_items`;
- broadcast results and completion;
- update module-global `_scan_state`;
- update `reg.config`;
- call `reg.backend.save_config()`;
- send notifications;
- invoke auto-grab.

It does not capture a lifespan generation and does not check ownership before those publications.

Other examples include:

- rename route workers broadcasting after their service calls;
- `scan_conflict_dv` publishing completion;
- re-identification publishing results;
- auto-rename package processing;
- RSS action failure paths reading `reg.db` after a service call returns.

A clean full-suite run proves those workers usually finish before teardown. It does not prove safety when a worker exceeds the deadline and resumes later.

### Required invariant

Every lifespan-owned operation must capture a generation-specific owner token and obey:

```text
after every uninterruptible operation:
    if ownership is stale:
        stop without publishing

before every DB/global/config/WebSocket/notification mutation:
    verify ownership again
```

The shared registry shutdown event is not sufficient because the next lifespan clears it.

Use a per-generation cancellation token or operation context that remains permanently cancelled after its generation ends.

### Required deterministic test

Block a foreground scan past the shutdown deadline, complete teardown, start a new lifespan, then release the old scan.

Assert that the old scan performs none of these:

```text
DB writes
config writes
last-result publication
scan-state publication
WebSocket publication
notifications
auto-grab
```

This is the direct failure-path test for the defect the branch is intended to eliminate.

---

## P0 — Q6: `begin_lifespan()` can orphan and reactivate workers

The current method does this:

```python
with self._lifespan_threads_lock:
    self._lifespan_threads = []
self._shutdown_event.clear()
```

This is unsafe.

### Failure path

1. startup begins;
2. one or more threads are spawned;
3. a later startup step raises;
4. the lifespan never reaches the post-`yield` teardown;
5. the next startup calls `begin_lifespan()`;
6. prior handles are discarded;
7. the shared shutdown event is cleared;
8. old workers that poll that event can continue.

The comment that prior-generation workers are "no longer this lifespan's problem" is the opposite of the safety requirement. They remain process-owned work capable of touching shared resources.

### Required corrections

- Wrap lifespan initialization and serving in `try/finally` so partial startup receives teardown.
- Do not blindly discard live thread handles.
- Use generation-specific cancellation events rather than clearing one shared event.
- Refuse or quarantine a new lifespan when prior live work has not been made harmless.
- Add a startup-failure test that spawns a worker and then raises during `_init_services()`.

The prior-generation thread may be excluded from the new lifespan's normal work, but it remains the process's cleanup responsibility.

---

## P1 — Q3: both joins can spend a full budget

The double-call is not fully idempotent under concurrency.

The first `join_lifespan_threads()`:

1. snapshots and clears the list;
2. releases the lock;
3. waits.

During or after that wait, another worker can call `spawn_lifespan_thread()` because shutdown does not close admission.

The `finally` call can then receive this newly tracked thread and spend another full five seconds. A spawn after the second snapshot can escape both joins entirely.

AppService's still-live scheduler is one possible producer of late work.

### Required correction

Under one lifecycle lock:

1. mark the registry `STOPPING`;
2. reject any new `spawn_lifespan_thread()` call;
3. capture the shared absolute deadline;
4. signal;
5. join using only remaining time.

The second join may remain as a safety net, but it must use the same deadline. It should normally have no work because new spawning has been closed.

---

## P1 — Q4: holding the lock across `Thread.start()` is not airtight

The race being addressed is real: an unstarted `Thread` cannot safely be treated like a completed one.

Holding the tracking lock across `start()` prevents a joiner from seeing a half-started object. For a standard Python `Thread`, the child target waiting on that same lock does not by itself deadlock the parent because `start()` does not wait for target completion.

The implementation still has three significant problems.

### 1. The join timeout does not cover lock acquisition

`join_lifespan_threads(timeout=0.1)` first performs an unbounded:

```python
with self._lifespan_threads_lock:
```

Only after acquiring the lock does it establish its deadline.

If another thread stalls inside `Thread.start()` while holding the lock, the join can block indefinitely before its timeout begins.

The deterministic test currently proves this behavior by asserting that the joiner remains blocked. That establishes atomic visibility, but it also demonstrates that the documented total wall-clock budget is false.

### 2. Failed `start()` leaves an unstarted object tracked

If `thread.start()` raises:

- the thread remains in `_lifespan_threads`;
- a later join can raise `RuntimeError: cannot join thread before it is started`;
- the list has already been drained;
- remaining handles can be lost from subsequent cleanup.

`spawn_lifespan_thread()` needs rollback in an exception path.

### 3. New work is accepted after shutdown

The method does not check lifecycle state or generation under the same lock.

### Preferred design

Track lifecycle records, not bare thread liveness:

```text
STARTING
STARTED
FAILED
FINISHED
```

Suggested flow:

1. under lock, reject shutdown and register a `STARTING` record;
2. release the lock;
3. call `thread.start()`;
4. under lock, mark `STARTED` or remove/mark `FAILED`;
5. notify a condition;
6. join waits for `STARTING` records only until the shared deadline;
7. never call `join()` on an unstarted object.

A simpler implementation is acceptable if it provides all of:

- failed-start rollback;
- shutdown admission closure;
- lock-acquisition time included in the global deadline;
- no joining of unstarted objects.

The current Q4 test should be changed to assert both atomic safety **and** deadline compliance.

---

## P1 — Q7: unconditional per-scan cleanup is correct, but the implementation is not portable

Calling executor cleanup for every scan is the right lifecycle.

Each call to `run_scan()` creates a fresh event loop. Cleanup only during application shutdown would still leak executor threads after normally completed scans.

On a clean scan, executor shutdown should normally be fast because the awaited scan has finished its submitted listing work.

### Python 3.11 incompatibility

The repository workflow declares Python 3.11 and 3.12 support.

The timeout parameter to:

```python
loop.shutdown_default_executor(timeout=5)
```

was added in Python 3.12. On Python 3.11 the call raises `TypeError`; the broad exception handler logs at debug and closes the loop, silently restoring the executor-leak behavior.

The reported full-suite run used the Python 3.12 production container and therefore does not cover this.

### Required correction

Use a compatibility helper.

For example:

```text
Python 3.12+: shutdown_default_executor(timeout=...)
Python 3.11:  shutdown_default_executor() on normal completion
```

For shutdown cancellation, the outer scan operation must still be governed by the application-wide deadline.

Better still, create and retain an explicit scan executor so the operation owns it directly, but the compatibility helper is sufficient for this patch if tests prove it.

Run the thread-leak suite on both Python 3.11 and 3.12 before merge.

### Timeout relationship

The nested executor timeout should not independently consume the same five seconds as the outer registry join. During app shutdown, it should receive the remaining global deadline.

---

## P1 — Q8: the NotificationBridge race is worth fixing

The current sequence is:

```text
loop.is_running()
run_coroutine_threadsafe(shutdown_default_executor(...))
future.result(timeout=3)
call_soon_threadsafe(loop.stop)
thread.join(timeout=2)
```

The loop can stop between the `is_running()` check and submission. A submitted future may never execute, consuming the three-second wait. The loop thread also does not visibly own `loop.close()`.

The same Python 3.11 timeout-parameter problem applies here.

### Preferred design

Make the loop thread own its complete lifecycle:

```python
def _run():
    loop = asyncio.new_event_loop()
    try:
        loop.run_forever()
    finally:
        # Drain/cancel work as appropriate.
        # Shut down executor using version-compatible helper.
        loop.close()
```

External shutdown should:

1. atomically request owner-loop shutdown;
2. wake the loop;
3. join the loop thread using the remaining application deadline.

Do not create a coroutine object that can be stranded if submission fails.

At minimum, use one shared deadline and make loop closure explicit.

---

## P1 — Q9: pause is correct only for durable metadata inventory

For `start_run()` / durable inventory:

- the target manifest is persisted;
- unfinished work is returned to a retryable state;
- `resume()` exists.

Pausing on app shutdown is the correct semantic.

The legacy `/plex/scan-metadata` route still calls:

```python
job.start(targets)
```

That path has no durable manifest or resumable run UUID. Its worker interprets the stop flag as cancellation, not durable pause.

The teardown comment currently claims all active metadata scans are persisted and resumable. That is false for the legacy job.

### Required correction

Use different shutdown behavior:

```text
if durable active_run_uuid exists:
    pause
else:
    cancel legacy scan
```

Alternatively, migrate or remove the legacy route.

Add one test for each mode:

- durable shutdown ends in `paused` and is resumable;
- legacy shutdown ends in `cancelled`, not falsely advertised as paused.

---

# Remaining question answers

## Q10 — save-and-restore test stubs

The save-and-restore correction is appropriate.

Those tests are replacing a registry field that production teardown uses to find the real owner. Leaving the stub installed does not test a meaningful production state; it accidentally prevents cleanup.

The `stub_backend` fixture dependency on `client` correctly causes restoration before the TestClient lifespan exits.

This does not weaken the route assertions. It restores the real ownership graph before testing lifespan teardown.

The rejected closer-list alternative is not inherently invalid, but it is unnecessary for this patch if registry fields remain the declared ownership source.

A repository-wide audit should still look for tests that replace any of:

```text
registry.backend
registry._download_queue_service
registry._background_scanner
registry._notification_bridge
registry._rename_service
registry._plex_metadata_scan_job
registry._scanner_service
```

without restoring them before lifespan exit.

## Q1 — `hookwrapper=True` versus `wrapper=True`

Keep `hookwrapper=True` for the stated compatibility range.

`wrapper=True` is the newer wrapper style introduced by Pluggy 1.1. `hookwrapper=True` remains the compatible choice when older Pluggy versions may be present through supported pytest environments.

The after-`yield` location is correct for the question:

> What new threads remain after this test's fixtures have finalized?

## Q2 — can the post-finalization snapshot under-report?

Yes.

### Case 1: thread identifier reuse

The plugin keys snapshots by `Thread.ident`. Python permits thread identifiers to be recycled after a thread exits.

A new leaked thread can therefore reuse an identifier present in `_BASELINE` and be missed.

Use thread-object identity rather than `ident` as the key:

```python
set(threading.enumerate())
```

or `id(thread)` while retaining the objects.

### Case 2: prior-test leak becomes baseline

A thread leaked by test A and still alive at test B setup is part of B's baseline. B will not report it again. This is reasonable for attribution to A, but a missed snapshot or session-start leak can remain invisible thereafter.

Add a session-end comparison to report all non-baseline application threads still alive.

### Case 3: short post-finalizer lifetime

A thread can outlive its fixture by a small interval and exit before the plugin takes its post-yield snapshot. That is technically an outliving thread but may not be observed.

A short optional settle/recheck can improve diagnostics, but it should not become a long sleep in every test.

### Case 4: delayed descendant

A baseline thread or timer can spawn a new descendant after the post-finalization snapshot. The descendant is not attributed to the completed test.

Explicit ownership tests remain more authoritative than enumeration alone.

### The plugin currently reports but does not fail

`THREADLEAK: none` is valuable evidence. If leaks occur, however, the plugin only prints them and does not make pytest return nonzero.

Add an enforcement mode, for example:

```text
--threadleak-fail
```

that changes the session exit status when leaks are recorded. Keep report-only mode available for exploratory use.

---

# Test review

The new tests are useful and the mutation table improves confidence in their targeted mechanisms.

The suite still lacks the tests needed for the blockers above.

## Required before merge

1. **One total deadline**
   - Make every service simulate a wedged worker.
   - Assert complete `_teardown_services()` returns inside one application deadline, not the sum of service timeouts.

2. **Spawn admission closes**
   - Begin teardown.
   - Attempt a spawn.
   - Assert it is rejected and no thread can appear after the final snapshot.

3. **Failed `Thread.start()` rollback**
   - Make `start()` raise.
   - Assert the record is removed.
   - Assert other tracked workers are still joined.

4. **Partial-startup failure**
   - Spawn a lifespan thread during initialization.
   - Raise before `yield`.
   - Assert it is signaled/joined and cannot publish into a subsequent lifespan.

5. **Timed-out stale scan**
   - Block `_run_scan` past the deadline.
   - Start a new lifespan.
   - Release the old scan.
   - Assert zero DB/global/config/WS/notification publication.

6. **Python 3.11 executor cleanup**
   - Run a real executor-using scan.
   - Assert no executor thread remains.
   - Run the thread-leak plugin on Python 3.11.

7. **Notification stop race**
   - Stop the loop between status check and submission deterministically.
   - Assert shutdown respects the global deadline and closes the loop.

8. **Durable versus legacy metadata semantics**
   - Durable job pauses and resumes.
   - Legacy job cancels.

9. **Actual bulk-loop cancellation**
   - The acknowledged `test_bulk_rename_loops_stop_on_shutdown` only tests the predicate.
   - Mutation-delete the shutdown check from each material loop and require a test failure.

10. **Thread-leak plugin enforcement**
    - Inject a leaked thread and assert the enforcement mode returns nonzero.

---

# Additional observations

## The normal-path improvement is real

The following changes are directionally correct:

- registry ownership for loose workers;
- interruptible poster-backfill wait;
- event-based results-poller sleep;
- foreground and scheduled scan registration;
- moving RSS-specific thread tracking into a shared owner;
- joining before DB closure;
- restoring teardown-visible test fields;
- correcting the thread-leak hook ordering.

The full Python 3.12 suite with no observed post-test threads is strong evidence that ordinary cleanup improved.

## Daemon status is not a safety property

Logging that a surviving thread is a daemon only means it will not keep the interpreter alive. It does not make that thread safe while the process remains alive, which is exactly the TestClient/repeated-lifespan environment involved here.

Warnings should describe the more important condition:

```text
worker exceeded shutdown deadline and has been fenced from publication
```

That second half must be enforced before abandonment is acceptable.

## Current `_reset_scan_state` fixture still overwrites state

The fixture now benefits from TestClient teardown joining the scan first, but the helper itself still resets module-global scan state rather than asserting the active operation has ended.

Once foreground scan ownership is formalized, change the fixture to stop/join through that ownership API before resetting result-only fields.

---

# Required implementation plan

## Step 1 — lifecycle state and absolute deadline

Add explicit states:

```text
NEW
RUNNING
STOPPING
STOPPED
```

Under one lock:

- capture generation;
- close thread admission when entering `STOPPING`;
- store one absolute shutdown deadline;
- reject new lifespan-owned work afterward.

## Step 2 — generation-specific cancellation

Each generation receives its own event/token.

Never clear a prior generation's token.

Each worker captures:

```text
generation
cancellation token
service references needed for its work
```

and checks ownership before publication.

## Step 3 — split signal, join, and resource close

Every service follows:

```text
request_stop()
join_until(deadline)
close_resources()
```

Signal every service first before waiting on any one service.

## Step 4 — make spawn atomic without unbounded lock waiting

Use a tracked state record or equivalent mechanism that:

- never exposes an unstarted thread as joinable;
- removes failed starts;
- includes lock/state waiting in the common deadline;
- rejects post-shutdown spawning.

## Step 5 — repair executor compatibility and ownership

- support Python 3.11 and 3.12;
- clean every per-scan event loop executor on normal completion;
- use remaining shutdown deadline during cancellation;
- close notification loops explicitly.

## Step 6 — distinguish durable and legacy metadata jobs

Pause only durable inventory. Cancel the legacy job.

## Step 7 — add the required failure-path tests

Then run:

```text
Python 3.11 full backend suite + threadleak enforcement
Python 3.12 full backend suite + threadleak enforcement
targeted deterministic lifecycle failure suite
existing mutation table
new mutations for stale publication and bulk-loop checks
```

---

# Final disposition by question

| Question | Verdict |
|---|---|
| Q5 — budget sizing | **Reject current design. Fold all waits into one absolute application deadline.** |
| Q4 — lock across start | **Not airtight. It can defeat the timeout and mishandles start failure. Revise.** |
| Q7 — per-scan executor cleanup | **Unconditional cleanup is correct; current timeout call breaks Python 3.11 support.** |
| Q3 — double join | **Both can spend a full budget because spawning remains open. Use one deadline and close admission.** |
| Q6 — clear thread list | **Unsafe. It can orphan workers and clear the event they rely on.** |
| Q8 — notification race | **Real and worth fixing; make the loop own cleanup and share the deadline.** |
| Q9 — metadata pause | **Correct for durable runs, incorrect description/behavior for legacy runs.** |
| Q10 — restore test stubs | **Correct. Continue auditing teardown-visible field replacements.** |
| Q1 — hook wrapper style | **Keep `hookwrapper=True` for compatibility.** |
| Q2 — post-finalization under-report | **Possible; key by thread object, add session-end/enforcement support.** |

## Merge gate

Do not merge `b25b330` as-is.

The next review should contain:

1. one application-wide deadline;
2. startup-failure cleanup;
3. closed admission after shutdown begins;
4. per-generation cancellation/publication fencing;
5. fixed spawn failure/timeout semantics;
6. Python 3.11-compatible executor cleanup;
7. durable-versus-legacy metadata shutdown tests;
8. deterministic stale-worker publication tests;
9. a full Python 3.11 and 3.12 test result with thread-leak enforcement.
