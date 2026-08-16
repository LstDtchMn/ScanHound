# Priority plan - 2026-08-16

Produced by verifying the 138-item unfinished-work audit against live state:
the running container, `main`, GitHub, and the Windows scheduled tasks.
**112 of 138 items survived verification.**

Companion to `UNFINISHED-WORK-2026-08-16.txt`, which is the raw unverified sweep.
Where the two disagree, THIS file is the checked one.

---

# ScanHound + Homelab: What's Actually Left, and In What Order

## The headline numbers

| | Count |
|---|---|
| Findings handed to me | **138** |
| Distinct items after removing restatements | **88** (50 were the same thing described twice or three times) |
| Already done, superseded, or your own closed decisions | **19** — delete these from tracking |
| Genuinely open | **69** |
| Worth doing | **49** (25 I can start now, 24 only you can do) |
| Recommend abandoning outright | **16** |
| Parked — blocked on someone or something outside this house | **4** |

The single most useful sentence in this document: **PR #59, four "abandoned" branches, the DV coverage gap, the 694-row FEL write, the Plex TV-share fix, and eight infrastructure items are already finished or already decided.** That is 19 things you can stop carrying.

---

## The one dependency that governs everything

Right now `main` is **red**. Its own test suite has been failing since this morning's merge, and the cause is a live bug (`queue_reason='item_retry'` violates the database's own rule, so that code path has never once succeeded). Every open PR inherits that red X, including PR #84, which is innocent.

```
feat/queue-review-followups   ← merging THIS turns main green and kills 4 live bugs
        ↓
PR #84 (clear-hold button)    ← goes green automatically once main is green
        ↓
feat/bulk-clear-download-results
        ↓
feat/item-history-sheet       ← needs one constant repointed BEFORE it merges
        ↓
one `up -d --build`           ← covers all of the above plus 8 small fixes
```

Other dependencies worth knowing:

- **TL-043 (IoT VLAN) must be fixed before TL-038 (VLAN segmentation)** — 61 devices are on the wrong network, so any firewall rules you write today protect nothing.
- **O-1 (missing MANIFEST.json) must land before O-2 (installer ordering fix)** — and both need one elevated re-install, so do them in the same sitting.
- **The Kometa badge file depends on nothing** — ScanHound is *already* writing DV8/DV5/HDR10 labels to Plex, and ~331 movies currently carry a label with no badge behind it.
- **The RSS canary build depends on your GO/NO-GO** — do not let me build it before you decide.

---

## MY QUEUE — work I can finish without you

### Tier 1 — Do this week (something is broken or unprotected right now)

1. **Push `feat/queue-declared-semantics` to GitHub** — six commits currently exist in exactly one place on earth, the X: drive with the failing disk; this is a plain push, no merge, no risk.
2. **Open the three queue PRs in stack order** — the fixes for four live bugs are done, reviewed three times, and have simply never been submitted; opening them costs nothing and puts them in front of you.
3. **Re-point `ATTEMPT_HISTORY_TRUSTED_FROM` to the deploy timestamp** — otherwise the new history screen will show you three fabricated "FAILED" rows as if they were real download failures.
4. **Fix Frigate's AI descriptions** — add two output-length settings so the AI stops getting cut off mid-sentence; right now 100% of the last 48 hours of descriptions failed, and this also silently broke the newer whole-alert threat summaries.
5. **Diagnose the hourly mount-task failure read-only** — I work out which of its three internal checks is tripping, so your elevated run is one command instead of an investigation.
6. **Settle the 711-title Dolby Vision contradiction with one measurement** — two audits disagree (386 titles undetected vs 676 already fine); one careful re-measure either creates real work or deletes it entirely.

### Tier 2 — Do next (real value, no fire)

