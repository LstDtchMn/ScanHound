# NAS mount readiness, retry, and mid-session recovery — design

**Date:** 2026-07-26 (revised same day after peer review)
**Status:** design, awaiting approval. No code or task configuration changed.
**Hard dependency:** PR #35 must be merged **and installed** first — enforced
mechanically by the installer, not by operator memory (see §7).

## The failure, observed

The host rebooted at 14:55. The mount task fired 80 seconds later and failed:

```
ScanHound-MountNASShares
  LastRunTime    : 2026-07-26 14:56:26
  LastTaskResult : 1
```

Docker Desktop's WSL2 distro was not up yet, so every `mount -t drvfs` failed.
With no retry, the NAS library **and `/library/tv`, the read-write TV
destination**, stayed offline until a human noticed.

Three restarts occurred on this host in one day (two clean shutdowns
consistent with UPS-initiated graceful shutdown, one Windows Update servicing
restart). One WSL/Docker-only restart around 13:44 had **no** corresponding
host boot — that is the mid-session case neither At Logon nor At Startup
catches.

Two things worked and must be preserved: PR #33's exit-code propagation
correctly reported the failure (the previous script would have logged `0` and
hidden the outage), and the At Startup trigger fired. The trigger is not the
problem; readiness and the absence of retry are. **Reporting without retry
does not help.**

## Design

Two layers. Task Scheduler already provides boot delay, process restart,
periodic invocation, missed-run recovery, and overlap policy; the script owns
only what the scheduler cannot know — whether Docker and WSL are actually
ready, and whether a failure is transient.

### 1. Readiness gate — passive, never a starter

**The probe must observe readiness, not cause it.** `wsl.exe -d docker-desktop
-e true` *launches* the distro if it is not running, which would turn this
recovery task into an undocumented Docker Desktop starter — bad during boot
convergence, a UPS shutdown sequence, a Docker Desktop update, or a
deliberately stopped engine.

Poll instead:

1. `docker-desktop` appears in `wsl.exe --list --running --quiet`;
2. the **server** responds: `docker version --format '{{.Server.Version}}'`
   (server-scoped, so a working client with a dead engine cannot pass).

```
docker-desktop not listed running -> not ready; DO NOT launch it
listed but server query fails     -> not ready
both pass                         -> proceed to mount stage
```

Verify the task runs under a principal that sees the expected Docker context —
a different account can silently reach a different configured endpoint.

### 2. Timeout hierarchy — every child command bounded

A five-minute polling loop is not bounded if a single `wsl.exe`,
`docker version` or `mount` invocation can hang forever. A timed-out probe
counts as *not ready* and the loop continues while the overall budget remains.

```
per readiness command        : 15 s
overall readiness gate       : 4 min
per mount attempt            : 60 s
mount attempts               : 3 TOTAL (initial + 2 retries), backoff 15 s / 30 s
post-recreate verification   : 3 probes at +5 s / +10 s / +20 s
internal script deadline     : 12 min (monotonic; cleanup + alert state written before it)
Task Scheduler limit         : PT15M — a last-resort kill switch, never the expected path
```

"3 attempts total" is stated explicitly because "retry three times" is
ambiguous. The scheduler limit must never hard-kill the script during a
recreate or a safety stop, so the internal deadline sits comfortably below it.

### 3. Retry only what can plausibly be transient

| retryable | not retryable |
|---|---|
| NAS still booting | wrong share mounted at the target |
| transient SMB unreachability | identity verification mismatch |
| drvfs failure after readiness passed | missing compose/deployment path |
| | malformed task configuration |

A deterministic wrong-share condition must fail immediately rather than
sleeping through the backoff ladder. PR #35 distinguishes critical from
read-only failures but does not expose every identity/config failure as a
distinct machine-readable code; this work must preserve enough result detail
to make that split.

### 4. Recreate: never repeat; verify with retries

**Do not repeat `docker compose up -d --force-recreate` within one run.** The
next scheduler restart or periodic invocation re-enters the whole verified
state machine, and because PR #35 probes first, a recreate that actually
succeeded despite an ambiguous failure is seen as healthy next time.

