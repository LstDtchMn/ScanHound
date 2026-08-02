# ScanHound Peer Review — Executor Cleanup Round 2

**Review date:** 2026-08-02  
**Repository:** `LstDtchMn/ScanHound`  
**Branch:** `claude/nostalgic-brattain-946f4f`  
**Branch head inspected:** `c8a60bfb17cc054550228e0e620597d8070fcba7`  
**Code commit reviewed:** `2b3c88086dd3e6cf3105c2fc05031bd3409827a7`  
**Review range:** `5c2704e..2b3c880`  
**Review package:** `docs/reviews/peer-rounds/2026-08-02-executor-cleanup-round2-for-chatgpt.md`

## Evidence boundary

The requested branch and head were checked out directly. The range contains only:

- `backend/notification_bridge.py`
- `backend/scanner_service.py`
- `tests/test_notification_bridge_lifecycle.py`

The branch-head commit after `2b3c880` is the review package, so the implementation verdict applies to the stated code commit.

I independently ran the five new notification lifecycle tests on Python 3.12.9; all five passed. I also reproduced the `/openapi.json` failure with no built frontend and ran a child-process probe for the permanently wedged executor case described below.

---

# Verdict

## CHANGES REQUIRED BEFORE MERGE

The correction behind Q11 is sound, and the narrow Q8 race between `is_running()` and submitting the executor-shutdown coroutine is eliminated. Q7 can close.

Q12 cannot be accepted as the chosen failure contract, however. A permanently wedged default-executor operation can still prevent interpreter exit after `NotificationBridge.shutdown()` returns. The new test proves only that the bridge method returns after its two-second join; it deliberately releases the worker afterward and therefore does not test the process-termination consequence.

There is also one incomplete part of Q8: the owner thread closes the loop without cancelling or awaiting ordinary notification tasks. The new code therefore does not yet provide the complete loop lifecycle claimed in its comment.

The three previously identified application-wide lifecycle P0s remain outside this range and are not re-reviewed here.

---

# Priority findings

## P1 — Q12: the wedged-executor contract is not process-bounded

`NotificationBridge.shutdown()` returns after its bounded join, but the owner thread may remain in:

```python
loop.run_until_complete(loop.shutdown_default_executor())
```

while a toast or email call remains permanently blocked in the default executor.

The package describes the cost as "one extra daemon thread" in an already degraded case. That description misses CPython's executor-exit behavior. Default-executor workers created from the daemon notification loop do inherit daemon status, but `concurrent.futures.thread` registers every worker and its `_python_exit()` hook joins those workers during interpreter shutdown regardless of their daemon flag.

I reproduced the actual bridge path in a child process:

```text
shutdown-returned:2.02
[('MainThread', False), ('notif-loop', True),
 ('asyncio_0', True), ('Thread-1 (_do_shutdown)', True)]
process-still-alive-after-5s
```

The worker was deliberately never released. The main thread had finished, all remaining enumerated threads were daemon threads, and the process still did not terminate because the executor exit hook was joining the wedged worker.

This is not solely an argument for replacing the explicit drain with `close()`-only. `BaseEventLoop.close()` calls `executor.shutdown(wait=False)`, but that does not stop a running callable or remove its worker from the interpreter's executor-exit join. Close-only lets the daemon loop thread exit sooner; it does not make a permanently wedged executor operation process-bounded.

So the Q12 trade is framed incorrectly:

- the explicit drain does buy deterministic worker retirement on the successful path;
- the two-second join bounds the `shutdown()` method;
- neither explicit drain nor close-only bounds interpreter termination when the underlying executor callable never returns.

The test at `tests/test_notification_bridge_lifecycle.py:80` should not pin stranding as an accepted terminal state. Its `finally` block releases the worker and hides the exact failure that matters. Add a subprocess-level test that leaves the backend permanently blocked and requires process exit within the application deadline. To satisfy that test, the blocking operation itself needs a real bound or a killable isolation boundary; a Python thread cannot safely cancel an arbitrary native or socket call after it has begun.

The current notification implementations make this material rather than theoretical:

- desktop notification dispatch runs a native notifier in the default executor;
- SMTP dispatch also runs in the default executor and does not configure an SMTP timeout.

## P1 — Q8 is only narrowly closed: ordinary loop tasks are abandoned

The owner-thread change removes the specific shutdown race from the prior review: cleanup is no longer submitted from another thread after an `is_running()` check, and `loop.close()` is now explicit.

But the `finally` block performs only:

```python
loop.run_until_complete(loop.shutdown_asyncgens())
loop.run_until_complete(loop.shutdown_default_executor())
loop.close()
```

That does not fully mirror `asyncio.run()`. The runner cancels and gathers remaining tasks before shutting down asynchronous generators and the default executor.

