# DV scan live progress — session handoff

**Date:** 2026-08-09 (evening)
**Author:** Claude (session `e7d059a1`)
**For:** the main ScanHound development chat
**Branch:** `fix/dv-scan-live-progress` — head `03ac38d`, pushed, **NOT merged**
**Base:** `agent/hdr10plus-design-review` @ `4809a41` (**not `main`** — see trap 1)

---

## 0. TL;DR

The DV scan wrapper used to write a log containing nothing but its own preflight lines for the
entire duration of a multi-hour run. It now streams the detector's output live, the detector
reports each file as it starts and finishes, and a heartbeat reports the `dv_host.db` row count.

Three commits, all pushed, none merged. The branch **is** checked out in the working tree at
Jesse's request, and because the scheduled task runs from the working tree, **the new code will
execute on the 03:00 run.** Nothing else was deployed. Peer review by ChatGPT is pending.

---

## 1. Why this work happened

On 2026-08-09 a five-hour DV scan produced a log with only wrapper preflight lines, so the run
was indistinguishable from a hang. With no progress visible, throughput was inferred from two
`ps` snapshots of `dovi_tool.exe`, giving **6.7 MB/s**. The real figure — read afterwards from
`data/dv_host.db`, which had recorded it all along — was **79 MB/s**.

That 12x error produced a review doc claiming the design "cannot finish" (59 days), which had to
be retracted in full (`docs/reviews/peer-rounds/dv-scan-throughput.md` rev 2). Both Claude and
ChatGPT independently named live progress visibility as the highest-value follow-up.

**The lesson the code now encodes:** the log's silence is what made inference feel acceptable.
Read the artifact, not the process list — and make the artifact visible while it is being made.

---

## 2. What was delivered

### 2.1 `scripts/run-dv-scan.ps1` — stream the output live (commit `6da812f`)

The detector is launched through `System.Diagnostics.Process` **only** so there is a handle to
poll. The command line handed to `cmd` is byte-for-byte what `& cmd /c "..."` produced, so every
previously hard-won property survives:

- OS-level redirection via `cmd`'s `>` — PowerShell never touches the streams
- no `NativeCommandError` decoration (the failure that killed the 11:00 run)
- no PowerShell encoding decision
- `cmd` still propagates the child's exit code

The capture file is then tailed line-by-line while the process runs. **The `0/1/10/11/12/13`
exit-code contract is unchanged** — every removed line in the diff was audited to confirm it.

Details that are load-bearing:

- **Only complete lines are emitted.** A poll can land mid-write; flushing half a line splits one
  detector line into two, which breaks "exactly once" as surely as dropping one. The partial tail
  is held until its newline arrives, and `-Final` flushes an unterminated last line after exit.
- **`WorkingDirectory` is set explicitly.** PowerShell sets a native command's working directory
  from the current location, so `Push-Location` was sufficient for `& cmd /c`. .NET does **not**.
  Without this the detector would run from the wrong directory and its repo-relative `--config`
  default would resolve elsewhere. There is a test assertion for exactly this.
- **`python -u`.** Probed directly: python's stderr (where `logging` writes) is line-buffered and
  streams on its own, but **stdout redirected to a file is block-buffered** and arrives only at
  exit. The detector uses no `print()` today; `-u` stops one added later from silently undoing
  this.
- **A safety net.** If the live tail ever reads zero lines, the old post-exit read still runs —
  gated on a zero count, the only state in which a re-read cannot duplicate anything.

An approach that does **not** work, recorded so nobody retries it:
`Start-Process -ArgumentList '/c', $inner` — the `>` redirection does not survive being split
across an argument array. Measured against a 24 s stub: the wrapper reported "finished OK" in
14 s having captured nothing, because the child exited immediately.

### 2.2 Per-file logging, DB heartbeat, encoding pin (commit `85082f1`)

Streaming alone was **necessary but not sufficient**, and this is the single most important
finding for anyone continuing this work: `dv_host_scan.py`'s scan loop logged **nothing per file**
on the success path. The only per-file line was `dv_detect.py:203` (`dovi_tool timed out`);
`:206` is DEBUG and suppressed at INFO. A healthy multi-hour run would have streamed silence.

So `dv_host_scan.py` now logs each file **before** reading it, then its result:

```
[37] scanning Little Buddha (1993).mkv (56.9 GB)
[37] -> mel in 555s (103 MB/s)
```

Before, not after: `detect_layer` streams the whole title over SMB and takes ~9 minutes for a
57 GB file, so a completion-only line leaves exactly the silence this set out to remove. The
per-file MB/s is the number that was inferred wrongly; recording it costs one division.

