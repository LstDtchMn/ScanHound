# ScanHound Peer Round 4 — Phase 2 Attribution Review

**Review date:** 2026-08-02
**Repository:** `LstDtchMn/ScanHound`
**Branch:** `claude/nice-meitner-2b717b`
**Head inspected:** `5cce3e0937d1bca51884ab72496762df6f2e6ee4`
**Base:** `7cc5275b4a518cb6986f34e26e1c0e9c98175b7c`
**Review package:** `docs/reviews/peer-rounds/2026-08-02-phase2-attribution-evidence.md`

> Relayed inline rather than as a downloadable file. Transcribed verbatim so the
> peer-round record is complete — it was the only round missing from this
> directory.
>
> **Disposition: fully actioned.** All six required follow-ups landed in
> `b45636b`, with results in
> `2026-08-02-phase2-followup-lifetime-evidence.md`. Both P1 findings were
> confirmed correct and two of the three original answers were overturned.

## Evidence boundary

I inspected the Phase 1 guard, Phase 2 context propagation, every instrumented
scan origin, all three executor domains, the publication markers, and the
generation API used by repeated `TestClient` lifespans.

I independently ran the 20 scan-context tests plus the two named tests at the
ends of the observed drift:

```text
22 passed, 1 warning
```

After pytest had printed its passing summary, the foreground scan created by
`TestScanner.test_scan_start` was still logging its failed HDEncode request and
finishing its scan. That independently confirms the central leak: the test
passes and its foreground scan continues afterward.

The full 600-second suite and the original container attribution run were not
repeated locally in this review. The conclusions below distinguish facts that
the committed trace can prove from conclusions it presently cannot.

---

# Verdict

## PHASE 2 PARTIALLY ACCEPTED — CORRECT THE LIFETIME MEASUREMENT BEFORE PHASE 3

The most important positive result is sound: both blocked HDEncode attempts
belong to foreground manual scans created by `POST /scan/start`. Event 2 is not
owned by `BackgroundScanner`; it merely becomes visible while a background
scanner test is active. The scan-specific executor name is sufficient to join
those attempts to the two `api_manual` operation UUIDs.

The package overstates what the remaining instrumentation proves, however:

- `0/22 crossed ownership` means only that no operation changed owner between
  **acceptance and worker entry**;
- it does not mean that no operation remained alive across a later lifespan
  generation;
- it does not prove that an executor outlived its outer scan thread;
- it does not prove that stale publication did not occur.

Those distinctions matter directly to Question 1. The immutable operation
object remains justified in Phase 3, although the acceptance-to-entry scanner
capture must now be described as preventive hardening rather than the fix for a
measured owner switch. The object as a whole is still the correct vehicle for
the measured operation lifetime, cancellation, deadline, and publication
authority.

Before changing behavior, land a small observational Phase 2 follow-up that
samples the active owner after entry and records real outer/worker completion.
Then rerun the attribution subset once. Otherwise Phase 3 will change the race
before the unanswered lifetime questions have actually been measured.

---

# Priority findings

## P1 — "0/22 crossings" uses the wrong end boundary

`ScanOperationContext.crossed_ownership` at `backend/scan_context.py:201-209`
compares only:

```text
accepted owner  ->  entered owner
```

The prior review explicitly required a third sample: the active generation
immediately before publication. Phase 2 records publication milestone names,
but calls such as `context.record(PUBLISH_LAST_SCAN_ITEMS)` do not pass the
registry's current generation. `ScanOperationContext.record()` therefore fills
the event from `entered_lifespan_generation` at
`backend/scan_context.py:234-242`; it repeats the entry snapshot rather than
observing the live owner.

That leaves this unmeasured sequence:

```text
accepted generation N
entered generation N
lifespan N shuts down
generation N+1 begins
old operation continues or publishes
```

For that sequence, `crossed_ownership` remains false forever even though the
operation crossed a lifespan boundary after entry. The test fixture makes this
distinction material: `tests/test_api_routes.py` creates a fresh `TestClient`
lifespan per test, while event 2 originates in `test_scan_start` and is observed
much later in `test_background_scanner.py`.

