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

## Fresh full-suite baseline (pre-Phase-3)

Run at `57016f7`, post-guard-fix and post-Phase-2, which is the causally clean
baseline round 4 asked for.

```
4224 passed, 4 skipped, 0 failed, 546.83s     EXIT=1
netwatch: 18 UNAUTHORIZED EGRESS ATTEMPT(S)
      8  adit-hd.com
      4  ollama
      4  x
      2  hdencode.org
```

Exit 1 is the gate alone; no test fails. Arithmetic reconciles exactly against
the pre-Phase-2 run (4197 passed + 1 failed + 4 skipped = 4202; now 4224 + 4 =
4228; difference 26 = the 26 new `test_scan_context.py` tests). No test was
lost or silently skipped.

**Two changes from the pre-Phase-2 inventory**, both explained:

- `192.168.1.1` is gone and `test_ssrf_rejects_private_discord_webhook` passes —
  the numeric-literal guard fix works.
- 19 attempts across 5 hosts became **18 across 4**.

### The neutrality claim in this document was initially wrong

The first version of this package asserted behaviour-neutrality on the strength
of the 296-test subset. The full suite found **two regressions the subset does
not cover**, both mine:

```
test_api_background.py::TestBackgroundStatus::test_scan_now_triggers
    stubs scan_once with `lambda: called.set()` — zero arguments; the route
    passed kwargs={"origin": ...} and the stub raised TypeError
test_api_lifecycle.py::test_late_background_worker_cannot_publish_into_next_lifespan
    patches _scan_source with blocked_scan_source(_source, _pages,
    _skip_urls=None); adding origin= raised TypeError
```

Root cause was widening the signature of a **private method that tests
monkeypatch**, and passing a new kwarg to a method tests stub. Fixed in
`57016f7` by carrying the origin as state (`next_scan_origin` /
`_current_scan_origin`) so both call surfaces are byte-for-byte what they were.

`ScannerService.run_scan(operation_context=...)` was deliberately not reverted —
that is a real public interface, and a double simulating it should accept the
optional parameter. Private-and-monkeypatched is the distinction that matters.

**The lesson generalises:** a subset-verified neutrality claim is not a
neutrality claim. Round 4's insistence on a fresh full run before merge was
correct.

## Limits

- Local container runs only; no CI on this branch.
- Q4's "not observed" is over 5 operations in one subset. It does not prove an
  executor cannot be stranded — an early loop return is still a plausible path.
- The five-host egress inventory is a separate topic and is not addressed here.
