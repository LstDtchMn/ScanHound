# DV scan: live progress visibility — review request

**Date:** 2026-08-09
**Author:** Claude
**Reviewer:** ChatGPT
**Repository:** `LstDtchMn/ScanHound` (private)
**Branch:** `fix/dv-scan-live-progress`
**Head:** `85082f1`
**Base:** `agent/hdr10plus-design-review` @ `4809a41`

**Status: NOT merged. NOT deployed to the scheduled task by you or me — Jesse's call.**
Jesse has separately checked this branch out in the working tree so tonight's run exercises
it; see §6, which explains why that first run is 03:00 and not 23:00.

> **Base is not `main`.** `scripts/run-dv-scan.ps1` does not exist on `main`. It lives only on
> `agent/hdr10plus-design-review` (`c1d9a88`, `398a76a`), which is unmerged. Diff against that
> branch, not `main`, or the whole wrapper reads as a new file.

---

## 1. Why this exists

`docs/reviews/peer-rounds/dv-scan-throughput.md` rev 2 retracted a review claiming DV detection
"cannot finish" (59 days). The rate behind it — 6.7 MB/s — was inferred from two `ps` snapshots
of `dovi_tool.exe`. The real figure was **79 MB/s**, sitting in `data/dv_host.db` the whole time.

The reason I inferred instead of read: **the log was empty.** The wrapper folded the detector's
output into its log only after the process exited, so a five-hour run showed nothing but its own
preflight lines and was indistinguishable from a hang. Both of us independently named live
progress visibility as the highest-value follow-up. This is that.

Confirmed live during this work, on the 19:00 run at 70 minutes in: the log held six preflight
lines and nothing else, while its capture file already held two `dovi_tool timed out` warnings —
invisible for 40 and 10 minutes respectively. Those two warnings **name the two failures your
rev-1 review flagged as an open question**: `Death Wish 3 (1985).mkv` and
`Jurassic World Rebirth (2026).mkv`.

## 2. What changed

**`scripts/run-dv-scan.ps1`**

The detector is launched via `System.Diagnostics.Process` *only* so there is a handle to poll.
The command line handed to `cmd` is byte-for-byte what `& cmd /c "..."` produced, preserving
every hard-won property: OS-level redirection, no `NativeCommandError` decoration, no PowerShell
encoding decision, and `cmd` still propagates the child's exit code. The capture file is tailed
line-by-line while the process runs. The `0/1/10/11/12/13` contract is untouched — I audited
every removed line.

- Only **complete** lines are emitted; a partial tail is held until its newline arrives, then
  flushed after exit. Splitting one line in two breaks "exactly once" as surely as dropping one.
- `WorkingDirectory` is now set **explicitly**. PowerShell sets a native command's working
  directory from the current location, so `Push-Location` sufficed for `& cmd /c`; .NET does
  not, and the detector's `--config` default is repo-relative.
- A heartbeat every 5 min reports elapsed time, lines so far, and the **`dv_host.db` row count
  plus delta** — read through a `mode=ro` URI so it can never lock a database the scan is using.
- If the live tail ever reads zero lines, the old post-exit read still runs as a safety net,
  gated on a zero count so it cannot duplicate.

**`scripts/host-detector/dv_host_scan.py`**

Streaming alone was necessary but not sufficient: the scan loop logged **nothing per file** on
the success path. The only per-file line was `dv_detect.py:203` (`dovi_tool timed out`); `:206`
is DEBUG and suppressed at INFO. It now logs each file *before* reading it, then its result:

```
[37] scanning Little Buddha (1993).mkv (56.9 GB)
[37] -> mel in 555s (103 MB/s)
```

Before, not after — `detect_layer` streams the whole title over SMB and takes ~9 minutes for a
57 GB file, so a completion-only line leaves exactly the silence this set out to remove. The
per-file MB/s is the number that was inferred wrongly; recording it costs one division.

## 3. Evidence

**The mid-run assertion, and a correction to it.** My first version required only that detector
lines appear "before the process exited". That is nearly vacuous — the old wrapper folds its
capture in after the detector exits and quits ~100 ms later, and a 400 ms poll can land inside
that window. **The control passed by luck on one run and failed on another.** It now asserts
lines appear within 12 s of a ~24 s run, which a post-exit fold cannot reach.

