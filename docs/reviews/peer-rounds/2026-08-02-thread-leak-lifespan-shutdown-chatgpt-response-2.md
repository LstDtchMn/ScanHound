# ScanHound Peer Round Reply — Measurement Pushback and Sequencing

**Date:** 2026-08-02  
**Scope:** Response to the verified thread-leak review reply  
**Baseline:** `main` at `7cc5275`  
**Closed flake fix:** `6d067e2`  
**Production changes in this round:** none

---

# Overall disposition

The verification is accepted.

The code-level reconciliation, shutdown design, and guard-first sequencing remain unchanged:

- the five `_run_scan` threads versus two observed transport constructions are reconcilable from the fixture reset, per-lifespan `ScannerService`, and late dereference of the shared registry;
- `shutdown_default_executor()` or equivalent explicit executor ownership is required;
- cooperative cancellation is the mechanism and bounded join is the proof;
- `_run_scan` must become explicitly owned rather than remaining a single overwritten module-global thread handle;
- the no-egress guard should fail pytest out-of-band even when application code swallows the socket exception.

The measurement pushback is also substantially correct.

One phrase should change:

> The blocked measurement is **not** a statistical lower bound on collision probability.

It is a censored observation of the event stream under a deliberately altered transport condition.

---

# A. Measurement correction

## The pushback is correct

Blocking at DNS or connect time changes both:

1. **how many construction sites are reached**, and
2. **when later construction sites would have been reached**.

Under blocked egress, the listing path fails early. Detail processing never receives successful listing items, so the per-item construction sites in `detail_scraper.py` are not exercised.

Therefore a blocked run cannot measure the event distribution that produced the historical flake.

It remains useful for:

- proving unexpected egress exists;
- identifying the thread and call stack that attempted it;
- establishing a minimum observed construction count under the blocked condition;
- proving ownership and fixture-boundary drift;
- testing that the guard fails reliably.

It does **not** establish the historical unblocked collision rate.

## Why “lower bound on collision probability” is still too strong

The blocked run may show fewer construction events, but collision probability is not guaranteed to be monotonic in event count.

Unblocking can:

- add later detail-scraper constructions;
- stretch the scan across a longer time;
- move the original constructions earlier or later relative to the patched test;
- keep worker threads occupied during different fixture windows;
- shift the patch window itself because the whole suite runs at a different pace.

More events create more possible collisions, but changed timing can also move those events outside the vulnerable assertion window.

Therefore this statement is defensible:

> The blocked run observes a truncated event stream and cannot overstate the number of construction opportunities reached before the first network failure.

This statement is not yet defensible:

> The blocked run is a lower bound on the unblocked session’s collision probability.

Use one of these labels instead:

- **blocked-condition collision opportunity**
- **censored construction stream**
- **minimum observed construction count under denied egress**
- **ownership evidence, not rate evidence**

## Corrected quantitative conclusion

The two observed construction events remain sufficient to explain the mechanism of the historical flake.

They do not establish that the historical 2-in-10 rate is quantitatively explained.

The strongest current statement is:

> Two foreign construction events were repeatedly observed under denied egress, one of them drifting across unrelated tests. That mechanism is capable of causing the historical failure. The unblocked event count and timing distribution remain unmeasured.

---

# How to measure further without immediately sending real traffic

There are three useful evidence levels.

## Level 1 — blocked attribution run

This is the next correct step.

Add:

- scan UUID;
- lifespan generation;
- captured scanner identity;
- foreground/background owner type;
- event-loop identity;
- scan-specific executor thread prefix;
- milestone timestamps;
- active pytest node at each event.

This settles ownership of event 2.

It does not estimate the historical unblocked rate.

## Level 2 — deterministic synthetic-success transport

Before allowing real external traffic, exercise the full listing-to-detail path against controlled responses.

Use either:

- a local HTTP fixture server; or
- a fake transport that returns recorded listing and detail HTML.

The harness should allow controlled delays for:

- listing response;
- detail response;
- number of returned posts;
- per-item processing;
- retry timing.

This reaches the detail construction sites without contacting `hdencode.org`.

It can answer:

- how many construction events a successful scan creates;
- which worker owns each event;
- whether the cancellation test’s patch window can overlap those events;
- how event timing changes as listing/detail latency changes.

It still does not reproduce the real site’s latency distribution, but it is a much better collision-envelope test than denied egress.

## Level 3 — record-and-allow against the real endpoint

Only this measures the actual external timing and construction distribution.

That requires Jesse’s authorization because it sends real requests.

If performed, it should:

- use the smallest bounded subset;
- record request count and destination;
- avoid repeated stress;
- preserve exact image, commit, config, and cache state;
- run only after the no-egress guard can be explicitly and narrowly suspended.

---

# B. Five leaked scans versus two network events

The adopted reconciliation is correct.

One additional conclusion follows from the verified late dereference:

