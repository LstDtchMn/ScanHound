# Corrected JD reconnect design — implement from this

Written after the PR #79 review, which returned REQUEST CHANGES on both the
built heartbeat and the unbuilt backoff design. **Two of my premises were
factually wrong.** Both are now verified against the running system, and the
verification commands are included so the next session does not take my word
for it.

Nothing in this document is implemented. Read it, then build.

---

## Corrections to what I previously wrote (both VERIFIED wrong)

### 1. "The myjdapi calls carry no timeout" — FALSE

Verified in the running container:

    docker exec scanhound python -c "import myjdapi,inspect,re; \
      print(myjdapi.__version__); \
      print([l.strip() for l in re.findall(r'.*timeout.*', \
             inspect.getsource(myjdapi.myjdapi))])"

    installed: 1.1.10
    self.__timeout = 3
    requests.get(api + query, timeout=self.__timeout)
    requests.post(..., timeout=self.__timeout)

Every HTTP request IS bounded at 3 seconds. I asserted otherwise without
reading the library, and then built a hazard narrative on it.

**The real hazard, stated correctly:** `_connect_jd_device()` holds `_jd_lock`
across a MULTI-REQUEST sequence (`Myjdapi()`, `connect()` — which itself calls
`update_devices()` — then ScanHound's own `update_devices()`, then
`get_device()`). Individual requests are bounded; the whole sequence is not,
and the lock has no acquisition deadline. So a pathological holder can still
block every other caller, just not forever on a single socket read.

Note also: `connect()` already calls `update_devices()`, so ScanHound's
subsequent `jd.update_devices()` is redundant. Worth removing AFTER pinning the
dependency — `requirements-docker.txt` floats `myjdapi>=1.1.6`, so do not build
policy on internals without pinning or verifying the installed version.

### 2. "Backoff on the poller is sufficient" — FALSE, and this is the blocker

Verified:

    frontend/src/routes/downloads/+page.svelte:381
      setInterval(() => { loadResults(); loadJdState(); }, 5000)

    backend/download_service.py  get_jd_state() -> self._connect_jd_device()

An open Downloads page attempts a connection **every 5 seconds**, bypassing any
poller-side backoff entirely — and 5s is MORE aggressive than the 8s poller.
Poller-only backoff would have bounded the smaller producer and left the larger
one untouched.

This also revises my earlier "~6,750 reconnect attempts over 15 hours": that
assumed the 8s poller alone. With a Downloads page open the real rate was
higher, and the estimate was derived from the wrong component.

---

## The design to build

### A. Heartbeat — move the authority to the outer cycle

