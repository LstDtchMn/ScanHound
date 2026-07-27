# NAS mount recovery — scope counter-proposal

**Date:** 2026-07-26
**Status:** counter-proposal, for peer review. No code, no task configuration,
nothing merged, nothing deployed.
**Responds to:** the review of spec `51f151b` and WIP `3707984`
**Baseline:** `main` @ `9d1a99c` (PR #35 merged and live)

---

## Position

I accept the review's findings. Every blocker I checked reproduced. I am not
disputing correctness.

I am disputing **scope**, and proposing a different first move.

The review's fourteen conditions describe a system with two modules, a
dependency-injection seam, a compiled or `powershell.exe`-backed native test
helper, a Windows CI job, ACL-hardened state, and roughly forty new tests. That
is a correct architecture for safety-critical shared infrastructure.

This is a script that mounts nine SMB shares into one WSL2 distro on one home
server. Its observed failure mode is *"ran 39 seconds before Docker was ready
and never tried again."* The proposal spends multiple sessions of engineering
on that, and the shares stay offline after every restart in the meantime.

My counter-proposal is **not** "patch the WIP." I agree the WIP should not
continue as written. It is: **move retry out of the script into Task Scheduler,
which already implements it, and let most of the blocker list dissolve rather
than be solved.**

---

## New evidence gathered after the review

Four things were measured on the live host today, after the review was written.
Two of them change the review's recommendations.

### 1. The readiness margin was 39 seconds, not minutes

| event | time (EDT) | offset from boot |
|---|---|---|
| host boot | 14:55:06 | T+0 |
| **mount task ran, `LastTaskResult: 1`** | **14:56:26** | **T+80 s** |
| Docker backend first accepts IPC | 14:57:05 | T+119 s |

Source: `System` event log ID 12; `Get-ScheduledTaskInfo`;
`%LOCALAPPDATA%\Docker\log\host\docker-desktop.exe.log`.

The task did not fail because readiness detection was missing. It failed
because it ran **once**, 39 seconds too early, and Windows was configured never
to run it again. Backend IPC availability is a lower bound on real engine
readiness, so the true margin is somewhat larger — but the direction is settled:
Docker converged on its own, in the same boot session, with no human action.

A boot delay plus native restart would have covered it with roughly fifteen
minutes to spare. That is a Task Scheduler settings change with **zero lines of
code**.

### 2. The task has no repetition trigger at all, and a 72-hour time limit

Exported XML (`schtasks /query /xml`):

```xml
<Triggers>
  <LogonTrigger />
  <BootTrigger />
</Triggers>
```

Neither trigger carries `<Delay>` or `<Repetition>`. Settings omit
`<RestartOnFailure>` and `<StartWhenAvailable>` entirely, set both battery
guards true, and set `ExecutionTimeLimit` to `PT72H`.

Also: `LogonType` is `InteractiveToken`. The boot trigger therefore only fires
when that account is already logged on interactively — worth resolving
deliberately, because it partly explains why the logon trigger is the one doing
the work.

### 3. The review's in-distro timeout syntax will not run

The review recommends (§4, Blocker E):

```
timeout --signal=TERM --kill-after=5s 110s sh mount-script data-file
```

and correctly asks that the distro be verified before relying on it. Verified:

```
$ wsl -d docker-desktop -e sh -c "timeout --help"
timeout [-s SIG] [-k KILL_SECS] SECS PROG ARGS
    ...multi-call binary   (BusyBox)
```

`timeout` **is** present, so the mechanism is available — but it is BusyBox,
not GNU coreutils. It rejects `--signal=`, `--kill-after=`, and the `s` suffix
on durations. The working form is:

```sh
timeout -s TERM -k 5 110 sh /mount-script /data-file
```

This is a correction to the review, not a disagreement with it: **accept the
mechanism, fix the invocation.**

### 4. Both executables are already pinnable and already admin-only

| logical tool | resolved path | non-admin writable? |
|---|---|---|
| Wsl | `C:\WINDOWS\system32\wsl.exe` | no |
| Docker | `C:\Program Files\Docker\Docker\resources\bin\docker.exe` | no |

ACLs on both parent directories grant write only to `TrustedInstaller`,
`SYSTEM`, and `Administrators`.

This matters for effort estimation. The review frames Blocker D's remedy as an
installer that discovers `docker.exe`, requires a real `.exe`, canonicalizes,
verifies non-writability, and records it in ACL-protected config. On this host
the answer is two constants and one startup assertion. The installer is a fine
eventual home for that logic; it is not a prerequisite for removing the
vulnerability.

---

## The architectural disagreement

The WIP grew from 463 lines to 749 (+62%) and the review found twelve issues in
the addition. That is not coincidence. Look at what the added lines do:

- an in-script retry ladder with backoff
- a bounded process runner with manual argument encoding
- CIM-based process-tree termination
- a readiness polling loop with its own stopwatch
- persistent failure-class state with flap detection

**Windows Task Scheduler already provides four of those five.** It has native
restart-on-failure with a configurable count and interval, trigger delays,
indefinite repetition, missed-run recovery, an overlap policy, and a hard
`ExecutionTimeLimit` that terminates the process tree for you.

The script re-implemented them, in PowerShell 5.1, and each re-implementation
produced blockers:

| blocker | caused by |
|---|---|
| A — not all calls bounded | having a bespoke bounded runner at all |
| E — killing `wsl.exe` may orphan Linux work | in-script timeouts |
| F — process-tree cleanup incomplete | in-script process killing |
| G — argument quoting unproven | in-script `ProcessStartInfo` |
| H — timing budget not truly shared | in-script deadline arithmetic |
| I — 60 s spec vs 120 s WIP | in-script timing constants |

Six of the twelve are self-inflicted. The review's answer is to build the
machinery properly. My answer is to **not build most of it**.

If the scheduler owns retry, then:

- there is no ladder, so no shared-deadline problem (H) and no attempt-count
  ambiguity (I);
- `ExecutionTimeLimit PT15M` is the kill switch, and Windows terminates the
  tree — so F is the OS's problem, not mine;
- with far fewer bounded calls, the encoder surface shrinks toward the fixed,
  known argument set this script actually uses (G, A);
- "never retry a critical failure" stops being a policy decision inside the
  script — the script reports and exits, and the scheduler retries (B).

A run that fails and exits non-zero is not a defect in this design. It is the
mechanism.

---

## Staged plan

### Stage 0 — scheduler settings and deployment path (no script change)

Registered from committed XML, per spec §8. No edit to `mount-nas-shares.ps1`.

| setting | now | proposed |
|---|---|---|
| boot trigger delay | none | `PT3M` |
| `RestartOnFailure` count / interval | absent | 3 / `PT5M` |
| `StartWhenAvailable` | absent (false) | true |
| `DisallowStartIfOnBatteries` | true | false |
| `StopIfGoingOnBatteries` | true | false |
| `ExecutionTimeLimit` | `PT72H` | `PT15M` |
| dedicated time trigger, `PT15M` repetition, indefinite | absent | added |
| `MultipleInstancesPolicy` | `IgnoreNew` | unchanged |

Coverage against the observed failure: first attempt ~14:58:06, restarts at
~15:03, ~15:08, ~15:13, then the periodic trigger indefinitely. Docker was
serving by 14:57:05.

Plus the spec's stable-path fix, which is independent of all of the above and
is a live hazard today: Task Scheduler currently points at
`X:\Docker Apps\ScanHound\scripts\mount-nas-shares.ps1` — the working tree.
Checking out a branch silently changes what production runs. Copy the reviewed
script to `C:\ProgramData\ScanHound\scripts\`, record its source commit, hash
it, register against that path.

**Stage 0 alone would have prevented every mount outage observed today.**

### Stage 1 — targeted script corrections

Small diff against the tested 463-line `main` script. Not a resurrection of
`3707984`; the WIP is reference material only.

1. **Separate criticality from retryability** (Blocker B). The concrete change
   is the inverse of the WIP's: delete `break`-on-critical (WIP line 517) and
   let the process exit non-zero so the *scheduler* retries. Criticality
   continues to govern only the PR #35 safety response — verify, stop, report.
   This is the review's most important correction and I am adopting it, by the
   cheapest available route.

2. **Pin the two executables** (Blocker D). Replace `Get-Command $File` (WIP
   line 234) with the two absolute paths measured above, asserted at startup.
   Tests receive a runner through an explicit parameter — never PATH, never
   `.bat`, never `cmd.exe`.

3. **One typed result → one exit-code map** (Blockers C and L). Named internal
   categories (`TRANSIENT_READONLY`, `TRANSIENT_CRITICAL`, `WRONG_SHARE_*`,
   `CONFIG_ERROR`, `SUCCESS`), mapped once at the boundary. Inner sh `3` stops
   colliding with outer `3 = docker-unavailable`, and wrong-share gets a real
   task exit code instead of being flattened to generic failure.

4. **In-distro timeout** (Blocker E), using the BusyBox form verified above,
   with the host-side bound slightly longer.

5. **Passive readiness gate, short budget.** Keep the spec's passive probe —
   `wsl --list --running --quiet` plus a server-scoped `docker version` — but
   at 60–90 s, not 240. The scheduler's 5-minute restart is the real waiting
   mechanism; a 4-minute in-script poll duplicates it.

6. **Atomic state write** (half of Blocker J). Temp file in the same directory,
   then replace. Roughly five lines, and it prevents a truncated JSON file from
   silently disabling failure reporting. ACL hardening deferred to Stage 2 —
   this is a single-admin host, and the state file is advisory, not a control
   input.

Explicitly **not** in Stage 1: flap detection (so Blocker K has nothing to
deduplicate), `Invoke-Bounded` as a module, DI container, CI, compiled helper.

Testing: extend the existing stubbed harness for the state-machine branches;
**all process-kill and timeout work happens off the production host** — that
condition I accept without reservation, and for a reason the review does not
know: I took ScanHound down for roughly twenty minutes today doing exactly what
the review warns against. Their caution was earned before I proved it.

### Stage 2 — reassess, deliberately not pre-committed

After Stage 0 + Stage 1 have survived a real reboot and a real Docker restart,
decide whether the module split, DI seam, native-process test module, Windows
CI, and remaining ~40 tests are still warranted — with evidence from a system
that is no longer failing weekly.

I am not arguing that answer is no. I am arguing it should be answered after
the bleeding stops, not before.

---

## Disposition of the review's fourteen conditions

| # | condition | disposition |
|---|---|---|
| 1 | inject internal runner, not top-level command params | **accept** — parameter on the internal function, sealed entry point |
| 2 | pin absolute production executable paths | **accept** — paths and ACLs verified above |
| 3 | eliminate PATH shadowing, `.bat`, `cmd.exe` | **accept, unconditionally** |
| 4 | extract + independently test native process runner | **defer to Stage 2** — fewer bounded calls to justify a module |
| 5 | fakes for orchestration, real `.exe` helper for native | **partial** — fakes in Stage 1; real-exe helper with Stage 2 |
| 6 | route *every* Docker/WSL/Compose call through the runner | **partial** — removing `Start-Job` (WIP 646/669): accept. Uniform bounding: Stage 2 |
| 7 | separate criticality from retryability | **accept — highest priority item on this list** |
| 8 | in-WSL timeout | **accept, with corrected BusyBox syntax** |
| 9 | PID-scoped verified process-tree cleanup | **dissolve** — `ExecutionTimeLimit` makes this the OS's job |
| 10 | Windows PowerShell 5.1 CI | **defer to Stage 2** |
| 11 | isolate timeout/kill testing from the production host | **accept, unconditionally** |
| 12 | state atomicity, ACLs, flap dedup | **split** — atomicity in Stage 1; ACLs Stage 2; flap dedup moot (no flap detection) |
| 13 | reconcile 60 s spec vs 120 s WIP | **accept** — verified real (WIP lines 92–93) |
| 14 | typed internal results, mapped once | **accept** — this is the fix for C and L |

Accepted now: 1, 2, 3, 7, 8, 11, 13, 14, plus half of 12.
Deferred: 4, 5, 6, 10, rest of 12.
Argued away by design change: 9.

---

## Where I may be wrong

Stated plainly, because the review has caught real errors of mine three rounds
running and this is the part worth attacking:

1. **`ExecutionTimeLimit` termination semantics.** I assert Windows terminates
   the whole process tree on limit expiry, which is what lets me drop Blocker F.
   I have **not** verified that it kills a `wsl.exe`-launched in-distro shell.
   If it does not, F comes back and Stage 1 needs the in-distro timeout (item 4)
   to carry more weight than I have assigned it.

2. **Scheduler restart vs. safety-stop interaction.** If a run reaches PR #35's
   critical path and stops the container, the next scheduler restart re-enters
   from a stopped-container state. PR #35 probes before acting, so I believe
   this converges — but "believe" is doing work in that sentence, and it is the
   interaction most likely to bite.

3. **Deferral risk.** Stage 2 is where deferred work goes to be forgotten. If
   the review's position is that items 4/5/6/10 are load-bearing rather than
   thorough, say so and I will move them forward — but I would like the argument
   to be about those four specifically, not the package.

4. **My judgement today was not good.** I caused a production outage
   experimenting with process termination. Weigh my "this is over-scoped"
   accordingly.

---

## What I am asking for

Not approval to build — that is Jesse's call and nothing here is authorized.

I am asking whether the **sequencing** is sound:

1. Does Stage 0 (settings only, zero code) fix the observed failure, or is
   there a case where scheduler-native retry is insufficient and only an
   in-script ladder works?
2. Is moving retry from the script to the scheduler architecturally sound, or
   does it lose something the review's design preserves?
3. Which of the deferred items (4, 5, 6, 10, ACLs) do you consider load-bearing
   rather than thorough, and why that one?
4. Is the `ExecutionTimeLimit`-terminates-the-tree assumption safe enough to
   drop Blocker F, or does that need proving first?
