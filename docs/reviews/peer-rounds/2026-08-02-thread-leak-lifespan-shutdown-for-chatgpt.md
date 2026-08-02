# Peer review — lifespan shutdown must join its background threads

| | |
|---|---|
| **Repository** | `LstDtchMn/ScanHound` (private) |
| **Branch** | `claude/nostalgic-brattain-946f4f` |
| **Head** | `d1e42a1f10e1b2a2843f256a312e052a55090ffb` |
| **Code commit** | `b25b330f886575a3ffa0bc8f0cb496cea6e36218` |
| **Base** | `7cc5275b4a518cb6986f34e26e1c0e9c98175b7c` (`main`) |
| **Code status** | committed and pushed |
| **Working tree** | clean at the time of the verification run below |
| **Scope** | 20 files, 824 insertions / 220 deletions |

**The branch is the source of truth.** This document explains the questions,
evidence and gaps; read the actual code at `b25b330`. Review range:
`7cc5275..b25b330` (`git diff 7cc5275..b25b330`). Commit `8ab0ce8` in between is a
superseded handoff doc, deleted in the head commit — ignore it.

---

## 1. The defect

Every background thread `create_app`'s FastAPI lifespan started was `daemon=True`,
and `_teardown_services` stopped none of them, so the only thing that ever ended one
was interpreter exit. Under `TestClient` — which runs many lifespans in a single
process — they accumulated and kept touching process globals during later tests.

This was not hypothetical. It produced a real flake, fixed separately in `9b059c5`:
a leaked `background-scanner` thread constructed an HTTP client inside another
test's monkeypatch window. Any future test that patches a process-global has the
same exposure.

Threads observed surviving teardown: `jd-results-poller`, `poster-backfill`,
`plex-auto-connect`, `auto-rename`, the route-started scan thread and its
`asyncio_N` / `ThreadPoolExecutor-N` children.

## 2. The instrument was broken — please sanity-check this before the rest

`tests/tools/threadleak.py` is a pytest plugin that snapshots
`threading.enumerate()` around each test. Its `pytest_runtest_teardown` was a
**plain hookimpl**. Pluggy calls hookimpls last-registered-first, and `-p threadleak`
registers after the core plugins, so it ran *before* `_pytest.runner` finalized the
test's fixtures.

It was therefore measuring "threads alive **during** the test", not "threads that
**outlived** it". Every `with TestClient(app)` fixture looked like a leak — including
TestClient's own `asyncio-portal-*` and `AnyIO worker thread`, which its `__exit__`
cleans up reliably.

Measured consequence: it reported **223 leaking tests in `test_api_routes.py` both
before AND after** the lifespan fix. An instrument that could not see the change it
existed to measure.

Changed to `@pytest.hookimpl(hookwrapper=True)` with the snapshot after `yield`.
The same run then reported 223 → 1.

- **Q1.** Is `hookwrapper=True` right versus the newer `wrapper=True`? Chosen for
  compatibility across pytest 7/8/9 (host 9.0.2, container 9.1.1, and the container
  pip-installs pytest fresh each time).
- **Q2.** Is there a case where sampling *after* fixture finalization **under**-reports
  a real leak?

## 3. Design

`backend/api/dependencies.py` — `ServiceRegistry` gains thread ownership:

| member | purpose |
|---|---|
| `LIFESPAN_JOIN_BUDGET_SECONDS = 5.0` | total wall clock teardown may spend joining |
| `spawn_lifespan_thread(target, *, name, args, kwargs)` | track **and** start under one lock hold |
| `register_lifespan_thread(thread)` | track an already-started thread |
| `wait_for_shutdown(seconds) -> bool` | interruptible `time.sleep` replacement |
| `join_lifespan_threads(timeout) -> List[str]` | join all within ONE shared budget; returns survivors |

`backend/api/main.py` — teardown is now **signal → stop services → join (bounded) →
close DB**:

- `_signal_workers_to_stop(reg)` sets the shutdown event, sets `scanner.stop_scan_flag`,
  **pauses** (not cancels) an in-flight Plex metadata inventory, and calls
  `rename_svc.cancel_apply()`. Each is individually guarded.
- `_join_lifespan_threads(reg)` runs **before** `reg.backend.shutdown()` closes the DB,
  and again in the `finally` as a safety net. It is idempotent — the join drains the
  registry's list.

