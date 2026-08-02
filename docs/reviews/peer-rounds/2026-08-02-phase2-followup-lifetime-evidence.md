# Phase 2 follow-up — lifetime measurement corrected

**Date:** 2026-08-02
**Repository:** `LstDtchMn/ScanHound`
**Branch:** `claude/nice-meitner-2b717b`
**Scope:** observational only. Still no ownership fix, no joins, no cancellation.

Implements the six required follow-ups from peer round 4 and restates
questions 3–5 from new evidence.

---

## The round-4 P1 correction was right, and my earlier answers were wrong

The previous package reported "0 crossed ownership" and read it as *no scan
crossed a lifespan*. `crossed_ownership` only ever compared **acceptance to
entry**. It could not see an operation that entered under its own owner and
then outlived a later rollover — which is exactly what happens here.

With live sampling added at completion and at every publication boundary:

| question | previous answer | **corrected answer** |
|---|---|---|
| 3. Did any scan cross lifespan generation? | "No — 0 of 22" | **Yes. 0 crossed at entry, but 4 of 22 crossed post-entry.** |
| 4. Did any executor outlive its outer scan thread? | "Yes" | **No — not observed.** Outer finished last in all 5, by 1.0–2.1 ms. |
| 5. Did any stale scan attempt publication? | "No" | **Yes. All 5 foreground scans published without ownership.** |

Two of my three answers were wrong and the third was overstated. The instrument
was measuring a narrower thing than the question asked.

## Q3 — post-entry crossing is real, and large

Four of 22 operations crossed a lifespan generation after entry. The
magnitudes matter:

```
scan c0b9ab57  generation rolled over 10 -> 16
scan f7b16c88  generation rolled over 11 -> 14
```

A scan accepted under generation 10 was still publishing under generation 16 —
it outlived **five** subsequent app lifespans. This is no longer a theoretical
hazard.

## Q4 — the executor did NOT outlive its outer thread

Decided on the monotonic clock rather than inferred:

```
scan ef106e6b api_manual: outer finished last (+1039993 ns)
scan 031ed18e api_manual: outer finished last (+1336135 ns)
scan bcdd034f api_manual: outer finished last (+1768856 ns)
scan f27b8f88 api_manual: outer finished last (+1105071 ns)
scan 030c0898 api_manual: outer finished last (+2052834 ns)
```

Consistent with the sequential `await loop.run_in_executor(...)` listing path:
the outer thread is *waiting on* the worker, so it cannot finish first.

**The leak is the whole scan outliving its test, not a stranded executor
worker.** That is a narrower and more accurate statement than the previous
package's, and it means Phase 3's executor-completion work is hardening rather
than a fix for an observed orphan.

## Q5 — publication without ownership, from two distinct causes

**All five** foreground operations published while they did not own the
lifespan, across every publication point — module-global results, websocket
broadcast, registry config mutation, notification, auto-grab.

The causes split, and they need different fences:

```
scan 118a0c91  shutdown requested during teardown of generation 9
scan c0b9ab57  generation rolled over 10->16
scan f7b16c88  generation rolled over 11->14
```

`ServiceRegistry.owns_lifespan(g)` is `g == current AND not shutdown_requested`
(`backend/api/dependencies.py:201-205`). So a same-generation failure means the
lifespan was **tearing down** mid-publication, which a generation comparison
alone would miss entirely. A Phase 3 fence that only compares generations would
let the `118a0c91` case straight through.

## What changed in the code

All six required follow-ups, observational only:

1. **Live ownership sampling.** `record()` takes `active_lifespan_generation`
   and `still_owns_lifespan`, sampled at call time rather than copied from the
   entry snapshot. Wired into all five publication points and outer completion.
2. **Unconditional completion markers.** `THREAD_FINISHED` now fires in
   `_run_scan`'s `finally` (previously only on the `not scanner` early return,
   so a normal scan left no completion marker at all). `run_with_scan_context`
   records `WORKER_FINISHED` in its own `finally`, including on the failing
   path.
3. **Direct netwatch attribution.** Attempts carry `monotonic_ns` and read the
   owning operation from a thread-local binding — no thread-name parsing:
   ```
   originating op: 4891be02-... origin=api_manual
   ```
4. **Lexical listing context.** `_fetch_page` captured `_own_ctx` at submission
   instead of re-reading `self._operation_context`, which an orphaned worker
   overlapping a later scan could otherwise misattribute to that later scan.
5. **`background_manual` distinguished.** `scan_once(origin=...)` threads
   through to `_scan_source`; `POST /background/scan-now` now passes
   `ORIGIN_BACKGROUND_MANUAL` instead of being silently labelled periodic.
6. **Honest property names.** `crossed_ownership_at_entry` (narrow, with the
   bound stated in its docstring), `observed_post_entry_crossing`, and
   `crossed_lifespan` for the union. `crossed_ownership` remains as an alias so
   nothing reads the narrow value under a broad name by accident.

## Verification

| run | result | exit |
|---|---|---|
| `tests/test_scan_context.py` | **26 passed** (20 + 6 new) | 0 |
| subset, tracing OFF (neutrality) | 296 passed, 4 skipped | 0 |
| subset, tracing ON + gate | 296 passed, 4 skipped | 1 *(gate)* |

Neutrality still holds: identical pass count with tracing off.

Six new tests cover the corrected semantics, including that the narrow entry
property stays `False` while `observed_post_entry_crossing` flips `True` after
a live sample, that the live generation is not copied from the entry snapshot,
that worker completion is recorded on the failing path, and that the
thread-local binding does not leak across threads.

## Limits

- Local container runs only; no CI on this branch.
- Q4's "not observed" is over 5 operations in one subset. It does not prove an
  executor cannot be stranded — an early loop return is still a plausible path.
- The five-host egress inventory is a separate topic and is not addressed here.