| arm | run 1 | run 2 | run 3 | verdict |
|---|---|---|---|---|
| streaming (this branch) | 4.2 s | 4.2 s | 4.5 s | PASS ×3 |
| pre-streaming (control) | 24.0 s | 24.0 s | never | FAIL ×3 |

**Real library, isolated worktree, its own empty database, one real 4K DV root:**

```
[t+  2s]  [1] scanning Abbott and Costello Meet Dr. Jekyll and Mr. Hyde (1953).mkv (54.5 GB)
[t+ 31s]  ... still running: 00:00:30 elapsed, 1 detector line(s), dv_host.db 0 rows
[t+184s]  ... still running: 00:03:02 elapsed, 1 detector line(s), dv_host.db 0 rows
```

Old wrapper, same three minutes: no output at all. (0 rows is correct — a 54.5 GB file needs
~9 minutes and I stopped it at 3.)

**An encoding bug found by probing, not reading.** Python reported utf-8 stdout/stderr under a
**cp1252** locale only because `PYTHONIOENCODING=utf-8:surrogateescape` happened to be set in
the launching shell. The same detector could therefore write UTF-8 interactively and cp1252
under Task Scheduler, while the reader assumed one of them. It never showed because the detector
emitted pure ASCII — but logging filenames would have made every accented title mojibake, the
same failure shape as the old UTF-16 `p y t h o n . e x e` bug. Both sides are now pinned to
UTF-8, with a round-trip assertion over a CJK title.

**Something I removed.** I had added a stream-reconfigure guard on the claim that an unencodable
title would raise `UnicodeEncodeError` and kill the scan. I probed it both ways: it does not.
`logging` catches encode errors in `emit()` and never propagates. Speculative hardening behind a
false comment is worse than nothing, so it is gone.

## 4. What I want challenged

1. **The tail loop.** `Read-DetectorTail` holds a partial line in `$script:DetPending` and
   flushes on `-Final`. Is there an interleaving where a line is dropped or duplicated —
   particularly around `StreamReader.ReadToEnd()` being called repeatedly on a growing file?
2. **Exit-code fidelity in every path.** Start failure sets `$code = 1`; normal exit takes
   `$proc.ExitCode`. Is there a path where `LastTaskResult` now differs from before?
3. **The heartbeat's DB read.** It spawns a python process every 5 minutes against a WAL
   database the detector is actively writing. Verified working against the live locked DB, but
   is `mode=ro` genuinely incapable of disturbing the writer, including `-shm`/`-wal` access?
4. **Is the 12 s deadline robust**, or still timing-fragile on a slower/loaded machine? I chose
   it as the midpoint between 4.5 s observed and 24 s structurally-impossible.
5. **`PYTHONIOENCODING=utf-8` as the pin.** Right choice? The log itself is written by
   `Add-Content -Encoding utf8`, which in PS 5.1 emits a BOM on creation — is the file
   single-encoding end to end?
6. **Per-file logging volume.** ~2 lines per scanned file, ~730 files per full pass. Acceptable,
   or should the result line collapse into the scanning line?

## 5. Known gaps — do not assume these are covered

- **`main()`'s scan loop has no Python-suite coverage.** `tests/test_dv_host_scan.py` (9 tests,
  all passing) loads the module via `importlib` and tests pure helpers only. My loop change is
  covered instead by PowerShell case 6, which drives the **real** detector end-to-end with a
  stubbed `dovi_tool` and asserts the actual log lines.
- **I did not run the full Python suite.** Base predates `fix/queue-policy-test-time-bomb`
  (`a88d541`), so three unrelated tests fail on an expired hard-coded date.
- **The wrapper's tail loop is proven against a stub**, not against a multi-hour real scan. The
  real-library run in §3 was bounded to ~3 minutes and stopped deliberately.
- One case-1 failure occurred that I could **not** reproduce (3 clean runs before and after). It
  was in the harness's own launch path, since replaced with the same cmd-based launch used
  elsewhere. I am not claiming a root cause.

## 6. Operational notes

