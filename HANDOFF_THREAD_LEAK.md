# Review request — ScanHound: lifespan shutdown must join its background threads

**Status:** uncommitted working tree, `claude/nostalgic-brattain-946f4f`, based on `main` @ `7cc5275`.
**Scope:** 19 modified files + 1 new test tool. 763 insertions / 220 deletions.
**The complete diff is inlined in section 7 below** (includes the new `tests/tools/threadleak.py`),
so this single file is self-contained — the code it describes is not committed anywhere yet.

Nothing is committed or pushed. This review is the gate before that.

---

## 1. The bug

`create_app`'s FastAPI lifespan started background threads (all `daemon=True`) that
`_teardown_services` never stopped. Under `TestClient` — which runs many lifespans in one
process — they accumulated and kept touching process globals during later tests. It had
already produced one real flake (fixed separately in `9b059c5`): a leaked `background-scanner`
thread constructed an HTTP client inside another test's monkeypatch window.

## 2. The measuring instrument was itself broken — please sanity-check this first

`tests/tools/threadleak.py` is a committed pytest plugin that snapshots `threading.enumerate()`
around each test. Its `pytest_runtest_teardown` was a **plain hookimpl**. Pluggy calls hookimpls
last-registered-first, and `-p threadleak` registers after the core plugins, so it ran *before*
`_pytest.runner` finalized the test's fixtures. It was therefore measuring "threads alive during
the test", not "threads that outlived it" — every `with TestClient(app)` fixture looked like a
leak, including TestClient's own `asyncio-portal-*` and `AnyIO worker thread`, which its
`__exit__` reliably cleans up.

Measured consequence: it reported **223 leaking tests in `test_api_routes.py` both before AND
after** the lifespan fix. An instrument that could not see the change it existed to measure.

Changed to `@pytest.hookimpl(hookwrapper=True)` with the snapshot after `yield`. Same run then
reported 223 → 1.

**Q1.** Is `hookwrapper=True` the right choice over the newer `wrapper=True`? I chose it for
compatibility (pytest 7/8/9; the container pip-installs pytest, host has 9.0.2, container 9.1.1).
**Q2.** Is there a case where sampling after fixture finalization *under-reports* a real leak?

## 3. Design

`ServiceRegistry` (`backend/api/dependencies.py`) gains thread ownership:

| member | purpose |
|---|---|
| `LIFESPAN_JOIN_BUDGET_SECONDS = 5.0` | total wall clock teardown may spend joining |
| `spawn_lifespan_thread(target, *, name, args, kwargs)` | start a tracked daemon thread |
| `register_lifespan_thread(thread)` | track an already-started thread |
| `wait_for_shutdown(seconds) -> bool` | interruptible `time.sleep` replacement |
| `join_lifespan_threads(timeout) -> List[str]` | join all within ONE shared budget; returns survivors |

`_teardown_services` is now: **signal → stop services → join (bounded) → close DB**.
`_signal_workers_to_stop` sets the shutdown event, sets `scanner.stop_scan_flag`, *pauses* an
in-flight Plex metadata inventory, and calls `rename_svc.cancel_apply()`.

Converted to registry-owned: `jd-results-poller`, `poster-backfill`, `plex-auto-connect`,
`auto-rename`, `scan-run`, `scheduled-scan`, `background-scan-now`, 6 rename routes, 2 RSS
routes, 3 `plex_metadata_scan` spawns, `rename-apply`.

Left deliberately as plain threads: `app_service` (maintenance, scheduler),
`background_scanner`, `download_queue`, `notification_bridge` — each already owns its thread
*and* a `stop()` that joins it with its own timeout.

## 4. Specific things I want challenged

**Q3 — join placement.** `_join_lifespan_threads(reg)` is called inside the `try`, *before*
`reg.backend.shutdown()` (which closes the DB), because scan/auto-rename workers write results
through `reg.db`. It is called a second time in the `finally` as a safety net for the case where
something above raised past its own guard. It's idempotent (the join drains the list, so the
second call returns `[]` immediately). Is the double-call reasoning sound, or is there a path
where both calls each spend a full budget?

**Q4 — lock held across `thread.start()`.** `spawn_lifespan_thread` holds
`_lifespan_threads_lock` across both tracking and `start()`. Rationale: a constructed-but-not-
started thread reports `is_alive() == False` exactly like a finished one, so between "track" and
"start" a concurrent `register()` reaps it as dead (worker runs untracked) and a concurrent
`join()` calls `Thread.join()` on it → `RuntimeError`, aborting the rest of shutdown. I argue no
deadlock because the new thread never needs this lock. **Is that airtight?** Note: a target that
itself spawns blocks until the parent's `start()` returns, which does not wait on the child.

**Q5 — budget sizing.** 5s shared across all registry threads, on top of existing per-service
joins (scheduler 3 + maintenance 3 + download-queue 5+5 + background-scanner 2 = up to 18s).
Worst-case shutdown is therefore ~23s. Too long for a container stop (Docker's default SIGKILL
grace is 10s)? Should the budget scale down, or should the per-service joins be folded into the
same budget?

**Q6 — `begin_lifespan` clears the thread list.** Intended for the abandoned-lifespan path
(startup raised, teardown never ran). Can this orphan threads a later teardown should have
joined?

**Q7 — per-scan cost.** `scanner_service.run_scan` now calls
`loop.shutdown_default_executor(timeout=5)` before `loop.close()` on **every** scan, not only at
shutdown. Without it, each scan stranded a pool of idle `asyncio_N` threads (the source adapters
all fetch via `run_in_executor(None, ...)`, and `loop.close()` does not touch the default
executor). Is a bounded 5s tail on every scan acceptable, or should this be conditional?

**Q8 — `NotificationBridge.shutdown()`.** Guarded by `self._loop.is_running()`, then
`run_coroutine_threadsafe(loop.shutdown_default_executor(timeout=2), loop)` +
`.result(timeout=3)`. There is a window where the loop stops between the check and the submit,
costing 3s. Worth closing, or acceptable?

**Q9 — behavior change, metadata scan.** Shutdown now *pauses* (not cancels) an in-flight Plex
metadata inventory, since the manifest is persisted and `_run_durable` writes `status="paused"`,
which `resume()` picks up. Is pause the right semantic here versus leaving it untouched?

**Q10 — test-side fixes.** Four tests replaced `registry.backend` / `registry._download_queue_service`
with stubs, which is *how teardown reaches* those services — so they stranded the real threads. I
made them save-and-restore (and in `test_dv_settings.py` moved it into a `stub_backend` fixture
that depends on `client`, so it restores before the lifespan exits). Is restoring the right fix,
or does it weaken what those tests assert? Alternative I rejected: have `_init_services` record
its own closers so teardown stops what startup started regardless of later registry mutation —
rejected because `test_teardown_clears_all_references_even_when_shutdown_hooks_fail` deliberately
drives teardown through the registry fields, and the closer list would have made it vacuous.

## 5. Verification (all re-run on the final tree)

- Full suite, Linux container (`scanhound:latest` + pytest): **4206 passed, 4 skipped, 0 failed**
  (9:32), **`THREADLEAK: none`**.
- `test_api_routes.py` alone: 223 leaking tests → **0**.
- Windows host shows 1 residual leak, `Thread-N (balloon_tip)` — plyer's own fire-and-forget
  Windows toast thread (`plyer/platforms/win/notification.py:17`). Third-party, Windows-only,
  absent on the Linux deployment target.

Mutation-checked (each mutation was verified to fail the named test, then reverted):

| mutation | test that caught it |
|---|---|
| `join_lifespan_threads` → no-op | `test_lifespan_threads_are_joined_by_teardown`, `test_teardown_does_not_hang_on_a_wedged_worker` |
| shared budget → per-thread timeout | `test_join_budget_is_shared_across_threads_not_per_thread` |
| track-then-start (drop the lock) | `test_spawn_is_atomic_so_a_join_cannot_see_a_half_spawned_thread` |
| `except RuntimeError` stops releasing ids | `test_jobs_releases_analyzing_ids_when_thread_start_fails` |

A first attempt at Q4's test — 24 concurrent spawners — passed against the *broken*
implementation on every run; the window is a few instructions wide. Replaced with a deterministic
version that stalls `Thread.start()` and asserts a concurrent joiner is still blocked on the lock.

## 6. Known gaps, stated deliberately

- **`scan_conflict_dv`** has no shutdown check. Its loop is at most 2 paths and the
  uninterruptible unit is a single RPU walk, so a between-items check saves at most one file.
  `dv-scan-folder` walks a whole folder and *did* get the check.
- **`balloon_tip`** — third-party, not ours, Windows-host-only.
- **The full suite caught three tests that had gone *vacuous*, not failing:** they patched
  `rename_routes.threading.Thread`, which the routes no longer call, so `_BoomThread` never
  simulated a thread-start failure and `_ImmediateThread` never ran anything inline. All now
  patch `spawn_lifespan_thread`. Worth a look for others of this shape I may have missed.
- Merge overlap with the in-flight `agent/hybrid-sweep-implementation` branch: of the 20 files
  touched, only 3 differ between it and `main`, and 2 don't collide (`rss.py` +1 line at ~165 vs
  my deletion at 25–89; `scanner_service.py` hunks at 12/159/940 vs my edit at ~371).
  `tests/tools/threadleak.py` exists only on that branch → add/add conflict, resolved by taking
  this version (their file plus the hook fix).

---

## 7. Complete diff

Working tree vs `main` @ `7cc5275`. `tests/tools/threadleak.py` is shown as a new file
because it is untracked on this branch — it exists on `agent/hybrid-sweep-implementation`,
and this version is that file plus the hook-ordering fix described in section 2.