The correct Phase 2 statement is:

> Zero of 22 operations switched `(generation, scanner_id)` between acceptance
> and entry.

It is not yet:

> Zero operations crossed a lifespan during their execution.

Add an active-owner snapshot at outer completion and at every publication
boundary. For FastAPI operations, also record the result of
`reg.owns_lifespan(accepted_generation)` rather than reconstructing authority
from stored identity alone.

## P1 — Event 2 does not prove the executor outlived its outer scan thread

The committed trace proves that the listing worker outlived the test that
created the scan. It does not establish the relative completion order of the
listing worker and `_run_scan`:

- normal `_run_scan` completion never records `THREAD_FINISHED`;
- the slot-rejected return also omits it;
- `THREAD_FINISHED` is recorded only on the `not scanner` early return at
  `backend/api/routes/scanner.py:156-158`;
- `run_with_scan_context()` records worker start but no worker-finished event;
- netwatch attempts have no monotonic timestamp to compare with the trace;
- netwatch's `originating_operation` field is still never populated directly;
  the review joins by the UUID embedded in the thread name.

Given the sequential `await loop.run_in_executor(...)` listing path, an egress
attempt normally occurs while the outer scan thread is waiting for that worker.
An early loop return can strand a worker, but proving that case requires a real
outer-finished marker before a later worker event. The present event establishes
"foreground scan work outlived its initiating test," not "executor outlived its
outer scan thread."

Record `THREAD_FINISHED` in the outer thread's unconditional `finally`, record
worker completion in `run_with_scan_context()`'s `finally`, and timestamp the
netwatch attempt. Then Question 4 becomes a direct monotonic comparison rather
than an inference.

## P1 — "No stale publication" is not established

The publication stages are useful, but they record an attempted action under
the context's stored entry generation. They do not sample the registry's active
generation and do not report whether the accepted generation still owns the
lifespan.

Consequently, accepted-owner equality at entry does not imply publication was
current later. `_run_scan` writes module-global results, broadcasts, updates the
mutable registry configuration/backend, sends notifications, and invokes
auto-grab after `scanner.run_scan()` returns. If the lifespan changes while the
scan is running, those are precisely the late publications Phase 3 must fence.

The correct answer to original Question 5 is therefore **unproven**, not no.
This strengthens rather than weakens the Phase 3 publication-fencing step.

## P2 — Two attribution seams are incomplete

### Background manual scans are labelled periodic

`ORIGIN_BACKGROUND_MANUAL` exists and the unit test proves only that its string
is distinct. No production path uses it. `POST /background/scan-now` calls the
same parameterless `BackgroundScanner.scan_once()` as the scheduler, and
`_scan_source()` always constructs `ORIGIN_BACKGROUND_PERIODIC`.

Pass the origin or a parent operation into `scan_once()` so manual and periodic
background work are distinguishable in real traces.

### The listing worker rereads mutable instance context

The executor wrapper captures `_ctx` at submission, which correctly names and
starts the worker under the originating UUID. Inside `_fetch_page`, however,
the transport marker rereads `self._operation_context` at
`backend/scanner_service.py:801` and `:809`.

If an orphaned worker overlaps a later scan on the same `ScannerService`, the
instance attribute can have been replaced. The transport marker can then land
on the later operation even though the worker name and `LISTING_STARTED` marker
belong to the original one. Capture and use the lexical `_ctx` throughout the
submitted callable.

This is an attribution-only correction; it need not alter cancellation or
executor behavior.

---

# Corrected answers to the original six evidence questions

| # | question | supported answer at this head |
|---|---|---|
| 1 | Is event 1 foreground manual? | **Yes.** `origin=api_manual`. |
| 2 | Is event 2 foreground, scheduler, or background? | **Foreground manual.** It is merely observed during a background-scanner test. |
| 3 | Did any scan cross lifespan generation? | **Not answered.** Zero switched owner between acceptance and entry; post-entry crossing is not measured. |
| 4 | Did any executor continue after its outer scan thread finished? | **Not answered.** Worker and normal outer completion are not both timestamped. |
| 5 | Did any stale scan attempt publication? | **Not answered.** Publication records the stored entry owner, not the active publication-time owner. |
| 6 | Did the gate return nonzero despite broad exception handling? | **Yes.** The reported `296 passed, 4 skipped, exit 1` is the intended gate behavior. |

