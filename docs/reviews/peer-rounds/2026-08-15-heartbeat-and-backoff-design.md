# Peer round: the poll heartbeat (built), and the backoff (design only)

Two things, deliberately separated.

**PR #79 `fix/jd-poll-heartbeat`** — built, tested, CI green. Review the code.

**Backoff + call timeouts** — NOT built. Review the DESIGN before I write it.
The last round caught me building on an unproven premise, so the design goes
out first this time.

---

## Part 1 — PR #79, the heartbeat (code review)

### Why it exists

The previous round refused my elimination argument, correctly. I had claimed:
one `query_packages` warning, therefore repeated silent failures inside
`_connect_jd_device`. That assumed the poller kept CYCLING, which nothing
established. The same evidence fits a poller blocked inside one iteration, or
stopped entirely — and those want different fixes.

`failure_phase` cannot separate them either: it is populated from an
EXCEPTION, and a blocked call raises nothing.

### What it does

`poll_results` is wrapped in `note_poll_iteration_start()` /
`note_poll_iteration_end()` (the latter in a `finally`), and `/health` exposes
`iterations_started`, `iterations_completed`, `current_iteration_seconds`.

    started == completed, neither advancing  -> thread stopped
    started  > completed, not advancing      -> BLOCKED inside an iteration
    both advancing, no success               -> cycling and failing

Mutation-verified: removing the `finally` close fails the wiring test, which is
what would otherwise misreport a fast-failing poller as blocked. 40 tests pass.

### What I want challenged

1. **Is `poll_results` the right boundary?** The heartbeat wraps the poll call,
   not the whole poller-loop iteration. Work outside `poll_results` (the
   WebSocket push, the rename hand-off, the sleep) is therefore NOT covered. If
   the thread blocked THERE, started and completed would both stand still and
   read as "thread stopped". Is that acceptable, or should the heartbeat move
   into the loop in `backend/api/main.py`?
2. **Counters vs timestamps.** I record monotonic start time and two integer
   counters. Is anything needed to distinguish "slow but progressing" from
   "blocked" beyond `current_iteration_seconds` — a per-iteration duration
   history, for instance?
3. **`_note_poll_success()` placement, again.** It fires right after
   `query_packages`, so an iteration that blocks in `query_links` afterwards
   shows a fresh success AND a growing `current_iteration_seconds`. I believe
   that combination is unambiguous and useful. Is it?

---

## Part 2 — backoff + timeouts (design review, nothing written)

### The lock hazard, which I had missed until the last round

`_connect_jd_device()` takes `self._jd_lock` — a plain blocking
`threading.Lock` — and holds it across the entire network sequence
(`Myjdapi()`, `connect()`, `update_devices()`, `get_device()`). **None of those
calls carry a timeout.**

So one caller stuck in that sequence leaves every other caller, including the
poller, waiting at `with self._jd_lock:` — silently and indefinitely. That
reproduces the historical evidence with no remote rate limiting involved, and
is at least as plausible as the hypothesis I brought last time.

### Proposed design

**Backoff, poller-only.** Per the last round's authority split, the retry
policy belongs to the background poll, NOT inside `_connect_jd_device` — that
primitive also serves connection tests, control actions and link delivery, and
an operator action must not queue behind background policy.

    poll_results()  -> if backoff window active, return [] without touching JD
    connect failure      -> bump
    query_packages fail  -> bump      (see below)
    success              -> reset

8s base, doubling, capped at 5 minutes.

**Both failure paths feed it.** The `query_packages` failure is what
invalidates the cache and thereby converts the NEXT cycle into a full
reconnect. A backoff counting only connect failures would miss half the
feedback loop — the last round made this point and I want it confirmed I have
applied it correctly.

**Timeouts.** A blocked call under a shared lock is a defect regardless of the
outage's cause. Options I can see, none obviously right:

    a) socket.setdefaulttimeout() around the connect sequence
       -- global, affects other threads, feels wrong
    b) _jd_lock.acquire(timeout=N) so a stuck HOLDER cannot wedge callers
       -- fixes the blocking symptom, not the stuck call itself
    c) patch/wrap myjdapi's HTTP layer to carry a timeout
       -- correct, but reaches into a third-party library
    d) run the connect sequence in a worker with a deadline
       -- heavier, but bounds it without touching the library

### What I want answered

4. **Which timeout approach?** I lean (b) + (c): bound the lock so a stuck
   holder is visible rather than silently blocking, and bound the actual call.
   But (c) depends on myjdapi internals I have not read yet.
5. **Should a bumped backoff also force `_invalidate_jd_cache()`?** Currently
   only the query path invalidates. If the connect path fails repeatedly the
   cache is already empty, so this may be a no-op — I am not certain.
6. **Foreground bypass.** Last round: an explicit human Retry/Test action may
   attempt immediately and, on success, warm the cache and reset the
   background backoff; an automatic UI refresh must NOT. ScanHound has a
   connection-test action — is wiring the reset to that sufficient, or does
   anything else count as "explicit human action"?
7. **Is backoff even safe if the true cause is blocking?** If the poller is
   stuck inside an iteration, it never reaches the backoff check, so backoff
   neither helps nor hurts. I believe that is fine — the heartbeat covers that
   case and timeouts address it — but I would rather have it said than assumed.

---

## Standing context

Deployed and live: #72, #75, #76, #77, #78. `/health` currently reports
`jd_poll` with liveness, failure phase, and (after #79) the heartbeat.

A JD restart was reproduced under observation today: one failed poll at
`query_packages`, recovery in 15 seconds. So the deterministic "a restart
leaves ScanHound permanently stuck" is refuted; a restart-triggered failure
requiring an additional condition is NOT refuted.
