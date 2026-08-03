# ScanHound — Completion Contract, rev 2

**Date:** 2026-08-04 · **Author:** Claude · **Reviewer:** ChatGPT (round-10 Q7 re-verdict requested) · **Arbiter:** Jesse
**Supersedes:** `2026-08-03-completion-contract.md` (rev 1 + appended amendments). Rev 2 exists because
round 10 correctly ruled that appended amendments are not an amended contract — the loophole fixes are
now IN the tables.

## Contract-wide rules (these bind every row)

1. **Anti-proxy:** the Evidence column names EXECUTED BEHAVIOUR — a test run, a
   harness verdict, a measured effect. A committed document, a source-grep, an
   xfail count, or an exit code is never sufficient evidence on its own.
2. **Three roles per row, never conflated:** *Builds* (does the work),
   *Verifies* (produces/attests the evidence — never solely the builder),
   *Approves* (accepts the row as done — 🔒 = Jesse only).
3. **Artifact binding:** evidence cites exact commits/digests/run URLs. A
   number without its SHA is a rumour.
4. **Deferral schema:** any DEFERRED row must record (a) reason, (b) residual
   risk in plain language, (c) the trigger that revisits it. A deferral
   missing any of the three is not a valid disposition.
5. Documented "do-not-promote" / "stay-frozen" outcomes complete their tracks.

## Track 1 — RSS-primary promotion

| ID | Exit criterion | Required evidence (executed) | Builds / Verifies / Approves | Status 2026-08-04 |
|---|---|---|---|---|
| R-1 | Media type resolved by authority end-to-end, and no lower-authority write path can revert it | round-9 closure + the Q3 clobber fix suite (real entries, real DB: detail facts survive changed polls; pre-hydration control) | Claude / ChatGPT + hermetic suites / 🔒 | fix landed `2638b24`; round-11 confirm pending |
| R-2a | Identity engineering: shared module, delegation, frontier bound truthfully, corpus measured BY THE REAL FUNCTIONS | contract tests 11/11; sweep binding tests; functional corpus pass (fixed-points 0/0/0, named-bridge joins) at a cited snapshot sha; refuter pass | Claude / ChatGPT round-11 / 🔒 | evidence complete: `f0ed051 ee9567f 3c54d9a 355e164`, snapshots 69fb7c2c…+662202ab… |
| R-2b | Post-deploy identity verification: the SAME functional corpus pass re-run against the PINNED deployed build's bootstrap corpus, before the window opens | corpus JSON from the deployed digest, denominators reconciled to R-2a's | Claude / ChatGPT verdict / 🔒 Jesse (window gate) | blocked on deploy (R-9..R-11) — BY DESIGN |
| R-3 | Detail-parser seam unified with every behavioural divergence declared-or-fixed | hermetic harness verify-exit-0 at declared SHAs (committed corpus + expected file); behavioural pins 6/6 + item-3 tests 7/7; full suite green at the SHA | Claude / harness + ChatGPT round-11 / 🔒 | rework + item-3 + ratified decisions landed through `be01638`+; round-11 confirm pending |
| R-4 | Derived-state versioning per the round-10 model: candidates offline-reparse; details/cache explicit stale/refetch or visible permanent-provisional; version columns not blob-only; stale rows visible to qualification | old-fail/new-pass tests incl. a version-bump simulation; concurrency test on the cache write path | Claude / ChatGPT / 🔒 | design ratified by round 10; build after R-5 inventory |
| R-5 | Consumer-boundary inventory + decision-equivalence suite over the contract fields | executed cross-path tests (real RSS + real listing entry points), one per consumer boundary | Claude / ChatGPT / 🔒 | inventory workflow queued |
| R-6 | Gate wired at LISTING-DEMOTION admission and EVERY autonomous side effect (auto-grab AND the real rename admission sites `rename/service.py:634`, `api/main.py:384`); RSS polling NEVER gated; invalid-gate-after-promotion demotes to listing/shadow + alerts, never silently skips | wiring tests: flags-on-without-pass still denied; drift-after-promotion restores listing behaviour | Claude / ChatGPT / 🔒 | boundaries ratified by round 10; build after R-4 |
| R-7 | T1/T2a/T2b + Phase A/B contract formally signed | recorded sign-off at gate closure | 🔒 | approved-in-principle 2026-08-03 |
| R-8..R-11 | Rebase onto current main → merge → ONE pinned build (digest recorded) → deploy exactly that digest | merge SHA; `docker buildx imagetools inspect` digest; container Image == digest | Claude preps rebase / CI / 🔒 all four | blocked on rounds + billing |
| R-12..R-16 | Bootstrap 30 h → 7-day window (integrity: same digest both ends) → Phase A grade → Phase B → combined decision | window log with digest checks; grader outputs; committed verdicts | Claude collects / ChatGPT verdicts / 🔒 opens, locks, decides | not started |