The numeric-literal guard correction is also sound for the reported SSRF test:
local parsing of a numeric address is not outbound DNS, while the subsequent
connection remains blocked and recorded.

---

# Answers to this round's questions

## 1. Keep the immutable operation object in Phase 3

Yes, keep it, with corrected justification.

The measured defect is not an acceptance-to-entry scanner switch. Capturing the
accepted scanner strongly should therefore be described as closing a real but
unobserved construction hazard.

The immutable operation object is nevertheless required for the observed
foreground operation that outlives its initiating test/lifespan. It should own:

- the accepted scanner reference;
- accepted lifespan generation and publication authority;
- operation-scoped cancellation;
- the absolute deadline;
- event loop and executor ownership;
- completion/join state.

Those fields must remain one coherent identity from acceptance through final
publication. Deferring only the scanner reference would save little and leave
the late registry dereference as an avoidable split-brain edge. Keep foreground
ownership as Phase 3 step 5, after metadata cancellation/deadline/publication
work as already agreed.

**Disposition:** retain in Phase 3; label the accepted-scanner substitution as
hardening, not as the cause of the observed egress.

## 2. Make full-suite egress its own review topic and branch

Yes. The five-host ledger is a broader test-isolation/security inventory, not
one scan-lifecycle defect:

- the two `hdencode.org` attempts remain Phase 3's red-before/green-after signal;
- `adit-hd.com`, `ollama`, `x`, and `192.168.1.1` need node-by-node ownership and
  intent classification;
- `ollama` and the LAN destination touch operator infrastructure and should not
  be normalized as harmless test behavior;
- placeholder `x` becoming a real hostname is likely a fixture/configuration
  boundary defect and deserves its own minimal reproduction.

Create a separate egress review with, for every attempt: pytest node, stack,
host/port, owning component/operation, whether the test intended network I/O,
and the missing fake or validation boundary. Do not mix unrelated fixture fixes
into the Phase 3 lifecycle patch.

That topic is independent in implementation but not optional before merge: the
permanent no-egress gate must eventually be green for the entire suite without
allowlisting real services or private infrastructure.

## 3. A fresh full suite is not required to start Phase 3, but is required before merge

Phase 3 may proceed against the focused reproduction subset after the Phase 2
measurement gaps above are corrected and the subset is rerun once. The subset
is sufficient to drive the known foreground leak and gives a faster red/green
loop for cancellation, ownership, executor completion, and bounded join.

The pre-Phase-2 full-suite count is not an authoritative current baseline because
it predates both the guard correction and the attribution change. A fresh full
run is required before merging the production fix and before treating 19/5 as
the final egress inventory.

Operationally, run it now if possible. A post-guard, pre-Phase-3 full-suite run
preserves the cleanest causal baseline; after Phase 3 changes behavior, a changed
count can no longer be attributed solely to the guard fix. But its ten-minute
runtime need not block writing or locally testing Phase 3.

**Disposition:** not a prerequisite to begin Phase 3; prerequisite to merge and
to close the separate full-suite egress review.

---

# Required Phase 2 follow-up before the behavior change

1. Record current generation/ownership at outer completion and immediately
   before every publication attempt.
2. Record unconditional outer-thread completion and per-executor-callable
   completion with monotonic timestamps.
3. Give netwatch attempts monotonic timestamps and a direct operation UUID when
   one is active.
4. Use the lexically captured listing context inside `_fetch_page`.
5. Distinguish `background_manual` from `background_periodic` in production.
6. Rerun the focused attribution subset and restate Questions 3–5 from the new
   evidence.

This is a small completion of Phase 2, not a request to delay the already agreed
Phase 3 architecture or fold the five-host egress inventory into it.