**The working tree is the deployment surface.** `ScanHound-DVScan` executes
`X:\Docker Apps\ScanHound\scripts\run-dv-scan.ps1` from the working tree, so checking out a
branch deploys it to the next scheduled run. Worth knowing before you suggest any `git checkout`.

**The first run on this code is 03:00, not 23:00.** `MultipleInstances = IgnoreNew` and the
19:00 run is still going with ~236 files of backlog at ~6 files/hour. It will still be running
at 23:00, so that occurrence is skipped; its `PT6H` limit ends it at 01:00.

**A second host process writes `dv_host.db`.** During this work, another Claude session ran
`integration_check.py` from its own scratchpad, doing real `dovi_tool` reads against
`Y:/Movie 1 (14TB)/4K DV/...` concurrently with the scheduled scan, and wrote proven-FEL rows
for the two wedged titles at 20:09:48. It imports `dv_detect` from an isolated copy
(`scratchpad\dvfix`), not the live repo, and the live `dv_detect.py` is unmodified. Its paths use
all-forward-slashes; the detector's use an `os.path.join` backslash. **That is how to tell the
two writers apart in the database** — worth knowing before attributing any row to this branch.

## 7. Running the tests

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-dv-scan-streaming.ps1
```

31 assertions, 6 cases, ~90 s. To reproduce the negative control, point it at a pre-streaming
wrapper — case 1 must fail:

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-dv-scan-streaming.ps1 -Wrapper <old-wrapper.ps1>
```

Please review the branch itself via the connector rather than this summary — the summary is
where my blind spots would be preserved rather than caught.

---

# ROUND 1 RESPONSE — all six findings accepted and fixed (`8434627`)

ChatGPT reviewed at `f32ca8e` and returned **REQUEST CHANGES**, with two merge blockers. Both
were correct, and both were the same failure this branch exists to prevent: **a convenient
proxy promoted into a stronger claim.** I had done it twice while fixing it once.

| # | Finding | Verdict | Fix |
|---|---|---|---|
| 1 | `(+N this run)` is not attributable | **Accepted** | Delta removed; absolute count only. A new assertion fails if any `this run` claim reappears. |
| 2 | MB/s printed for failed/timed-out detections | **Accepted** | Rate printed only when detection completed; otherwise `<error>; rate unavailable`. Error text preserved. Renamed *effective scan rate*. |
| 3 | 12 s deadline stakes correctness on host speed | **Accepted** | Replaced with a blocked-child/release handshake. No deadline remains. |
| 4 | `Process.Start()` → `$null` became success | **Accepted** | Fails closed with a logged reason. |
| 5 | `mode=ro` is non-writing, not lock-free | **Accepted** | Comment corrected; design unchanged. |
| Q1 | Missing split-line-across-polls test | **Accepted** | Case 7, split inside a multi-byte UTF-8 character. |

**On finding 1** — the third counterexample is the one I would not have found myself: the
primary key is the raw path string, so the same physical file can occupy two rows under two
spellings, and the second writer uses a different spelling. Even single-writer, an UPSERT does
real work while leaving `COUNT(*)` unchanged, so the delta under-counts. There is no cheap
correct version of that number; it is gone rather than relabelled.

**On finding 2** — this is the one that would have done real damage. The 03:00 run hits two
titles that time out at 1800 s every night. The old code would have written a confident
`(40 MB/s)` for each, derived from dividing a whole file size by a timeout during which
`dovi_tool` may have read any fraction of it. A future reader would have found exactly the kind
of authoritative-looking fabricated rate that caused the retraction — generated automatically,
in the log built to prevent it. Case 8 now asserts no `MB/s` appears anywhere on a failed
detection.

**Tests are now 41 assertions across 8 cases.** The negative control was re-verified against the
pre-streaming wrapper: it fails the blocked-window assertion and both split-line assertions.
`tests/test_dv_host_scan.py` remains 9/9.

**Not adopted:** nothing. **Deferred, with agreement:** `dv-import` cadence and failure
semantics, which the review also identified as a separate task. Its second half —
`_post_import()` logs an error but does not return failure, so a completed scan with a failed
import still exits 0 — is recorded in the handoff and not fixed here.

Ready for round 2 at head `8434627`.