The wrapper's heartbeat (default every 5 min) now reports elapsed time, lines so far, and the
**`dv_host.db` row count plus this-run delta**, read through a `mode=ro` URI connection so it can
never take a lock on a database the scan is using. Verified against the live database while the
19:00 run held it open in WAL mode.

### 2.3 Peer-review request (commit `03ac38d`)

`docs/reviews/peer-rounds/dv-scan-live-progress.md` — evidence-first, six specific questions, and
an explicit list of what is **not** covered. Sent to ChatGPT via the connector.

---

## 3. Bugs found along the way — including my own

**A latent cross-environment encoding bug (real, now fixed).** Python reported utf-8
stdout/stderr under a **cp1252** locale only because `PYTHONIOENCODING=utf-8:surrogateescape`
happened to be set in the launching shell. The same detector could therefore write UTF-8
interactively and cp1252 under Task Scheduler, while the wrapper's reader assumed one of them.
It never showed because the detector emitted pure ASCII — but logging filenames would have turned
every accented title into mojibake, the same failure shape as the old UTF-16
`p y t h o n . e x e` bug. Both sides are now pinned to UTF-8, with a round-trip assertion over a
CJK title.

**A guard I added on a false premise (removed).** I added a stream-reconfigure guard claiming an
unencodable title would raise `UnicodeEncodeError` and kill the scan. Probed both ways: it does
not. `logging` catches encode errors in `emit()` and never propagates. Speculative hardening
behind a confident-but-false comment is worse than no hardening, so it was removed rather than
left to mislead.

**A nearly-vacuous test (corrected — the most important item here).** The mid-run assertion
originally required only that detector lines appear *before the process exited*. The old wrapper
folds its capture in after the detector exits and quits ~100 ms later, and a 400 ms poll can land
inside that window — so **the control passed by luck on one run and failed on another.** It now
asserts lines appear within 12 s of a ~24 s run, which a post-exit fold structurally cannot reach.

| arm | run 1 | run 2 | run 3 | verdict |
|---|---|---|---|---|
| streaming (this branch) | 4.2 s | 4.2 s | 4.5 s | PASS ×3 |
| pre-streaming (control) | 24.0 s | 24.0 s | never | FAIL ×3 |

---

## 4. Evidence

**Test suite:** `scripts/test-dv-scan-streaming.ps1` — 31 assertions, 6 cases, ~90 s, stable
across 3 consecutive runs. Cases: streaming happy path (incl. mid-run deadline, exactly-once
accounting, DB heartbeat, working directory, unicode round-trip); nonzero detector exit → 1;
unreachable root → 11 with the detector never invoked; single-root config (the scalar `.Count`
case); unterminated final line; and the **real** detector end-to-end with a stubbed `dovi_tool`.

**Python:** `tests/test_dv_host_scan.py` 9/9 pass.

**Real library**, isolated git worktree, own empty database, one real 4K DV root:

```
[t+  2s]  [1] scanning Abbott and Costello Meet Dr. Jekyll and Mr. Hyde (1953).mkv (54.5 GB)
[t+ 31s]  ... still running: 00:00:30 elapsed, 1 detector line(s), dv_host.db 0 rows
[t+184s]  ... still running: 00:03:02 elapsed, 1 detector line(s), dv_host.db 0 rows
```

Old wrapper, same three minutes: no output at all.

**The symptom, caught live.** At 70 minutes into the 19:00 run the log held six preflight lines
and nothing else, while its capture file already held two `dovi_tool timed out` warnings —
invisible for 40 and 10 minutes. Those warnings **name the two failures the throughput review
left as an open question**: `Death Wish 3 (1985).mkv` and `Jurassic World Rebirth (2026).mkv`.

---

## 5. Live state as of 2026-08-09 20:56 (all re-measured, not carried over)

| | |
|---|---|
| `dv_host` rows | **499** |
| NULL-signature rows (the retry set) | **0** |
| Rows written since the 19:00 run began | **7** in 116 min |
| Layer mix | fel 186 / mel 171 / profile8 92 / profile5 37 / none 13 |
| `unknown` rows | **0** |
| Library total / remaining | 730 / **~231** |
| 19:00 run | still active at 116 min |

**Do not turn "7 rows in 116 min" into a throughput figure.** Two of those minutes-blocks were
30-minute timeouts (60 min of the 116), and a second host process was reading the same NAS
concurrently. That is exactly the inference that produced the retracted 12x error. The per-file
MB/s now written to the log is the number to use instead.