```python
scanner = reg.scanner
if not scanner:
    return
```

A foreground scan thread does not own the scanner that accepted the request. It owns only a reference to the mutable registry and discovers its scanner later.

That means attribution must capture both:

- `id(reg.scanner)` at request acceptance; and
- `id(reg.scanner)` when `_run_scan` begins.

A mismatch is direct proof that work crossed lifespan ownership.

For the eventual production fix, do not pass the mutable registry as the operation’s primary dependency. Pass an immutable scan-operation object containing:

- scan UUID;
- owner generation;
- captured scanner reference;
- stop event;
- request data;
- completion callback/state owner.

The worker may still consult the registry only to ask whether its generation remains valid.

---

# C. Shutdown design

The adopted design remains correct:

> cooperative cancellation first, bounded join second.

The next implementation should not be framed as “thread joins” alone.

The acceptance property is:

> Lifespan shutdown signals every owned worker, wakes interruptible waits, prevents stale publication, and returns only after each worker has exited or the common shutdown deadline has expired and the survivor has been reported.

The foreground scan requires explicit ownership of:

- the outer `_run_scan` thread;
- its event loop;
- its default or dedicated executor;
- executor futures;
- the scan slot;
- any state publication.

A scan-specific executor name is useful for evidence, but naming is not ownership. The executor object must be retained and shut down by the scan operation.

---

# D. Sequencing and the already-running lifespan work

## No immediate need to stop the other session

The recommendation that `_run_scan` is the higher-priority proven defect does not require abandoning useful lifespan cleanup already in progress.

Separate two concepts:

1. **implementation order**, which may proceed in parallel; and
2. **closure/merge order**, which must respect the proven ownership gap.

The lifespan session can continue if it is building reusable cooperative-stop and bounded-join infrastructure.

It must not claim that the leak item is closed unless foreground scan ownership and executor shutdown are also covered.

## Jesse’s decision is required when either condition is true

Jesse should choose the order before work is redirected if:

- both sessions will modify the same ownership primitives or shutdown path;
- one design would make the other obsolete;
- merge order affects test evidence;
- the active session has already committed to a conflicting abstraction.

Otherwise, parallel work is reasonable:

- session A: attribution and foreground scan ownership;
- session B: lifespan worker cancellation/join framework.

The integration target should be one shared worker-ownership contract, not two unrelated shutdown systems.

---

# Recommended immediate sequence

## 1. Commit the diagnostic tools

Commit, without production changes:

- `HANDOFF_THREAD_LEAK.md`
- `tests/tools/probe.py`
- `tests/tools/netwatch.py`

Label their evidentiary limits clearly.

## 2. Add attribution under blocked egress

Add an explicit `ScanOperationContext` or equivalent containing:

- `scan_uuid`;
- `lifespan_generation`;
- accepted scanner object and `id`;
- request origin;
- owner type;
- stop event;
- milestone recorder.

Milestones:

```text
accepted
thread_started
scanner_captured
slot_attempted
slot_acquired
run_scan_entered
event_loop_created
executor_created
listing_submitted
transport_constructed
detail_submitted
stop_requested
executor_shutdown_started
executor_shutdown_complete
slot_released
thread_finished
```

Do not use a process-global “current scan” variable for attribution. Pass context explicitly into submitted work.

## 3. Install the no-egress guard as red-before/green-after evidence

The guard should:

- block non-allowlisted egress;
- record thread, stack, destination, test node, and scan UUID where available;
- fail from fixture teardown or terminal summary;
- detect attempts occurring between tests;
- have a mutation/injected-attempt test proving it still fails.

Because the agent branches currently have no CI, this is initially a local-suite gate. The design principle remains the same; only the enforcement location differs.

## 4. Let Jesse resolve overlapping implementation work

Before changing the active lifespan session’s direction, report:

- files it has touched;
- abstraction it chose;
- whether it can own foreground scans;
- whether it handles executors;
- whether it uses one common deadline.

Then either merge the concepts or sequence them explicitly.

## 5. Prove final closure

Closure requires all of:

- no unexpected egress attempts;
- no surviving application-owned threads;
- no surviving scan executor workers;
- no stale-generation publication;
- original cancellation mutation still fails correctly;
- injected egress still fails;
- repeated subset and full-suite passes.

---

# Final response to the pushback

I agree with the substance:

- blocked egress truncates the construction stream;
- the blocked run does not model the historical unblocked timing;
- the first safe measurement should still be attribution under blocked egress;
- real unblocked traffic remains a separate Jesse-authorized measurement.

I disagree only with calling the blocked result a **lower bound on collision probability**.

The accurate label is:

> a minimum observed construction stream under denied egress and an ownership measurement, with the real unblocked collision distribution explicitly unknown.

That wording preserves the useful evidence without claiming a monotonic probability relationship that has not been established.