**Do** retry *post-recreate verification* (+5 s / +10 s / +20 s) — a transient
startup delay after Compose returns 0 is plausible. Those retries must never
trigger another recreate. If the critical TV target still cannot be proven,
PR #35's verified-stop behaviour stands.

### 5. Task Scheduler settings

Verified against the live task before proposing:

| setting | live now | proposed | note |
|---|---|---|---|
| `DisallowStartIfOnBatteries` | **True** | False | will not run during the exact power events most likely to disturb WSL |
| `StopIfGoingOnBatteries` | **True** | False | **separate setting** — verified live; changing only the first still lets Windows kill a running task on switch to battery |
| `RestartCount` / `RestartInterval` | 0 / — | 3 / 5 min | native retry |
| `StartWhenAvailable` | False | True | a missed trigger runs on return |
| `ExecutionTimeLimit` | PT72H | PT15M | bound a hung run |
| Boot trigger delay | none | 2 min | Docker is not up at T+80 s |
| `WakeToRun` | False | *unchanged* | already correct — must not wake a sleeping host to poll |
| `RunOnlyIfNetworkAvailable` | False | *unchanged* | already correct — the script does the real NAS check |
| `MultipleInstances` | IgnoreNew | *unchanged* | already prevents stacking natively |

Battery Saver can additionally delay non-interactive tasks depending on logon
type, so the principal is part of the correctness model (§7), not deployment
trivia.

### 6. Exactly one repeating trigger

The previous draft said "every 15 minutes" without saying which trigger owns
it. Repetition on **both** boot and logon triggers creates two independent
schedules with different phase offsets; `IgnoreNew` prevents parallel runs but
the task still fires more often than intended.

- boot trigger: one-shot, 2 min delay
- logon trigger: one-shot, or dropped if it adds no recovery beyond boot
- **one dedicated time trigger**: starts at registration, repeats every
  15 minutes, no duration (indefinite)

A dedicated time trigger also starts the periodic schedule **immediately on
install**; repetition attached only to a boot trigger may not begin until the
next reboot.

**Registration caveat.** `New-ScheduledTaskTrigger` exposes
`RepetitionInterval` only in its one-time parameter set, not with
`-AtStartup`. Attempting the `-AtStartup -RepetitionInterval` combination in
this environment hung on an interactive parameter-set prompt rather than
binding. The underlying XML schema *does* support repetition on boot triggers,
so registration must use deterministic task XML, the COM API, or a dedicated
time trigger — and must then **export the installed task and assert the actual
XML**, never trust that the registration command returned success.

### 7. Alerting — class-aware, flap-visible, on stable storage

The script knows when *its own* budgets are exhausted; it cannot know that
Task Scheduler has just performed its final restart. So: alert when the
internal budget is exhausted, let the marker deduplicate subsequent scheduler
restarts and periodic runs, and clear it only after a fully verified healthy
run (with an outage-duration recovery alert).

Markers are **per failure class** — a standing `not-ready` marker must never
suppress a later `critical-share-unverified` alert.

Alerting only on exhaustion would hide a mount that fails once and succeeds on
attempt two every 15 minutes, so:

```
one recovered-with-retry run           -> structured log only
>=3 recovered-with-retry runs in 24 h  -> Gotify warning (flapping)
fully exhausted run                    -> immediate failure alert
```

Persist: failure class, first/latest failure time, last exit code, consecutive
failed runs, last success, script version. Store under
`%ProgramData%\ScanHound` — **not** `%TEMP%`, the WSL distro, the container,
or a NAS share whose outage is being diagnosed.

### 8. Version-control the whole operational definition

The task was registered by hand, so its settings are undiscoverable from the
repository. Commit a registration package covering: name and folder;
description with purpose, owner and the PR #35 dependency; exact executable,
arguments and working directory; every trigger; every setting in §5;
compatibility/schema version; and the full principal (account/SID, logon type,
run level, whether interactive logon is required) with verified access to
Docker Desktop, the expected WSL distro, NAS credentials and the compose
directory. No passwords or reusable secrets committed; no Gotify credentials —
read those from an ACL-protected local config.