Current (PR #79) wraps `poll_results()` only. A block AFTER it returns — in
`annotate_source_links()`, `ws_manager.broadcast_sync()`,
`capture_jd_confirmed_names()`, or the rename hand-off — leaves both counters
equal and stationary, which the documented table reads as "thread stopped".
The thread is alive and blocked. That is the same class of unsupported
conclusion this PR exists to prevent.

Build the primary heartbeat around the active outer cycle in
`backend/api/main.py` (`_start_results_poller`), ending it BEFORE the 8-second
sleep — otherwise healthy sleeping looks like an in-flight operation.

Keep the `poll_results()` span as a NESTED sub-span; it usefully localises a
block to JD polling specifically.

    cycle_started / cycle_completed / current_cycle_seconds
    seconds_since_cycle_completed          <-- REQUIRED, see below
    poll_results_started / poll_results_completed / current_poll_results_seconds

`seconds_since_cycle_completed` is required because equal counters in a single
snapshot carry no age: "neither advancing" otherwise needs two snapshots to
establish, and the host checker only takes one.

Do NOT add per-iteration duration history — it establishes a baseline but does
not prove the current operation is progressing.

**Test that discriminates (the current tests do not):** the existing blocked
tests call `note_poll_iteration_start()` directly, so a mutation moving the
start/end calls to AFTER the inner call would still pass. Needed instead:

    worker thread enters the cycle
    a stage AFTER poll_results blocks on an Event
    main thread reads jd_poll_health()
    assert cycle_started > cycle_completed and current_cycle_seconds is not None
    release, join, assert both equal and current is None

### B. One shared automatic-reconnect gate

**The invariant:** no AUTOMATIC caller may establish a fresh MyJDownloader
connection more often than the shared policy allows. Only an explicit operator
action may bypass it, once.

Automatic callers that must obey it:

    background poll_results()
    the 5-second jdState() refresh
    page-initialisation jdStatus() when no usable cache exists

Shape it as a policy wrapper AROUND `_connect_jd_device()`, not inside it —
that primitive also serves control actions and link delivery, and an operator
action must not queue behind background policy.

    automatic_connection():
        usable cache            -> use it
        no cache, gate active   -> return BACKING_OFF (do not attempt)
        otherwise               -> attempt; on failure bump + schedule

### C. Reset authority is `query_packages`, not connect

If the exponent resets whenever `_connect_jd_device()` returns a device, this
never escalates:

    connect ok -> reset; query_packages fails -> bump to 8s; wait 8s; repeat

It sits at 8s forever while the end-to-end poll stays broken. Reset ONLY at the
liveness authority point — successful `query_packages` / `_note_poll_success()`.

**Test:** fake clock, connect always succeeds, `query_packages` always raises.
Assert attempts at 8, 16, 32, 64 — not 8, 8, 8, 8. Positive control: one
successful `query_packages` resets the next failure to the base delay.

### D. Cache invalidation stays separate from pacing

    cached device's query_packages fails -> invalidate + bump
    connect fails while cache absent     -> bump only
    force=True probe fails, older cache exists -> do NOT destroy that cache

A backoff counter is not evidence that a cached handle went bad.

### E. Telemetry must distinguish "waiting" from "failing"

A skipped cycle still advances the heartbeat counters, which would make
"both advancing, no success -> cycling and failing" false. Expose:

    reconnect_backoff_active
    reconnect_backoff_seconds_remaining

and do NOT increment `consecutive_failures` for a cycle where no remote call
was attempted. Also keep "not attempted" distinguishable from "observed empty"
— both currently return `[]`.

### F. Lock containment (optional, secondary)

`_jd_lock.acquire(timeout=N)` so callers fail visibly rather than queue behind
a wedged holder. N must exceed a legitimate full connect sequence (several
individually 3s-bounded requests) — 5s would be too aggressive. Report it as a
distinct condition (`jd_connection_lock_timeout`), not as an auth failure.

**Rejected:** `socket.setdefaulttimeout()` (process-global, affects unrelated
threads); worker-thread-with-deadline (Python cannot kill a thread; the
abandoned worker keeps holding the lock and may later mutate shared state).

If a genuinely indefinite holder is ever observed despite the bounded upstream
requests, the right answer is to move network I/O out of the shared-state lock
(claim a generation under lock, connect outside it, publish under lock again) —
a larger redesign that should be driven by evidence, not anticipation.

---

## Still unproven, and worth stating plainly

The cause of the ~15-hour stall remains unestablished. What is known:

* a JD restart alone does NOT cause it (reproduced: one failure at
  `query_packages`, recovery in 15s);
* the failures during the outage were not repeated `query_packages`
  exceptions, because that path logged at WARNING and appeared once;
* that does NOT establish repeated reconnect failures — the poller may have
  been blocked, or stopped, and nothing recorded which.

The heartbeat (once moved to the outer cycle) is what answers it. **On the next
occurrence, do not restart the container first.** Capture `/health`, a thread
stack if practical, and whether a MyJDownloader connection from OUTSIDE the
stuck service succeeds. Then try pausing only the JD attempts for several
minutes without restarting the process: if it recovers, a self-sustaining
remote block is likely; if only process recreation helps, process-local state
is likely.
