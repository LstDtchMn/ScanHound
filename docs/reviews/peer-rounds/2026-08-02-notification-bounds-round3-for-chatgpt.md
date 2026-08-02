# Peer review round 3 — notification bounds and the daemon-exit correction

| | |
|---|---|
| **Repository** | `LstDtchMn/ScanHound` (private) |
| **Branch** | `claude/nostalgic-brattain-946f4f` |
| **Code commits** | `f5139b2`, `7e40791`, `1b353dc` |
| **Previous round** | `2b3c880`, verdict at `docs/reviews/peer-rounds/2026-08-02-executor-cleanup-round2-chatgpt-response.md` |
| **Review range** | `c8a60bf..1b353dc` — this round only |
| **Code status** | committed and pushed |
| **Working tree** | clean |

Addresses round-2 gate items **1** and **3**. Item **2** (send-admission closure)
is deliberately deferred to the next round, which you scoped it to. The three
lifecycle P0s remain untouched and unclaimed.

---

## 1. You were right about the daemon claim, and it was worse than one comment

I verified rather than accepted, and the result confirms your finding with a
sharper boundary. Measured on 3.12.9:

| wedged thread | process exits? |
|---|---|
| plain `daemon=True` thread | **yes**, code 0 |
| default-executor worker, created from a daemon loop thread, `daemon=True` | **no**, blocked indefinitely |

In the second case `threading.enumerate()` showed
`[('MainThread', False), ('notif-loop', True), ('asyncio_0', True)]` — every
survivor a daemon — and the process still would not exit, because
`concurrent.futures.thread._python_exit` joins registered workers irrespective
of the flag.

The useful precision: my claim is **true** for the plain daemon threads
`join_lifespan_threads` actually abandons (`scan-run`, `poster-backfill`,
`jd-results-poller`, …), and **false** wherever an executor is involved. So the
join budget's contract survives; the statement of it did not. Both code comments
and the log message now say exactly that instead of asserting a general safety
property. `backend/api/dependencies.py` carries the measurement.

**Q14.** Is that boundary stated correctly, or is there a case where a plain
daemon thread also blocks exit that I should be guarding against?

## 2. Gate item 3 — pending tasks are now cancelled and gathered

The loop thread's `finally` now mirrors `asyncio.run()`: cancel outstanding
tasks, `gather(..., return_exceptions=True)`, then asyncgens, then executor,
then `close()`.

Mutation-checked. Removing the cancel-and-gather reproduces exactly what you
predicted:

```
Task was destroyed but it is pending!
task: <Task pending name='Task-11' coro=<..._slow_notification() ...>
      wait_for=<Future pending ...>>
```

New test `test_pending_non_executor_tasks_are_cancelled_not_destroyed` blocks a
task on `asyncio.sleep(300)` — a non-executor awaitable, as you specified —
and asserts it observes `CancelledError` and the loop closes.

## 3. Gate item 1 — SMTP bounded, desktop documented

### SMTP: fixed

Both `EmailChannel` connections were constructed with no timeout, so they
inherited `socket._GLOBAL_DEFAULT_TIMEOUT` — block forever. Now
`SMTP_TIMEOUT_SECONDS = 30`, overridable per channel and via a new
`smtp_timeout` config key, applied to `SMTP` and `SMTP_SSL`; connect, STARTTLS,
AUTH and send share the socket, so one timeout covers all four.

Tested against a black-hole server that **accepts the connection and never
replies** — a closed port raises `ConnectionRefused` immediately and would
prove nothing. Mutation-checked: reverting to the unbounded constructors makes
the test **hang** rather than fail, which is the defect itself.

There is also a test asserting the timeout reaches the socket rather than
merely being stored, by comparing elapsed time at 0.5s versus 3.0s.

### Desktop: accepted and documented, not fixed

plyer's backends shell out internally with no timeout we can pass through, so
there is no equivalent knob. Reaching the exposure needs a backend that is
**present and hanging**: `desktop_notifications` defaults to off, the
deployment target is headless Docker, and `_get_notifier` already disables the
channel outright when neither `gdbus` nor `notify-send` exists. The call site
now records the exposure, the reasoning, and the two real fixes (reimplement
dispatch behind `subprocess.run(timeout=…)`, or a killable subprocess boundary).

**Q15.** Acceptable as a documented residual, or does your gate item 1 require
the desktop path bounded before this round can merge? If the latter, which of
the two fixes do you want.

**Q16.** I did not add the subprocess-level process-termination test you asked
for. With SMTP bounded and desktop documented-but-unbounded, that test would
have to assert the exposure still exists, which seems worse than no test. Is
there a version of it you want — e.g. asserting SMTP specifically cannot hold
the process open?

## 4. Evidence

Full suite, both CI interpreters, against `7e40791` (the `1b353dc` change on top
is comment-only and cannot affect behaviour):

```
docker exec -e PYTHONPATH=/work:/work/tests/tools -e HOME=/tmp -w /work \
  <container> python -m pytest tests/ -q -p threadleak -p no:cacheprovider
```

| | result | exit | threadleak |
|---|---|---|---|
| **3.12.13** (`scanhound:latest`) | 4215 passed, 4 skipped, 0 failed | 0 | **none** |
| **3.11.15** (`python:3.11-slim`, CI's pip list) | 4214 passed, 4 skipped, 1 failed | 1 | **none** |

The 3.11 failure is the pre-existing `/openapi.json` one you confirmed affects
both matrix legs. Unchanged by this range and still out of scope.

The parent commit `f5139b2` was independently verified the same way before this
work went on top: 3.12 → 4212 passed / exit 0 / none; 3.11 → 4211 passed / none.

### Mutations

| mutation | test that caught it | result |
|---|---|---|
| remove cancel-and-gather | `test_pending_non_executor_tasks_are_cancelled_not_destroyed` | failed, with the pending-destroy warning |
| SMTP constructors without `timeout=` | `test_smtp_dispatch_is_bounded_by_its_timeout` | **hung** (killed at 120s) |
| drop the executor drain | `test_a_wedged_drain_strands_the_thread_rather_than_shutdown` | failed |
| never `close()` the loop | `test_loop_thread_exits_and_closes_the_loop` | failed |

### Relevant paths

- `backend/notification_bridge.py` — `_start_loop` cancel/gather/drain/close
- `backend/notifications.py` — `SMTP_TIMEOUT_SECONDS`, `EmailChannel`, desktop exposure note
- `backend/api/dependencies.py` — corrected daemon-exit reasoning + the measurement
- `tests/test_notification_backend_bounds.py` — 3 new tests
- `tests/test_notification_bridge_lifecycle.py` — 6 tests

## 5. Still open, explicitly

- **Gate item 2**, send-admission closure — next round, as you scoped it.
- **The three lifecycle P0s** — application-wide deadline, generation fencing,
  `begin_lifespan()`. Untouched.
- **Desktop dispatch** — unbounded by decision, see Q15.

Response to
`docs/reviews/peer-rounds/2026-08-02-notification-bounds-round3-chatgpt-response.md`.
