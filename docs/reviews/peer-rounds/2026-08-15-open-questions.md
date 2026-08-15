# Everything still unresolved at the end of 2026-08-15

A closing round. No new code to review — this is the backlog of open questions,
unproven claims and loose ends from a long day, gathered so nothing survives
only in my head. Where I have a view I say so; where I do not, I say that too.

Three prior rounds today are context, not required reading:
`2026-08-15-full-day.md`, `2026-08-15-jd-stall-hypothesis.md`,
`2026-08-15-heartbeat-and-backoff-design.md`, plus the corrected design at
`2026-08-15-jd-corrected-design.md`.

---

## 1. JDownloader — the largest open thread

**The ~15-hour stall's cause is still unproven.** Established: a JD restart
alone does not cause it (reproduced — one failure at `query_packages`, recovery
in 15s). Not established: whether the poller was cycling and failing, blocked
inside an iteration, or stopped. The corrected design's outer-cycle heartbeat
is what would answer it.

Open questions:

1. **Is the corrected design in `2026-08-15-jd-corrected-design.md` right?**
   It was written from your last round but has not itself been reviewed. In
   particular: is a policy wrapper around `_connect_jd_device()` the right
   seam for the shared automatic-reconnect gate, or does the gate belong at
   each automatic call site?
2. **Should `myjdapi` be pinned?** `requirements-docker.txt` floats
   `myjdapi>=1.1.6`; 1.1.10 is installed. The design leans on its 3-second
   request timeout. Pin it, or treat the timeout as unguaranteed and bound the
   sequence ourselves regardless?
3. **The redundant `update_devices()`.** `connect()` already calls it, and
   ScanHound calls it again. Safe to remove after pinning, or is there a reason
   to keep the second call?
4. **Is there a fourth explanation** for a 15-hour stall that neither the
   rate-limit loop, a blocked thread, nor a stopped thread covers? I have been
   wrong about this twice.

## 2. The C:\Tools ACL incident — two things never closed

5. **The causal gap stands.** A damaged `C:\Tools\dovi_tool.exe` proves that
   copy was broken, not that the failing scheduled task resolved it. The pinned
   wrapper prepends `C:\Tools` to PATH, which supports my reading; the
   repo-local wrapper uses its own `scripts\host-detector\dovi_tool.exe`. I
   never preserved the resolved executable path from a failing run, and the
   task that would have proven it is now disabled. Is there any way to
   establish this retrospectively, or is it simply lost?
6. **Per-file explicit ACEs: recovery or policy?** You called it a maintenance
   trap as policy, with the durable invariant being "the whole tree used by an
   elevated scheduled task inherits from a hardened parent and is not writable
   by lower-trust principals". I applied per-file ACEs as *recovery* and left
   them. Should they be unwound in favour of the parent-inheritance invariant,
   and if so what is the safe migration given this broke twice already?
7. **Auditing is still off.** Security 4670 and PowerShell 4104 are both
   disabled, so a third recurrence would again be unattributable. Worth
   enabling narrowly for `C:\Tools`, or is the noise not worth it?

## 3. Download pipeline — a small unexplained artefact

8. **Two orphaned rows.** Two packages sit in `download_results` at
   `state='downloaded'`, 100% complete, `extraction='na'`, last touched
   2026-08-01, and JDownloader no longer reports them. ScanHound never prunes
   rows for packages that vanish from JD, so they persist in the Downloads list
   indefinitely. Is pruning correct here, or is retaining history deliberate
   and the UI should distinguish "no longer in JD" instead?
9. **A caveat on my own earlier evidence.** The poller only writes rows whose
   signature CHANGED, so a stale `updated_at` means "nothing changed", not
   necessarily "the poll stopped". My original stall diagnosis leaned on that
   timestamp more than it should have. Does that weaken anything else in the
   record?

## 4. Carried over from the #77 round, not done

10. **The threshold asymmetry is undocumented.** After a prior success the
    checker waits 30 minutes; a poller that has never succeeded but is already
    failing alerts immediately. You asked for this to be documented or unified
    via `failure_since`. Which is better?

## 5. RSS — a decision that has been open for a while

11. **192 relevant misses across 500 shadow cycles**, against a 70.4% request
    saving if promoted. Feeds all healthy (12/12). Misses break down as 158
    absent entirely, 16 upgrades, 13 whole seasons, 5 DV upgrades — the feeds
    structurally lack them, so this is not lag. The coverage-canary hybrid
    (PR #61) is spec'd and unbuilt. Is the hybrid still the right answer, or do
    these numbers argue for something else?

## 6. Process questions, since today produced a lot of self-inflicted work

12. **Nearly every real problem today produced silence** — a stripped
    permission, an empty scan, a swallowed exception, a test block after
    `sys.exit()`, an unread library. And nearly every mistake I made was
    reading silence as confirmation. Is there a check that would have caught
    the *class* rather than each instance?
13. **Two of my premises this round were wrong in ways one command would have
    settled** (the myjdapi timeout; the 5-second UI reconnect). Both were in
    documents I presented as reasoned. What would you require of a design
    document before treating its premises as established?

---

## Current state, for reference

Deployed and live: #72, #73, #74, #75, #76, #77, #78. JDownloader healthy,
polling every few seconds, telemetry visible at `/health`. DV detection
restored — 889 of 5,254 files classified, zero permission failures, roughly
4-6 days of scanning left at observed throughput. `C:\Tools` immunised against
a repeat of the lockdown that broke it. Health check running every 30 minutes
with Gotify delivery proven end-to-end.

Open PRs: #79 (heartbeat — needs the outer-cycle rework before merging),
#61 (RSS hybrid design), #59 (detector runbook).
