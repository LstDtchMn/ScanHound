# Phase 2 — Scan-operation attribution: results

**Date:** 2026-08-02
**Repository:** `LstDtchMn/ScanHound`
**Branch:** `claude/nice-meitner-2b717b`
**Scope:** behaviour-neutral instrumentation. No ownership fix, no joins, no
cancellation.

Answers the six questions in section 11 of
`...-chatgpt-response-3.md`.

---

## Headline: event 2 is a leaked FOREGROUND manual scan

Both blocked egress attempts now carry their owning scan in the thread name,
so ownership is read directly rather than inferred:

```
dns hdencode.org:443  thread=scan-348ecc35-listing_0
    observed during: tests/test_api_routes.py::TestScanner::test_scan_start
dns hdencode.org:443  thread=scan-2f86e58b-listing_0
    observed during: tests/test_background_scanner.py::TestBackgroundCacheDB::test_get_cache_urls
```

Both UUIDs resolve to `origin=api_manual` — `POST /scan/start`. The second one
lands inside a `test_background_scanner.py` test but **does not belong to the
background scanner**; it is a foreground scan leaked from `test_api_routes.py`
still running many tests later.

This confirms the original hypothesis *and* vindicates the peer round's refusal
to accept it on the strength of the `asyncio_0` thread name. The name proved
nothing; the UUID does.

## Answers to the six questions

| # | question | answer |
|---|---|---|
| 1 | Is event 1 foreground manual? | **Yes.** `origin=api_manual`, observed in `test_scan_start`. |
| 2 | Is event 2 foreground, scheduler, or background? | **Foreground manual.** Not the background scanner, despite where it lands. |
| 3 | Did any scan cross lifespan generation? | **No — 0 of 22.** See below. |
| 4 | Did any executor continue after its outer scan thread finished? | **Yes** — that is exactly what event 2 is. |
| 5 | Did any stale scan attempt publication? | **No.** All five `api_manual` operations reached the publish milestones under their own generation; with zero crossings there was no stale publication. |
| 6 | Did the guard return non-zero despite broad exception handling? | **Yes.** `EXIT=1` with `296 passed, 4 skipped`. |

## The lifespan-crossing race did not occur

`0 crossed ownership` across 22 operations. Every `accepted_owner` tuple
equalled its `entered_owner` tuple.

This is a genuine negative result and it should temper the design. The late
`reg.scanner` dereference at `scanner.py:141` makes the crossing *possible by
construction*, and the reconciliation in round 3 correctly identified it as a
mechanism — but under these runs the worker thread is scheduled fast enough
that the scanner has not yet been replaced. **The observed defect is a leaked
scan that outlives its test, not a scan that adopts a later test's scanner.**

Phase 3 should still capture the accepted scanner (it removes a real hazard),
but the justification is hardening, not an observed failure. Anything scoped as
"fix the crossing bug" is scoped against something not yet measured.

## Why five leaked scans produce two events

The stage traces show it directly. All five `api_manual` operations acquire the
slot and enter `run_scan`; only two reach transport construction:

```
scan A  ... listing_submitted -> listing_started -> listing_transport_constructed
           -> detail_executor_created -> detail_submitted -> detail_finished -> ...
scan B  ... listing_submitted -> listing_started -> results_ready -> ...
```

Three stop at `listing_started -> results_ready` without ever constructing.
Round 3 reconciled the five/two gap by inspection; the trace now shows it.

## Behaviour neutrality

| run | result | exit |
|---|---|---|
| subset, tracing OFF, pre-Phase-2 | 296 passed, 4 skipped | 0 |
| subset, tracing OFF, post-Phase-2 | 296 passed, 4 skipped | 0 |
| subset, tracing ON, with gate | 296 passed, 4 skipped | 1 *(gate, by design)* |
| `tests/test_scan_context.py` | 20 passed | 0 |

Identical pass counts with tracing off. The only exit-code change is the
netwatch gate firing, which is Phase 1 working as intended.

One test double needed widening: `tests/test_background_scanner.py:55`
`_FakeScanner.run_scan` had an explicit signature and did not accept the new
optional `operation_context`. The other four doubles in the suite already use
`**kwargs`. Production behaviour is unchanged; the double was simply an
incomplete stand-in for a widened interface.

## Full-suite triage (Phase 1 follow-up)

Run at the pre-Phase-2 baseline: **4197 passed, 1 failed, 600s, EXIT=1**, with
**19 unauthorized egress attempts across 5 distinct hosts**:

| count | host |
|---|---|
| 8 | `adit-hd.com` |
| 4 | `ollama` |
| 4 | `x` |
| 2 | `hdencode.org` |
| 1 | `192.168.1.1` |

The reproduction subset showed only the 2 `hdencode.org` attempts. The real
blast radius is nearly ten times larger and reaches **the operator's own
infrastructure** — `ollama:11434` and a LAN address. `x` is almost certainly a
placeholder config value being used as a real hostname, which is worth a look
on its own.

### The one failure was my guard's fault, not a defect

`test_ssrf_rejects_private_discord_webhook` failed because the app's SSRF
protection **resolves** a configured webhook to decide whether it points at a
private range, and the guard was blocking that resolution.

`getaddrinfo` on a *numeric literal* issues no DNS query — it parses locally,
and nothing leaves the host. Blocking it was simply wrong. The guard now allows
literal-IP resolution while still blocking connections to those addresses.
Re-verified after the fix:

| case | exit |
|---|---|
| guard control (no egress) | 0 |
| swallowed-egress mutation | 1 |
| direct-egress mutation | 1 |
| `test_ssrf_rejects_private_discord_webhook` | **0** (was 1) |
| `TestScanner` (real detection) | 1 |

This is precisely what the full-suite triage was for, and it argues for running
it *before* trusting a gate rather than after.

## Limits

- Local container runs only; no CI on this branch, nothing machine-attested.
- The full-suite figure above predates Phase 2 and predates the guard fix. It
  has **not** been re-run since either change.
- Unblocked egress volume and timing remain unmeasured.
- `0 crossed ownership` is an observation over 22 operations in one subset, not
  a proof that the crossing cannot happen.