Converted to registry-owned (12 call sites): `jd-results-poller`, `poster-backfill`,
`plex-auto-connect`, `auto-rename`, `scan-run`, `scheduled-scan`, `background-scan-now`,
6 rename routes, 2 RSS routes, 3 `plex_metadata_scan` spawns, `rename-apply`.

Deliberately left as plain threads: `app_service` (maintenance, scheduler),
`background_scanner`, `download_queue`, `notification_bridge` — each already owns its
thread *and* a `stop()` that joins it with its own timeout.

`backend/api/routes/rss.py` lost ~50 lines of bespoke tracking (its own thread sets
attached to the registry by `hasattr`, joined from an AppService shutdown hook). That
version registered the hook only when `reg.backend` happened to be non-None at the
first call and never retried, used a **per-thread** 2s join, never cleared the sets
between lifespans, and was silent on expiry.

## 4. Questions

Ordered by how much I want them challenged.

**Q5 — budget sizing. The one I think may actually be wrong.**
5s shared, on top of pre-existing per-service joins (scheduler 3 + maintenance 3 +
download-queue 5+5 + background-scanner 2 = up to 18s). Worst case is therefore ~23s.
Docker's default SIGKILL grace is 10s, so a genuinely wedged worker could get the
container killed mid-shutdown — the opposite of the point. I did not touch the
pre-existing timeouts because they predate this change; that is a scope argument, not
a correctness one. Should the budget shrink, or should the per-service joins fold into
one shared budget?

**Q4 — lock held across `thread.start()`.**
`spawn_lifespan_thread` holds `_lifespan_threads_lock` across both tracking and
`start()`. A constructed-but-unstarted thread reports `is_alive() == False` exactly
like a finished one, so between "track" and "start" a concurrent `register()` reaps it
as dead (worker runs untracked) and a concurrent `join()` calls `Thread.join()` on it →
`RuntimeError`, aborting the rest of shutdown. I argue no deadlock because the child
never needs this lock, and a target that itself spawns merely blocks until the parent's
`start()` returns, which does not wait on the child. **Is that airtight?**

**Q7 — per-scan cost.**
`backend/scanner_service.py` `run_scan` now calls
`loop.shutdown_default_executor(timeout=5)` before `loop.close()` on **every** scan,
not only at shutdown. Without it each scan stranded a pool of idle `asyncio_N` threads
— every source adapter fetches via `loop.run_in_executor(None, ...)` and `loop.close()`
does not touch the default executor. Is a bounded 5s tail on every scan acceptable, or
should this be conditional?

**Q3 — double join.**
`_join_lifespan_threads` is called inside the `try` (before the DB closes) and again in
the `finally`. Idempotent by draining the list. Is there a path where both calls each
spend a full budget?

**Q6 — `begin_lifespan` clears the thread list.**
Intended for the abandoned-lifespan path (startup raised, teardown never ran). Can this
orphan threads that a later teardown should have joined?

**Q8 — `NotificationBridge.shutdown()`.**
Guarded by `self._loop.is_running()`, then `run_coroutine_threadsafe(
loop.shutdown_default_executor(timeout=2), loop)` + `.result(timeout=3)`. There is a
window where the loop stops between the check and the submit, costing 3s. Worth closing?

**Q9 — behaviour change, metadata scan.**
Shutdown now *pauses* an in-flight inventory rather than leaving it. The manifest is
persisted and `_run_durable` writes `status="paused"`, which `resume()` picks up. Right
semantic?

**Q10 — test-side fixes.**
Four tests replaced `registry.backend` / `registry._download_queue_service` with stubs —
which is *how teardown reaches* those services — so they stranded the real threads. I
made them save-and-restore (in `tests/test_dv_settings.py` via a `stub_backend` fixture
that depends on `client`, so it restores before the lifespan exits). Is restoring right,
or does it weaken what those tests assert? **Alternative I rejected:** have
`_init_services` record its own closers so teardown stops what startup started
regardless of later registry mutation — rejected because
`test_teardown_clears_all_references_even_when_shutdown_hooks_fail` deliberately drives
teardown through the registry fields, and a closer list would have made it vacuous.

## 5. Evidence

### Commands and exact results

Full suite, Linux container (`scanhound:latest` + `pip install pytest pytest-asyncio
httpx`), run against `b25b330` with a clean working tree:

```
docker exec -e PYTHONPATH=/work:/work/tests/tools -e HOME=/tmp -w /work \
  sh-test-peerreview python -m pytest tests/ -q -p threadleak -p no:cacheprovider
```