**Stable deployment path.** Do not point Task Scheduler at whichever branch
happens to be checked out — that is how the pre-#35 script stayed live while a
fixed one sat on a branch. Copy the reviewed script atomically to
`C:\ProgramData\ScanHound\scripts\mount-nas-shares.ps1`, record its source
commit, register against that path, and verify the deployed file hash.

The installer must be idempotent, able to update an existing task, export and
back up the previous XML, restore or uninstall, require administrator
explicitly, offer a dry run — and **refuse to deploy unless the installed
script is the hardened PR #35-era version**, which is how the dependency gate
becomes mechanical rather than remembered.

Post-registration verification: export XML and assert every security-critical
field; confirm exactly one trigger owns indefinite repetition; run once
manually; verify exit code and logs; verify a healthy run performs no
recreate; verify the periodic trigger's next-run time; verify a non-zero
result activates scheduler restart.

## What was verified before writing this

- `StopIfGoingOnBatteries = True` on the live task, distinct from
  `DisallowStartIfOnBatteries = True` — the two-setting correction is real.
- `WakeToRun = False` and `RunOnlyIfNetworkAvailable = False` already match
  the recommendation; no change needed.
- `wsl --list --running --quiet` returns `docker-desktop`, so the passive
  probe is viable.
- `-AtStartup -RepetitionInterval` did not bind; it prompted and hung.

**Not verified, and stated as such:** that `wsl -d docker-desktop -e true`
starts a stopped distro. This host has exactly one distro and it is running,
so testing would require stopping Docker, the container, the corpus sweep and
production. The passive probe is adopted because it is strictly safer whether
or not the claim holds — not because it was proven here.

## Explicitly out of scope

- What counts as a verified mount, and container recreate policy — PR #35.
- Auto-remediating a genuinely down NAS. Retry, alert, stop.
- Replacing Task Scheduler with a service or daemon.
- Event-triggered recovery on Docker/WSL event IDs — version-sensitive and
  unnecessary given a verified periodic check.

## Risks

1. **Periodic runs masking flapping** — mitigated by the recovered-with-retry
   counter and the ≥3-in-24 h warning, not by exhaustion alerts alone.
2. **Battery-time work** — bounded by PT15M and idempotent; an interrupted run
   leaves nothing the next cannot redo. The passive probe means it will not
   start Docker during a UPS shutdown.
3. **Readiness gate hiding a permanently-down Docker** — distinct not-ready
   exit code plus its own alert class.
4. **Scheduler restart colliding with the periodic trigger** — `IgnoreNew`
   discards the overlap. Acceptable, but tested (§tests 19) rather than
   assumed.

## Tests

Original ten, plus the review's additions:

1. Not-ready (distinct code) when the distro is not listed running.
2. Not-ready when the server query fails.
3. Gate passes as soon as both succeed, without burning the budget.
4. Mount retry backs off and succeeds on attempt 2.
5. Mount retry exhausts with the mount-failed code, not not-ready.
6. Recreate is not retried.
7. A healthy periodic run performs no recreate and exits 0.
8. Alert fires once on exhaustion, not per repetition.
9. Marker clears on the next healthy run.
10. Overlapping invocations serialize.
11. Readiness probing does **not** start a stopped `docker-desktop`.
12. A hung `wsl.exe` probe times out without defeating the overall deadline.
13. A hung Docker probe times out.
14. A hung drvfs mount times out and advances to the next attempt.
15. Wrong-share/config failure is not retried as transient.
16. Exactly one trigger owns 15-minute indefinite repetition.
17. The periodic trigger begins without requiring another reboot.
18. Task may start on battery and is not stopped on switch to battery.
19. Periodic trigger colliding with a scheduler restart starts no parallel work.
20. Post-recreate verification retries without another force-recreate.
21. Alert markers are class-specific.
22. A more severe failure alerts despite an existing lower-severity marker.
23. Recovery clears the marker and reports outage duration.
24. Repeated recovered-with-retry runs raise a flap warning.
25. Installed task XML matches the committed definition.
26. Installer refuses deployment when the PR #35-era script is absent.

1–7 and 11–15 belong in the stubbed-docker harness added by PR #35
(`scripts/test-mount-safety-branches.ps1`), where `wsl` and `docker` are
already stubbable.
