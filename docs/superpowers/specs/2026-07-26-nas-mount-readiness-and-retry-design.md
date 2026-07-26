# NAS mount readiness, retry, and mid-session recovery — design

**Date:** 2026-07-26
**Status:** design, awaiting approval. No code or task configuration changed.
**Depends on:** PR #35 (conditional recreate) — see "Why this is safe now".

## The failure, observed

On 2026-07-26 the host rebooted at 14:55. The mount task fired 80 seconds
later and failed:

```
ScanHound-MountNASShares
  LastRunTime    : 2026-07-26 14:56:26
  LastTaskResult : 1
```

Docker Desktop's WSL2 distro was not up yet, so every `mount -t drvfs` failed.
There is no retry, so the NAS library **and `/library/tv`, the read-write TV
destination**, stayed offline until a human noticed and fixed it by hand.

This is not hypothetical or rare: three restarts happened on this host in one
day (two clean shutdowns consistent with UPS-initiated graceful shutdown, one
Windows Update servicing restart at 23:35 the previous night). Every restart
of this machine currently leaves storage offline indefinitely.

Two things are working and must not be lost:

* PR #33's exit-code propagation **did its job** — the old script would have
  recorded `LastTaskResult: 0` and the outage would have been invisible. The
  failure was correctly reported. Reporting is simply not sufficient.
* The `At Startup` trigger fired correctly. The trigger is not the problem;
  readiness and the absence of retry are.

## Current task configuration

```
Triggers  : MSFT_TaskLogonTrigger (no delay), MSFT_TaskBootTrigger (no delay)
            no repetition on either
RestartCount               : 0        <- Task Scheduler retry is OFF
StartWhenAvailable         : False    <- a missed trigger never runs later
DisallowStartIfOnBatteries : True     <- will NOT run while on UPS battery
MultipleInstances          : IgnoreNew
ExecutionTimeLimit         : PT72H    <- a hung run can sit for three days
```

`DisallowStartIfOnBatteries = True` deserves attention: on a UPS-backed host,
Windows reports "on battery" during exactly the power events most likely to
cause a restart. The task can therefore decline to run at the moment it is
most needed.

## Design

Two layers, deliberately. Task Scheduler already provides retry, delay and
repetition; there is no reason to reimplement them in PowerShell. The script
owns only what the scheduler cannot know — whether Docker and WSL are actually
*ready*.

### 1. In-script readiness gate

Before attempting any mount, poll for readiness with a bounded budget:

* `wsl -d docker-desktop -e true` succeeds (distro is up), **and**
* `docker ps` returns 0 (engine is accepting commands).

Poll every 10 s up to a ceiling of ~5 minutes, then give up and exit non-zero
with a distinct code meaning *not ready* rather than *mount failed* — the two
have different operator responses and must not share an exit code.

A ready gate alone would have prevented the 14:56 failure: the mounts
succeeded by hand minutes later with no other change.

### 2. Bounded retry with backoff inside a single run

Readiness can pass and a mount still fail transiently (the NAS is reachable
over SMB and may itself be booting). Retry the *mount stage* up to 3 times
with 15/30/60 s backoff. Do not retry the container recreate — that is not
transient, and repeating it would churn the container.

### 3. Task Scheduler settings

| setting | now | proposed | why |
|---|---|---|---|
| Boot trigger delay | none | **2 min** | Docker Desktop is not up at T+80 s |
| `RestartCount` / `RestartInterval` | 0 | **3 / 5 min** | native retry if the run exits non-zero |
| Repetition | none | **every 15 min, indefinitely** | the only thing that catches a *mid-session* WSL/Docker restart, which fires neither At Logon nor At Startup |
| `StartWhenAvailable` | False | **True** | a trigger missed while off runs on return |
| `DisallowStartIfOnBatteries` | True | **False** | must run during UPS events |
| `ExecutionTimeLimit` | PT72H | **PT15M** | bound a hung run; longer than any healthy run by a wide margin |

The repetition interval is the mid-session fix. Nothing else detects a WSL
bounce that happens while the machine stays up — which is what occurred around
13:44, with no corresponding host boot.

### 4. Why this is safe now (and was not before)

A 15-minute repetition would have been unacceptable against the original
script, which recreated the container unconditionally: the container would
have been torn down four times an hour. PR #35 makes a periodic run a **no-op
when healthy** — it probes the container's own view of the mounts and only
recreates when it is genuinely blind. **This design must not ship before
#35.**

`MultipleInstances = IgnoreNew` already prevents overlap natively, so a
15-minute repetition cannot stack on a slow run. The named mutex in #35 stays
as defence in depth for manual invocations.

### 5. Persistent failure notification

`LastTaskResult` is not a monitoring surface — nobody reads it. On a run that
exhausts its retries, push a Gotify alert through the existing notification
stack, deduplicated by a marker file so a persistent outage does not alert
every 15 minutes. Clear the marker on the first successful run so recovery is
also visible.

Distinguish in the alert: *not ready* (Docker/WSL down — often benign and
self-correcting) from *mount failed* (the NAS is unreachable or auth failed —
needs a human) from *critical share unverified* (`/library/tv` — the
data-safety case, already exit 2/7 in #35).

## Explicitly out of scope

- Changing what counts as a verified mount — #35 owns that.
- Any change to container recreate policy — #35 owns that too.
- Auto-remediating a NAS that is genuinely down. Retry, alert, stop.
- Replacing Task Scheduler with a service or daemon.

## Risks

1. **Repetition masking a real problem.** A share that fails every 15 minutes
   and self-heals would look fine. Mitigated by alerting on exhausted retries
   rather than only on final state, so flapping is visible.
2. **Battery-time work.** Allowing the task on battery means it may run during
   a UPS shutdown sequence. Bounded by `ExecutionTimeLimit` PT15M, and the
   work is idempotent — a run interrupted by shutdown leaves no partial state
   the next run cannot redo.
3. **Readiness gate hiding a permanently-down Docker.** Exiting non-zero with
   a distinct not-ready code plus the alert keeps it visible.

## Tests

1. Readiness gate returns not-ready (distinct exit code) when `wsl` fails.
2. Readiness gate returns not-ready when `docker ps` fails.
3. Gate passes as soon as both succeed, without waiting the full budget.
4. Mount retry backs off and succeeds on attempt 2.
5. Mount retry exhausts and exits with the mount-failed code, not not-ready.
6. Container recreate is *not* retried.
7. A healthy periodic run performs no recreate and exits 0 (regression guard
   against the #35 conditional-recreate behaviour).
8. Alert fires once on exhausted retries, not on every repetition.
9. Alert marker clears on the next successful run.
10. Overlapping invocations still serialize.

The stubbed-docker harness added in #35
(`scripts/test-mount-safety-branches.ps1`) is the natural place for 1–7: `wsl`
and `docker` are already stubbable there.

## Rollout

Task settings are host configuration, not repo state, so they need a
`Register-ScheduledTask` script committed alongside — the current task was
registered by hand and its settings are undiscoverable from the repository.
Ship that script so the configuration is reproducible and reviewable, and note
that `schtasks` silently creates a broken task when the path contains spaces;
`Register-ScheduledTask` handles it.
