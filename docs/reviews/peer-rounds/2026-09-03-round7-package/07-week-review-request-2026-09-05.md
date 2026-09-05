# ScanHound — Whole-week review request, 2026-08-31 to 2026-09-05

**Repository:** `LstDtchMn/ScanHound` (you now have direct repository access; read the branches and PRs yourself; nothing in this request is a substitute for the code).
**Base for everything below:** `main` @ `0a2751daa4708f3a64439596663e5e4c9699f206` (unchanged all week).
**Evidence record:** branch `review/2026-09-02-complete-review` @ `3d9198c31c1a76a6c0a75eb7eb0f88e515130c22`, directory `docs/reviews/peer-rounds/2026-09-03-round7-package/` (the complete review report `00-complete-review.md`, sections 1 to 15; per-item evidence files `02` to `05`; `provenance.txt`). The patches in that directory are now redundant with your repository access; the evidence files are not.
**Working trees:** every branch below is committed and pushed; nothing reviewed here is uncommitted. HDE-5 (docstrings) is in progress and NOT yet pushed; it will arrive as a separate PR stacked on #113 and is out of scope here.

No merge, deployment, permission change or enablement is requested or authorized by this document. Merging is the owner's decision; the merge order that the stacking requires is given at the end.

## 1. What this week's work is, in one paragraph

A five-week complete review (report sections 1 to 9) produced nine open findings. Seven review rounds (7, 7b, 7c, 8, 8-closure, TST-2, HDE-6, HDE-4) then fixed them one PR each, each PR reviewed by you and closed or conditionally passed, each verified the same way: a written spec, a lower-tier implementation lane, an adversarial read by a separate lane, and the supervisor's own mutation runs on a whole-tree copy showing that every guard fails when the defect it exists to catch is put back. Two of the findings were about the test suite itself (it wrote into the host's real trash root; two tests depended on order), and fixing those changed what the later runs could prove. Three production bugs were found during the reviews of other fixes (the coordinator identity reset, the source-disabled-counted-as-challenge classification, and the unavailable-reads-as-zero read path) and fixed in their own commits.

## 2. The branches, with heads

| PR | branch | head (full SHA) | base / stacked on | scope | your last verdict |
|---|---|---|---|---|---|
| #101 | `ops/deploy-and-permission-scripts` | `b4375a058491accdaa74f3b7b6009d469b4613fe` | main | NAS-outage resilience: mount recovery task, degraded mode, Docker test harness hardened after the LAN-hijack incident, crash consistency C1 to C6 | round-5 crash blocker CLOSED IN CODE; deployment qualification is the remaining gate (marked ready by the owner) |
| #102 | `feat/share-identity-guard` | `094fbda59e7a954eb5fb59519881a13f73dda498` | main | app-side write guard: a destination must be a share-backed root with the expected UNC identity before any file moves | APPROVE CODE (round 7b) |
| #103 | `fix/r7-undo-restores-its-own-trash` | `d107b6203684db44168c95bfd3c24af5d6011f5b` | main | undo restores exactly the trash entry it displaced; unknown ownership refuses before bytes move | R7B-103-2 closed (round 7c) |
| #104 | `fix/r7-checker-three-queue-keys` | `b74ebc338a6836380b6e0320a6303bbeddcc2f95` | main | DV health checker: three distinct queue alert keys | APPROVE |
| #105 | `fix/r7-stall-report-hold-scope` | `9b94ddefee3e37c2991ec05cc55964b51227f08b` | main | stall report names the hold's scope | APPROVE |
| #106 | `fix/r7-dv-scan-abort-names-the-real-cause` | `40c549ca05e615873fef0726613068a447cce944` | main | DV scan abort names the real cause | APPROVE |
| #107 | `fix/r7-checker-delivery-failure-visible` | `f0da60690e33c41e96f6fa78ef4af46e76d11816` | main | Gotify delivery failure visible; token file support | APPROVE |
| #108 | `fix/r7b-refuse-blind-rss-primary` | `2e91de013d372144bb68cd47f730cd669de0d055` | main | one shared authority refuses `rss_primary` until the listing canary exists; a persisted primary runs as shadow; forward requirement recorded | R7C-108-1/2 CLOSED |
| #109 | `fix/r7b-one-reveal-one-source-observation` | `3abb575236d7915ccad9e8a8597f8204dfa6e333` | main | one production entry for a recorded reveal (`DownloadService.scrape_links_recorded`); one observation per reveal; ONE source-matched hold-release predicate used by the queue and the boundary | R7C-109-1 CLOSED; HDE-2 and HDE-3 CLOSED |
| #110 | `fix/tst1-suite-trash-isolation` | `6ae62dc64d7112969c653d26058a3cadd98a35d6` | main | the suite never writes into a real volume trash root: redirect of derivation, ancestor walk, fallback and discovery into tmp_path; per-test and per-session real-root guard; derivation-only marker | TST-1 CLOSED, APPROVE CODE (after R8-TST1-1..3, R8R-TST1-4) |
| #111 | `fix/tst2-queue-order-dependence` | `1db4ac4f913cc2767552f97776f04812d5a9ea0a` | main | every test owns its own HDEncode traffic coordinator (the two order-dependent queue tests were a leaked one-hour cooldown) | TST-2 CLOSED, APPROVE CODE |
| #112 | `fix/hde6-coordinator-context-reset` | `061a6a03d9d6b02d6a0ecc387b1fb178ab16074a` | **#111** | `configure()` never clears a live cooldown on object identity; a caller without a db cannot detach one; bridges expose the db; recovery is any 2xx or 304, redirects neutral | HDE-6 CLOSED, APPROVE CODE (after HDE6-R1) |
| #113 | `feat/hde4-reveal-accounting` | `436819d484140fdf67399d548b60b861b6d79e96` | **#109** | one append-only observation per reveal-boundary invocation with outcome, caller, context, hashed url, UTC time; read API and `/sources` surface; no policy; unavailable is not zero | PASS (your fresh read-only review of 2026-09-05): HDE-4 CLOSED, HDE4-R1 resolved; two optional test nits recorded, not actioned (no dedicated row-conversion-failure test; route tests call the function, not the HTTP layer) |

