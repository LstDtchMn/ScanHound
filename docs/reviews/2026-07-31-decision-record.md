# Decision record — 2026-07-31

All decisions made by Jesse in one sitting, collected before any work began.
This file is the authority for what was agreed. Nothing here was inferred.

**Guardrails unchanged:** merge, deploy, force-push, production settings and
feature enablement remain Jesse-only. Auto-rename and auto-grab stay off.

---

## Jesse's actions (nobody else can do these)

| # | Action | Notes |
|---|---|---|
| J1 | Merge + deploy `security/external-report-review-2026-07-30` (`41d0193`) | With J2 in one rebuild |
| J2 | Merge + deploy `agent/resolution-key-canonicalization` (`2b29896`) | 4K filter fix |
| J3 | Merge `agent/nas-mount-stage0-deployment` into main | Ends the worktree/`-SourceRepo` awkwardness |
| J4 | Switch Windows default terminal to **Console Host** | Settings → System → For developers → Terminal. Unblocks C6 |

Deploy is one rebuild >10 min. Watch it.

---

## Decisions

### Security review
1. **Security fixes** — merge and deploy soon (J1).
2. **The sender** — **ignore entirely.** No reply, no engagement. Not a holding
   response, not later. Closed.
3. **Public-repo topology** (hostname, NAS + share names, drive letters) —
   **accept it, do not scrub, do not rewrite history.** Instead add a
   **pre-commit secret scanner** so the next mistake is never published.
4. **Audit** — finish the remaining sections: full route inventory with auth
   levels, secrets audit, reverse-proxy header trust.

### App
5. **4K filter fix** — merge + deploy alongside the security fix.
6. **Full-disc releases** — keep excluding them at scan time (unchanged), but
   **surface the setting in the UI** so it is visible and reversible.
7. **TV resolution filtering** — enable it, **and add a 720p chip** (currently
   228 movies + 230 TV items cannot be filtered to any resolution).
8. **Documentaries** — measured 72% of in-scope releases missing. **Full design
   pass first** (brainstorm → spec → plan), not straight to code. Destination in
   Plex still undecided; settle during design. Note the genre page uses a
   different template (`h5`, no `article`), so the existing parser returns zero.

### RSS
9. **Do not end the window yet** — keep it running while the misses are
   investigated.
10. **Get the titles behind the 97 misses.** Decides whether RSS is fixable or
    unsuitable, and whether the number is honest.
11. **Close the RSS full-disc gap now** (#191) — the page path excludes
    full-disc, the RSS path does not. Entangled with #10: full-disc releases may
    be inflating the miss count.
12. **Fix the blind readiness check** — it calls host `127.0.0.1:9721`, which
    does not exist because no host port is published. Point it at the container.

### Renaming
13. **Investigate the 44% failure rate** (69 of 158 jobs). Read-only.
14. **Auto-rename stays paused**, but **plan a supervised manual run soon** —
    a handful of files, watched, nothing unattended.

### Infrastructure
15. **Docker Port Watchdog** — Jesse switches default terminal (J4), hide flags
    added, then **re-enable at full 2-minute frequency**. Currently DISABLED.
16. **Frigate Watchdog** — same fix, same time.
17. **Gotify token in plaintext** in `docker-port-watchdog.ps1` — handle with
    the secret-scanner work (decision 3).

### Backlog
18. **#185** HDR10+ labels + Kometa badges — do it.
19. **#184** Scan metrics + reason codes — do it. (This instrumentation would
    have exposed the full-disc problem in a day rather than months.)
20. **#192** Correct the parked RSS criterion spec — do it.
21. **Unfinished-work audit** — re-run, but **after** this session's work.

### Dropped
22. **Disk 9** — Jesse: not involved with Plex or ScanHound. Removed from the
    active list. Do not raise again.

---

## Proposed execution order (Claude)

Dependency-driven, most entangled first:

1. **RSS full-disc symmetry** (#191) — must land before the miss analysis is clean
2. **RSS miss analysis** — the 97 titles, decomposed by cause
3. **Rename failure analysis** — why 69 of 158 failed
4. **Blind readiness check fix**
5. **Secret scanner + Gotify token**
6. **Finish the security audit** (routes, secrets, header trust)
7. **TV resolution + 720p chip**
8. **Full-disc setting surfaced in UI**
9. **#192** docs correction (quick)
10. **#184** scan metrics, **#185** HDR10+ labels
11. **Documentary design pass**
12. **Auto-rename supervised-run plan**

Blocked on Jesse: watchdog hide flags (needs J4), all merges and deploys.

---

> **SUPERSEDED 2026-07-31** by `2026-07-31-plan-rev2-AUTHORITATIVE.md`.
> In particular the full-disc/RSS-miss reasoning in this file is WRONG and was
> retracted; see rev2 section 1.1. Jesse's 22 decisions are unchanged.
