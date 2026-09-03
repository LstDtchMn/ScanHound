# Complete review of ScanHound, 2026-08-01 to 2026-09-02

**Scope.** Everything merged to `main` since 2026-08-01 (410 commits, 79 merges, 43 PRs #58–#99, `main` @ `0a2751d`), plus the open branches: #101 `ops/deploy-and-permission-scripts` @ `b4375a0`, #94 `agent/hybrid-sweep-rebased` @ `ac2baa3`, #100 `chore/retire-desktop-stratum` @ `8c51df0`, #102 `feat/share-identity-guard` @ `0adbd72`, and the design branch @ `e99fe73`.

**Method.** Nine review lanes, one per area, each in its own fresh worktree, each running that area's tests and then trying to break the two or three claims that would hurt most if false. An adversarial verifier per lane re-executed every finding rated MEDIUM or above and hunted for what the lane missed. Nothing below is asserted from reading alone: every defect was reproduced by a test or a command, in most cases twice, by two different agents. Where a lane could not execute something, it says so. Five defects rated HIGH were fixed the same day, each on its own branch from `main`, each fix verified by a third agent and shown to fail with the defect reintroduced.

**Evidence boundary.** The `scanhound` container has been stopped since 2026-08-31 11:51 UTC (the NAS is off on purpose), so "delivered to production" could not be checked for anything in this window; the last-running build was `d08c989`. The Docker deploy suite was not run by any lane (it is the harness that took the cameras offline on 2026-08-31); it was run three times under guard earlier the same day: 45/0. Author-reported numbers from before this review were re-measured; several were wrong and are listed in section 3.

**Lane status.** Eight of nine lanes complete with verifiers: downloads/queue, DV + Kometa, rename/fileops, NAS resilience, API + frontend, docs and handoffs, whole-suite health, deploy and recovery scripts. The HDEncode/RSS lane is being re-run on a stronger model after its first run returned placeholder text in every field (the verifier caught it, and the lesson is in section 7); its section will be appended.

---

## 1. What is wrong, ordered by what it costs the owner

| # | finding | severity | area | status |
|---|---|---|---|---|
| RN-1 | **Undo is a no-op for copy placements and inverts an overwrite.** `undo` trashes the placed file, then restores "the newest trash entry at that path" — the one it just made. Reports ok. For an overwrite placed by copy it puts the NEW file back and leaves the displaced original in trash. | HIGH | rename | **fixed, PR #103** |
| DV-2 | **The watchdog's Gotify channel has been dead since about 2026-08-21.** Every push is rejected with HTTP 401 (512 rejections in 72 h, zero delivered). The checker truncated the error to 120 characters, so its own log shows only a chopped traceback, 218 times, undated. Nothing this watchdog watches has been able to alert for twelve days. | HIGH | watchdog | **visibility + own-token override fixed, PR #107**; the token is the owner's, deferred until ScanHound is back |
| DLQ-1 | **A verification hold anywhere silences the queue alerts.** The stall report counted holds on cancelled and completed batches and on other sources, suppressing `executor_starved` and `source_no_progress` for the whole queue; `cancel_batch` and the last `cancel_item` never cleared the hold. | HIGH | queue | **fixed, PR #105** |
| DLQ-2 | **The watchdog keyed all three queue conditions under one marker**, so after the first queue alert a new condition was "already alerted" forever. With DLQ-1, one lingering hold made the queue alert path permanently silent. | HIGH | watchdog | **fixed, PR #104** |
| DV-1 | **DV detection has failed every scheduled run since 2026-08-31 07:00** (exit 16) because `Y:` does not answer with the NAS off — expected. Defect: the abort named a mapping mismatch that did not happen (the two strings it printed are byte-identical). | HIGH (message: MEDIUM) | DV | **diagnosis fixed, PR #106**; detection resumes when the NAS does |
| API-1 | **A rescan permanently strips a row's category attestation** (`true` → `false`), after which `get_scan_category` returns nothing for it. Reproduced through the real route. | HIGH | API | fixed on #94 (R4-94-4), **not on `main`** |
| OPS-1 | **The live recovery task is the 2026-08-16 script.** `C:\ProgramData\ScanHound\deploy\mount-nas-shares.ps1` (what the Scheduled Task runs) lacks the entire crash-consistency block and the 2026-09-01 self-protecting-container change; the runbook describes protections the live task does not have. The repo ships the copier (`scripts/install-mount-task.ps1`). | HIGH (stale doc / undeployed) | ops | owner's deploy step, after #101 review |
| DOC-1 | **STATE-OF-PLAY.md still says "`main` does not contain the current DV work — do not build on main's parser."** All four branches it names as "where the work is" have been ancestors of `main` for weeks; `dv_detect.py` on `main` has the FEL/MEL logic. No superseded banner. The consolidation map of the same date says the same. | HIGH (stale doc) | docs | open |
| DLQ-3 | Nothing merged since 8/1 is provably running: container stopped 2026-08-31, `unless-stopped` will not restart it, the NAS is off on purpose. | MEDIUM | delivery | expected; documented |
| TST-1 | **The test suite writes into a real host directory.** `C:\.scanhound-trash` holds 390 leftover bucket directories from test runs (with copies of fixture media and manifests). `test_rename_core.py:550` asserts an exact basename that dedup renames whenever a leftover of the same name exists — the source of the two full-suite "flakes" (5214 passed / 2 failed / 5 skipped, 15m40s; both pass in isolation). Not a flake: pollution. | MEDIUM | tests | open |
| DV-3 | Every detector run re-stamps `last_seen_at` on all 9,652 rows, so it no longer dates an observation and the hourly auto-sync gate re-arms on zero-scan runs. | MEDIUM | DV | open |
| DV-4 | 58% of the detector store (5,602 of 9,652) is `unknown`; 1,811 are "No HEVC video track" — a permanent condition classified as a detection failure and retried on a 168-hour backoff forever. | MEDIUM | DV | open |
| DV-5 | `_vocab_from_config` raises on valid JSON that is not an object (a list, a string, a number), killing the whole label sync; the settings route accepts any string. | MEDIUM | DV | open |
| V-DV | 3,875 rows (40% of the detector store) carry a mixed path shape written 2026-08-14/15 by the now-disabled 'ScanHound DV Detector' task. | MEDIUM | DV | open |
| RN-3 | `results.py:750` normalises cached rows unguarded: one row whose title is not a string 500s the entire `/results/cached` endpoint. (Bridge code, #94.) | MEDIUM | API | open on #94 |
| RN-5 | Merging #94 as it stands makes the JDownloader auto-rename entry point a silent no-op until two config keys nobody sets are recorded; the PR body does not say so. | MEDIUM | rename | open on #94 |
| API-2 | #100 deletes the only recipe for the sidecar binary the retained Tauri shell still launches. | MEDIUM | desktop | open on #100 |
| V-DLQ | `queue_source_observations()` uses a lexical `MAX(started_at)` over mixed timestamp shapes and returns the OLDER row as newest — the class that killed three alerts in August. It has no consumer today, which is the only reason it is not live. | MEDIUM | queue | open |
| OPS-2 | `scripts/claude-permissions.ps1` has no `Set-StrictMode`; deleting the `$keptCopy` assignment is silent and survives the mutation checker — the recovery-command branch it gates has no test. | MEDIUM | ops | open |
| DOC-2 | `docs/TODO.md` says "last updated 2026-06-29" (git says 07-01) and describes the DV feature as blocked on a seed importer; `README.md` is titled "MediaScout", documents only the retired desktop app, and never mentions Docker, the API or the web UI. | MEDIUM | docs | open |

## 2. The resilience change, reviewed as new code

`feat/share-identity-guard` (#102) and the recovery-task change in #101 were written on 2026-09-01 and reviewed here with no prior review. The direction holds (35 pin cases, 24 then 35 unit tests, every guard shown to fail against its own defect), and the review found six defects in it, all fixed the same day in `0adbd72`:

- NAS-1 (HIGH): the wrong-share rule was a prefix match — a share named `kids` verified as the TV share `k`. The host task anchors both sides; the app now does too.
- NAS-2: share names with spaces could never verify (the kernel prints super-options raw; the parser split on whitespace).
- NAS-3: two restore paths (trash restore, startup repair) wrote under a blind root without the guard.
- NAS-4: deleting the source-side undo guard left every test green.
- NAS-5: `/health` published the NAS host and share name on an unauthenticated route.
- NAS-6: the refusal promised a retry nobody schedules.

Still open and stated in the commit: "verified" means identity, not writability — a stale 9p mount of a dead share reports verified and the write then fails with a raw I/O error rather than the guard's message; and refused TV jobs are not retried on their own when the share returns.

## 3. Claims that did not survive re-measurement

| claim | where | what was measured |
|---|---|---|
| mount-safety pin "28 passed / 0 failed in a CRLF worktree" | R5-101-1 lane, 09-01 | fresh checkout: 0 tests run, setup throws. Fixed `7c0428c`; 35/0 in CRLF and LF |
| Docker suite "44 passed / 0 failed" | same lane | fresh checkout: 38/6, the six being the entire deliverable. After the fix: 45/0, three guarded runs |
| "the same identity rule the host task uses" | `0355101` | prefix match, not anchored (NAS-1) |
| "/health emits only state, reason and fstype" | same | it emitted origin and mountpoint (NAS-5) |
| "a normal undo simply finds no matching trash entry" | `service.py` comment since #62 | false for every copy / replaced-source-hardlink undo; it finds its own (RN-1) |
| "durable health check: alert when DV detection is broken" (#73) | PR title | no alert delivered since ~08-21 (DV-2); its store check returns no problems two days into a total detector outage |
| "Three stall alerts: separate what one timer cannot" (#81) | PR title | separate from JD, not from each other (DLQ-2) |
| the runbook's recovery-task guarantees | `docs/runbooks/2026-08-28-…` | describe the branch, not the live task file (OPS-1) |
| "two ordering flakes" in the full suite | tests-health lane's own first reading | refuted by its verifier: real host-directory pollution (TST-1) |
| "STATE-OF-PLAY: main does not contain the DV work" | `docs/reviews/STATE-OF-PLAY.md` | all four named branches are ancestors of `main` (DOC-1) |

## 4. What survived attack (so it is not re-litigated)

- #83 failure-scope promotion: item-local on one stall, batch-wide on a second distinct item, siblings parked, cooldown honoured.
- #85 remove-package epoch race: a removal between snapshot and persist stops the real poll writing.
- #92 category forwarding through the queue reaches `download_item`.
- #62 undo data-loss guard and #63 paused-scan resume: merged, in the last-running build, three line-numbered mutants each caught.
- #72, #74 (for dict inputs), the DV ingest key (method+path scoped, constant-time, fail-closed).
- The bridge on #94: 3/12 → 0/12 API-vs-matcher disagreements, 37/77 → 0/77 across the closure, no new persisted authority state.
- Frontend: 32 vitest files, 463 passed; five line-numbered mutations each killed.
- HANDOFF-2026-08-17-EVENING.md: every merge claim checks out exactly. The 2026-08-10 runbook carries an accurate superseded banner.
- PR #101's three PowerShell suites on a fresh checkout: 35/0, 14/0/1, 57/0; the permission mutation checker runs to completion with every declared mutant behaving as declared.
- Whole Python suite on `main`: 5214 passed, 5 skipped (all platform-conditional), 2 failures explained by TST-1.

## 5. The five fixes, and the open pull requests

| PR | what | proof |
|---|---|---|
| #103 undo restores its own trash entry | `undo_place` returns what it trashed; `undo` excludes that entry | 3 consumer tests fail with the exclusion disabled; 274 pass |
| #104 watchdog: one key per queue condition | `check_queue` returns `{key: message}`; each keyed separately | the reviewer's three-run probe sends three alerts; 4 tests fail with the single key restored |
| #105 stall report: live holds, held source only; cancel clears | `queue_stall_report` + two clearing points | 4 line-numbered mutations each kill exactly their test; 257 + 228 pass. `human_required` now also ignores dead holds (a narrowing, stated) |
| #106 DV scan abort names which of three things went wrong | pure diagnosis helper; exit 16 unchanged | verifier drove the real script with `Test-Path`/`Get-SmbMapping` shadowed to the live state |
| #107 watchdog delivery failures visible; own-token override | timestamps, full 401 body, ACTION line, `SCANHOUND_GOTIFY_TOKEN_FILE` read by BOM | 12 tests; the verifier's blocker (UTF-16 token file killed the check) fixed and shown to fail |

| open PR | state | blocked by |
|---|---|---|
| #101 deploy path + crash consistency + task change | ready, CI green at `b4375a0` | a ChatGPT round on the crash-consistency and task changes; the supervised first run needs the NAS back; OPS-1 is the deploy step after it |
| #94 hybrid sweep + V6/V7 bridge | draft | RN-2 (conflict-bit delivery test still missing), RN-3, RN-4 (cites a design doc not on the branch), RN-5 (auto-rename no-op) |
| #100 desktop retirement | draft | API-2 |
| #102 share-identity guard | draft, CI green at `0adbd72` | review of the round-7 fixes; deploy order after #101's task change |
| #103 #104 #105 #106 #107 | draft | review |

## 6. Decisions the owner has made during this review

- The NAS is off on purpose for a couple of days; do not raise it.
- The Gotify token stays as it is until ScanHound is back up; until then no watchdog alert can be delivered, and the checker now says so in its log.
- The three missing DV badges (DV8, DV5, HDR10) were made, installed beside the existing two, and the overlay file gained three entries; Kometa picks them up on its next run.
- The resilience work goes through review before anything touches the live task or production.

## 7. Lessons for the next review, recorded so they are not paid for twice

- **One workflow at a time, cheap models for finding.** Two concurrent review workflows on the session model hit the session limit four times in one day, each time killing every in-flight agent. Banked results replay on resume only as a prefix of the pipeline, so a killed run loses everything after its first interrupted agent. The four remaining lanes ran on Sonnet at a third of the cost with Opus verification.
- **A cheap finder can return junk that satisfies the schema.** One Sonnet lane returned the word "test" in every field and one fix lane returned "a" for its test list. The verifiers caught both; a review without a verifier per lane would have read them as "no defects found". Never score a lane by its report alone.
- **The test harness is a production change to the host.** Pre-flight and post-run network checks are now in the Docker suite; the Python suite's writes into `C:\.scanhound-trash` are the same class and are still open (TST-1).
- **"Fixed" is not "live".** Five of the eight areas had a document or PR claiming something was deployed, alerting or protected that the running system did not have. Check the container, the task file and the log, not the merge.