Note the layer mix has **no `unknown` and no NULL signatures**, which differs from the throughput
review's "2 unknowns with NULL sigs". Those two were resolved to `fel` by another session (§6).

---

## 6. Traps for whoever picks this up

1. **`scripts/run-dv-scan.ps1` is NOT on `main`.** It exists only on
   `agent/hdr10plus-design-review` (`c1d9a88`, `398a76a`), unmerged. An instruction to "branch off
   main" to edit it cannot be followed literally, and diffing this branch against `main` makes the
   whole wrapper read as a new file. Check `git cat-file -e main:<path>` before branching.

2. **The working tree IS the deployment surface.** `ScanHound-DVScan` executes
   `X:\Docker Apps\ScanHound\scripts\run-dv-scan.ps1` from the working tree, so **checking out a
   branch deploys it** to the next scheduled occurrence. Leaving a feature branch checked out is
   an unapproved deploy. Restore the branch you found before ending a session, and say so.

3. **`MultipleInstances = IgnoreNew`.** A scheduled occurrence is **silently skipped** while a
   previous run is still going. The 19:00 run blocks 23:00; its `PT6H` limit ends it at 01:00, so
   03:00 is the first occurrence that runs this branch's code.

4. **A second host process writes `dv_host.db`.** Another Claude session (`b4645335`) ran
   `integration_check.py` from its own scratchpad, doing real `dovi_tool` reads against
   `Y:/Movie 1 (14TB)/4K DV/...` concurrently with the scheduled scan, and wrote proven-FEL rows
   for the two wedged titles at 20:09:48. It imports `dv_detect` from an **isolated copy**
   (`scratchpad\dvfix`), not the live repo, and the live `dv_detect.py` is unmodified.
   **Its paths are all-forward-slashes; the detector's carry an `os.path.join` backslash.** That
   is how to tell the two writers apart in the database before attributing any row.

5. **That session is building a stall watchdog** (~180 s) to replace the 1800 s wall cap for those
   same two titles — `dv_detect._EXTRACT_STALL`. It touches `dv_detect.py` while this branch
   touches `dv_host_scan.py`'s loop; **the two will interact at merge time.** Coordinate.

6. **`SCANHOUND-CLOUD-SESSIONS-CATCHUP.md` was being actively written** by another session at
   20:54. This handoff was deliberately written as a separate file rather than appended to it.

---

## 7. What remains

**STATUS: peer review COMPLETE — approved for merge at `03194ad`. The merge itself is Jesse's
call and has NOT been performed.** Round 2 (`e074840`) APPROVE, round 3 (`03194ad`)
APPROVE / MERGE, zero remaining blockers. Response table and both verdicts are in
`docs/reviews/peer-rounds/dv-scan-live-progress.md`.

**LIVE-VERIFIED on the 2026-08-10 03:00 run.** 127 log lines where the old wrapper wrote six and
then nothing: `[1] scanning Rocky Balboa (2006).mkv (43.3 GB)` four seconds in, per-file results
at 93 / 46 / 75 / 111 / 149 MB/s, heartbeats every five minutes with `dv_host.db` climbing
590 → 622, 32 files in 4 h 20 m and zero `rate unavailable` (the two formerly-wedged titles now
carry valid signatures and are skipped). `MultipleInstances=IgnoreNew` also confirmed
empirically: the 07:00 occurrence returned LastTaskResult **2147946720 = 0x80070420**, "an
instance is already running" — that code means SKIPPED, not failed.

**One bug of mine that the live log found, and nothing else would have.** The heartbeat's
elapsed hours used `[int]$span.TotalHours`; PowerShell's `[int]` cast ROUNDS rather than
truncating, so 3 h 35 m rendered as `04:35` — an hour ahead of its own minute field, wrong for
every span with minutes ≥ 30. Fixed in `03194ad` with `[math]::Floor` and a direct regression
test (case 9) that extracts the shipped `Format-Elapsed` rather than restating it. No synthetic
test would have caught it without waiting hours.

**Historical (round 1):**
- **ChatGPT round 1 came back REQUEST CHANGES and all six findings are fixed** (`8434627`).
  Response table in `docs/reviews/peer-rounds/dv-scan-live-progress.md`. **Round 2 not yet run.**
  Two were merge blockers, and both were the same error this branch exists to prevent — a proxy
  promoted into a claim:
  - the heartbeat's `(+N this run)` counted every writer's rows and under-counted UPSERTs, so it
    could not mean "this run". Removed; absolute count only.
  - the per-file `MB/s` printed on **failed and timed-out** detections too, dividing a whole file
    size by a timeout during which `dovi_tool` may have read any fraction of it. That would have
    fabricated an authoritative-looking rate for the two nightly timeout titles, in the very log
    built to stop that. Now `<error>; rate unavailable`, with the error text preserved.
  Also: fail closed when `Process.Start()` yields neither process nor code; the 12-second test
  deadline replaced by a blocked-child handshake (no timing assumption); a corrected `mode=ro`
  comment; and two new cases — a line split inside a multi-byte UTF-8 character across polls,
  and a failed detection printing no rate. **41 assertions across 8 cases; pytest 9/9.**
