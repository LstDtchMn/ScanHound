# Complete review of ScanHound, 2026-08-01 to 2026-09-02

**Scope.** Everything merged to `main` since 2026-08-01 (410 commits, 79 merges, 43 PRs #58–#99, `main` @ `0a2751d`), plus the open branches: #101 `ops/deploy-and-permission-scripts` @ `b4375a0`, #94 `agent/hybrid-sweep-rebased` @ `ac2baa3`, #100 `chore/retire-desktop-stratum` @ `8c51df0`, #102 `feat/share-identity-guard` @ `0adbd72`, and the design branch @ `e99fe73`.

**Method.** Nine review lanes, one per area, each in its own fresh worktree, each running that area's tests and then trying to break the two or three claims that would hurt most if false. An adversarial verifier per lane re-executed every finding rated MEDIUM or above and hunted for what the lane missed. Nothing below is asserted from reading alone: every defect was reproduced by a test or a command, in most cases twice, by two different agents. Where a lane could not execute something, it says so. Five defects rated HIGH were fixed the same day, each on its own branch from `main`, each fix verified by a third agent and shown to fail with the defect reintroduced.

**Evidence boundary.** The `scanhound` container has been stopped since 2026-08-31 11:51 UTC (the NAS is off on purpose), so "delivered to production" could not be checked for anything in this window; the last-running build was `d08c989`. The Docker deploy suite was not run by any lane (it is the harness that took the cameras offline on 2026-08-31); it was run three times under guard earlier the same day: 45/0. Author-reported numbers from before this review were re-measured; several were wrong and are listed in section 3.

**Lane status.** All nine lanes complete with verifiers: downloads/queue, DV + Kometa, rename/fileops, NAS resilience, API + frontend, docs and handoffs, whole-suite health, deploy and recovery scripts, and HDEncode/RSS (re-run on a stronger model after its first run returned placeholder text in every field; the verifier caught it, and the lesson is in section 7). The HDEncode/RSS findings are in section 8 and in the table below.

---

## 1. What is wrong, ordered by what it costs the owner

| # | finding | severity | area | status |
|---|---|---|---|---|
| RN-1 | **Undo is a no-op for copy placements and inverts an overwrite.** `undo` trashes the placed file, then restores "the newest trash entry at that path" — the one it just made. Reports ok. For an overwrite placed by copy it puts the NEW file back and leaves the displaced original in trash. | HIGH | rename | **fixed, PR #103** |
| DV-2 | **The watchdog's Gotify channel has been dead since about 2026-08-21.** Every push is rejected with HTTP 401 (512 rejections in 72 h, zero delivered). The checker truncated the error to 120 characters, so its own log shows only a chopped traceback, 218 times, undated. Nothing this watchdog watches has been able to alert for twelve days. | HIGH | watchdog | **visibility + own-token override fixed, PR #107**; the token is the owner's, deferred until ScanHound is back |
| DLQ-1 | **A verification hold anywhere silences the queue alerts.** The stall report counted holds on cancelled and completed batches and on other sources, suppressing `executor_starved` and `source_no_progress` for the whole queue; `cancel_batch` and the last `cancel_item` never cleared the hold. | HIGH | queue | **fixed, PR #105** |
| DLQ-2 | **The watchdog keyed all three queue conditions under one marker**, so after the first queue alert a new condition was "already alerted" forever. With DLQ-1, one lingering hold made the queue alert path permanently silent. | HIGH | watchdog | **fixed, PR #104** |
| DV-1 | **DV detection has failed every scheduled run since 2026-08-31 07:00** (exit 16) because `Y:` does not answer with the NAS off — expected. Defect: the abort named a mapping mismatch that did not happen (the two strings it printed are byte-identical). | HIGH (message: MEDIUM) | DV | **diagnosis fixed, PR #106**; detection resumes when the NAS does |
| HDE-1 | **The switch to make RSS the primary discovery path is live on `main`** (`POST /rss/mode` → `rss_primary`), gated only on the shadow-readiness figure, while every safeguard the merged decision record (#61) makes a condition of promotion — the listing canary, automatic demotion on a proven gap, canary-health surfacing — is unbuilt. Promoting also stops the shadow comparison that produces the gate's own evidence, as the readiness docstring itself says. #94 adds a promotion gate and a per-cycle demotion; the canary is still not built there either. | HIGH | RSS | open; **do not promote** |
| HDE-3 | **RSS actions spend HDEncode reveals without recording the outcome** to source health or the drift detector: `hdencode_action_service.py` calls `scrape_links` and on failure records only a per-action row. The verifier escalated it from latent to live: `POST /rss/actions` is an operator-facing route with no auto-grab flag check. Its mirror also holds: a successful RSS-action reveal cannot release an armed verification hold. | HIGH | HDEncode | open |
| HDE-2 | **The source match on the verification-hold release is untested.** Removing the `WHERE verification_hold_source = ?` match survives 272 tests; "a DDLBase reveal never releases an HDEncode hold" is asserted only in a docstring. The code at `main` is correct; nothing pins it. | HIGH (test gap) | queue | open |
| API-1 | **A rescan permanently strips a row's category attestation** (`true` → `false`), after which `get_scan_category` returns nothing for it. Reproduced through the real route. | HIGH | API | fixed on #94 (R4-94-4), **not on `main`** |
| OPS-1 | **The live recovery task is the 2026-08-16 script.** `C:\ProgramData\ScanHound\deploy\mount-nas-shares.ps1` (what the Scheduled Task runs) lacks the entire crash-consistency block and the 2026-09-01 self-protecting-container change; the runbook describes protections the live task does not have. The repo ships the copier (`scripts/install-mount-task.ps1`). | HIGH (stale doc / undeployed) | ops | owner's deploy step, after #101 review |
| DOC-1 | **STATE-OF-PLAY.md still says "`main` does not contain the current DV work — do not build on main's parser."** All four branches it names as "where the work is" have been ancestors of `main` for weeks; `dv_detect.py` on `main` has the FEL/MEL logic. No superseded banner. The consolidation map of the same date says the same. | HIGH (stale doc) | docs | open |
| DLQ-3 | Nothing merged since 8/1 is provably running: container stopped 2026-08-31, `unless-stopped` will not restart it, the NAS is off on purpose. | MEDIUM | delivery | expected; documented |
| TST-2 | **Two queue tests are order-dependent on `main`.** `test_queue_review_followups.py::test_the_exemption_is_spelled_the_same_way_retry_item_writes_it` and `::test_retry_ready_does_not_bypass_pacing` fail when run after the sixteen scrape-boundary files plus seven other queue suites, and pass alone and when their file runs alone — identically at `main` and at #109. Observation, not cause: which earlier test leaves the state behind is not established. | MEDIUM | tests | open |
| TST-1 | **The test suite writes into a real host directory.** `C:\.scanhound-trash` holds 390 leftover bucket directories from test runs (with copies of fixture media and manifests). `test_rename_core.py:550` asserts an exact basename that dedup renames whenever a leftover of the same name exists — the source of the two full-suite "flakes" (5214 passed / 2 failed / 5 skipped, 15m40s; both pass in isolation). Not a flake: pollution. | MEDIUM | tests | open |
| DV-3 | Every detector run re-stamps `last_seen_at` on all 9,652 rows, so it no longer dates an observation and the hourly auto-sync gate re-arms on zero-scan runs. | MEDIUM | DV | open |
| DV-4 | 58% of the detector store (5,602 of 9,652) is `unknown`; 1,811 are "No HEVC video track" — a permanent condition classified as a detection failure and retried on a 168-hour backoff forever. | MEDIUM | DV | open |
| DV-5 | `_vocab_from_config` raises on valid JSON that is not an object (a list, a string, a number), killing the whole label sync; the settings route accepts any string. | MEDIUM | DV | open |
| V-DV | 3,875 rows (40% of the detector store) carry a mixed path shape written 2026-08-14/15 by the now-disabled 'ScanHound DV Detector' task. | MEDIUM | DV | open |
| RN-3 | `results.py:750` normalises cached rows unguarded: one row whose title is not a string 500s the entire `/results/cached` endpoint. (Bridge code, #94.) | MEDIUM | API | open on #94 |
| RN-5 | Merging #94 as it stands makes the JDownloader auto-rename entry point a silent no-op until two config keys nobody sets are recorded; the PR body does not say so. | MEDIUM | rename | open on #94 |
| API-2 | #100 deletes the only recipe for the sidecar binary the retained Tauri shell still launches. | MEDIUM | desktop | open on #100 |
| V-DLQ | `queue_source_observations()` uses a lexical `MAX(started_at)` over mixed timestamp shapes and returns the OLDER row as newest — the class that killed three alerts in August. It has no consumer today, which is the only reason it is not live. | MEDIUM | queue | open |
| HDE-4 | The ~20-reveals-per-UTC-day quota has no representation in code — no counter, no midnight-UTC reset; the only reveal counters are in memory and die with the container. | MEDIUM | HDEncode | open |
| HDE-5 | Two docstrings say a pasted direct link leaves `source_reveal_succeeded` False; since the round-8 dispatch change it is True (shown by executing the real dispatch), and the real protection is a source-name mismatch elsewhere. | MEDIUM (stale doc) | HDEncode | open |
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

## 8. The HDEncode / RSS lane (appended after re-run)

Four attack questions, four executed answers, 574 tests across the lane green at both `main` and #94; every finding re-executed by the verifier and held.

1. **Does anything merged promote RSS beyond shadow?** Not automatically. But the manual switch is live and gated only on readiness, while the decision record it ships beside says NO-GO and lists the safeguards that must exist first; none do (HDE-1). Promoting kills the evidence the gate reads. The verifier closed one bypass in the code's favour: the discovery mode cannot be set through the generic settings route.
2. **Is the hold still held for a real reveal, never a timer?** Yes — all four writers and three clearers enumerated, and a mutation on the release guard is killed. But the *source match* half of that guard has no test (HDE-2).
3. **What does the code enforce of the daily reveal quota?** Nothing (HDE-4). And the RSS action loop spends reveals through a path that records nothing to source health (HDE-3) — live today via the operator route, not only under the auto-grab flag.
4. **Is the R4-94 laundering class closed at #94?** It looks closed: 0 of 300 listing-reachable rows move on a second identical rescan; 0 order-dependent outcomes when a conflict is recorded mid-orbit. 43 non-stationary row shapes exist in the abstract input space, none producible from a production writer (HDE-6, LOW).

Two line-number citations in the lane's report drifted by about five lines; the verifier corrected them and they are corrected here.

## 9. Round-7 peer review: disposition (2026-09-03)

The peer reviewer (frozen at package head `3d262c4`, before the HDEncode addendum) returned: APPROVE #104, #105, #106 and the code of #107; REQUEST CHANGES on #103 and #102; the round-5 crash-consistency blocker on #101 **closed in code**, its remaining gate being deployment qualification. Every finding was checked against the code before being acted on.

| finding | verified? | what changed |
|---|---|---|
| R7-103-1 — #103 used negative identity ("not the entry undo just made") where positive identity exists at apply | yes: the apply path holds `trashed_to` and discarded it after success | a `displaced_trash_path` column recorded at apply; undo restores exactly that entry; the exclusion search kept only for jobs applied before the column; an undo refuses when a newer job has since been applied to the same destination |
| R7-102-1 — a root written without `=> SERVER\share` silently meant "any 9p mount" | yes, by design of the first version | now malformed (falls back to the default); the wildcard must be spelled `=> *` and is logged loudly at configure |
| R7-102-2 — the wrong-share `reason` on unauthenticated `/health` still named the expected share | yes | `/health` publishes a fixed reason code, never the free-text reason; a test checks every reachable state's values for the host and share names |
| R7-DOC-3 — the Aug 9 hold document says release is per-batch; code is source-wide since `d4832ea` | yes | superseded banner on the document |
| #105's `human_required` narrowing | reviewer: correct, not a defect | none |
| #105's source scope | reviewer withdrew after checking current code | none — the process lesson is recorded below |

Re-ratings adopted from the review, on top of section 1: **RN-5 is a HIGH merge blocker for #94**; DV-1's technical severity is MEDIUM (the outage is expected, the false diagnosis is the defect); DLQ-3 is deployment state, not a defect; TST-1's review priority is raised (a suite that changes its own future outcomes is an evidence problem); DV-2's fix makes failures visible and the token configurable — it does **not** make delivery live, which stays owner-deferred.

Adopted for every claim from here on, from the reviewer's five rules: results carry their environment and execution shape (a setup failure that runs zero tests is not "0 failed"); "same", "separate" and "only" are replaced by predicates and the negative case is executed; "alert" is end-to-end or it is not the word; every operational claim names its locus — branch, merged, installed host artifact, running container, external sink; a cause label is a hypothesis until reproduced. The status vocabulary the reviewer proposed (FOUND · CODE FIXED · CI VERIFIED · HOST VERIFIED · MERGED · INSTALLED · LIVE OBSERVED · EXTERNAL DELIVERY VERIFIED · OWNER-DEFERRED) replaces bare "fixed / deployed / verified" in these documents.

## 10. Round-7b delta review: disposition (2026-09-03)

The peer reviewer, at package head `6b619f4`, closed all four round-7 findings and returned one new one on our own fix, plus the HDEncode addendum's verdicts. Owner decisions taken the same day.

| item | verdict | what changed |
|---|---|---|
| R7B-103-2 — the newer-owner check in #103 failed OPEN (a listing failure read as "no newer job"; the listing itself defaulted to `[]`; 100,000-row caps) | HIGH, confirmed | strict uncapped read that raises; tri-state owner (newer / none / **unknown refuses before any bytes move**); negative test with the read forced to fail after the job was read; fail-open mutant killed. #103 @ `d107b62` |
| HDE-1 — the `rss_primary` switch is live while the accepted decision record says NO-GO | HIGH, confirmed; **owner decision: refuse, through one shared authority, not the route alone** | `backend/rss_primary_authority.py`, consulted by the route (409 with blockers), the scanner's per-cycle mode, the poll cycle and `/rss/status` (`promotion`: requested vs effective mode, blockers, canary fields). A persisted primary runs as shadow. Three mutants killed. #108 @ `9fe5ff6` |
| HDE-3 — RSS actions spend reveals unrecorded; a successful RSS reveal cannot release a hold | HIGH, confirmed; escalated to live via `POST /rss/actions` | **landed, #109 @ `625201e`**: `DownloadService.scrape_links_recorded()` is the one entry point — one observation per reveal, source-matched hold release, all five consumers on it; three mutants caught (one of them closes HDE-2); the verifier caught the lane running a self-selected test list that hid five broken tests — moved to the new boundary in a second commit |
| HDE-2 — the source match on hold release is untested | re-rated: MEDIUM test gap, HIGH consequence | **closed in #109**: removing the `WHERE` source match from the release now fails two tests |
| daily reveal quota | MEDIUM, retain; source-global, not queue-global; do not invent a number | open |
| stale dispatch docstrings | LOW technical / MEDIUM process | open |
| #102 | APPROVE CODE | — |
| #104 #105 #106 #107 | prior APPROVE unchanged | — |
| #101 round-5 crash blocker | CLOSED IN CODE; remaining gate is deployment qualification | — |

Reviewer's updated priority, adopted: HDE-3, HDE-1, R7B-103-2, quota, HDE-2, docstrings. Their one-line summary of the whole review, kept here because it is the thing to fix next time before anything else: *ScanHound has become good at adding local safeguards faster than it has become good at stating exactly what each safeguard proves, which artifact contains it, and which consumer actually uses it.*

## 11. Round-7c delta review: disposition (2026-09-03, evening)

The peer reviewer, reading #108 @ `9fe5ff6` and #109 @ `625201e`, accepted both designs and returned one follow-up on each. Both follow-ups are landed; neither PR is merged.

| item | verdict | what changed |
|---|---|---|
| R7C-108-1 — two tests still encoded the readiness-alone contract and made CI red on #108 | confirmed; **"update them to the new authority; do not skip or delete them"** | Both migrated, neither skipped or deleted. `test_feature_pack_integration`: an unready persisted primary no longer asserts a *skipped* poll; it asserts effective mode `rss_shadow`, both blockers, and the service status showing requested `rss_primary` / effective `rss_shadow`. `test_rss_routes`: readiness alone now asserts the 409 with the canary blocker and that nothing was persisted; the old round-trip runs under the shared authority stubbed to say yes. Five RSS suites: 33 passed. #108 @ `2e91de0` |
| R7C-108-2 — forward requirement: a persisted `rss_primary` written before the hybrid exists must not become effective merely because the canary lands and readiness is green | accepted as a requirement on the FUTURE canary change | Recorded in the module docstring of `backend/rss_primary_authority.py`: that change must require a fresh explicit promotion, or migrate old persisted requests to `rss_shadow` first. The requested/effective split on `/rss/status` is deliberate; the persisted value is not rewritten at startup. No code change now |
| R7C-109-1 — the source-matched hold release existed twice (the queue's inline SQL and the new `DatabaseManager` helper), so the round-7b tests pinned only one copy, and the original HDE-2 was about the other | confirmed | The predicate now exists once: `DatabaseManager._release_verification_hold_for_source_conn(conn, source)`, connection-level, no commit. The queue calls it on its own transaction connection; the public `release_verification_hold_for_source()` wraps it in its own transaction for every other consumer. New test drives the queue's own release method on the queue's transaction with another source's hold armed beside it. Mutants on a whole-tree copy: WHERE match removed from the one primitive → queue-path, direct-helper and RSS cross-source tests all fail; queue path given back a blanket clear → queue-path test fails. Sixteen scrape-boundary files plus the queue suites: 457 passed, 0 failed. #109 @ `3abb575` |
| HDE-2 | — | now closed at both callers |
| daily reveal quota (HDE-4), stale dispatch docstrings (HDE-5), TST-1, TST-2 | unchanged | open. TST-2's two order-dependent failures did not appear in this evening's 457-test run; the observation stands as recorded, not as reproduced tonight |

One instrument note, in the reviewer's own rule: the first mutant run on #109 reported the control failing. The copy had carried only `backend/` and `tests/`, and the Qt batch test needs `ui/`. The copy was rebuilt as the whole tree before any result was recorded; the mutant verdicts above are from that run. A copy that is not the whole tree is not a control.

## 12. Round 8: TST-1 landed (2026-09-03, late)

The round-7c closure ordered TST-1 first: the Python suite was writing into the host's real per-volume trash root and so damaging the instrument every other change is judged with. PR #110 @ `03905e6`, draft, unmerged.

| item | what was measured | what changed |
|---|---|---|
| the defect | `C:\.scanhound-trash` on the development host held 400 buckets and 873 files (0.2 MB) written between 2026-08-09 and 2026-09-03 08:26, all test residue: the app runs in Docker and its volumes never resolve to the host's `C:\` | `fileops._trash_root_for` sites a bucket at the root of the source's own volume; every test that trashed a tmp file wrote there |
| the redirect | with the redirect removed on a copy and the guard kept, the six trash-exercising files produced 12 to 15 tests named as writers into `C:\.scanhound-trash` (two runs), one root named, and 6 to 7 new buckets, removed again by the demonstration itself | both roots, and the ancestor walk, are pinned into tmp_path for every test; three derivation-only tests opt out by marker and still sit under the guard |
| the guard | first full run: 5449 passed, the guard fired 0 times across the whole suite; the one failure was a local-socket abort in a DV host-scan test that passes alone three times and with its file, and does not touch any trash path | every real volume root snapshotted before each test and the session, one level inside each bucket, failing at teardown with the test's name and the root; it never deletes |
| the adversarial read | four defects in the first cut: the root-watch test would have failed on Linux CI; the in-bucket mtime test proved nothing, and fixing it showed that a bucket's mtime does not move on Windows when a file is added inside it; the ancestor walk was unpinned; a silent fallback | all four closed before the commit; the snapshot lists inside each bucket instead of trusting mtimes |

Not done: the 400 pre-existing buckets are untouched (owner's call). TST-2 not started; nothing assumes TST-1 caused it.

**Round 8 follow-up (2026-09-04), #110 @ `3f32681`.** The peer reviewer returned REQUEST CHANGES, narrow, with one HIGH: on POSIX `all_trash_roots()` joins mount points directly and the default-roots mutators (sweep, empty, repair) consume it, so the redirect did not cover discovery. Closed: the ordinary fixture now replaces discovery with an isolated function that keeps only tmp-rooted paths by construction plus the already-isolated registered roots; a regression with a sentinel POSIX mount proves the three mutators never probe it, and a marked counterpart proves the real function would have. Also per review: the bucket snapshot records name, type and size per direct child plus a bounded, symlink-safe digest of `manifest.json`; a plain-marked test cannot call a mutator (raises) unless it opts back in. Process, as Jesse directed: a lower-tier lane implemented from a written spec; an adversarial lane found six defects in that implementation, all closed before commit; the supervisor's mutation run found a seventh (the proof test trashed its own module when the raise was removed). Five mutants killed on a whole-tree copy. The historical 400 buckets were deleted by the owner's hand on 2026-09-04 after verification; test code deleted nothing.

**Round 8 closure (2026-09-04), #110 @ `6ae62dc`.** The reviewer closed R8-TST1-1/2/3 and R8-DOC-1 and asked for one final narrow change, R8R-TST1-4: registered roots were re-added to the isolated discovery without the under-tmp_path filter, so a test could register a real path and re-expose it to the default mutators. Now all three candidate kinds pass one predicate; a registered root survives only when test-owned. Regression: an external root (never created) is registered, read back by the registry, dropped by the isolated discovery, and never probed by repair, sweep or empty; a marked read-only counterpart shows the real discovery would have surfaced it. Mutant (filter removed) killed by exactly that test. Eight trash-related files: 479 passed. TST-1 awaits the reviewer's close; TST-2 is next.

## 13. TST-2: cause isolated and fixed at the ownership boundary (2026-09-04)

PR #111 @ `458c3db`, off `main` @ `0a2751d`, draft, unmerged. The reviewer's ten-step workflow, followed in order.

| step | what was measured | what changed |
|---|---|---|
| reproduce, fresh tree | victims alone 68 passed; 22 predecessor files then the victims' file: 2 failed, 590 passed, `DownloadQueueSourceHeld: The source is temporarily paused` from `backend/download_queue.py:338` | not a flake |
| bisect | by file 22 → 1 (`test_scrape_outcomes.py`); by node → 1 test, `test_challenge_iframe_signal_drops_path_query_and_fragment`; that test then the victims: 2 failed, 67 passed; omitted: 68 passed | one predecessor |
| the leaked state | a real one-hour cooldown on the module singleton `HDEncodeTrafficCoordinator` (`hdencode_coordinator.py:596`), set by a real `observe_challenge()` from `download_service.py:~2520`, read by `download_queue.py:335`; `configure()` resets only on config/db object-identity change, which the queue never triggers | proven with probes: state printed, a fresh queue raised, clearing the fields cured it |
| fix | every consumer reaches the singleton via the getter at call time (grep); two services cache it at construction, safe under function-scoped fixtures only | autouse fixture: a fresh coordinator per test; no production change |
| regression, both orders | test_a holds through a real challenge; test_b (after it) builds a real queue service and must start unheld; test_c distinct objects by reference; forward 3 passed, reverse 2 passed | mutant (fixture neutered): test_b/test_c fail AND the original two victims fail again; control clean |
| larger suite | 22 predecessors + victims + regression in the old failing order: 595 passed; 13 files naming the coordinator: 196 passed; full suite on a combined copy with #110 isolation: 5462 passed, 1 failed (a DV host-scan socket abort, TST-3 candidate, unrelated), real root absent before and after; CI on `458c3db`: green on 3.11, 3.12 and frontend | — |

**Separate finding, filed, not folded in (HDE-6 candidate):** the behaviour that masked this leak is a latent production hazard. `configure()` wipes a live cooldown whenever it is handed a different config or db object, and `backend/detail_scraper.py:~95` reconfigures the coordinator with `getattr(parent_app, "db", None)` through app bridges that define no `db`; constructing a `DetailScraper` therefore clears any live Turnstile cooldown and detaches health persistence until something reconfigures with the real db. Bounded today by startup order (the scanner service is constructed immediately after, before any cooldown exists). Second path: `backend/background_scanner.py:~308` builds `HDEncodeRSSService(cfg, db)` per cycle with `cfg = reg.config or {}`.

Process, per the owner's direction: bisection and proof by a Sonnet lane; fix by a Sonnet lane from a written spec; an Opus adversarial read required three changes (an id-based test that could fail on a working fixture; an over-strong reachability sentence; the `monkeypatch.undo()` trap) and surfaced the separate finding; the supervisor verified the minimal reproduction and the mutants at first hand.

## 7. Lessons for the next review, recorded so they are not paid for twice

- **One workflow at a time, cheap models for finding.** Two concurrent review workflows on the session model hit the session limit four times in one day, each time killing every in-flight agent. Banked results replay on resume only as a prefix of the pipeline, so a killed run loses everything after its first interrupted agent. The four remaining lanes ran on Sonnet at a third of the cost with Opus verification.
- **A cheap finder can return junk that satisfies the schema.** One Sonnet lane returned the word "test" in every field and one fix lane returned "a" for its test list. The verifiers caught both; a review without a verifier per lane would have read them as "no defects found". Never score a lane by its report alone.
- **The test harness is a production change to the host.** Pre-flight and post-run network checks are now in the Docker suite; the Python suite's writes into `C:\.scanhound-trash` are the same class and are still open (TST-1).
- **"Fixed" is not "live".** Five of the eight areas had a document or PR claiming something was deployed, alerting or protected that the running system did not have. Check the container, the task file and the log, not the merge.