7. **Build SMART disk monitoring plus an alert** — you have had **zero** disk-health data for 23 days and nothing anywhere is watching any drive; the collect-and-alert half needs no elevation.
8. **Fix the three monitoring-script bugs (TL-044 M2/M4/M5)** — a log search that grabs a whole 10-minute window, two checks that return no verdict at all when data is missing, and a backup counter that mislabels every file; then request round 2 from ChatGPT against the new SHA.
9. **Investigate the WUD update-check failures** — Immich has had no working update check for at least 13 consecutive scans, and the recorded cause ("Docker Hub rate limit") is wrong because both images come from ghcr.io.
10. **Run the reveal warm-up experiment** — your own observation suggests the link-verification only happens once per browser session; the cheap test is already written down and needs no behaviour change.
11. **Small fix: stop retrying `.mp4` files forever** — 50 files (Twisters plus 22 episodes of one show) are re-scanned every run and can never succeed because the tool cannot read that container.
12. **Small fix: three Dolby Vision correctness bugs** — one failed scan currently downgrades a known-good record, stale records still authorise label removal, and multi-copy movies get pinned to "unknown" permanently.
13. **Small fix: Dolby Vision label names in three stale places** — cosmetic today, but the settings screen shows retired names and the saved config still holds them.
14. **Small fix: make LLM failures visible** — the file that identifies movies by AI has 8 log lines, all at a level that never prints, so seven days of logs contain zero records of any failure; three lines fixes it (not the 4,111-line branch someone built for this).
15. **One documents PR** — 11 review documents, including your 12 unanswered questions, exist only on branches; folding them into `main` and rewriting the 6-day-stale STATE-OF-PLAY file stops anyone reading `main` from being actively misled.
16. **Clean up 17 stray test containers** — they've been running 3 days, pin disk images, and block cleanup; the rule that would have prevented them was bypassed again.
17. **Correct three false lines in my own memory** — they claim a driver removal is pending (it's done), a commit isn't merged (it is), and three PRs aren't deployed (they are).
18. **Close TL-034/TL-035** — the measurement was already taken: front-door alerts dropped from ~12.7/day to ~5.4/day and held, so the answer is "the fix worked, no further tuning".

### Tier 3 — Worth doing, in this order, once the merges land

19. **Build the JDownloader reconnect backoff and call timeouts** — the design is finished and on `main`, only the code is missing; this is the suspected cause of the 15-hour download stall.
20. **Build the per-item history screen** — the data half exists on a branch, the screen was explicitly never built.
21. **Harvest two fixes from the dead branches** — a database-update bug and a latent security hole (see abandon list) rewritten as two small PRs instead of merging 134 commits.
22. **Cherry-pick the four secret-scanning files** — a working scanner was built and never turned on; it's workflow files only, so it lifts out cleanly.
23. **Write the log-cap and image-pin change set** — 46 of 53 containers have unlimited log growth on your constrained C: drive; I prepare it, you apply it.
24. **The five remaining architecture recommendations** — days of work you already authorised; do not start these until the queue stack is merged and deployed.
25. **The RSS coverage-canary build** — designed twice, planned once, zero code; **hold until you answer the GO/NO-GO below.**

---

## JESSE'S QUEUE — nothing here can move without you

### Do this week

1. **Merge the queue stack (followups → #84 → bulk-clear → item-history), then one rebuild.** Turns `main` green, kills four bugs that are live in the container right now, revives a starvation alert that has *never* been able to fire, and gives you the button to release a stuck download. **This one action closes 11 of my items.**
2. **Answer the RSS GO/NO-GO.** The evidence collector has been shouting "MANDATORY STOP CONDITION — 28 misses never resolved, roll back" three times a day, and — importantly — **it stopped notifying you** because it only alerts when the *set* of problems changes, not when the count grows.
3. **Copy `dv_badges.yml` into the Kometa config.** ~331 movies already carry DV8/DV5 labels in Plex with no badge; note the repo file uses a different style and corner than your current badges, so decide placement first.
4. **Register the ScanHound database backup task.** A verified 52 MB backup was taken today by hand, but nothing schedules it and the dead-man's-switch token is unset — one good copy is not a backup policy.
5. **Run the mount script elevated once and send me the output.** It has been reporting failure 288 times a day; mounts are fine, so this is the guard that protects your TV files crying wolf — or not.
6. **Fix the IoT VLAN (TL-043).** ~2 minutes of clicking plus 10 minutes of waiting, must be done while you're home; until then every segmentation plan is decorative.
7. **Turn on UniFi auto-backup and download a copy now.** If the router resets today you lose 7 VLANs, 68 firewall rules, every DHCP reservation and all WiFi config, with no copy anywhere.

### Do soon

8. **Remove the stale Blue Iris port-891 forward** — an open door to a service that no longer exists.
9. **Remove the plaintext admin password from Vaultwarden's config.json** — the secure hashed version is already set but is being overridden by the plaintext file, which then goes offsite to Backblaze.
10. **Delete `Cosole Log.txt` in the Procare extension folder** — it contains your Procare password in plain text.
11. **Put UAC back to its default setting** — it is currently at its weakest: any program running as you can take administrator rights with no prompt and no visible sign.
12. **Regenerate the deployment manifest and re-install the mount task (O-1 then O-2)** — rollback is currently *blocked*, and a stale pre-fix copy of the rollback script on disk makes it look like it works.
13. **Re-register four monitoring jobs to run when you're signed out** — backups and watchdogs currently only run while you're logged in; they've never been tested against a signed-out reboot.
14. **Remove the dead `ai.turtleland.us` hostname** — still answering publicly with an empty placeholder. (The bigger half of this — the apex leaking your whole service map — is already fixed.)
15. **Provide the Immich API key** — one value unblocks a fully built, fully staged photo-enhancement job that has never run.
16. **Apply the log caps and image pins** — needs container restarts.
17. **Decide the elevated-script question (F2)** — the highest-privilege scheduled task runs a script that any non-admin process can rewrite.
18. **Clean up the C:\Tools file permissions and check auditing** — execution works; this is the leftover trap from the incident, and only an elevated shell can read the audit setting.

### Decisions I need from you (no work, just an answer)

19. **Does the Dolby Vision key hash go in git?** — the compose file has been uncommitted for five days; the live deployment is safe, but a fresh checkout silently breaks DV detection.
20. **Which always-on box hosts the external probe (TL-029)?** — full design is done, waiting on two answers since 26 July.
21. **Gate `ebooks.turtleland.us` or accept it?** — 1,090 hacking probes in the current log; the app itself does enforce login, so this is tidiness, not exposure.
22. **Remove the dead `L:\TV Shows 2` folder from Plex** — and note something new: Plex's intro/credit marker task has thrown an error on all nine of its last runs.
23. **Test restoring one photo from Backblaze** — never done; the database restore was tested, the actual photos never were, and it needs your Backblaze login.
24. **MedSpa (your sister's site)** — the domain is connected but the site is still password-gated and unpublished, now 2.5 weeks past the soft-launch date; seven small items need her answers or your mouse.

---

## NOT WORTH DOING — 16 items to delete

These are real, verified, unmerged work. I am recommending you throw them away anyway, and here is why.

**The six stale branch programmes (134 commits, ~40,000 lines).** All were written around 3–5 August. `main` has moved **268–302 commits** since. These are no longer merges, they are re-implementations, and every one of them would land untested code on a codebase that has changed underneath it.

1. **`agent/hybrid-sweep-*` (3 branches, 118 commits)** — the "Completion Contract" programme; abandon the merge, but let me lift **one** real fix out of it (a database update that silently drops two columns).
2. **`agent/audit-fixes-pass2` (16 commits)** — abandon the branch, but let me re-write **one** finding as a fresh 20-line PR: if the database is ever rebuilt from scratch, the login gate opens and anyone can set a new password. Latent today (your login is armed), genuinely dangerous if it ever triggers.
3. **`agent/scan-metrics-wiring` + sibling (4,111 lines)** — scan-failure visibility built twice, landed never; if you still want it, it should be re-scoped small, not merged.
4. **`agent/rename-safety-gate` (11 commits)** — a safety net for data-loss bugs that were already fixed on `main` by another route.
5. **`claude/nice-meitner` + `claude/nostalgic-brattain` (37 commits)** — machine-named branches nobody can identify at a glance; 277 commits behind, low-value contents.
6. **`agent/nas-mount-readiness` + 2 competing specs** — the approach that mattered (move retry to the scheduler) already shipped; the branch is an explicit "wip" commit, 302 behind.
7. **Track 2's fault-injection CI programme (N-2…N-7)** — keep auto-rename frozen (that's the safe state), abandon the elaborate test harness; two of its steps can't even run on the drive they were scoped for.

**Things that are already answered or already decided:**

8. **PR #59 (DV runbook)** — close it; it explains how to turn on a task you deliberately disabled, replaced by a task that ran successfully today.
9. **Track A's 12-step closeout** — its endpoint (RSS promotion) was overtaken by the hybrid decision on 11 August.
10. **The two "H-series" security items** — websocket token in the URL and root-in-container were *deliberately* scoped out for a single-user home deployment; stop re-raising them.
11. **The threshold-asymmetry question from PR #77** — the code already documents both cases where it lives; close the question.
12. **A pruning policy for two orphaned download rows** — the bulk-clear button (already written) removes them; don't build a policy for two rows.
13. **The stray `item_history.py` in the container** — nothing imports it and it vanishes on the next rebuild; no action.
14. **The unfiled dovi_tool bug report** — a genuine upstream bug, written up, never submitted; file it in five minutes or delete the file, but stop tracking it.
15. **The AM6B+ Android/SmartTube test** — YouTube already works through the existing add-on; the migration half is done and verified.
16. **Procare per-child/EXIF expansion** — stalled 63 days, blocked on capturing a live logged-in session, and it's a hobby item. (The plaintext password file in that folder is *not* on this list — that's J10.)

## PARKED — 4 items, nothing to do today

- **The Frigate blackout's root cause (TL-042)** — two instruments are built and watching; it cannot be closed until it recurs, and detection has been healthy for 5 days.
- **The Frigate 0.18 upgrade** — beta3 is the newest tag upstream has published; there is nowhere to move to.
- **The bricked PTZ camera** — every network recovery path is proven dead; it's an RMA or a soldering iron.
- **The Amyuni virtual-display removal** — the driver swap already happened and you've had **zero** crashes in 45 days; leave it unless a blue screen returns.

## DELETE FROM TRACKING — 19 verified non-items

**Already done (12):** four "abandoned" branches whose code is already on `main`; the Frigate update-filter (applied at the August 9 recreate and persisted in source); three of the five August 5 audit actions (Beszel re-paired, reboot taken, clock fixed); the mystery of what recreates the container (answered — it was the pinned compose file, fixed August 12); the Plex TV share (already indexed under a different share name, 1,350 episodes visible); the two HDEncode off-switch/cancellation packages (live in the container); the HDEncode end-to-end proof (37 real staggered batches, 358 completed downloads); the duplicate-compare quality-tier bug; Home Assistant (back up on its own); the Plex LAN bandwidth setting (439 of 439 sessions correctly classified); Uptime Kuma retention (776 MB → 124 MB); Beszel (metrics flowing again).

**Superseded (3):** PR #59; the "~2,900 4K titles never scanned" figure (now 534 genuinely undetermined, and the scanner is actively working through them); the database-snapshot timing problem (fixed, and the bigger versioning defect behind it fixed too).

**Your closed decisions (4):** the failing disk, the three legacy attempt rows, the dead HDEncode adapter, and the per-host source identity decline. **One piece of paperwork:** the backlog file still lists the disk as P0/open, which is what keeps regenerating it on every sweep — that line should be marked WONT-DO the way TL-002 was.