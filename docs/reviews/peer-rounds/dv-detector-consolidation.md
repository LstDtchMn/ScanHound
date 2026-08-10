# Consolidating two parallel DV branches — plan review (before execution)

**Date:** 2026-08-10
**Author:** Claude (session `e7d059a1`)
**Reviewer:** ChatGPT
**Repository:** `LstDtchMn/ScanHound` (private)

**Nothing here has been executed.** This asks for review of a *plan*, plus a first review pass
over a branch that has never been through one. Jesse asked for the review before the consolidation.

| Branch | Head | Base | Author | State |
|---|---|---|---|---|
| `agent/dv-scan-hang-and-starvation` | `db16ed6` | `main` @ `6813260` | Claude session `46af8201` (**still running**) | never peer-reviewed |
| `fix/dv-import-cadence` | `20a804a` | `agent/hdr10plus-design-review` @ `8fbac87` | this session | review requested, not yet reviewed |
| `agent/hdr10plus-design-review` | `8fbac87` | — | this session | live-progress work **merged**, 3 rounds APPROVE |

---

## 1. What happened

Two sessions worked the same problem area overnight without knowing about each other. I found
the other one at 20:41 from a `dovi_tool` process I could not account for, traced it to
`integration_check.py` running out of another session's scratchpad, and identified the session
and branch this morning. Jesse asked us to condense.

**We reached the same conclusion from opposite directions.** My throughput review retracted its
"cannot finish" claim and measured the scan healthy at 79 MB/s. Their `48cbd53` says *"the defect
is two files wedging dovi_tool, not throughput"*, and `b2453b8` goes further, eliminating SMB and
the tool version to land on a **dovi_tool parser bug**. Their branch title
("DV scan cannot converge") predates their own finding and is stale.

## 2. The duplication

Both branches independently implemented, in `scripts/host-detector/dv_host_scan.py`:

- per-file progress logging — `[N] scanning <file> (N GB)` then a result line;
- an `--import-every N` interim `dv-import` during the walk.

`dv_host_scan.py` and `tests/test_dv_host_scan.py` therefore conflict between the branches.
`scripts/run-dv-scan.ps1` does **not** conflict — their branch never touches it.

## 3. Why theirs should win the detector

Their branch is materially broader and fixes the actual defect rather than its symptom:

- `probe_fel_bounded()` with `bounded_first=True` — a bounded probe (1000 frames / 300 s / 60 s
  stall) tried before the full extract, so a wedged title need not stream 80 GB to be classified;
- `_EXTRACT_STALL = 180` — a stall watchdog replacing reliance on the 1800 s wall cap;
- `process_read_bytes(pid)` via kernel32 — stall detected from **bytes actually read**, not
  elapsed time. That is the right distinction: a wall-clock cap cannot tell *slow* from *wedged*,
  and the two want opposite responses;
- `partition_work()` — never-scanned-first ordering, and a `[N/M]` denominator my logging lacks;
- `retry_delay_hours()` / `is_retry_due()` / persisted `attempts` + `error` — the retry-starvation
  half of your round-1 throughput review;
- a scan time budget that stops cleanly instead of being killed mid-file;
- 96 tests across `test_dv_host_scan.py` (23), `test_dv_detect.py` (34),
  `test_process_control.py` (6), `test_dv_labeler.py` (33).

My `fix/dv-import-cadence` is a strict subset of that on the import axis. **The plan is to retire
it**, not to merge it.

## 4. Two things that must NOT be lost — both were merge blockers on my branch

Their branch reintroduces both, through no fault of theirs: it was written in parallel and never
saw the round-1 review.

**(a) A fabricated rate on failed detections.** `dv_host_scan.py`, their line 368:

```python
logger.info("[%d/%d] -> %s (%s) in %.0fs  %.0f MB/s%s",
            scanned + 1, len(work), layer,
            result.get("evidence") or result.get("error") or "?",
            secs, (st.st_size / 1e6) / secs,
            "" if layer != "unknown" else "  FAILED")
```

The rate is computed and printed **unconditionally**. An 80 GB title that wedges for 1800 s logs
`44 MB/s  FAILED` — a number derived by dividing a whole file size by a duration during which
`dovi_tool` may have read any fraction of it, sitting next to the word FAILED. They do surface
`evidence`/`error`, which is better than my original, but your round-1 point was that the
*number* must not be printed at all. This is the defect that motivated the entire live-progress
branch, and it would land on exactly the two titles that already fail nightly.

*Note:* with `probe_fel_bounded` their failure rate should drop sharply, which reduces how often
this fires but not whether it is sound.

**(b) A failed import still reports success.** Their `_post_import()` is unchanged — it logs an
`OSError` and returns nothing — and `main()` ends:

```python
    _post_import(args.api)
    return 0
```

So a completed scan whose import failed exits 0. That is the second half you identified in
round 1 of the live-progress review.

## 5. The proposed plan

1. **Consolidate the detector onto `agent/dv-scan-hang-and-starvation`.** It supersedes
   `fix/dv-import-cadence`, which is retired unmerged.
2. **Port two properties into it**, with tests:
   - print a rate only when detection completed; otherwise `<error>; rate unavailable`;
   - `_post_import()` returns a status, and a failed **final** import makes `main()` return
     nonzero. My branch's `tests/test_dv_host_scan.py` additions cover both and are
     mutation-proven (reverting each fails exactly the tests asserting it, 12 controls unmoved).
3. **Leave the wrapper alone.** `run-dv-scan.ps1` is already merged into
   `agent/hdr10plus-design-review`, approved over three rounds, and live-verified on the 03:00
   run. No conflict.
4. **Resolve `dv_host_scan.py` once**, keeping their `[N/M]` logging and `partition_work`
   ordering over my simpler `[N]` form.

## 6. Questions

1. **Is the direction right?** Consolidating onto theirs and retiring mine — or is there
   something in the narrower, reviewed branch worth keeping as the base instead?
2. **Are the two ported properties specified correctly**, or should a failed final import be a
   distinct exit code rather than a generic failure? Their time-budget stop is a *clean* early
   exit, which may deserve different treatment from a killed run.
3. **Their branch has never been reviewed.** `probe_fel_bounded` decides a layer from a bounded
   probe rather than a full extract — under what conditions could that return a *wrong* layer
   rather than an inconclusive one? A false `fel`/`mel` writes a wrong Plex label, which is worse
   than no label. Their `dv_labeler` change ("make the manual DV label sync additive by default")
   is adjacent and worth reading together.
4. **`process_read_bytes` via kernel32** — Windows-only. What happens to `run_cancellable` on the
   container's Linux path, and is the fallback inert there?
5. **Merge order.** Their branch is based on `main`; mine sits on
   `agent/hdr10plus-design-review`, which is 21 commits ahead of `main` and carries the wrapper
   the scheduled task actually executes. **Neither branch contains the date-bomb fix `a88d541`**,
   and their own checklist says it should merge first. What is the safe order?

## 7. Not verified

- I have **not** run their test suite, and I have not executed their branch.
- I have **not** verified that a container-side reader sees WAL commits made by the Windows-side
  writer across the bind mount. If it does not, interim imports deliver stale data — better than
  never, but unproven. Jesse has declined a live production import until this review lands.
- The 622-row host store vs 466-row container gap is unchanged; Plex DV labels remain stale since
  2026-07-26 on either plan until an import actually runs.

Please review the two branches via the connector rather than this summary.
