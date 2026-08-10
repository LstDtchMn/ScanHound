# DV round-4 POST-rows — re-review response

**Repository:** `LstDtchMn/ScanHound`
**Branch:** `agent/dv-detector-consolidation`
**Re-reviewed head:** `4544ed6`
**Remediation head:** `355e9d2`
**Base:** `6813260`
**Date:** 2026-08-10

All three findings from the round-4 re-review of `4544ed6` are closed, plus the four
non-blocking cleanups. Whole-tree suite **4697 passed / 4 skipped / 0** off
`scanhound:latest` (tree copied in with these edits; `pip install pytest pytest-asyncio
"httpx<0.28"`).

## F1 (HIGH) — real write failures converted to success — FIXED

You were right, and it was in three places, not one. `DatabaseManager._mutate()` returns
`True` on commit / `False` on exception, but `upsert_dv_scan`, `upsert_media_inventory`,
and `upsert_media_probe` each returned `self._mutate(...) is not None`. Since
`False is not None` is `True`, a failed write reported success — `/dv-host-rows` returned
`ok:true`/200 and `_post_rows` accepted it. All three now return the adapter result
directly (`backend/database.py`).

Note for the record: a naïve `grep -E "_mutate(...) is not None"` returns zero matches
because the call spans multiple lines; the defect is only visible by reading the return
statement. Confirmed by reading, not grep.

**Test change is the substance of the fix.** The old failure tests monkeypatched
`upsert_dv_scan` itself, which bypasses the buggy `...is not None` expression — they could
not have caught this. They now force the REAL `dm._mutate` to return `False` with the REAL
`upsert_dv_scan` in the chain, assert HTTP 500 / `ok:false` / `processed:0` / `failed:1`,
and confirm nothing persisted. Added a positive control (real success → one processed row,
persisted). **Mutation-verified:** re-injecting `is not None` on the `upsert_dv_scan`
return fails all three F1 tests (the endpoint returns 200 again); removing it passes 46/46
DV + 4697 whole-tree.

## F2 (contract) — blank-path row labelled successful — FIXED

`DvHostRow.path` now has a field validator that rejects blank/whitespace at request
validation (422). The endpoint's success invariant is `processed == source_rows` with no
skipped-row term. Producer and server now share one invariant: every accepted body row is
processed exactly once or the request is non-2xx.

## F3 (contract) — legacy endpoint 200 on partial upsert — FIXED

Both endpoints share `_import_response(result)`: `failed != 0` or
`processed != source_rows` → HTTP 500 with `ok:false`. `/dv-import` now runs its result
through it after the read (503 on read failure is unchanged). Legacy-route partial-upsert
test added using the real `upsert_dv_scan` over a forced `_mutate` failure.

## Non-blocking cleanups — all done

- Removed the duplicate `import json` in `dv_host_scan.py`.
- `_post_rows` guards a non-object JSON body (`isinstance(body, dict)`) before `.get`.
- `schema_version` is now SENT by the producer (`DV_ROWS_SCHEMA_VERSION = 1`) and ENFORCED
  server-side (`422` on mismatch).
- Stale `dv-import` help/comments reworded to describe the row POST.

## Still open (deploy runbook, not code — as you noted)

- Canary reorder around the new protocol proving ONE exact sentinel row's VALUES land in
  `crawler.db` (count equality proves cardinality, not correctness).
- Authenticated-access preflight if production auth is enabled (the `/rename` route would
  otherwise 401 and the run fails loudly).
- The PT6H runtime-limit guard resolved or independently verified before scheduling.

## Ask

Please verify `355e9d2` against your three findings — in particular that the F1 tests now
exercise the real adapter and discriminate, and that both endpoints share the one
result-to-response invariant. If anything is not closed, name it; otherwise this is ready
for the deploy-runbook items above and Jesse's merge call.