## Track 2 — Rename safety (unchanged route; evidence rules now bind)

N-1 ✅ note committed (`eec7f2e`). N-2 B3-in-CI · N-3/N-4 B5 probe+rehearsal (🔒 authorises) · N-5 B6
interruption (🔒) · N-6 B7 sacrificial file (🔒) · N-7 decision (🔒). Every N-row's evidence must be the
run log with negative controls, per rule 1.

## Track 3 — Shutdown branches (round-10 Q4 order is binding)

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| S-1 | CI green on both branch heads | runs `30789418560` (ac463c7) + `30789421160` (2f8c898) — the GREEN runs, not the superseded billing-failed ones | Claude / CI / 🔒 | ✅ DONE |
| S-2 | Reconciliation per round 10: hybrid merges first; brattain becomes the INTEGRATION BASE (no standalone merge while the three lifecycle P0s are open); meitner rebases onto it; ONE combined shutdown branch merges after the combined gate | updated reconciliation doc + ChatGPT confirm | Claude / ChatGPT / 🔒 order | doc update in flight |
| S-3 | The nine-item merge gate closed on the COMBINED branch (incl. app-wide deadline, generation fencing, admission closure, startup safety) | per-item executed evidence | Claude / ChatGPT / 🔒 deferrals | open |
| S-4 | Flake demonstrated on what merges: 10 consecutive full-suite runs, zero occurrences | committed manifest at the merge SHA (current evidence: 10/10 at `6f22f3d`, `eec7f2e` — re-run at merge) | Claude / manifest / 🔒 | provisional ✅ |
| S-5 | `--threadleak-fail` enforcement + injected-leak self-test, REQUIRED before the combined merge (round-10: not deferrable past it) | plugin commit + red/green self-test | Claude / ChatGPT / 🔒 | open |
| S-6 | desktop_notifications explicit opt-in migration (ratified 2026-08-04): one-time off for existing installs | migration + test | Claude / ChatGPT / 🔒 | ratified, queued on integration line |

## Track 4 — Security (round-10 Q6a additions integrated)

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| C-1 | Scan on every push/PR + weekly full-history baseline; versions pinned (action SHA + scanner version); exact-finding suppression PREFERRED (`.gitleaksignore`) with the path allowlist narrowed or retired; checkout action pinned; crafted-repo test using the REAL CI command; no literal secrets in any evidence; scope gaps declared (LFS/submodules/binaries) | probe report: FP precision AND TP recall, both directions, at the pinned versions | Claude / probe + ChatGPT / 🔒 merge | hardening round 1 done (`c64e591`); round-2 items open |
| C-2 | True-positive procedure with correct mechanism + operator roles (owner rotates; agent preps) + preventive controls tracked | doc + the measured .gitleaksignore no-op note | Claude / ChatGPT / 🔒 | ✅ landed in `c64e591`; role wording confirm in round 11 |
| C-3 | Workflow self-protection: CODEOWNERS/required-review on scanner files + required check | repo settings screenshot/state | 🔒 Jesse (settings) | waiting on Jesse |
| C-4 | Gotify token + stale gists decisions | rotation or recorded deferral (schema rule 4) | 🔒 | deferred-by-decision; schema fields TBD at closeout |

## Track 5 — Product items

D-1 filter ⬜ · D-2 full-disc UI ⬜ · D-3 HDR10+ 🔨 half · D-4 documentary ⬜ · D-5 #192 ✅ (`2919cfa`) ·
D-6 scan metrics: **held for wiring per round-10 Q6b** — merging requires real call sites, the full-disc
taxonomy slot, durable persistence, conservation proof, and workflow coverage; the rebased branch
(`ce3f10b`, patch-ids verified) is the base for that work. Acceptance columns per rule 1 (executed
demonstrations, screenshots for UI, API queries for labels).

## Track 6 — Ops

O-1 mount MANIFEST mystery: wrapper delivered, 🔒 Jesse runs elevated · O-2 preflight-assert proposal
staged (`c57034e`, own review round + fresh pin required) · O-3 escape hatch 🔒 · O-4/O-5 ✅ ·
O-6 GitHub billing 🔒 (blocks all CI attestation) · O-7 final rebase onto current main before merge
(round-10: patch-identical cherry-picks do not remove the need).

## Programme completion

Unchanged from rev 1: R-16 + N-7 decisions recorded, Tracks 3–6 dispositioned per the rules above,
rollback/operating docs match the deployed system.
