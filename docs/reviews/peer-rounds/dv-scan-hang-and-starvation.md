# DV scan: the defect is a two-file hang, not throughput

**Date:** 2026-08-09
**Author:** Claude
**Branch:** `agent/dv-scan-hang-and-starvation` (off `main` @ `6813260`)
**Status:** implemented + tested. **NOT merged, NOT deployed** — Jesse's calls.

---

## 0. What this supersedes

Two previous accounts of the same system were both wrong, in opposite directions.

| | Claim | Verdict |
|---|---|---|
| Review rev 1 | 6.7 MB/s; 1,417 h per pass; 59 days; "cannot finish" | **Wrong.** The rate was inferred from two `ps` snapshots. |
| Review rev 2 (retraction) | 79 MB/s; scan is healthy; 2 failures are an open question | **Right about the rate, wrong about the failures.** |
| ChatGPT's review | the 30-minute cap is the blocking defect; scale it by file size | **Right that the cap is enforced, wrong about the remedy.** |

The rate question is settled and is not the problem. The two failures rev 2 left
open *are* the problem, and they are not a tail risk — they are a permanent,
self-renewing loss of roughly one hour in every six-hour run.

## 1. What was actually measured

Everything below was read from a running system on 2026-08-09, not inferred.

**Throughput is healthy.** From `dv_host.db` (copied aside with its WAL), the 31
files that completed a full pass that day:

```
per-file end-to-end   57.3 – 152.9 MB/s      (median ~95)
aggregate incl. skips 83.8 MB/s              (1.78 TB over 5.90 h)
```

**The storage path is faster still.** Sequential reads of the same SMB files:

```
Jurassic World Rebirth, first 2 GB          162.9 MB/s
Jurassic World Rebirth, 4 GB across 24 GB   221.3 MB/s   <- the stall offset
Death Wish 3, first 2 GB                    145.7 MB/s
```

So `6.7 MB/s` never existed, and at the *slowest* observed healthy rate the
30-minute cap covers 103 GB — more than the 89.9 GB largest file in the library.
**The cap is not squeezing normal files.**

**Two files wedge dovi_tool.** `dovi_tool.exe` pid 19848 on Jurassic World
Rebirth, sampled over a 60-second window while the scan was live:

```
read_bytes_delta : 0            <- zero read operations, not "slow"
read_ops_delta   : 0
cpu_percent      : 95.7         <- one thread, pegged
total_read       : 27.37 GB     of a 74.3 GB file  (36.8%)
rpu output       : 0 bytes, not growing
```

It had read 37% of the file and then stopped reading entirely while holding a
core. A plain sequential read streams straight through that same offset at
221 MB/s, so neither the file nor the link is at fault.

**The freeze offset is exact and reproducible.** An independent run hours later,
under different load, stalled at **byte 27,367,062,473** — the same position to
the byte. That rules out a transient and points at a specific position in the
stream, which is the single most useful fact for an upstream report.

**It is deterministic and it recurs.** From `data/dv-scan-logs/`:

```
12:35 run  13:05:54  timeout  Death Wish 3 (1985).mkv            (1800 s exactly)
12:35 run  13:35:55  timeout  Jurassic World Rebirth (2026).mkv  (1800 s exactly)
19:00 run  19:30:02  timeout  Death Wish 3 (1985).mkv            (1800 s exactly)
19:00 run  20:00:02  timeout  Jurassic World Rebirth (2026).mkv  (1800 s exactly)
```

Three hangs in one day, on two files, always the same two.

## 2. Why this compounds into a loop

Four mechanisms interlock. None is dangerous alone.

1. **`classify_to_row()` stores `unknown` with NULL signatures**, so a failed
   file is retried on *every* subsequent run. Correct in isolation.
2. **Both wedged files live in `Y:/Movie 1 (14TB)/4K DV`**, which `os.walk`
   reaches first and which is otherwise 231/231 complete. So they are the first
   two things every run attempts.
3. **The cap is enforced.** `run_cancellable` takes the non-cancellable branch
   when `cancel_requested is None` — how `dv_host_scan.py` calls it — and goes
   straight to `subprocess.run(..., timeout=1800)`. ChatGPT verified this and it
   holds.
4. **`_post_import()` runs only after the loop finishes**, and the run never
   finishes: Task Scheduler kills it at `PT6H`. The 12:35 run's last write was
   18:33:56, two minutes before its 18:35:52 deadline.

Result: **1 hour of every 6-hour run burned before any new work begins**, and
the container never receives anything. `dv_scan`'s newest row is
`2026-07-25 16:45:35` while the host database has grown to 494 rows. Labels
cannot move until an import lands, and no import has landed in two weeks.