Older, still open, not part of this week's work: #94 `agent/hybrid-sweep-rebased` @ `ac2baa310a7f77437e7efa7ea8cb27a1776d9f03` (one HIGH merge blocker, RN-5, from the five-week review) and #100 `chore/retire-desktop-stratum` @ `8c51df0e5f77adc514c4df59e309813b06ae3c79` (draft).

## 3. What the per-PR rounds could not see, and what this request adds

Every round reviewed one branch against `main`. Nobody has reviewed the **integrated stack**. Measured today, on a scratch worktree of `main` @ `0a2751d`, merging in this order with `--no-ff`:

```
#108 → #109 → #110 → #111 → #112 → #113   all six merge CLEAN, no conflicts
```

Full suite on that integrated head (scratch merge commit `c1e7914`, discarded afterwards; the stack includes #110's isolation, so the host was never written):

```
real root before: absent
5543 passed, 5 skipped, 14 warnings in 534.75s (0:08:54)   exit=0
guard firings: 0
real root after:  absent
```

Nothing failed, no socket abort, and the count is the sum the per-PR runs predicted (main's suite plus every PR's added tests, none lost to the merges). This is a HOST VERIFIED integration run on the Windows development machine, not an installed or live claim.

Not rehearsed: #101 to #107 in the same stack (they predate this week's rounds and were each merged-rehearsed against `main` only at their own review time). If you want the full thirteen-branch rehearsal, say so and it will be run the same way.

## 4. What to review, specifically

You have the code. The per-PR evidence is in the package. The questions below are the ones a whole-week reading can answer and a per-PR reading cannot.

1. **Stack coherence.** #110 and #111 both add autouse fixtures to `tests/conftest.py`; #112 rewrites `configure()` on top of #111; #113 adds a write site inside #109's boundary and a strict read helper beside `_query_dicts`. Read the merged shape (or merge them yourself in that order) and say whether any fixture ordering, any docstring, or any invariant stated in one PR is contradicted by another.
2. **The three invariants that now hold, stated in code, each pinned by a mutant:** (a) a source's reveal may not release another source's hold (#109); (b) protection state clears only on a successful source response, never on reconfigure (#112); (c) reveal counts play no part in admission, cooldown, enable/disable, hold arming or release (#113's negative policy test). Are these consistent with each other, and with the verification-hold design you approved in August?
3. **Test-instrument claims.** Since #110's guard existed, the full suite has run six times on the Windows host (results 5449, 5452, 5458, 5462, 5469, 5508 passed); since the 400 stale buckets were deleted on 2026-09-04, four of those runs started and ended with the real trash root absent and the guard silent, and the integrated-stack run above makes five. Is there any path you can see by which a test still reaches a real drive root or shares the process coordinator?
4. **The side findings that were deliberately not fixed**, each recorded as an open decision for the owner: the health recorder counts stripped reveals as a source success (`backend/source_health.py`, `record_scrape_outcome`); no operator reset for a cooldown exists; the `or {}` config trap in the background scanner is unreachable today; the Windows socket aborts in `tests/test_dv_host_scan.py` (2 of those six full runs, 2026-09-03 and 09-04, a different test each time, each passing alone and with its file afterwards) remain FOUND and undiagnosed. Rank them, or confirm they can wait.
5. **Anything the per-PR reviews accepted that you would not accept for the stack as a whole.**

## 5. What the reviews already established, so you need not re-derive it

- Status vocabulary in every evidence file: FOUND, CODE FIXED, CI VERIFIED, HOST VERIFIED (combined-copy runs on the Windows host, stated as such), MERGED (none), INSTALLED (none), LIVE OBSERVED (none).
- Every fix commit names the mutants run on a whole-tree copy and which tests killed each; the supervisor's own runs found and fixed four vacuous tests along the way (a mtime channel that does not exist on Windows; a symptom-only assertion masked by the database read; a fail-soft test that never reached the handler; an id-based identity test that address reuse could fail), all recorded in the evidence.
- CI (ubuntu-latest, Python 3.11 and 3.12, frontend) is green at every head in the table.
- Retractions made this week, so you do not re-find them: the background-scanner "second path" for HDE-6; the "sixteen scrape-boundary files" (there are fourteen); the "eleven complete days at 20" (the table holds five).

## 6. What is not authorized

No merge, deployment, force-push, mark-ready, production setting change or enablement. The owner decides those. Merge order required by the stacking, if and when he does: `#109 → #113`, `#111 → #112`; the rest are independent of each other.

## 7. Process, for the record

Supervisor: the session model, which wrote every spec, verified every lane's claim at first hand, ran the mutants, and made the final calls. Implementation and read-only investigation: Sonnet lanes from written specs. Adversarial reads: Opus lanes. Owner decisions taken during the week and recorded in the report: delete the 400 stale trash buckets; refuse RSS primary until the canary exists; release a hold on returned links; HDE-6 before HDE-4 before HDE-5.