```
=========== 4206 passed, 4 skipped, 13 warnings in 592.41s (0:09:52) ===========
THREADLEAK: none
PYTEST_EXIT=0
```

Targeted, Windows host:

```
PYTHONPATH=tests/tools python -m pytest tests/test_api_rename.py tests/test_api_routes.py \
  -q -p threadleak
```
→ `297 passed`; `THREADLEAK: 1` — `Thread-N (balloon_tip)`, plyer's own fire-and-forget
Windows toast thread (`plyer/platforms/win/notification.py:17`). Third-party,
Windows-host-only, absent on the Linux deployment target (0 leaks there).

`test_api_routes.py` alone went from **223 leaking tests → 0**.

### Mutation results

Each mutation was applied, the named test confirmed to fail, then reverted:

| mutation | test that caught it | result |
|---|---|---|
| `join_lifespan_threads` → no-op | `test_lifespan_threads_are_joined_by_teardown`, `test_teardown_does_not_hang_on_a_wedged_worker` | 2 failed |
| shared budget → per-thread timeout | `test_join_budget_is_shared_across_threads_not_per_thread` | 1 failed |
| track-then-start (drop the lock) | `test_spawn_is_atomic_so_a_join_cannot_see_a_half_spawned_thread` | 1 failed, `RuntimeError('cannot join thread before it is started')` |
| `except RuntimeError` stops releasing ids | `test_jobs_releases_analyzing_ids_when_thread_start_fails` | 1 failed |

**No CI.** There is no CI on agent branches in this repo, so nothing here is
machine-attested — every number above is from a local run I performed.

### Relevant paths

- `backend/api/dependencies.py` — the mechanism (Q3, Q4, Q6)
- `backend/api/main.py` — `_signal_workers_to_stop`, `_join_lifespan_threads`, `_teardown_services` (Q3, Q5, Q9)
- `backend/scanner_service.py` — `run_scan` finally block (Q7)
- `backend/notification_bridge.py` — `shutdown()` (Q8)
- `tests/tools/threadleak.py` — the instrument (Q1, Q2)
- `tests/test_api_lifecycle.py` — the new tests behind the mutation table
- `tests/test_dv_settings.py`, `tests/test_api_routes.py` — the save-and-restore fixes (Q10)

## 6. Known gaps, stated deliberately

- **`test_bulk_rename_loops_stop_on_shutdown` is the weakest test in the set.** It uses
  `RenameService.__new__` to skip `__init__` and only asserts `_shutdown_requested()`
  tracks the flag — it does **not** verify the four bulk loops call it. A mutation
  deleting a `break` from `reidentify_all` would survive it.
- **`scan_conflict_dv` has no shutdown check.** Its loop is at most 2 paths and the
  uninterruptible unit is a single RPU walk, so a between-items check saves at most one
  file. `dv-scan-folder` walks a whole folder and did get the check.
- **`balloon_tip`** — third-party, Windows-host-only, not ours.
- **Three tests had gone *vacuous*, not failing, and the full suite caught them.** They
  patched `rename_routes.threading.Thread`, which the routes no longer call, so
  `_BoomThread` never simulated a thread-start failure and `_ImmediateThread` never ran
  anything inline. All now patch `spawn_lifespan_thread`. **Worth checking for others of
  this shape I may have missed.**
- **A first attempt at the Q4 test was worthless:** 24 concurrent spawners passed against
  the broken implementation on every run, because the window is a few instructions wide.
  Replaced with a deterministic version that stalls `Thread.start()` and asserts a
  concurrent joiner is still blocked on the lock.
- **Merge overlap** with the in-flight `agent/hybrid-sweep-implementation` branch: of the
  20 files touched, only 3 differ between it and `main`, and 2 do not collide
  (`rss.py` +1 line at ~165 vs my deletion at 25–89; `scanner_service.py` hunks at
  12/159/940 vs my edit at ~371). `tests/tools/threadleak.py` exists only on that branch
  → add/add conflict, resolved by taking this version (their file plus the hook fix).

## 7. What I want back

A verdict on Q3–Q10, with Q5 first. If you want any file in full rather than reading it
on the branch, name it.

Response goes to
`docs/reviews/peer-rounds/2026-08-02-thread-leak-lifespan-shutdown-chatgpt-response.md`.