- **Merge is Jesse's call.** So is any deploy beyond the working-tree checkout already made.

**Known gaps, stated so they are not assumed covered:**
- `main()`'s scan loop has no Python-suite coverage. `tests/test_dv_host_scan.py` loads the module
  via `importlib` and tests pure helpers only. The loop change is covered by PowerShell case 6,
  which drives the real detector end-to-end with a stubbed `dovi_tool`.
- The full Python suite was **not** run. The base predates `fix/queue-policy-test-time-bomb`
  (`a88d541`), so three unrelated tests fail on an expired hard-coded date.
- The tail loop is proven against a stub and a ~3-minute real run, **not** a multi-hour scan.
- One case-1 failure occurred that I could **not** reproduce (3 clean runs before and after). It
  was in the test harness's own launch path, since replaced. No root cause is claimed.

**Carried over from the retracted throughput review, still open:**
- **`POST /rename/dv-import` has NEVER run — now root-caused, and it is structural.**
  This was follow-up #2 of the throughput review, recorded there as "has not run". It is worse
  than that: **no scheduled run has ever reached the import step at all,** and it cannot under
  the present design. Verified end to end, read-only:

  | evidence | value |
  |---|---|
  | Host `dv_host.db` | **499** rows |
  | Container `dv_scan` `source='scan'` | **466** rows |
  | Container `MAX(last_seen_at)` | **2026-07-26 02:29:47** (14 days stale) |
  | Every detector capture file today | exactly **218 bytes** — the two timeout warnings and nothing else |
  | `scanned N file(s)` in any capture | **none** |
  | `dv-import ->` in any capture | **none** |

  The chain:
  1. `_post_import()` is called only **after** the `for path in _iter_files(...)` loop finishes
     (`dv_host_scan.py:223`) — it is the last statement of `main()`.
  2. The loop cannot finish: ~231 files remain at roughly 6 files/hour, ~38 hours of work,
     against the task's `ExecutionTimeLimit = PT6H`.
  3. So every run is killed mid-loop and the POST never fires.
  4. The container's `dv_scan` therefore stays at 466 and `last_seen_at` stays frozen.
  5. The label sync fires only when `MAX(last_seen_at)` **rises**, so **Plex DV labels have not
     updated since 2026-07-26** and will not, no matter how well detection performs.

  **Detection working and results being visible are two different things, and only the first is
  fixed by this branch.** The shape of a fix is to import incrementally — POST every N files, or
  in a `finally` so a killed run still hands off what it completed — rather than once at the very
  end of a scan that never ends. Not attempted here; it is a behaviour change to the detector's
  contract with the container and wants its own review. ChatGPT independently reached the same
  conclusion and added **a second half I had missed: `_post_import()` logs an HTTP/OSError but
  does not return failure to `main()`, so even a scan that DOES complete can exit 0 with its
  import having failed.** The import is therefore both unreachable and, if reached, silently
  fallible. A repeated import is designed as an upsert of the host store into `dv_scan`, so
  decoupling it from full-scan completion is technically plausible; cadence and the label-sync
  trigger want reviewing together.
- The 30-minute `_EXTRACT_TIMEOUT` remains a latent risk on the extreme tail — being addressed by
  the other session's stall watchdog, not here.

**First observable checkpoint:** the 03:00 run. Its log should show per-file `scanning`/`-> layer`
lines appearing live and a heartbeat every 5 minutes with a rising `dv_host.db` count. If it shows
only preflight lines, the streaming did not take and the branch should be reverted in the working
tree.

---

## 8. Commands

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-dv-scan-streaming.ps1
```

Negative control — case 1 must FAIL against a pre-streaming wrapper:

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-dv-scan-streaming.ps1 -Wrapper <old-wrapper.ps1>
```

Read the live progress of any run:

```
Get-Content (Get-ChildItem 'X:\Docker Apps\ScanHound\data\dv-scan-logs\dv-scan-*.log' | Sort-Object LastWriteTime -Desc | Select-Object -First 1).FullName -Wait
```