Per-root arithmetic, which also corrects the "236 remaining" figure from a
subtraction into a measurement:

| root | files | scanned | remaining |
|---|---|---|---|
| Y:/Movie 1 (14TB)/4K DV | 231 | 231 | 0 |
| Y:/Movie 2 (8TB)/4K DV | 113 | 113 | 0 |
| Y:/Movie 3 (2TB)/4K DV | 27 | 27 | 0 |
| //TURTLELANDSRV2/4K Magellan/DV | 359 | 123 | **236** |

The entire backlog is in one root. The entire waste is in a different one.

## 3. Why "scale the timeout by file size" is the wrong fix

It is the natural remedy for slowness, and the failure is not slowness. A
size-proportional cap would grant these two files ~90 minutes each instead of
30, taking the loss from **1 hour per run to about 3**. The measurement that
distinguishes the two cases — bytes read — is the one the old design never took.

So the cap stays as an outer bound for genuinely slow reads, and a **stall
watchdog** ends a process that stops reading: 180 s of zero progress resolves to
`unknown` with `error="stalled"`. Catches the real failure in 3 minutes instead
of 30, and cannot fire on a slow-but-working file, which reads continuously.

## 4. The bounded read is not an optimisation here — it is the fix

`dovi_tool 2.3.2` supports `-l/--limit N` on direct MKV input. Validated against
22 titles whose layer came from a *completed* full pass:

```
8 FEL, 8 MEL, 3 Profile 8, 2 Profile 5, 1 none  ->  22/22 agreed
extract time: 1.8 – 9.6 s   (versus 2 – 24 minutes for the same titles)
```

And, decisively:

```
Jurassic World Rebirth (2026)  ->  Profile: 7 (FEL)  in 6.2 s
Death Wish 3 (1985)            ->  Profile: 7 (FEL)  in 4.2 s
```

Both files that can *never* complete a full pass answer correctly in seconds.

**The semantics are asymmetric and only the FEL half is used.** A bounded sample
containing a FEL frame proves the title contains FEL — no later frame retracts
it. A sample showing only MEL, Profile 5, Profile 8, or no RPU proves nothing,
because a mixed title may open on MEL. So `probe_fel_bounded()` returns a bool,
never a layer: `explicit FEL -> final`, anything else -> full pass.

**What the property means.** `dv_scan` records *"this title contains at least one
frame of the reported layer"*, not *"this file completed a full successful scan"*.
That is not a new choice — `dv_labeler.pick_layer` already aggregates parts with
"one part proving Dolby Vision proves it for the title". Under that contract an
early FEL result is **final**, not provisional.

**Limits of the validation, stated plainly.** No title reporting a mixed
`(MEL, FEL)` appeared in the 22, so the MEL half is unvalidated *by construction*
— which is exactly why nothing but FEL is trusted. The FEL half is validated in
both directions that matter: every known FEL was FEL within 1000 frames, and no
known non-FEL produced a FEL token.

## 5. Changes

| File | Change |
|---|---|
| `process_control.py` | `ProcessStalled`, `process_read_bytes()`, `stall_timeout=` on `run_cancellable` |
| `dv_detect.py` | `probe_fel_bounded()`; FEL fast path in `detect_layer`; `evidence` key; stall wired in; `Profiles:` regex fix |
| `dv_host_scan.py` | retry backoff + metadata, work ordering, per-file logging, time budget, periodic import, schema migration |

**Retry backoff.** Failed rows now carry `attempts`, `last_error` and
`next_retry_at`, escalating 6 h → 24 h → 72 h → 168 h and then holding. A
permanently-failing file costs one attempt per week instead of one per run, and
is never declared unscannable.

**Work ordering.** Never-scanned first, then changed, then retries that are due.
The load-bearing detail is that the never-scanned bucket sorts **newest mtime
first**: a fresh acquisition and a two-month-old backlog entry are *both* simply
"never scanned", so bucket order alone would not have stopped a new grab queueing
behind 200 others. Retries sort longest-waiting first so backoff cannot starve
one file forever.

**Graceful stop.** `--max-runtime-minutes` (default 330, i.e. 5 h 30 m under a
`PT6H` limit) stops *between* files and exits 0, so Task Scheduler never hard-kills
a run and the final `dv-import` always runs. `--import-every` (default 25)
publishes progress during a long backfill instead of only at the end.

**Backfill vs steady state.** `--mode steady` processes only never-scanned and
changed files, skipping the retry sweep; `--mode backfill` (default) does
everything, ordered.

**Separate bug fixed — with the severity stated precisely.** Two claims here, and
only one of them is verified.

