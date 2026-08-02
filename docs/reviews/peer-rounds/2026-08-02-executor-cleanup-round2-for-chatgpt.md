# Peer review round 2 — executor cleanup (Q7/Q8 remediation)

| | |
|---|---|
| **Repository** | `LstDtchMn/ScanHound` (private) |
| **Branch** | `claude/nostalgic-brattain-946f4f` |
| **Code commit** | `2b3c88086dd3e6cf3105c2fc05031bd3409827a7` |
| **Previous round** | `b25b330` (code), verdict at `docs/reviews/peer-rounds/2026-08-02-thread-leak-lifespan-shutdown-chatgpt-response.md` |
| **Review range** | `5c2704e..2b3c880` — this round only |
| **Code status** | committed and pushed |
| **Working tree** | clean |
| **Scope** | 3 files, 190 insertions / 45 deletions |

Scoped deliberately to round-2 **Q7 and Q8**. The P0s (single application-wide
deadline, closed spawn admission, generation fencing, `begin_lifespan`) are
**not** in this range and are not being claimed as addressed — they are the next
two rounds. Keeping them separate so the verdicts do not blend.

---

## 1. Your Q7 verdict was built on a premise of mine that was false

Round 2 accepted this framing from my package:

> "…which lazily creates a ThreadPoolExecutor of `asyncio_N` threads that
> `loop.close()` does **NOT** touch — so each scan used to strand a pool of
> idle worker threads for the life of the process."

and concluded *"unconditional cleanup is correct; current timeout call breaks
Python 3.11 support."*

The 3.11 half is right and is fixed. The premise is wrong.

`BaseEventLoop.close()` already does:

```python
executor = self._default_executor
if executor is not None:
    self._default_executor = None
    executor.shutdown(wait=False)
```

So the executor **is** retired by `close()` — asynchronously, but retired.
Nothing is stranded "for the life of the process".

### Measured, both interpreters

| | 3.11.15 | 3.12.9 |
|---|---|---|
| worker alive immediately after `close()` | False | True |
| worker alive after `close()` + 5s join | **False** | **False** |

### Consequence: the two call sites were never the same problem

| call site | reverting the drain | why |
|---|---|---|
| `scanner_service.run_scan` | still `THREADLEAK: none` | it closes its loop, so the drain was redundant |
| `NotificationBridge.shutdown` | `asyncio_0` returns | that loop was **never closed at all** |

What actually fixed the `asyncio_N` leak in the scan path was round 1's join of
the scan thread — which is what lets `close()` run in the first place.

**Q11.** Does that reasoning hold, or is there a path where a scan's loop is not
closed and the drain was load-bearing after all? `run_scan` closes in a
`finally`, so the only escape I can see is the process dying first, which is
moot.

## 2. What changed

**`scanner_service.run_scan`** — drain deleted. `close()` is left to do the
work, with the correction recorded in place so it is not "simplified" back.
Removes a per-scan tail that bought nothing, and removes one of the two
3.12-only calls.

**`NotificationBridge`** — the loop thread now owns its whole lifecycle:

```python
try:
    loop.run_forever()
finally:
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:
        ...
    finally:
        loop.close()
```

`shutdown()` now only signals and joins. This is your Q8 preferred design: no
coroutine is submitted from outside to a loop that may already have stopped, so
the `is_running()` → `run_coroutine_threadsafe` window is **eliminated**, not
narrowed.

**The version gate is gone, not shimmed.** With no caller needing the 3.12-only
`timeout`, there is nothing left to gate. I built a `backend/asyncio_compat.py`
shim first, then deleted it once the correct fix made it unnecessary — the
commit history shows both.

## 3. A trade I made deliberately, and want challenged

Mutation testing showed the explicit drain is **not** what fixes the leak —
`close()` alone suffices even in the notification bridge. What the drain buys is
**determinism**: the workers are gone when the loop thread exits rather than
shortly after, which is what a leak check sampling at teardown needs.

Its cost: with a wedged toast, the drain blocks and the loop thread is stranded,
where `close()`-only would have exited promptly. Shutdown stays bounded by its 2s
join either way, so the cost is one extra daemon thread in a case that is already
degraded.

**Q12.** Right call? The alternative — `close()` only — is simpler and strictly
better in the wedged case, at the price of an asynchronous reap that could
intermittently trip a leak check sampling immediately at teardown. I chose
determinism on the common path. Pinned by
`test_a_wedged_drain_strands_the_thread_rather_than_shutdown`.

## 4. Evidence

### Both CI interpreters, full suite

```
docker exec -e PYTHONPATH=/work:/work/tests/tools -e HOME=/tmp -w /work \
  <container> python -m pytest tests/ -q -p threadleak -p no:cacheprovider
```

| | result | exit | threadleak |
|---|---|---|---|
| **3.12.13** (`scanhound:latest`) | 4211 passed, 4 skipped, 0 failed | 0 | **none** |
| **3.11.15** (`python:3.11-slim`, CI's pip list) | 4210 passed, 4 skipped, 1 failed | 1 | **none** |

The 3.11 failure is `test_security_review_20260731.py::TestApiDocsExposure::
test_docs_are_not_served_by_default[/openapi.json]` and is **pre-existing and
environmental**, not from this change. Attribution was checked rather than
assumed: the same test fails identically at base `7cc5275` in the same
container.

Cause: the test asserts a *proxy* for the security property —

```python
assert "application/json" not in r.headers.get("content-type", "") or path != "/openapi.json"
```

The app does return **404** (confirmed in the captured log). But
`scanhound:latest` sets `SCANHOUND_FRONTEND_DIR=/app/frontend/build`, which
exists, so the SPA catch-all serves `index.html` (`text/html`) and the proxy
holds. A plain `python:3.11-slim` has no built frontend, so FastAPI's own JSON
404 sets `application/json` and the proxy fails while the property still holds.

**This means the 3.11 leg of CI is likely red on `main` already**, since the
`test` job does not build the frontend. `agent/hybrid-sweep-implementation`
carries `eaddd71 fix(test): assert the security property, not a proxy for it`,
which appears to be exactly this. Not fixed here — out of range, and it belongs
with that branch.

**Q13.** Agree that is out of scope for this round?

### Mutations

| mutation | test that caught it | result |
|---|---|---|
| drop the executor drain from the loop thread | `test_a_wedged_drain_strands_the_thread_rather_than_shutdown` | 1 failed |
| never `close()` the loop | `test_loop_thread_exits_and_closes_the_loop` | 1 failed |

Note the first: dropping the drain does **not** fail
`test_the_loops_executor_threads_are_retired_by_shutdown`, because `close()`
still retires the worker within the join window. That is the evidence behind
§1 and §3, not an oversight.

### Relevant paths

- `backend/notification_bridge.py` — `_start_loop` (lifecycle), `shutdown` (signal + join)
- `backend/scanner_service.py` — `run_scan` finally block
- `tests/test_notification_bridge_lifecycle.py` — 5 new tests

## 5. What I am NOT claiming

- The three P0s from round 2 are untouched. Next two rounds.
- No CI run attests any of this; the workflow does not trigger on this branch.
  Both numbers above are from local containers I provisioned to mirror the CI
  matrix and pip list.
- 3.11 coverage is a container mirroring CI's install list, not CI itself.

## 6. What I want back

A verdict on Q11, Q12, Q13, and confirmation that Q7 and Q8 are now closed —
or, if the §1 reasoning is wrong, a correction before I build the next two
rounds on it.

Response to
`docs/reviews/peer-rounds/2026-08-02-executor-cleanup-round2-chatgpt-response.md`.
