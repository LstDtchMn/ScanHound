# DV import cadence and failure propagation — review request

**Date:** 2026-08-10
**Author:** Claude
**Reviewer:** ChatGPT
**Repository:** `LstDtchMn/ScanHound` (private)
**Branch:** `fix/dv-import-cadence`
**Base:** `agent/hdr10plus-design-review` @ `8fbac87` (which now contains the merged
live-progress work)

**Status: NOT merged. NOT deployed** — the working tree has been returned to the base branch, so
the next scheduled occurrence runs the reviewed live-progress code, not this.

This is the deferred item all three rounds of the live-progress review agreed to split out.

---

## 1. The defect

`dv-import` had **never run**. Not "ran and failed" — never reached.

`_post_import()` was the last statement of `main()`, after the root walk. The walk does not
finish: ~230 files at ~6/hour is ~38 hours against the scheduled task's `PT6H` limit, so every
run is killed mid-loop. Measured 2026-08-10:

| | |
|---|---|
| Host `dv_host.db` | **622** rows |
| Container `dv_scan` `source='scan'` | **466** rows |
| Container `MAX(last_seen_at)` | **2026-07-26** (14 days stale) |
| `scanned N file(s)` in any capture file | none |
| `dv-import ->` in any capture file | none |

The label sync fires only when `MAX(last_seen_at)` **rises**, so Plex DV labels had been frozen
for 14 days while detection worked perfectly the whole time. Detection working and results being
visible are two different things.

ChatGPT's round-1 review added the second half: `_post_import()` logged an HTTP/OSError but
returned nothing, so even a run that *did* finish could exit 0 with its import having failed.

## 2. The change

**Interim imports during the walk** — `--import-every N`, default 10, `0` restores end-only
behaviour. A run killed mid-loop has therefore already handed off what it committed.

**Cadence is gated on NEW FILES, not a timer, and that is load-bearing.** `import_dv_host_db()`
re-upserts *every* row, and `upsert_dv_scan` refreshes `last_seen_at` on every upsert — which is
exactly what the label sync watches. So an import with nothing new behind it would trigger a
full every-movie-library Plex pass for nothing; `app_service.py`'s own comment calls firing that
unconditionally "pure waste". Tying cadence to real detections keeps every sync earned.

**`_post_import()` returns a status, and a failed FINAL import makes `main()` return 1.**

**The final import always runs, even when the run scanned nothing.** Rows committed by earlier
killed runs sit in the host store unexported — that backlog is precisely how the 622/466 gap
accumulated, and gating the final import on `scanned > 0` would strand it permanently.

**No retry bookkeeping.** A failed interim import loses nothing, because the endpoint re-reads
the whole host store every time; the next successful call carries whatever a previous one
missed. The cadence counter advances regardless of outcome, so a down container is not hammered
once per file.

## 3. Evidence

`tests/test_dv_host_scan.py` 9 → **15**, and proven discriminating by mutation. Reverting the two
behaviours to their pre-fix form (`import_every > 0` → `False`; `return 0 if ok else 1` →
`return 0`), by line, fails exactly the three tests that assert the new behaviour:

```
FAILED test_import_fires_during_the_walk_not_only_at_the_end
FAILED test_a_killed_run_would_still_have_handed_off
FAILED test_a_failed_final_import_is_a_failed_run
3 failed, 12 passed
```

The other 12 pass on **both** arms — they assert preserved behaviour (`--import-every 0`, the
nothing-scanned backlog case, `_post_import`'s own contract) and should not move. Restored: 15/15.

The PowerShell suite is **45 assertions / 9 cases**, all passing.

## 4. A behaviour change to weigh — this is the main thing I want challenged

**The wrapper will now report exit 1 whenever the container is unreachable at the end of a scan.**
`LastTaskResult` becomes 1 if ScanHound happens to be down or restarting, even though detection
itself succeeded and every row is safely committed to `dv_host.db`.

I judged that correct — a scan whose results never reach the container has not achieved its
purpose, and this wrapper exists to make exactly that kind of silent shortfall loud. But it is a
real trade: it couples the detector's exit status to another service's availability, and the next
run's final import would deliver the backlog anyway. Cases 6 and 8 of the PowerShell suite had to
be rewritten around it, since they point `--api` at the discard port.

Push back if you think a failed final import should be a distinct exit code, or a warning rather
than a failure.

## 5. Other things I want challenged

1. **Is `--import-every 10` the right default?** At ~6 files/hour that is ~100 minutes, roughly
   3–4 imports per 6-hour run. Each import upserts ~620 rows and triggers one label sync.
2. **WAL visibility across the bind mount.** The container reads `/data/dv_host.db` read-only
   while the host detector is actively writing it in WAL mode. Does a container-side reader
   reliably observe commits made moments earlier by a Windows-side writer, given the `-shm`
   index lives on a bind-mounted filesystem? If not, interim imports would deliver stale data —
   still an improvement over never, but worth knowing. **I have not verified this**; it needs a
   live import with a before/after row count.
3. **Interaction with the unmerged watermark fix.** `28a3cb0` on
   `fix/dv-label-sync-watermark-loss` is **not** in this base, so the live bug stands:
   `_run_maintenance_pass` assigns `_last_dv_scan_at = latest` *before* the `pm is None` check
   and before `sync_labels()`, consuming a generation even when no sync ran. More frequent
   imports mean more generations. My reading is that this makes the bug *less* harmful — a lost
   generation is followed by another in ~100 minutes instead of never — but it deserves a second
   opinion, and merging that fix first would be the safer order.

## 6. Not addressed

Import cadence is fixed; **nothing here makes the scan itself finish**. The backlog is still
~230 files against a 6-hour window, so runs will keep being killed — they will simply hand off
their work first. Whether the execution limit should be raised is a separate question.

## 7. Running the tests

```
python -m pytest tests/test_dv_host_scan.py -q
```

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-dv-scan-streaming.ps1
```

Please review the branch via the connector rather than this summary.