*Verified, and the reason it matters:* **any** summary line `_PROFILE_RE` fails to
match falls through to `LAYER_NONE` with `error=None` — an *authoritative* "no
Dolby Vision", not an "unknown". `dv_labeler.is_authoritative()` accepts that
value and `desired_label()` maps it to no label, so an unparsed summary does not
merely fail to classify: it **authorises removing the DV badge**. That fail-open
direction is real today, independent of any particular trigger, and it is the
part worth fixing.

*NOT verified:* that `dovi_tool 2.3.2` ever emits a plural `Profiles:`. It was
asserted upstream, and I could not confirm it against the pinned binary. A
string search finds zero occurrences of `Profiles` — but that zero is **not
trustworthy**, because the same search also finds zero occurrences of
`Profile: `, which this binary demonstrably printed 24 times during the bounded
experiment. The profile line is assembled from fragments rather than stored as
one literal, so absence proves nothing about exactly that line. (The control
holds for every other summary label: `Scene/shot count`, `DM version`,
`RPU mastering display`, `L5 offsets` and `Parsing RPU file` are all present as
literals, so the method works everywhere except the one line in question.)

So the regex now accepts both spellings and a comma-separated value list, which
is correct whether the plural is emitted by 2.3.2, by a later version, or never.
**Treat the mixed-profile trigger as latent rather than live** until someone
produces a real multi-profile summary from this binary.

## 6. Verification

**Suite, both arms, same session, from complete worktrees** — so failures are
attributed by baseline rather than by eye:

```
main   6813260 : 3 failed, 4626 passed, 5 skipped   (711.20 s)
branch 48cbd53 : 3 failed, 4656 passed, 5 skipped   (739.28 s)
```

The same three `test_queue_recovery_policy.py` tests fail on both — the
`a88d541` date bomb, pre-existing. **Zero regressions; +30 passing tests.**

**End-to-end against the real library and real dovi_tool** (not mocks):

```
bounded  Jurassic World Rebirth   13.4 s   fel        evidence=bounded
bounded  Death Wish 3             19.5 s   fel        evidence=bounded
full     If These Walls Could Sing 95.9 s  profile5   evidence=full     <- non-FEL still takes the full path
full     Jurassic (forced)        504.6 s  unknown    error=stalled     <- watchdog fired, vs 1800 s before
```

The third line is the one that proves the accelerator is not over-applied: a
non-FEL title is *not* short-circuited and still returns its true layer.
- **Mutation-checked.** Reverting the regex to singular fails 3 of the 4
  multi-profile tests; changing the bounded probe to finalise any non-`none`
  sample fails 2 of the 6 accelerator tests. Baseline restored and re-verified
  after each.
- The stall watchdog is tested against **real** subprocesses: a spinning process
  that reads nothing is killed on the stall window, and — the control that makes
  that meaningful — a process that keeps reading is *not*. Plus a positive
  control that read progress is measurable at all, without which both would pass
  for the wrong reason.
- Existing `detect_layer` tests were re-pointed at `dv_detect.run_cancellable`
  (a `subprocess.run` patch no longer intercepts the polling path) and pinned to
  `bounded_first=False` so they still cover the full-pass branch they were
  written for.

## 7. Applied to live data tonight (with Jesse's approval)

The two proven results were written into `data/dv_host.db` so the running scan
stops re-attempting them before any of this is deployed:

```
Death Wish 3 (1985).mkv            unknown/NULL -> fel, sig 65447127320 @ 1761515260.493
Jurassic World Rebirth (2026).mkv  unknown/NULL -> fel, sig 74277195186 @ 1756951257.506
rows carrying a NULL signature: 2 -> 0
```

Verified through `dv_host_scan.sig_is_current` itself, not a re-derivation of the
rule. Prior values saved to `scratchpad/dv_host_rows_before.json`; reverting is
setting `dv_layer='unknown'` and both signature columns back to NULL.

## 8. Open items

1. **Not deployed.** Needs `up -d --build`, and `main` needs
   `fix/queue-policy-test-time-bomb` (`a88d541`) merged first or three unrelated
   tests fail on a hard-coded date.
2. **The container has never imported this backlog.** Once deployed, the first
   completed run posts `/rename/dv-import` and 494 rows land at once.
3. **Why those two files wedge dovi_tool is still unknown.** Both are Profile 7
   FEL, both stop reading partway, and Jurassic World Rebirth does so at exactly
   byte 27,367,062,473 on repeated independent runs. A `-l` bisection to convert
   that offset into a frame number is in progress; that plus the byte offset is
   what an upstream report needs. It no longer blocks anything.
4. **`--mode steady` is not yet wired into the scheduled task.** The task still
   runs one 4-hourly job doing both jobs; splitting it is a task-definition
   change and therefore Jesse's.
