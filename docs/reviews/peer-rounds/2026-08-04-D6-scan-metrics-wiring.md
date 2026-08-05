# Peer review request — D-6, scan metrics wired end to end

**Repository:** LstDtchMn/ScanHound
**Branch:** `agent/scan-metrics-wiring`
**Head:** `11762b6`
**Base:** `af9c299` (main)

Read the branch through the GitHub connector. **Review the code and the tests,
not this summary** — if you find yourself assessing my description rather than
the diff, stop and say so.

---

## What this closes

Contract row **D-6**: scan metrics wired, taxonomy slot, persistence,
conservation, injected failure/cancellation tests.

`backend/scan_metrics.py` already existed and had been through adversarial
self-review, but **nothing ever called it**. This branch supplies both callers
and a place to keep the results.

The production symptom it addresses: a scan fetched ~128 HDEncode detail pages,
kept 1–4, logged both discard paths at DEBUG, and published as an ordinary
success. A ~98% loss was invisible.

## The two commits that matter

- `d106ddd` — the detail scraper books which of its **seven** exits fired.
- `48dbb91` — the scanner books scheduling / cancellation / construction, adds
  the `scan_metrics` table, and reconciles every ticket after the pool drains.

## The property under test

Conservation, checked at the end of every pass:

```
detail_scheduled = detail_started + detail_cancelled_before_start
detail_started   = returned_data + returned_none + raised_exception
                 + cancelled_after_start
```

When it fails the log says the numbers are incomplete rather than publishing a
ratio derived from broken books, and the stored row carries
`conservation_ok = 0`.

## Where I would attack this if I were you

1. **The except-clause ordering.** `HDEncodeRequestCancelled` **subclasses**
   `HDEncodeTrafficDenied`. Caught parent-first, every operator Stop books
   `detail_traffic_denied` — the source blamed for the user's own action. I
   catch the subclass first and pin it with a negative control
   (`test_a_cancelled_request_is_not_recorded_as_the_source_denying_us`).
   **Check I have not got this backwards, and that the control actually
   discriminates.** I verified it does: reversing the order fails that test
   alone, with `HDEncodeRequestCancelled booked detail_traffic_denied`, while
   the other 12 still pass.

2. **Double-booking between the two layers.** Both the scanner and the scraper
   call `note_started()` and `data_returned()`. That is safe only because both
   are idempotent. If that ever stops being true every count doubles.
   `test_the_two_instrumented_layers_do_not_double_count` runs the real scraper
   under the real scan loop to pin it. **Is that test actually exercising both
   layers, or have I stubbed my way out of the thing I claim to be testing?**

3. **Cancellation semantics.** A Stop strands posts in three different states
   (cancelled while queued, cancelled after start, completed-but-never-
   consumed). I assert the failure buckets stay at zero through a mid-scan Stop
   of 40 posts. **Is `_future_terminal_state` correct?** It checks
   `cancelled()` then `done()` before `exception()`, because `.exception()`
   raises on a cancelled future and blocks on an unfinished one.

4. **The staging of the outer `except`.** It spans the whole scrape method. I
   stage on whether a response exists rather than always saying "parse
   failure". **Is that distinction real, or am I over-claiming?**

## A bug my own tests caught, disclosed

The scanner initially let the *scraper* book "this post started" and "data came
back". Those are the *scanner's* facts. The end-to-end test stubs the scraper —
as any caller swapping that layer would — and returned `detail_started = 0`
while 6 items shipped: silently zeroed accounting on a scan that worked. Both
layers now book them.

## Attestation

Local runs, in a throwaway container with the code copied in (not the 9p bind
mount), byte-verified by raw sha256 before each run:

- 25 new tests across `test_scan_metrics_wiring.py` (13),
  `test_scan_metrics_persistence.py` (6), `test_scan_metrics_end_to_end.py` (10)
- 431 pass across scanner, extended-scanner, background-scanner, database,
  detail-scraper, pacing and scan-metrics suites

**CI had produced zero runs on this branch** until `11762b6`, because the base's
workflow triggers only on `[main, master, develop]`. That fix exists on
`agent/audit-fixes-2026-08` and has not reached main, so every branch cut from
main inherits the gap. CI is running now — **check the run result rather than
taking the numbers above on trust.**

## Not in scope

Nothing reads these metrics yet — no API route, no UI. Recording only. A
follow-up should surface them; that is deliberately not here, so the recording
contract can be reviewed on its own.