`NotificationBridge.send()` creates fire-and-forget `NotificationManager.notify()` tasks and discards the returned concurrent future. Those tasks can be awaiting an aiohttp webhook, a batch delay, or another channel when `loop.stop()` runs. They are neither tracked nor cancelled here. Once executor shutdown completes, `loop.close()` can destroy them while pending, lose notifications, and emit pending-task/resource warnings.

Before calling this a complete owner-loop lifecycle, shutdown needs an admission boundary for new sends plus a defined policy for accepted notification tasks: drain them within the shared application deadline or cancel and gather them on the owner loop. Add a deterministic test with a notification task blocked on a non-executor awaitable and assert that teardown leaves no pending task.

This does not reopen the old `is_running()` → `run_coroutine_threadsafe()` cleanup race. That specific race is fixed. It means Q8's broader lifecycle requirement is not fully closed yet.

---

# Answers to Q11–Q13

## Q11 — Yes, for `ScannerService.run_scan()`

The corrected reasoning holds for the actual scan path.

After `asyncio.new_event_loop()` succeeds, `run_until_complete()` is enclosed by an inner `try/finally`, and `loop.close()` runs for normal return and for exceptions, including exceptions later caught by the outer scan handler. There is no alternate return between loop creation and that `finally`. If loop creation itself fails, no created loop or default executor exists to retire.

`BaseEventLoop.close()` detaches the default executor and calls `shutdown(wait=False)`. That is sufficient to retire idle `asyncio_N` workers and explains why the explicit drain was redundant for the reported per-scan leak. The round-1 scan-thread join was load-bearing because it allows `run_scan()` to reach this `finally` during lifespan teardown.

One precision should remain in the wording: close does not terminate a currently running executor callable. Such a worker retires only after its callable returns. That is a cancellation/deadline and stale-publication concern for the later lifecycle rounds, not evidence of a path that skips this loop close.

**Disposition: accept Q11; Q7 is closed for this range.**

## Q12 — No, not as currently justified or tested

Deterministic draining on the healthy path is defensible, but the claimed pathological cost is incomplete and the pinned test encodes a false process-exit safety story. A wedged executor callable remains capable of blocking interpreter shutdown even though every surviving thread reports `daemon=True` and the bridge method returned on time.

Do not select between explicit-drain and close-only on the basis of the current test. First make the underlying operation or isolation boundary honor the shutdown contract, then retain explicit draining if deterministic common-path teardown is still useful.

**Disposition: reject Q12; change required.**

## Q13 — Yes, the security-test correction is outside this range

The failing assertion and API behavior are unchanged by `5c2704e..2b3c880`, and the endpoint still returns the security-correct `404`. Fixing the test to assert that the OpenAPI schema is not served belongs in its own change or the already identified branch.

One correction to the package: this is not specifically a Python 3.11 CI problem. The backend `test` matrix runs the full suite independently on both Python 3.11 and 3.12, and neither backend job builds the frontend first. With no `SCANHOUND_FRONTEND_DIR` pointing to a built SPA, the same FastAPI JSON `404` should fail the assertion on both matrix legs. I reproduced it on Python 3.12.9:

```text
HTTP/1.1 404 Not Found
content-type: application/json
1 failed, 18 passed
```

That strengthens the case for a separate test fix but does not pull it into this executor-cleanup range.

**Disposition: accept Q13 as out of scope; correct the CI-impact description to both Python versions.**

---

# Test assessment

The new tests establish useful normal-path facts:

- the owner loop closes;
- an ordinary default-executor worker retires;
- repeated clean shutdown is quick;
- no-loop shutdown is harmless;
- the public shutdown method returns after a bounded join when executor draining is blocked.

They do not establish:

- that the process can exit with a permanently wedged executor callable;
- that non-executor notification tasks are drained or cancelled;
- that new sends are rejected once shutdown begins.

The first two are required for the lifecycle claims made by this range. Send-admission closure can be implemented consistently with the next closed-admission round, but the notification task test should accompany it.

---

# Final disposition

| Question / item | Verdict |
|---|---|
| Q11 — scan-loop close makes the drain redundant | **Accept. No scan path bypasses the inner `finally` after loop creation.** |
| Q12 — pin deterministic drain plus stranded loop thread | **Reject. Method return is bounded; process termination is not.** |
| Q13 — unrelated security test | **Out of scope, but both Python CI legs are affected.** |
| Q7 | **Closed for this range.** |
| Q8 narrow external-submission race | **Closed.** |
| Q8 complete owner-loop teardown | **Still open: ordinary pending tasks are abandoned.** |

## Merge gate for this round

Before this executor-cleanup round is mergeable:

1. replace the wedged-drain test's accepted-stranding contract with a process-level termination contract and make the blocking notification path honor it;
2. close notification task admission during shutdown;
3. drain or cancel-and-gather accepted notification tasks on the owner loop before closing it;
4. add deterministic tests for both a permanently wedged executor callable and a pending non-executor notification task.

The application-wide deadline, generation fencing, `begin_lifespan()`, and global spawn-admission work remain the explicitly separate next rounds.
