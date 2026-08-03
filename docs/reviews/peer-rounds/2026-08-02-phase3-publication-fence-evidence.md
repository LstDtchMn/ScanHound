# Phase 3 steps 1–2 — foreground publication fence

**Date:** 2026-08-02
**Repository:** `LstDtchMn/ScanHound`
**Branch:** `claude/nice-meitner-2b717b`
**Scope:** first production behaviour change. Publication authority only —
no joins, no cancellation, no deadline work yet.

Implements round 5's reordering, which moved publication fencing to the front
of Phase 3 because it is the failure that was actually measured.

---

## What it fixes

Phase 2 measured that **all five** foreground scans published while they did
not own the lifespan, from two causes:

```
scan 118a0c91  shutdown requested during teardown of generation 9
scan c0b9ab57  generation rolled over 10 -> 16
```

## The fence is a lease, not a check

Round 5's P1: `if reg.owns_lifespan(g): publish()` leaves a
time-of-check/time-of-use window — shutdown can begin between the check and the
write.

`ServiceRegistry.acquire_publication(generation)` decides admission **and**
takes the lease under one lock shared with `request_shutdown()` and
`begin_lifespan()`. Once `request_shutdown()` returns, no further lease can be
admitted.

The lock is **not** held across the caller's body. The lease is the
synchronisation token, so a slow WebSocket, notification or auto-grab call
cannot stall lifespan rollover —
`test_lease_does_not_hold_the_lock_across_the_body` starts a rollover thread
while a lease is held and asserts it completes.

Also corrected: `owns_lifespan()` previously read the generation under the lock
and the shutdown flag outside it, so its answer could reflect two different
instants. Both reads are now one acquisition, and `begin_lifespan()` clears the
shutdown event under the lock for the same reason.

## The publication denominator is now complete

Round 5 was right that the five sampled terminal groups were not every path.
Thirteen call sites now route through `_fenced()`:

| path | note |
|---|---|
| `scan:progress` | previously unfenceable — the module-level callback had no operation to consult; now built per-operation |
| `log` | same |
| `scan:error` (slot rejection) | early-return path |
| `scan:error` (outer exception) | failure path |
| `_last_scan_items` | module-global result store |
| `scan:result` per item | fenced **individually**; authority can be lost mid-loop, and the loop now breaks rather than emitting a partial set |
| `scan:complete` | its own broadcast, separate from the item loop |
| config persistence | `last_scan_time` + `save_config` |
| notification | `notify_scan_complete` |
| `autograb:started` | entry |
| `autograb:complete` × 2 | success **and** exception broadcasts fenced separately — the only path taking a real external action between two broadcasts |

A refused publication is recorded in the trace rather than silently dropped, so
it stays auditable. Registries predating the lease (test doubles) fall back to
publishing, so nothing silently stops.

## Red baseline retired

The two `xfail(strict=True)` placeholders described the failures against a local
simulation of the unfenced code. They are replaced by
`tests/test_publication_fence.py`, which exercises the real production path with
a real `ServiceRegistry`, covering both measured causes — including the
same-generation teardown case that a generation-equality fence would pass.

## Verification

| run | result | exit |
|---|---|---|
| `tests/test_publication_fence.py` | 9 passed | 0 |
| `tests/test_scan_context.py` | 28 passed | 0 |
| `test_api_lifecycle` + `test_api_background` + `test_api_routes` | 238 passed | 0 |
| subset, ×2 | clean both runs | 0 |
| **full suite** | **4235 passed, 4 skipped, 0 failed**, 552s | 1 *(gate only)* |

Counts reconcile exactly against the previous clean baseline: 4224 → 4235 is
+11 = 9 new fence tests + net 2 in `test_scan_context.py`. No test lost.

### The flake was live on this branch until now

This branch is off `main`, which never received the de-flake fix — it existed
only as `6d067e2` and `9b059c5` on the two agent branches. The original
`assert constructors == []` was still here and failed **2 of 4** subset runs,
which would have poisoned every Phase 3 red/green measurement.

Cherry-picked as `c20745c`; `git patch-id --stable` gives
`fba0caf2314217f086b0b4c7e63ae5d916b6545a` for all three. The subset then ran
clean twice. **Every "296 passed" reported from this branch before this point
was a flake-susceptible measurement.**

## What this does NOT fix

Egress is unchanged at **18 attempts across 4 hosts**. That is expected and
worth stating plainly: the fence stops a stale scan from *publishing*, not from
*running*. The two `hdencode.org` attempts still happen because the leaked
foreground scan still executes to completion — that is steps 6–7 (operation
ownership, executor completion, bounded join), not this change.

## Limits

- Local container runs only; no CI on this branch.
- Steps 3–7 are unstarted: metadata future cancellation, deadline propagation
  through `tmdb_client`/`rt_scraper`, staged metadata commit, executor
  completion, bounded join.
- The four-host egress inventory remains a separate topic and is untouched.