```diff
diff --git a/backend/api/dependencies.py b/backend/api/dependencies.py
index 9cc9af9..0f5d86c 100644
--- a/backend/api/dependencies.py
+++ b/backend/api/dependencies.py
@@ -5,6 +5,7 @@ import logging
 import os
 import secrets
 import threading
+import time
 from dataclasses import dataclass, field
 from typing import Any, Callable, Dict, List, Optional, Set
 
@@ -25,6 +26,13 @@ EMOJI_DV = "[DV]"
 EMOJI_INFO = "\u2139\ufe0f"
 EMOJI_WARNING = "\u26a0\ufe0f"
 
+# Total wall-clock a lifespan teardown may spend waiting for registry-owned
+# threads. It is a BUDGET SHARED BY ALL of them, not a per-thread timeout: a
+# handful of workers wedged in a long network call must not multiply into a
+# minutes-long shutdown. Whatever is still alive when it expires is logged by
+# name and abandoned (every such thread is a daemon, so the process still exits).
+LIFESPAN_JOIN_BUDGET_SECONDS = 5.0
+
 
 class ScannerAppBridge:
     """Adapter providing the interface MatchingEngine/WebScrapers expect from parent_app.
@@ -113,6 +121,12 @@ class ServiceRegistry:
     _shutdown_event: threading.Event = field(default_factory=threading.Event)
     _lifespan_generation: int = 0
     _lifespan_generation_lock: threading.Lock = field(default_factory=threading.Lock)
+    # Background threads this lifespan started and must join before it ends.
+    # Services that own a thread AND their own stop() (download queue,
+    # background scanner, notification bridge, AppService) keep joining it
+    # themselves; this list is for the loose threads nobody else owns.
+    _lifespan_threads: List[threading.Thread] = field(default_factory=list)
+    _lifespan_threads_lock: threading.Lock = field(default_factory=threading.Lock)
     # Auth nonce — generated on startup, validated by middleware.
     # If SCANHOUND_AUTH_NONCE env var is set, use that (Tauri passes it).
     # If empty string, auth is disabled (dev mode).
@@ -182,7 +196,8 @@ class ServiceRegistry:
                             "data": status_dict,
                         })
 
-                    self._plex_metadata_scan_job = PlexMetadataScanJob(self.db, progress_cb=_broadcast)
+                    self._plex_metadata_scan_job = PlexMetadataScanJob(
+                        self.db, progress_cb=_broadcast, registry=self)
         return self._plex_metadata_scan_job
 
     def begin_lifespan(self) -> int:
@@ -190,9 +205,112 @@ class ServiceRegistry:
         with self._lifespan_generation_lock:
             self._lifespan_generation += 1
             generation = self._lifespan_generation
+        # A fresh lifespan owns no threads yet. Normally join_lifespan_threads()
+        # has already emptied this during teardown; clearing again matters for
+        # the ABANDONED-lifespan path (startup raised, so teardown never ran) —
+        # otherwise the next shutdown would try to join a previous generation's
+        # workers, which are no longer this lifespan's problem.
+        with self._lifespan_threads_lock:
+            self._lifespan_threads = []
         self._shutdown_event.clear()
         return generation
 
+    def spawn_lifespan_thread(
+        self,
+        target: Callable,
+        *,
+        name: str,
+        args: tuple = (),
+        kwargs: Optional[Dict[str, Any]] = None,
+    ) -> threading.Thread:
+        """Start a daemon thread whose lifetime is bounded by this lifespan.
+
+        Use this instead of a bare ``threading.Thread(...).start()`` for any
+        background work started from the lifespan (or from a route, on the
+        lifespan's behalf). The thread is joined by ``join_lifespan_threads``
+        during teardown, so it cannot survive into the next lifespan and reach
+        a service whose DB was already closed.
+
+        The target is still responsible for NOTICING cancellation — poll
+        ``shutdown_requested`` or block on ``wait_for_shutdown`` rather than
+        ``time.sleep`` — the join only bounds how long teardown waits for it.
+        """
+        thread = threading.Thread(
+            target=target, name=name, args=args, kwargs=kwargs or {}, daemon=True)
+        # Tracking and start() happen under ONE lock hold, which is what makes
+        # this safe against a concurrent spawn or teardown. A thread that has
+        # been constructed but not started reports is_alive() == False exactly
+        # like a finished one, so a racing register() would reap it as dead and
+        # a racing join() would call join() on an unstarted thread (RuntimeError,
+        # aborting the rest of the shutdown). Holding the lock across start()
+        # closes both windows; the new thread never needs this lock, so it
+        # cannot deadlock against us. (A target that itself spawns simply
+        # blocks until start() returns, which does not wait on the child.)
+        with self._lifespan_threads_lock:
+            self._track_locked(thread)
+            thread.start()
+        return thread
+
+    def register_lifespan_thread(self, thread: threading.Thread) -> None:
+        """Track an ALREADY-STARTED thread as owned by this lifespan.
+
+        Prefer ``spawn_lifespan_thread``; this exists for threads constructed
+        elsewhere. Pass a started thread — an unstarted one is indistinguishable
+        from a finished one here, and ``join_lifespan_threads`` would raise on
+        it rather than wait for it.
+        """
+        with self._lifespan_threads_lock:
+            self._track_locked(thread)
+
+    def _track_locked(self, thread: threading.Thread) -> None:
+        """Append ``thread`` to the tracked list. Caller holds the lock."""
+        # Drop finished entries as we go: the real app runs one lifespan for
+        # weeks, and per-scan/per-package threads would otherwise pile up
+        # dead Thread objects for its whole life.
+        self._lifespan_threads = [
+            t for t in self._lifespan_threads if t.is_alive()]
+        self._lifespan_threads.append(thread)
+
+    def wait_for_shutdown(self, seconds: float) -> bool:
+        """Sleep up to ``seconds``, waking immediately if shutdown is requested.
+
+        The interruptible replacement for ``time.sleep`` in background workers:
+        a plain sleep makes the thread unjoinable for its full duration, which
+        is what turns a 30-second settle delay into a 30-second shutdown stall.
+
+        Returns True if shutdown was requested (i.e. the caller should return).
+        """
+        return self._shutdown_event.wait(seconds)
+
+    def join_lifespan_threads(
+        self, timeout: float = LIFESPAN_JOIN_BUDGET_SECONDS
+    ) -> List[str]:
+        """Join every registry-owned thread within one shared time budget.
+
+        ``timeout`` is the TOTAL wall clock spent here, not per thread, so N
+        wedged workers cost the same as one. Returns the names of the threads
+        still alive when the budget ran out (empty on a clean shutdown) for the
+        caller to log — they are daemons, so abandoning them still lets the
+        process exit.
+        """
+        with self._lifespan_threads_lock:
+            threads = list(self._lifespan_threads)
+            self._lifespan_threads = []
+        current = threading.current_thread()
+        deadline = time.monotonic() + max(0.0, timeout)
+        for thread in threads:
+            # A worker that triggered shutdown itself (e.g. the /shutdown route
+            # handler's thread) would deadlock for the whole budget joining
+            # itself.
+            if thread is current:
+                continue
+            remaining = deadline - time.monotonic()
+            if remaining <= 0:
+                break
+            thread.join(timeout=remaining)
+        return sorted({
+            t.name for t in threads if t is not current and t.is_alive()})
+
     @property
     def lifespan_generation(self) -> int:
         with self._lifespan_generation_lock:
diff --git a/backend/api/main.py b/backend/api/main.py
index e11289d..cfceaee 100644
--- a/backend/api/main.py
+++ b/backend/api/main.py
@@ -3,8 +3,6 @@ from __future__ import annotations
 
 import asyncio
 import logging
-import threading
-import time
 from contextlib import asynccontextmanager
 from typing import Any, Dict, Optional
 
@@ -14,6 +12,7 @@ from fastapi.responses import JSONResponse
 
 from backend.api.dependencies import (
     ServiceRegistry, ScannerAppBridge, registry,
+    LIFESPAN_JOIN_BUDGET_SECONDS,
     auth_enabled as _auth_enabled,
     token_authorized as _token_authorized,
     has_any_credential as _has_any_credential,
@@ -205,8 +204,6 @@ def _init_services(
 
     # Auto-connect to Plex on startup if configured (direct or account mode).
     if _should_auto_connect_plex(reg.config):
-        # (threading is imported at module level — a local import here would
-        # shadow it for the whole function and break earlier uses.)
         def _auto_connect_plex():
             from backend.api.ws import ws_manager
             try:
@@ -247,7 +244,10 @@ def _init_services(
                     "type": "plex:status",
                     "data": {"connected": False, "server": "", "movie_count": 0, "tv_count": 0},
                 })
-        threading.Thread(target=_auto_connect_plex, daemon=True, name="plex-auto-connect").start()
+        # Registry-owned: plex_svc.connect() talks to the network and cannot be
+        # interrupted, so this is the archetypal "wedged worker" the join budget
+        # exists to survive — teardown waits briefly, warns, and moves on.
+        reg.spawn_lifespan_thread(_auto_connect_plex, name="plex-auto-connect")
 
     # Backfill resolution/size/HDR/DV onto older download-history rows that
     # were grabbed before the metadata was captured (e.g. via batch grabs that
@@ -288,7 +288,7 @@ def _init_services(
         })
 
     reg._plex_metadata_scan_job = PlexMetadataScanJob(
-        reg.db, progress_cb=_broadcast_metadata_scan_progress)
+        reg.db, progress_cb=_broadcast_metadata_scan_progress, registry=reg)
 
     # Crash recovery: any job left in the transient 'applying' state (process
     # died mid-move) is reset to 'matched' so it can be retried. The move is
@@ -306,13 +306,17 @@ def _init_services(
     # (they render as "No poster" otherwise). Delayed + threaded so startup
     # never blocks on TMDB; idempotent (only touches empty poster_path rows).
     def _poster_backfill():
-        time.sleep(30)  # let the app settle first
+        # Interruptible settle delay: a plain time.sleep(30) here made the
+        # thread unjoinable for its whole duration, so every shutdown in the
+        # first 30 seconds of a lifespan either stalled or leaked it. Returns
+        # True the moment shutdown is requested.
+        if reg.wait_for_shutdown(30):
+            return
         try:
             reg._rename_service.backfill_posters()
         except Exception:
             logger.debug("poster backfill failed (non-fatal)", exc_info=True)
-    threading.Thread(target=_poster_backfill, name="poster-backfill",
-                     daemon=True).start()
+    reg.spawn_lifespan_thread(_poster_backfill, name="poster-backfill")
 
     # Surface a DB corruption quarantine (if init_db() hit one) now that the
     # notification bridge actually exists — DatabaseManager._notify_corruption
@@ -345,8 +349,6 @@ def _start_results_poller(reg: ServiceRegistry, interval: float = 8.0) -> None:
     state to the DB, and broadcasts changes over the WebSocket so the Downloads
     page updates live. Stops when the registry signals shutdown.
     """
-    import threading
-    import time as _time
     from backend.api.ws import ws_manager
 
     def _loop():
@@ -387,11 +389,11 @@ def _start_results_poller(reg: ServiceRegistry, interval: float = 8.0) -> None:
                             if (r.get("state") == "extracted" and r.get("save_to")
                                     and key not in handed_to_rename):
                                 handed_to_rename.add(key)
-                                threading.Thread(
-                                    target=reg._rename_service.process_package,
+                                reg.spawn_lifespan_thread(
+                                    reg._rename_service.process_package,
                                     args=(r.get("name"), r.get("save_to")),
-                                    name="auto-rename", daemon=True,
-                                ).start()
+                                    name="auto-rename",
+                                )
                     # Prune stale keys so the set doesn't grow unbounded. Only
                     # when this poll actually returned rows — poll_results()
                     # returns [] on a transient JD failure, and clearing the
@@ -402,13 +404,12 @@ def _start_results_poller(reg: ServiceRegistry, interval: float = 8.0) -> None:
                         handed_to_rename &= live_keys
             except Exception as e:
                 logger.debug("results poller error: %s", e)
-            # Sleep in short slices so shutdown stays responsive.
-            waited = 0.0
-            while waited < interval and not reg.shutdown_requested:
-                _time.sleep(0.5)
-                waited += 0.5
+            # Wait out the interval on the shutdown event rather than sleeping:
+            # the poller now wakes the instant teardown signals, so the join
+            # below costs microseconds instead of up to half a second.
+            reg.wait_for_shutdown(interval)
 
-    threading.Thread(target=_loop, daemon=True, name="jd-results-poller").start()
+    reg.spawn_lifespan_thread(_loop, name="jd-results-poller")
     logger.info("Download results poller started")
 
 
@@ -535,9 +536,81 @@ def _within(path: str, base: str) -> bool:
     return real_path == real_base or real_path.startswith(real_base + _os.sep)
 
 
+def _signal_workers_to_stop(reg: ServiceRegistry) -> None:
+    """Tell every long-running worker to wind down, before anything is joined.
+
+    A worker that is merely joinable is not enough: without a signal it can
+    reach, the join just spends the whole budget and logs a straggler. Each
+    flag here is one a loop is already polling, and each is individually
+    guarded so a stub or half-built service cannot stop the others being told.
+    """
+    # Wakes every wait_for_shutdown()/shutdown_requested loop (results poller,
+    # poster backfill, RSS hydration/action, rename bulk loops).
+    reg.request_shutdown()
+    # In-flight scan from /scan/start or the scheduler: BackgroundScanner.stop()
+    # covers the scans IT started; nothing was cancelling the route-initiated
+    # ones, so they ran the crawl to completion.
+    try:
+        scanner = reg.scanner
+        if scanner is not None:
+            scanner.stop_scan_flag = True
+    except Exception:
+        logger.debug("scan cancellation on shutdown failed", exc_info=True)
+    # PAUSE, not cancel, an in-flight Plex metadata inventory: the manifest is
+    # persisted, so pausing leaves the run resumable on the next startup.
+    try:
+        job = reg._plex_metadata_scan_job
+        if job is not None and job.is_running():
+            job.pause()
+    except Exception:
+        logger.debug("metadata scan pause on shutdown failed", exc_info=True)
+    # Stops a running apply queue after the file currently in flight (never
+    # mid-move); documented as harmless when nothing is running.
+    try:
+        rename_svc = reg._rename_service
+        if rename_svc is not None:
+            rename_svc.cancel_apply()
+    except Exception:
+        logger.debug("apply-queue cancel on shutdown failed", exc_info=True)
+
+
+def _join_lifespan_threads(reg: ServiceRegistry) -> None:
+    """Wait out the lifespan's own background threads, bounded, and report.
+
+    Covers the workers nobody else owns: the JD results poller, the poster
+    backfill, Plex auto-connect, auto-rename hand-offs and in-flight scans.
+    (The services with their own ``stop()`` — download queue, background
+    scanner, notification bridge, AppService — still join their own threads.)
+
+    Idempotent: the join drains the registry's list, so a second call is a
+    no-op. That is what lets the caller invoke it on both the normal path and
+    the failure path without risking two full budgets.
+    """
+    try:
+        # Passed explicitly (rather than relying on the method's default) so
+        # this module-level constant is the one knob that governs both the
+        # wait and the message — including when a test shrinks it.
+        stragglers = reg.join_lifespan_threads(LIFESPAN_JOIN_BUDGET_SECONDS)
+        if stragglers:
+            logger.warning(
+                "Shutdown timed out after %.1fs waiting for background "
+                "thread(s): %s — abandoning them (daemon threads; the "
+                "process can still exit)",
+                LIFESPAN_JOIN_BUDGET_SECONDS, ", ".join(stragglers),
+            )
+    except Exception:
+        logger.warning("background thread join failed", exc_info=True)
+
+
 def _teardown_services(reg: ServiceRegistry) -> None:
-    """Gracefully shut down one lifespan, then erase its complete object graph."""
-    reg.request_shutdown()  # stop the background results poller
+    """Gracefully shut down one lifespan, then erase its complete object graph.
+
+    Signal first, then join. Every worker is a daemon, so before this the only
+    thing that ended them was interpreter exit — under TestClient, which starts
+    and stops many lifespans in one process, they simply accumulated and kept
+    touching process globals during later tests.
+    """
+    _signal_workers_to_stop(reg)
     try:
         if reg._download_queue_service:
             try:
@@ -559,12 +632,21 @@ def _teardown_services(reg: ServiceRegistry) -> None:
                 reg._watchlist_manager.close()
             except Exception:
                 pass
+        # BEFORE the DB closes, not after: a scan or auto-rename worker still
+        # unwinding writes its results through reg.db, and joining it only
+        # after backend.shutdown() would hand it a closed database for exactly
+        # the window it needs. Waiting here costs nothing on a clean shutdown
+        # (the workers have already seen the flag set at the top).
+        _join_lifespan_threads(reg)
         if reg.backend:
             try:
                 reg.backend.shutdown()
             except Exception:
                 pass
     finally:
+        # Safety net: if anything above raised past its own guard, the join
+        # never ran. Idempotent, so on the normal path this is a no-op.
+        _join_lifespan_threads(reg)
         # Even a failing shutdown hook must not leak a closed DB/service into the
         # next lifespan.  Leave the shutdown event set until the next startup so
         # late old threads still observe cancellation.
diff --git a/backend/api/routes/background.py b/backend/api/routes/background.py
index 52c2174..e4e1aea 100644
--- a/backend/api/routes/background.py
+++ b/backend/api/routes/background.py
@@ -1,6 +1,5 @@
 """Background pre-cache scanner endpoints: status + manual trigger."""
 import logging
-import threading
 from datetime import datetime, timezone
 from typing import Optional
 
@@ -48,6 +47,8 @@ def background_scan_now(reg: ServiceRegistry = Depends(get_registry)):
         raise HTTPException(status_code=503, detail="Background scanner not initialized")
     if scanner.is_scanning:
         raise HTTPException(status_code=409, detail="A background scan is already running")
-    threading.Thread(
-        target=scanner.scan_once, name="background-scan-now", daemon=True).start()
+    # Registry-owned: BackgroundScanner.stop() joins its own scheduler thread,
+    # but this ad-hoc one is not that thread, so nothing was waiting for it.
+    # stop() does set the scanner's stop flag, so it unwinds on the signal.
+    reg.spawn_lifespan_thread(scanner.scan_once, name="background-scan-now")
     return {"status": "triggered"}
diff --git a/backend/api/routes/rename.py b/backend/api/routes/rename.py
index 4b39997..fb85afd 100644
--- a/backend/api/routes/rename.py
+++ b/backend/api/routes/rename.py
@@ -204,6 +204,11 @@ def list_jobs(status: Optional[str] = None, limit: int = 200, archived: bool = F
             def _run(job_ids):
                 try:
                     for jid in job_ids:
+                        # Stop at an item boundary on shutdown; the ids are
+                        # released by the finally below either way, so a
+                        # short run just leaves them to the next poll.
+                        if reg.shutdown_requested:
+                            break
                         try:
                             job = reg.db.get_rename_job(jid)
                             if job:
@@ -216,7 +221,8 @@ def list_jobs(status: Optional[str] = None, limit: int = 200, archived: bool = F
                     with _analyzing_lock:
                         _analyzing_job_ids.difference_update(job_ids)
             try:
-                threading.Thread(target=_run, args=(fresh,), name="conflict-analyze", daemon=True).start()
+                reg.spawn_lifespan_thread(
+                    _run, args=(fresh,), name="conflict-analyze")
             except RuntimeError:
                 # Thread creation itself failed (e.g. OS thread exhaustion). The
                 # _run() finally that releases these ids never runs, so release
@@ -403,7 +409,7 @@ def scan_dv_conflict(job_id: int, reg: ServiceRegistry = Depends(get_registry)):
         except Exception:
             logger.exception("scan-dv-conflict failed")
 
-    threading.Thread(target=_run, name="scan-dv-conflict", daemon=True).start()
+    reg.spawn_lifespan_thread(_run, name="scan-dv-conflict")
     return {"status": "scanning", "job_id": job_id}
 
 
@@ -461,7 +467,7 @@ def reidentify_all(reg: ServiceRegistry = Depends(get_registry)):
                                        "data": public.notification_data(
                                            title="Re-identify failed")})
 
-    threading.Thread(target=_run, name="rename-reidentify-all", daemon=True).start()
+    reg.spawn_lifespan_thread(_run, name="rename-reidentify-all")
     return {"status": "started"}
 
 
@@ -628,7 +634,7 @@ def process_folder(req: ProcessFolderRequest, reg: ServiceRegistry = Depends(get
                                        "data": public.notification_data(
                                            title="Process folder failed")})
 
-    threading.Thread(target=_run, name="rename-process-folder", daemon=True).start()
+    reg.spawn_lifespan_thread(_run, name="rename-process-folder")
     return {"status": "started", "folder": folder, "dry_run": dry_run}
 
 
@@ -677,7 +683,7 @@ def dv_scan_folder(req: DvScanRequest, reg: ServiceRegistry = Depends(get_regist
                                        "data": public.notification_data(
                                            title="Dolby Vision scan failed")})
 
-    threading.Thread(target=_run, name="dv-scan-folder", daemon=True).start()
+    reg.spawn_lifespan_thread(_run, name="dv-scan-folder")
     return {"status": "started", "folder": folder, "force": force}
 
 
@@ -719,7 +725,8 @@ def dv_sync_labels(req: DvSyncRequest, reg: ServiceRegistry = Depends(get_regist
                     "done": done, "total": total}})
             result = dv_labeler.sync_labels(
                 reg.db, plex_manager, reg.config,
-                dry_run=dry_run, progress_cb=_progress)
+                dry_run=dry_run, progress_cb=_progress,
+                stop_requested=lambda: reg.shutdown_requested)
             ws_manager.broadcast_sync({"type": "notification", "data": {
                 "title": "Dolby Vision label sync",
                 "body": (f"{result['matched']} matched, "
@@ -739,7 +746,7 @@ def dv_sync_labels(req: DvSyncRequest, reg: ServiceRegistry = Depends(get_regist
         finally:
             ws_manager.broadcast_sync({"type": "dv:sync_done", "data": result})
 
-    threading.Thread(target=_run, name="dv-sync-labels", daemon=True).start()
+    reg.spawn_lifespan_thread(_run, name="dv-sync-labels")
     return {"status": "started", "dry_run": dry_run}
 
 
diff --git a/backend/api/routes/rss.py b/backend/api/routes/rss.py
index 7eacaa0..1c41ff0 100644
--- a/backend/api/routes/rss.py
+++ b/backend/api/routes/rss.py
@@ -3,7 +3,6 @@ from __future__ import annotations
 
 import json
 import logging
-import threading
 from typing import Literal, Optional
 
 from fastapi import APIRouter, Depends, HTTPException
@@ -22,69 +21,19 @@ logger = logging.getLogger(__name__)
 router = APIRouter(prefix="/rss", tags=["rss"])
 
 
-def _join_rss_hydration_threads(reg):
-    threads = list(getattr(reg, "_rss_hydration_threads", set()))
-    for thread in threads:
-        if thread.is_alive():
-            thread.join(timeout=2.0)
-
-
-
-
-def _join_rss_action_threads(reg):
-    threads = list(getattr(reg, "_rss_action_threads", set()))
-    for thread in threads:
-        if thread.is_alive():
-            thread.join(timeout=2.0)
-
-
+# These two used to keep their own thread sets on the registry (attached by
+# hasattr, joined from an AppService shutdown hook). ServiceRegistry now owns
+# that job for every lifespan thread, which fixes what the bespoke version got
+# wrong: the hook was only registered when reg.backend happened to be non-None
+# at the first call — and never retried, because the attribute then existed —
+# the 2s join was PER THREAD rather than a shared budget, the sets were never
+# cleared between lifespans, and an expired join was silent.
 def _start_tracked_action_thread(reg, target):
-    if not hasattr(reg, "_rss_action_threads"):
-        reg._rss_action_threads = set()
-        if reg.backend is not None:
-            reg.backend.add_shutdown_hook(
-                lambda: _join_rss_action_threads(reg)
-            )
-    holder = {}
+    return reg.spawn_lifespan_thread(target, name="rss-candidate-action")
 
-    def wrapped():
-        try:
-            target()
-        finally:
-            reg._rss_action_threads.discard(holder["thread"])
-
-    thread = threading.Thread(
-        target=wrapped,
-        name="rss-candidate-action",
-        daemon=True,
-    )
-    holder["thread"] = thread
-    reg._rss_action_threads.add(thread)
-    thread.start()
-    return thread
 
 def _start_tracked_hydration_thread(reg, target):
-    if not hasattr(reg, "_rss_hydration_threads"):
-        reg._rss_hydration_threads = set()
-        if reg.backend is not None:
-            reg.backend.add_shutdown_hook(
-                lambda: _join_rss_hydration_threads(reg)
-            )
-    holder = {}
-    def wrapped():
-        try:
-            target()
-        finally:
-            reg._rss_hydration_threads.discard(holder["thread"])
-    thread = threading.Thread(
-        target=wrapped,
-        name="rss-explicit-hydration",
-        daemon=True,
-    )
-    holder["thread"] = thread
-    reg._rss_hydration_threads.add(thread)
-    thread.start()
-    return thread
+    return reg.spawn_lifespan_thread(target, name="rss-explicit-hydration")
 
 
 class ModeRequest(BaseModel):
diff --git a/backend/api/routes/scanner.py b/backend/api/routes/scanner.py
index e9731cb..1b6c124 100644
--- a/backend/api/routes/scanner.py
+++ b/backend/api/routes/scanner.py
@@ -364,8 +364,11 @@ def scan_start(
         if _scan_state["state"] == "running":
             raise HTTPException(status_code=409, detail="Scan already running")
         _scan_state["state"] = "running"
-        _scan_thread = threading.Thread(target=_run_scan, args=(reg, req), daemon=True)
-        _scan_thread.start()
+        # Registry-owned so lifespan teardown joins it: a scan started by a
+        # request is still the app's background work, and outliving the
+        # lifespan lets it publish results into the *next* one's globals.
+        _scan_thread = reg.spawn_lifespan_thread(
+            _run_scan, args=(reg, req), name="scan-run")
     return {"status": "started", "type": req.type}
 
 
diff --git a/backend/api/routes/scheduler.py b/backend/api/routes/scheduler.py
index 08e9315..94a4320 100644
--- a/backend/api/routes/scheduler.py
+++ b/backend/api/routes/scheduler.py
@@ -1,5 +1,4 @@
 """Scheduler endpoints: status, config, trigger."""
-import threading
 import time
 from fastapi import APIRouter, Depends, HTTPException
 from pydantic import BaseModel
@@ -99,8 +98,10 @@ def scheduler_trigger(reg: ServiceRegistry = Depends(get_registry)):
         if _scan_state["state"] == "running":
             raise HTTPException(status_code=409, detail="Scan already in progress")
         _scan_state["state"] = "running"
-        threading.Thread(
-            target=_run_scan, args=(reg, req), name="scheduled-scan", daemon=True
-        ).start()
+        # Registry-owned, exactly as /scan/start does it — this is the same
+        # _run_scan under the same lock and state, so it needs the same
+        # lifespan-bounded lifetime.
+        reg.spawn_lifespan_thread(
+            _run_scan, args=(reg, req), name="scheduled-scan")
 
     return {"status": "triggered"}
diff --git a/backend/app_service.py b/backend/app_service.py
index 8a0c332..1cd8ca3 100644
--- a/backend/app_service.py
+++ b/backend/app_service.py
@@ -702,7 +702,8 @@ class AppService:
                                     "initialized — skipping this pass")
                     else:
                         result = dv_labeler.sync_labels(
-                            self.db, pm, self.config, additive_only=True)
+                            self.db, pm, self.config, additive_only=True,
+                            stop_requested=self._maintenance_stop.is_set)
                         logger.info(
                             "DV auto-sync: %d matched, %d label(s) added "
                             "(additive-only)",
diff --git a/backend/notification_bridge.py b/backend/notification_bridge.py
index f088d45..6549086 100644
--- a/backend/notification_bridge.py
+++ b/backend/notification_bridge.py
@@ -144,6 +144,30 @@ class NotificationBridge:
     def shutdown(self):
         """Stop the async loop and cleanup."""
         if self._loop:
+            # Drain the loop's DEFAULT executor before stopping it. Desktop
+            # toasts are dispatched with ``run_in_executor(None, ...)``, which
+            # lazily creates a pool of ``asyncio_N`` worker threads; stopping
+            # the loop leaves them alive for the whole process, so under
+            # TestClient they piled up one lifespan after another.
+            #
+            # Both waits are bounded: this runs on the shutdown path, and a
+            # notification backend wedged in a native OS call must not be able
+            # to hold it open. On expiry we stop the loop regardless — the
+            # executor threads are daemons.
+            # Only if the loop is actually running: a coroutine submitted to a
+            # stopped loop is never executed, so the wait below would burn its
+            # full timeout on every shutdown that follows a crashed loop.
+            if self._loop.is_running():
+                try:
+                    pending = asyncio.run_coroutine_threadsafe(
+                        self._loop.shutdown_default_executor(timeout=2.0),
+                        self._loop,
+                    )
+                    pending.result(timeout=3.0)
+                except Exception:
+                    logger.debug("notification executor shutdown timed out or "
+                                 "failed; stopping the loop anyway",
+                                 exc_info=True)
             self._loop.call_soon_threadsafe(self._loop.stop)
         if self._thread:
             self._thread.join(timeout=2.0)
diff --git a/backend/plex_metadata_scan.py b/backend/plex_metadata_scan.py
index 07a7e9d..6e3e2e8 100644
--- a/backend/plex_metadata_scan.py
+++ b/backend/plex_metadata_scan.py
@@ -32,9 +32,14 @@ class PlexMetadataScanJob:
     endpoints always observe the current run; a new start() call is rejected
     while a previous run is still "running"."""
 
-    def __init__(self, db, progress_cb: Optional[Callable[[dict], None]] = None):
+    def __init__(self, db, progress_cb: Optional[Callable[[dict], None]] = None,
+                 registry=None):
         self._db = db
         self._progress_cb = progress_cb
+        # Optional so ad-hoc/test construction still works. When present, worker
+        # threads are owned by the app lifespan and joined at shutdown instead
+        # of running on into the next one.
+        self._registry = registry
         self._lock = threading.Lock()
         self._stop_flag = False
         self._stop_mode: Optional[str] = None
@@ -70,9 +75,7 @@ class PlexMetadataScanJob:
             self.started_at = time.time()
             self.error = None
         self._emit()
-        threading.Thread(
-            target=self._run, args=(targets,),
-            name="plex-metadata-scan", daemon=True).start()
+        self._spawn(self._run, (targets,), "plex-metadata-scan")
         return True
 
     def start_run(self, scope: str, targets: list[dict]) -> dict:
@@ -104,10 +107,8 @@ class PlexMetadataScanJob:
             self.started_at = time.time()
             self.error = None
         self._emit()
-        threading.Thread(
-            target=self._run_durable, args=(run["run_uuid"],),
-            name="plex-metadata-inventory", daemon=True,
-        ).start()
+        self._spawn(self._run_durable, (run["run_uuid"],),
+                    "plex-metadata-inventory")
         return self._db.get_metadata_scan_run(run["run_uuid"])
 
     def pause(self, run_uuid: Optional[str] = None) -> dict:
@@ -151,10 +152,8 @@ class PlexMetadataScanJob:
             self.started_at = time.time()
             self.error = None
         self._emit()
-        threading.Thread(
-            target=self._run_durable, args=(run_uuid,),
-            name="plex-metadata-inventory", daemon=True,
-        ).start()
+        self._spawn(self._run_durable, (run_uuid,),
+                    "plex-metadata-inventory")
         return self._db.get_metadata_scan_run(run_uuid)
 
     def retry_failures(self, run_uuid: str) -> dict:
@@ -187,6 +186,20 @@ class PlexMetadataScanJob:
         except Exception:
             logger.exception("plex-metadata-scan progress callback failed")
 
+    def _spawn(self, target, args: tuple, name: str) -> None:
+        """Start a worker, lifespan-owned when a registry is available.
+
+        Shutdown pauses an active run (see backend.api.main.
+        _teardown_services), so the worker reaches its _stop_flag check and
+        the join costs a moment rather than the whole budget. The run is left
+        persisted as "paused", which resume() picks back up.
+        """
+        if self._registry is not None:
+            self._registry.spawn_lifespan_thread(target, args=args, name=name)
+            return
+        threading.Thread(target=target, args=args, name=name,
+                         daemon=True).start()
+
     def _cancel_requested(self) -> bool:
         with self._lock:
             return bool(self._stop_flag)
diff --git a/backend/rename/dv_labeler.py b/backend/rename/dv_labeler.py
index a25f116..c413abc 100644
--- a/backend/rename/dv_labeler.py
+++ b/backend/rename/dv_labeler.py
@@ -144,12 +144,17 @@ def _vocab_from_config(config):
 
 
 def sync_labels(db, pm, config, *, dry_run=False, progress_cb=None, mappings=None,
-                additive_only=False):
+                additive_only=False, stop_requested=None):
     """Reconcile every movie against dv_scan (source='scan'). Returns a summary.
 
     ``additive_only`` never removes labels from an unmatched movie — see
     reconcile_movie. The scheduled auto-sync passes it; the manual button does
     not.
+
+    ``stop_requested`` is polled once per movie so app shutdown can end a
+    long library walk at an item boundary. Every movie already reconciled
+    has been written, so a short run is a partial sync, not a corrupt one,
+    and the next pass simply picks up the rest.
     """
     vocab = _vocab_from_config(config)
     rows = db.get_dv_scans(source="scan", limit=1000000)
@@ -184,6 +189,9 @@ def sync_labels(db, pm, config, *, dry_run=False, progress_cb=None, mappings=Non
     added_n = removed_n = matched_n = 0
     details = []
     for i, mv in enumerate(movies):
+        if stop_requested is not None and stop_requested():
+            logger.info("dv sync: stopping at %d/%d on request", i, total)
+            break
         try:
             res = reconcile_movie(mv, index, vocab, pm,
                                   dry_run=dry_run, mappings=mappings,
diff --git a/backend/rename/service.py b/backend/rename/service.py
index c3ec73f..24ea6b2 100644
--- a/backend/rename/service.py
+++ b/backend/rename/service.py
@@ -690,6 +690,18 @@ class RenameService:
         with self._inflight_lock:
             self._inflight.discard(path)
 
+    def _shutdown_requested(self) -> bool:
+        """Whether the app lifespan is tearing down — bulk loops poll this.
+
+        These bulk background runs are joined by lifespan teardown within a
+        shared budget. Without a check like this the join would simply burn
+        that budget and log a straggler every time one is in flight, so each
+        loop stops at its next item boundary instead. Partial results are
+        already the contract here (every method reports counts, and each item
+        it did finish is persisted before the next begins).
+        """
+        return bool(getattr(self._reg, "shutdown_requested", False))
+
     def process_folder(self, folder: str, dry_run: bool = False) -> dict:
         """Manually scan a folder for video files and create rename jobs for any
         not already tracked — for processing an existing download backlog (no JD
@@ -718,6 +730,8 @@ class RenameService:
             files = self._video_files(resolved)
             created, skipped, failed_db = [], 0, 0
             for path in files:
+                if self._shutdown_requested():
+                    break
                 if not self._claim_path(path):
                     skipped += 1
                     continue
@@ -756,6 +770,8 @@ class RenameService:
         files = self._video_files(resolved)
         previews = []
         for path in files:
+            if self._shutdown_requested():
+                break
             filename = os.path.basename(path)
             tracked = bool(db and db.path_has_rename_job(path))
             try:
@@ -819,6 +835,8 @@ class RenameService:
             scanned, skipped = 0, 0
             by_layer: dict = {}
             for i, path in enumerate(files):
+                if self._shutdown_requested():
+                    break
                 # Skip-check is itself fail-safe: a stat error just means "scan it".
                 try:
                     st = os.stat(path)
@@ -965,6 +983,8 @@ class RenameService:
                     if j.get("status") in ("needs_review", "failed")]
             count = 0
             for job in jobs:
+                if self._shutdown_requested():
+                    break
                 try:
                     self.reidentify(job["id"])
                     count += 1
@@ -2252,9 +2272,18 @@ class RenameService:
                     # apply run behind a permanently-held lock.
                     self._bulk_lock.release()
 
-            t = threading.Thread(target=_worker, args=(eligible,), daemon=True,
-                                 name="rename-apply")
-            t.start()
+            # Lifespan-owned when the registry supports it, so shutdown joins
+            # this worker instead of letting it apply files into the next
+            # lifespan. _teardown_services calls cancel_apply() first, which is
+            # the flag the loop already checks each iteration, so it stops after
+            # the file currently in flight rather than mid-move. getattr because
+            # tests construct RenameService with a minimal registry stub.
+            spawn = getattr(self._reg, "spawn_lifespan_thread", None)
+            if spawn is not None:
+                spawn(_worker, args=(eligible,), name="rename-apply")
+            else:
+                threading.Thread(target=_worker, args=(eligible,), daemon=True,
+                                 name="rename-apply").start()
             # The worker now owns the lock; don't release it in our finally.
             release_lock = False
             return {"ok": True, "queued": len(eligible), "skipped": skipped}
diff --git a/backend/scanner_service.py b/backend/scanner_service.py
index d4c9437..1761161 100644
--- a/backend/scanner_service.py
+++ b/backend/scanner_service.py
@@ -371,6 +371,23 @@ class ScannerService:
                                         track_urls, skip_urls, early_stop)
                 )
             finally:
+                # Shut the loop's DEFAULT executor down before closing it.
+                # Every source adapter fetches pages via
+                # ``loop.run_in_executor(None, ...)``, which lazily creates a
+                # ThreadPoolExecutor of ``asyncio_N`` threads that loop.close()
+                # does NOT touch — so each scan used to strand a pool of idle
+                # worker threads for the life of the process.
+                #
+                # Bounded: a fetch still blocked on a socket must not hold the
+                # scan thread (and therefore lifespan shutdown) open forever.
+                # On expiry Python logs the still-running threads and we close
+                # regardless; they are daemons.
+                try:
+                    loop.run_until_complete(
+                        loop.shutdown_default_executor(timeout=5))
+                except Exception:
+                    logger.debug("default executor shutdown failed",
+                                 exc_info=True)
                 loop.close()
         except Exception as e:
             self._log(f"Scan error: {e}", "error")
diff --git a/tests/test_api_lifecycle.py b/tests/test_api_lifecycle.py
index 57322fb..969addf 100644
--- a/tests/test_api_lifecycle.py
+++ b/tests/test_api_lifecycle.py
@@ -1,6 +1,8 @@
 """Regression tests for repeated API lifespans and registry ownership."""
 
 import threading
+import time
+from unittest.mock import patch
 
 import pytest
 from fastapi.testclient import TestClient
@@ -234,3 +236,275 @@ def test_late_background_worker_cannot_publish_into_next_lifespan():
     # These assertions are expected to FAIL on the current PR #17 head.
     assert old_db.late_writes == []
     assert reg.config == {"new_lifespan": True}
+
+
+# ── Background-thread ownership (lifespan shutdown joins what it started) ──
+
+
+def test_lifespan_threads_are_joined_by_teardown():
+    """The registry's own workers must not outlive the lifespan that made them.
+
+    They are all daemon=True, so before teardown joined them the only thing that
+    ended them was interpreter exit — under TestClient (many lifespans, one
+    process) they accumulated and kept touching process globals during later
+    tests.
+    """
+    reg = ServiceRegistry()
+    reg.begin_lifespan()
+    started = threading.Event()
+
+    def _worker():
+        started.set()
+        # Exactly how a well-behaved worker waits: interruptible, so the join
+        # below costs microseconds rather than the full nominal interval.
+        reg.wait_for_shutdown(300)
+
+    thread = reg.spawn_lifespan_thread(_worker, name="join-me")
+    assert started.wait(timeout=2), "worker never started"
+
+    _teardown_services(reg)
+
+    assert not thread.is_alive(), "teardown returned with the worker still live"
+
+
+def test_teardown_does_not_hang_on_a_wedged_worker(caplog, monkeypatch):
+    """A worker stuck in an uninterruptible call must not block shutdown.
+
+    This is the whole reason every join is bounded: a background thread blocked
+    on a socket read has no way to observe the shutdown flag, so an unbounded
+    join would wedge the app's shutdown behind an unreachable server.
+    """
+    import backend.api.main as api_main
+
+    # Shrink the production budget so the test spends 0.3s, not 5s, proving it.
+    monkeypatch.setattr(api_main, "LIFESPAN_JOIN_BUDGET_SECONDS", 0.3)
+
+    reg = ServiceRegistry()
+    reg.begin_lifespan()
+    wedged = threading.Event()
+    release = threading.Event()
+
+    def _wedged_worker():
+        wedged.set()
+        release.wait(timeout=30)  # stands in for a socket read that never returns
+
+    thread = reg.spawn_lifespan_thread(_wedged_worker, name="wedged-worker")
+    assert wedged.wait(timeout=2), "worker never reached the blocking seam"
+
+    try:
+        with caplog.at_level("WARNING"):
+            started = time.monotonic()
+            _teardown_services(reg)
+            elapsed = time.monotonic() - started
+
+        # Bounded: the budget, plus slack for the service stop() calls around it.
+        assert elapsed < 3.0, f"teardown blocked for {elapsed:.1f}s"
+        assert thread.is_alive(), "worker was supposed to still be wedged"
+        # And it is not silent — the straggler is named in the log.
+        warnings = [r.getMessage() for r in caplog.records
+                    if r.levelname == "WARNING"]
+        assert any("wedged-worker" in m for m in warnings), warnings
+    finally:
+        release.set()
+        thread.join(timeout=5)
+
+
+def test_join_budget_is_shared_across_threads_not_per_thread():
+    """N wedged workers must cost the same as one, or shutdown scales with them."""
+    reg = ServiceRegistry()
+    reg.begin_lifespan()
+    release = threading.Event()
+    ready = []
+
+    for i in range(5):
+        started = threading.Event()
+        ready.append(started)
+
+        def _wedged(ev=started):
+            ev.set()
+            release.wait(timeout=30)
+
+        reg.spawn_lifespan_thread(_wedged, name=f"wedged-{i}")
+    for ev in ready:
+        assert ev.wait(timeout=2)
+
+    try:
+        began = time.monotonic()
+        stragglers = reg.join_lifespan_threads(timeout=0.3)
+        elapsed = time.monotonic() - began
+
+        # 5 threads x 0.3s per-thread would be 1.5s; one shared budget is ~0.3s.
+        assert elapsed < 1.0, f"budget applied per-thread: {elapsed:.1f}s"
+        assert len(stragglers) == 5, stragglers
+    finally:
+        release.set()
+
+
+def test_poster_backfill_settle_delay_is_interruptible():
+    """The 30s settle delay must be a shutdown-aware wait, not time.sleep.
+
+    A plain sleep makes the thread unjoinable for its full duration, so every
+    shutdown in the first 30 seconds of a lifespan either stalled on it or
+    leaked it.
+    """
+    reg = ServiceRegistry()
+    reg.begin_lifespan()
+    entered = threading.Event()
+    finished = threading.Event()
+
+    def _settles():
+        entered.set()
+        if reg.wait_for_shutdown(30):
+            finished.set()
+            return
+        raise AssertionError("settle delay was not interrupted by shutdown")
+
+    thread = reg.spawn_lifespan_thread(_settles, name="poster-backfill")
+    assert entered.wait(timeout=2)
+
+    began = time.monotonic()
+    reg.request_shutdown()
+    thread.join(timeout=5)
+    elapsed = time.monotonic() - began
+
+    assert finished.is_set(), "worker did not observe the shutdown signal"
+    assert elapsed < 2.0, f"waited {elapsed:.1f}s for an interruptible sleep"
+
+
+def test_registry_threads_do_not_accumulate_across_a_long_lifespan():
+    """Finished threads are reaped, or a weeks-long lifespan grows a dead list."""
+    reg = ServiceRegistry()
+    reg.begin_lifespan()
+
+    for _ in range(20):
+        thread = reg.spawn_lifespan_thread(lambda: None, name="short-lived")
+        thread.join(timeout=2)
+
+    reg.spawn_lifespan_thread(lambda: None, name="last")
+    assert len(reg._lifespan_threads) <= 2, len(reg._lifespan_threads)
+
+
+def test_spawn_is_atomic_so_a_join_cannot_see_a_half_spawned_thread():
+    """Tracking and start() must happen under one lock hold.
+
+    A constructed-but-not-started thread reports is_alive() == False exactly
+    like a finished one, so between "track" and "start" the entry is a trap: a
+    concurrent register() reaps it as dead (the worker then runs untracked),
+    and a concurrent join() calls Thread.join() on it — RuntimeError, which
+    aborts the rest of the shutdown.
+
+    Driven by a stalled start() rather than by racing threads and hoping: the
+    window is a few instructions wide, and a 24-way concurrent-spawn version of
+    this test passed against the unlocked implementation on every run.
+    """
+    reg = ServiceRegistry()
+    reg.begin_lifespan()
+
+    inside_start = threading.Event()
+    let_start_proceed = threading.Event()
+    real_start = threading.Thread.start
+
+    def stalled_start(self):
+        # Stall only the thread under test, never pytest's own machinery.
+        if self.name == "half-spawned":
+            inside_start.set()
+            assert let_start_proceed.wait(timeout=10)
+        real_start(self)
+
+    observed = {}
+
+    def _joiner():
+        try:
+            observed["stragglers"] = reg.join_lifespan_threads(timeout=0.1)
+        except BaseException as exc:      # RuntimeError on the unlocked version
+            observed["exception"] = exc
+
+    with patch.object(threading.Thread, "start", stalled_start):
+        spawner = threading.Thread(
+            target=reg.spawn_lifespan_thread, args=(lambda: None,),
+            kwargs={"name": "half-spawned"}, name="spawner")
+        spawner.start()
+        assert inside_start.wait(timeout=5), "spawn never reached start()"
+
+        joiner = threading.Thread(target=_joiner, name="joiner")
+        joiner.start()
+        time.sleep(0.3)  # ample time for the joiner to finish, if it can run
+
+        # The whole point: the joiner is still blocked on the lock. Without the
+        # lock held across start() it would have sailed through and tripped
+        # over the unstarted thread.
+        raced = dict(observed)
+
+        # Released in a finally so a failure here reports its own message
+        # instead of stalling the spawner for the full 10s stall timeout.
+        let_start_proceed.set()
+        spawner.join(timeout=5)
+        joiner.join(timeout=5)
+
+    assert not raced, f"join observed a half-spawned thread: {raced}"
+
+    assert "exception" not in observed, repr(observed.get("exception"))
+    assert observed.get("stragglers") == [], observed
+
+
+def test_bulk_rename_loops_stop_on_shutdown():
+    """RenameService's bulk loops must poll the shutdown flag, not just be joinable.
+
+    Registering these workers without this is a net loss: teardown would wait
+    out the whole join budget and log a straggler every time one is in flight.
+    """
+    from backend.rename.service import RenameService
+
+    reg = ServiceRegistry()
+    reg.begin_lifespan()
+    svc = RenameService.__new__(RenameService)   # no DB/config needed for this
+    svc._reg = reg
+
+    assert svc._shutdown_requested() is False
+    reg.request_shutdown()
+    assert svc._shutdown_requested() is True
+
+
+def test_dv_sync_labels_stops_at_an_item_boundary():
+    """The Plex library walk takes a stop hook, and honours it mid-list."""
+    from backend.rename import dv_labeler
+
+    reconciled = []
+
+    class _Movie:
+        def __init__(self, key):
+            self.ratingKey = key
+
+    class _Lib:
+        def all(self):
+            return [_Movie(i) for i in range(10)]
+
+    class _PM:
+        def get_library_section(self, _name):
+            return _Lib()
+
+    class _Db:
+        def get_dv_scans(self, **_kw):
+            return []
+
+        def upsert_dv_scan(self, *a, **k):
+            pass
+
+    stop_after = 3
+
+    def _fake_reconcile(mv, *a, **k):
+        reconciled.append(mv.ratingKey)
+        return {"added": [], "removed": [], "matched": False}
+
+    original = dv_labeler.reconcile_movie
+    dv_labeler.reconcile_movie = _fake_reconcile
+    try:
+        result = dv_labeler.sync_labels(
+            _Db(), _PM(), {"movie_libs": ["Movies"]},
+            stop_requested=lambda: len(reconciled) >= stop_after)
+    finally:
+        dv_labeler.reconcile_movie = original
+
+    # Stopped early rather than walking all 10, and still returned a summary.
+    assert len(reconciled) == stop_after, reconciled
+    assert isinstance(result, dict)
diff --git a/tests/test_api_rename.py b/tests/test_api_rename.py
index 6ec2987..f6ed69e 100644
--- a/tests/test_api_rename.py
+++ b/tests/test_api_rename.py
@@ -197,15 +197,15 @@ class TestRenameApi:
         # must release the just-reserved ids itself in that except branch, or
         # those job ids stay pinned "in flight" forever (never re-analyzed).
         import backend.api.routes.rename as rename_routes
+        from backend.api.dependencies import registry
 
-        class _BoomThread:
-            def __init__(self, *a, **k):
-                pass
-
-            def start(self):
-                raise RuntimeError("can't start new thread")
+        def _boom(*_a, **_k):
+            raise RuntimeError("can't start new thread")
 
-        monkeypatch.setattr(rename_routes.threading, "Thread", _BoomThread)
+        # The route spawns through the registry now (so teardown joins the
+        # thread); that is the call that has to raise for this to exercise
+        # the except-RuntimeError branch.
+        monkeypatch.setattr(registry, "spawn_lifespan_thread", _boom)
         dest, name = "/lib/movies", "Dup (2020) [2160p].mkv"
         _seed_job(status="matched", title="Dup", destination_path=dest, new_filename=name)
         _seed_job(status="matched", title="Dup", destination_path=dest, new_filename=name)
@@ -256,14 +256,12 @@ class TestRenameApi:
             events.append,
         )
 
-        class _ImmediateThread:
-            def __init__(self, target, *args, **kwargs):
-                self._target = target
+        from backend.api.dependencies import registry
 
-            def start(self):
-                self._target()
+        def _run_inline(target, *, name, args=(), kwargs=None):
+            target(*args, **(kwargs or {}))
 
-        monkeypatch.setattr(rename_routes.threading, "Thread", _ImmediateThread)
+        monkeypatch.setattr(registry, "spawn_lifespan_thread", _run_inline)
 
         response = client.post(
             "/rename/dv-scan-folder",
@@ -774,16 +772,14 @@ class TestPathConfinement:
         root.mkdir()
         events = []
 
-        class _ImmediateThread:
-            def __init__(self, target, *args, **kwargs):
-                self._target = target
+        from backend.api.dependencies import registry
 
-            def start(self):
-                self._target()
+        def _run_inline(target, *, name, args=(), kwargs=None):
+            target(*args, **(kwargs or {}))
 
-        # TestClient must create its AnyIO portal with the real stdlib Thread.
-        # Scope the synchronous route-thread replacement inside the live client
-        # context, and restore it before TestClient begins shutdown.
+        # Patching the registry's spawn (rather than threading.Thread) keeps
+        # this scoped to the route: TestClient's own AnyIO portal thread is
+        # unaffected. Still restored before shutdown, which joins real threads.
         with _client_with_library(str(root)) as client:
             with monkeypatch.context() as scoped_patch:
                 scoped_patch.setattr(
@@ -801,9 +797,9 @@ class TestPathConfinement:
                     events.append,
                 )
                 scoped_patch.setattr(
-                    rename_routes.threading,
-                    "Thread",
-                    _ImmediateThread,
+                    registry,
+                    "spawn_lifespan_thread",
+                    _run_inline,
                 )
 
                 response = client.post(
diff --git a/tests/test_api_routes.py b/tests/test_api_routes.py
index a1d62cb..09c17d7 100644
--- a/tests/test_api_routes.py
+++ b/tests/test_api_routes.py
@@ -680,26 +680,32 @@ class TestDownloads:
             "interval_seconds": 600,
             "items": items,
         }
+        # Keep the real service: lifespan teardown stops the queue through this
+        # very field, so leaving the mock in place would orphan the live worker
+        # + watchdog threads past the end of the test.
+        real_queue = registry._download_queue_service
         registry._download_queue_service = mock_queue
+        try:
+            resp = client.post("/download/batch", json={"items": items})
 
-        resp = client.post("/download/batch", json={"items": items})
-
-        assert resp.status_code == 200
-        body = resp.json()
-        # Response fields are mapped from the scheduled batch.
-        assert body["status"] == "scheduled"
-        assert body["count"] == 2
-        assert body["batch_uuid"] == "batch-test-uuid"
-        assert body["mode"] == "staggered"
-        assert body["interval_minutes"] == 10          # 600s / 60
-        assert body["items"] == items
-        # The route delegated to the durable queue once, with the default
-        # interval (download_batch_interval_minutes=10) and staggered mode.
-        mock_queue.schedule_batch.assert_called_once()
-        args, kwargs = mock_queue.schedule_batch.call_args
-        assert [i["url"] for i in args[0]] == [i["url"] for i in items]
-        assert kwargs["interval_minutes"] == 10
-        assert kwargs["mode"] == "staggered"
+            assert resp.status_code == 200
+            body = resp.json()
+            # Response fields are mapped from the scheduled batch.
+            assert body["status"] == "scheduled"
+            assert body["count"] == 2
+            assert body["batch_uuid"] == "batch-test-uuid"
+            assert body["mode"] == "staggered"
+            assert body["interval_minutes"] == 10          # 600s / 60
+            assert body["items"] == items
+            # The route delegated to the durable queue once, with the default
+            # interval (download_batch_interval_minutes=10) and staggered mode.
+            mock_queue.schedule_batch.assert_called_once()
+            args, kwargs = mock_queue.schedule_batch.call_args
+            assert [i["url"] for i in args[0]] == [i["url"] for i in items]
+            assert kwargs["interval_minutes"] == 10
+            assert kwargs["mode"] == "staggered"
+        finally:
+            registry._download_queue_service = real_queue
 
     def test_download_batch_empty_items(self, client):
         resp = client.post("/download/batch", json={"items": []})
@@ -1643,10 +1649,17 @@ class TestScheduler:
 
     def test_config_no_backend_still_updates_dict(self, client):
         """Config dict is updated even if backend is None (save_config skipped)."""
+        # Restored before the fixture exits: lifespan teardown reaches the
+        # AppService through registry.backend, so leaving it None would strand
+        # the maintenance thread that startup began.
+        real_backend = registry.backend
         registry.backend = None
-        resp = client.put("/scheduler/config", json={"enabled": True})
-        assert resp.status_code == 200
-        assert registry.config["scheduler_enabled"] is True
+        try:
+            resp = client.put("/scheduler/config", json={"enabled": True})
+            assert resp.status_code == 200
+            assert registry.config["scheduler_enabled"] is True
+        finally:
+            registry.backend = real_backend
 
     def test_trigger_no_scanner(self, client):
         """POST /scheduler/trigger returns 503 when scanner not initialized."""
@@ -1672,17 +1685,19 @@ class TestScheduler:
         mock_scanner.is_scanning = False
         mock_scanner.scan_in_progress = False
         registry._scanner_service = mock_scanner
-        with patch("backend.api.routes.scheduler.threading.Thread") as mock_thread:
-            mock_thread.return_value.start = MagicMock()
+        # The scan thread is registry-owned now — that is what lets lifespan
+        # teardown join it — so intercept the spawn there rather than at
+        # threading.Thread.
+        with patch.object(registry, "spawn_lifespan_thread") as mock_spawn:
             resp = client.post("/scheduler/trigger")
         assert resp.status_code == 200
         assert resp.json()["status"] == "triggered"
         # Verify the thread target is the scanner route's _run_scan
-        mock_thread.assert_called_once()
-        call_kwargs = mock_thread.call_args
-        assert call_kwargs.kwargs["target"] is _run_scan
+        mock_spawn.assert_called_once()
+        call = mock_spawn.call_args
+        assert call.args[0] is _run_scan
         # Verify the request is an incremental scan
-        req_arg = call_kwargs.kwargs["args"][1]
+        req_arg = call.kwargs["args"][1]
         assert isinstance(req_arg, ScanRequest)
         assert req_arg.type == "incremental"
 
diff --git a/tests/test_dv_settings.py b/tests/test_dv_settings.py
index 6dca964..88f314d 100644
--- a/tests/test_dv_settings.py
+++ b/tests/test_dv_settings.py
@@ -31,6 +31,30 @@ def client():
         yield c
 
 
+@pytest.fixture
+def stub_backend(client):
+    """Swap in a save_config no-op backend, then put the real one back.
+
+    Restoring is the point: lifespan teardown stops the AppService — and
+    joins its maintenance thread — THROUGH registry.backend, so a test that
+    leaves a stub in that slot strands the real thread for the rest of the
+    session. Depends on ``client`` so this finalizer runs BEFORE the
+    TestClient exits and shutdown reads the field.
+    """
+    class _Backend:
+        _cleared_keys = set()
+
+        def save_config(self):  # no-op; config isolated by conftest
+            pass
+
+    real = registry.backend
+    registry.backend = _Backend()
+    try:
+        yield
+    finally:
+        registry.backend = real
+
+
 def test_settings_model_accepts_dv_keys_and_4k():
     m = SettingsUpdate(
         dv_library_roots="Y:\\Movies;E:\\4K",
@@ -109,16 +133,9 @@ def test_settings_model_accepts_plex_library_path_mappings():
     assert dumped["plex_library_path_mappings"] == "A: => /library/plex-source/a"
 
 
-def test_put_settings_accepts_plex_library_path_mappings(client):
-    from backend.api.dependencies import registry
+def test_put_settings_accepts_plex_library_path_mappings(client, stub_backend):
     registry.config = {}
 
-    class _Backend:
-        _cleared_keys = set()
-        def save_config(self):  # no-op; config isolated by conftest
-            pass
-    registry.backend = _Backend()
-
     payload = {"plex_library_path_mappings": "A: => /library/plex-source/a"}
     r = client.put("/settings", json=payload)
     assert r.status_code == 200, r.text
@@ -126,16 +143,9 @@ def test_put_settings_accepts_plex_library_path_mappings(client):
     assert registry.config["plex_library_path_mappings"] == "A: => /library/plex-source/a"
 
 
-def test_put_settings_round_trips_dv_and_4k(client):
-    from backend.api.dependencies import registry
+def test_put_settings_round_trips_dv_and_4k(client, stub_backend):
     registry.config = {}
 
-    class _Backend:
-        _cleared_keys = set()
-        def save_config(self):  # no-op; config isolated by conftest
-            pass
-    registry.backend = _Backend()
-
     payload = {
         "dv_library_roots": "Y:\\M",
         "dv_detection": True,
@@ -156,16 +166,9 @@ def test_settings_model_accepts_pipeline_keys():
     assert upd.pipeline_verify_grace_margin_minutes == 45
 
 
-def test_put_settings_round_trips_pipeline_keys(client):
-    from backend.api.dependencies import registry
+def test_put_settings_round_trips_pipeline_keys(client, stub_backend):
     registry.config = {}
 
-    class _Backend:
-        _cleared_keys = set()
-        def save_config(self):  # no-op; config isolated by conftest
-            pass
-    registry.backend = _Backend()
-
     resp = client.put("/settings", json={"pipeline_reconcile_enabled": False,
                                          "pipeline_verify_grace_margin_minutes": 45})
     assert resp.status_code == 200
@@ -179,16 +182,9 @@ def test_settings_model_accepts_rename_detect_moved_files_enabled():
     assert upd.rename_detect_moved_files_enabled is False
 
 
-def test_put_settings_round_trips_rename_detect_moved_files_enabled(client):
-    from backend.api.dependencies import registry
+def test_put_settings_round_trips_rename_detect_moved_files_enabled(client, stub_backend):
     registry.config = {}
 
-    class _Backend:
-        _cleared_keys = set()
-        def save_config(self):  # no-op; config isolated by conftest
-            pass
-    registry.backend = _Backend()
-
     resp = client.put("/settings", json={"rename_detect_moved_files_enabled": False})
     assert resp.status_code == 200
     got = client.get("/settings").json()
diff --git a/tests/test_public_error_boundary.py b/tests/test_public_error_boundary.py
index de3010c..d7f4d54 100644
--- a/tests/test_public_error_boundary.py
+++ b/tests/test_public_error_boundary.py
@@ -77,12 +77,6 @@ def test_http_exception_detail_does_not_expose_raw_exception():
 def test_process_folder_background_route_closes_exception(monkeypatch, tmp_path):
     notifications = []
 
-    class ImmediateThread:
-        def __init__(self, *, target, **_kwargs):
-            self.target = target
-        def start(self):
-            self.target()
-
     class FailingService:
         def _translate_path(self, folder):
             return folder
@@ -90,16 +84,27 @@ def test_process_folder_background_route_closes_exception(monkeypatch, tmp_path)
             raise RuntimeError(_SENTINEL)
 
     monkeypatch.setattr(rename, "_service", lambda _reg: FailingService())
-    monkeypatch.setattr(rename.threading, "Thread", ImmediateThread)
     monkeypatch.setattr(rename.ws_manager, "broadcast_sync", notifications.append)
 
     root = tmp_path / "library"
     root.mkdir()
-    reg = SimpleNamespace(config={
-        "auto_rename_movie_library": str(root),
-        "auto_rename_movie_library_4k": "",
-        "auto_rename_tv_library": "",
-    })
+    def _run_inline(target, *, name, args=(), kwargs=None):
+        """Stand in for ServiceRegistry.spawn_lifespan_thread.
+
+        The route hands its background work to the registry now, so the
+        stub needs that method; running it inline is what lets this test
+        inspect the notification the worker broadcasts.
+        """
+        target(*args, **(kwargs or {}))
+
+    reg = SimpleNamespace(
+        config={
+            "auto_rename_movie_library": str(root),
+            "auto_rename_movie_library_4k": "",
+            "auto_rename_tv_library": "",
+        },
+        spawn_lifespan_thread=_run_inline,
+    )
     result = rename.process_folder(
         rename.ProcessFolderRequest(folder=str(root), dry_run=False),
         reg,
diff --git a/tests/test_rss_routes.py b/tests/test_rss_routes.py
index c7f2560..cebec19 100644
--- a/tests/test_rss_routes.py
+++ b/tests/test_rss_routes.py
@@ -113,6 +113,16 @@ class Registry:
     def owns_lifespan(self, generation):
         return self._owns and generation == self.lifespan_generation
 
+    def spawn_lifespan_thread(self, target, *, name, args=(), kwargs=None):
+        """Run the worker inline so its behaviour is observable here.
+
+        The real ServiceRegistry starts a tracked daemon thread that
+        lifespan teardown joins; running it synchronously is what lets this
+        test watch stop_requested() flip as ownership is lost.
+        """
+        target(*args, **(kwargs or {}))
+        return SimpleNamespace(name=name, is_alive=lambda: False)
+
 
 def test_status_reports_readiness_unknowns_and_safe_defaults():
     result = rss.rss_status(Registry())
@@ -192,15 +202,9 @@ def test_explicit_hydration_uses_captured_lifespan_generation(monkeypatch):
                 "cancelled": 0,
             }
 
-    class ImmediateThread:
-        def __init__(self, *, target, **_kwargs):
-            self.target = target
-
-        def start(self):
-            self.target()
-
+    # The spawn itself is intercepted by Registry.spawn_lifespan_thread above,
+    # which runs the worker inline.
     monkeypatch.setattr(rss, "HDEncodeCandidateService", CandidateService)
-    monkeypatch.setattr(rss.threading, "Thread", ImmediateThread)
 
     result = rss.hydrate_candidate(
         rss.CandidateRequest(
diff --git a/tests/tools/threadleak.py b/tests/tools/threadleak.py
new file mode 100644
index 0000000..de2f9d6
--- /dev/null
+++ b/tests/tools/threadleak.py
@@ -0,0 +1,61 @@
+"""Pytest plugin: report which tests leave live threads behind.
+
+The flake under investigation fails intermittently, but the LEAK that causes it
+is deterministic — a thread either survives a test or it does not. So find the
+leak, not the flake.
+
+Loaded with `-p threadleak`; the module itself is the plugin, so the hooks are
+module-level functions rather than a separately registered class. It needs to be
+importable, so put tests/tools on the path:
+
+    PYTHONPATH=tests/tools python -m pytest tests/ -q -p threadleak
+
+THE TEARDOWN HOOK MUST BE A WRAPPER. A plain `pytest_runtest_teardown` impl
+runs BEFORE `_pytest.runner`'s — pluggy calls hookimpls last-registered-first,
+and `-p threadleak` registers after the core plugins — so it samples while the
+test's fixtures are still open. That measures "threads alive during the test",
+not "threads that outlived it": every `with TestClient(app)` fixture then looks
+like a leak, including TestClient's own portal and AnyIO worker threads, which
+its `__exit__` reliably cleans up. Measured 2026-08-01: sampling early reported
+223 leaking tests in test_api_routes.py both before AND after the lifespan was
+fixed to join its background threads — an instrument that could not see the
+change it existed to measure. Sampling after finalization reported 223 -> 1.
+"""
+import threading
+
+import pytest
+
+_BASELINE = {}
+_LEAKS = []
+
+
+def _snapshot():
+    return {t.ident: t for t in threading.enumerate()
+            if t is not threading.main_thread()}
+
+
+def pytest_runtest_setup(item):
+    _BASELINE.clear()
+    _BASELINE.update(_snapshot())
+
+
+@pytest.hookimpl(hookwrapper=True)
+def pytest_runtest_teardown(item, nextitem):
+    yield  # let the runner finalize this test's fixtures first
+    new = {i: t for i, t in _snapshot().items()
+           if i not in _BASELINE and t.is_alive()}
+    if new:
+        _LEAKS.append((item.nodeid,
+                       [f"{t.name}(daemon={t.daemon})" for t in new.values()]))
+
+
+def pytest_terminal_summary(terminalreporter):
+    terminalreporter.write_line("")
+    if not _LEAKS:
+        terminalreporter.write_line("THREADLEAK: none")
+        return
+    terminalreporter.write_line(f"THREADLEAK: {len(_LEAKS)} test(s) leaked threads")
+    for nodeid, names in _LEAKS:
+        terminalreporter.write_line(f"  {nodeid}")
+        for n in names:
+            terminalreporter.write_line(f"      -> {n}")
```
