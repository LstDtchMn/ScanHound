# ScanHound — Completion Contract, rev 3.1 (SELF-CONTAINED)

**Date:** 2026-08-05 · **Author:** Claude · **Reviewer:** ChatGPT (round-13 narrow re-verdict) ·
**Arbiter:** Jesse
**Supersedes:** rev 3 (`2026-08-04-completion-contract-rev3.md`). Round-12 Q3 defects fixed here:
the governing rules are INCLUDED (self-containment), every artifact binding is an exact SHA on the
REBASED branch, R-8..R-16 are individual rows, and statuses are honest to the reviewed head.
**Branch of record:** `agent/hybrid-sweep-rebased` (90+ commits rebased onto `main@7adb17b`, zero
conflicts, cherry-pick dedupe verified). The pre-rebase branch is retained read-only.

## The five governing rules (verbatim, binding every row)

1. **Anti-proxy:** the Evidence column names EXECUTED BEHAVIOUR — a test run, a harness verdict, a
   measured effect. A committed document, a source-grep, an xfail count, or an exit code is never
   sufficient evidence on its own.
2. **Three roles per row, never conflated:** *Builds* (does the work), *Verifies* (produces/attests
   the evidence — never solely the builder), *Approves* (accepts the row as done — 🔒 = Jesse only).
3. **Artifact binding:** evidence cites exact commits/digests/run URLs. A number without its SHA is
   a rumour.
4. **Deferral schema:** any DEFERRED row must record (a) reason, (b) residual risk in plain language,
   (c) the trigger that revisits it. A deferral missing any of the three is not a valid disposition.
5. Documented "do-not-promote" / "stay-frozen" outcomes complete their tracks.

Status legend: ✅ done+evidence · 🔨 built, verdict pending · ⬜ open · 🔒 Jesse.

## Track 1 — RSS promotion

| ID | Exit criterion | Evidence (executed, exact) | B/V/A | Status |
|---|---|---|---|---|
| R-1 | Authority end-to-end: upsert guard, every consumer follows the carried verdict, all 17 fields proven | guard fix `2638b24`(pre-rebase)/rebased-in; results consumers + ambiguous bookmark `3089f6f`; 17-field parameterization `3089f6f` — which exposed and fixed two hydration drops (episode_end, hevc_evidence, same commit) | C / CG round-13 / 🔒 | 🔨 |
| R-2a | Identity engineering + real-function corpus | CLOSED by round 11 (`f0ed051 ee9567f 3c54d9a 355e164`, snapshots 69fb7c2c…/662202ab…) | — | ✅ |
| R-2b | Same functional corpus pass on the PINNED deployed build before the window | corpus JSON at the deployed digest | C / CG / 🔒 window gate | ⬜ post-deploy BY DESIGN |
| R-3 | Seam unified; single year authority by construction; harness attested at the rebased head | delegation+perturbation `3a51fce`(rebased `12c6c61`-series); provenance-checked harness verify exit 0 at `c48246c` (one policy-narrowing divergence consciously re-baselined with an honesty note); CI machine run ⬜ (billing) | C / harness+CG / 🔒 | 🔨 |
| R-4 | Versioning per the ratified model: stamps `f7e271d`, consequences `3b510b8`, offline reparse `23d9d17`, cache heal + REAL two-handle both-orders race `12c6c61`+`3089f6f` | 14/14 R-4 suite at `3089f6f` | C / CG round-13 / 🔒 | 🔨 |
| R-5 | EXECUTED final-consumer suite | part 1 `627bab6`(rebased `976380b`) 20/20 (commit msg says 21/21 — the message is wrong, the relay's 20 is right); part 2 `c48246c` 12/12 at the real consumers — which exposed and fixed the production-inert provisional gate (same commit) | C / CG round-13 / 🔒 | 🔨 |
| R-6 | Gate wired; demotion restores the safety net end-to-end, durably visible; mutation case in the committed harness | wiring `393dbd6`; F1 fix `12c6c61`; committed mutation case + full harness discrimination run (post-`c48246c` commit) | C / CG round-13 / 🔒 | 🔨 |
| R-7 | FORMAL recorded sign-off at gate closure (approved-in-principle ≠ final) | recorded sign-off vs the final rebased artifact | — / — / 🔒 | ⬜ |
| R-8 | Merge exactly the reviewed rebased commit | merge SHA on main | C preps / CI / 🔒 | ⬜ |
| R-9 | ONE pinned build; digest recorded via `docker buildx imagetools inspect` | digest string in the window log | 🔒 builds / C records / 🔒 | ⬜ |
| R-10 | Diagnostic-only rollback DB copy, labelled NOT ADMISSIBLE | file + checksum | C / — / 🔒 | ⬜ |
| R-11 | Deploy exactly that digest; running container Image == digest | docker inspect equality | 🔒 / C verifies / 🔒 | ⬜ |
| R-12 | Bootstrap ~30 h: three auto-flags false; readiness cross-check succeeds in production; per-source bootstrap complete | captured log lines | C / CG / 🔒 | ⬜ |
| R-13 | 7-day window; same digest at open and close; config unchanged | window log with digest checks both ends | C collects / CG / 🔒 opens+locks | ⬜ |
| R-14 | Phase A graded against the predeclared thresholds | grader output committed | C / CG / 🔒 | ⬜ |
| R-15 | Phase B per its own predeclared plan on its own pinned artifact; T2b recorded | Phase B report committed | C / CG / 🔒 | ⬜ |
| R-16 | Combined promotion decision recorded (promote and do-not-promote both valid) | decision doc | — / — / 🔒 | ⬜ |

## Track 2 — Rename safety

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| N-1 | B1 limitations note (no rate inference from pre-ledger rows) | `eec7f2e` | C / CG / 🔒 | ✅ |
| N-2 | B3 old-fail/new-pass + fault injection in CI with discrimination | green CI run URL incl. mutation step | C / CI / 🔒 | ⬜ (billing) |
| N-3 | B5 capability probe, scratch-only, errno-detected | probe output + negative controls | C / CG / 🔒 authorises | ⬜ |
| N-4 | B5 copy-only rehearsal on real storage, hashes verified | rehearsal log + hash table | C / CG / 🔒 authorises | ⬜ |
| N-5 | B6 interruption invariants, separate pinned rename-test container only | test log | C / CG / 🔒 authorises | ⬜ |
| N-6 | B7 sacrificial file, all seven checks | run log | C / CG / 🔒 authorises | ⬜ |
| N-7 | Rollout-or-freeze decision | decision doc | — / — / 🔒 | ⬜ |

## Track 3 — Shutdown branches

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| S-1 | CI green on both heads | runs 30789418560 + 30789421160 | C / CI / 🔒 | ✅ |
| S-2 | Brattain = integration base; ONE combined merge (round-10 order) | revised doc `f6fb68d` | C / CG / 🔒 | ✅ doc; execution ⬜ |
| S-3 | Nine-item gate closed on the combined branch | per-item executed evidence | C / CG / 🔒 deferrals | ⬜ |
| S-4 | 10×full-suite zero-flake ON THE MERGE SHA | manifest at merge SHA (method proven 10/10 at `6f22f3d`) | C / manifest / 🔒 | ⬜ |
| S-5 | threadleak-fail + injected-leak self-test pre-merge | plugin + red/green | C / CG / 🔒 | ⬜ |
| S-6 | notifications opt-in migration (ratified) | migration + test | C / CG / 🔒 | ⬜ |

## Track 4 — Security

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| C-1 | Pinned scanning + exact-fingerprint suppression + weekly baseline + crafted-repo proof both directions | `c64e591`+`637889e`+comment repair | C / probes+CG / 🔒 merge | 🔨 |
| C-2 | Procedure: correct mechanism + operator roles + preventive tracking | `c64e591`; role wording UNCONFIRMED | C / CG / 🔒 | 🔨 built/pending |
| C-3 | CODEOWNERS/required-check on scanner files | repo settings state | — / — / 🔒 | ⬜ Jesse-side |
| C-4 | Scope gaps (LFS/submodules/binaries) + literal-secret notes | doc additions | C / CG / 🔒 | ⬜ |
| C-5 | Gotify token + gists: act or defer per rule 4 | rotation or schema-complete deferral | — / — / 🔒 | ⬜ |

## Track 5 — Product

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| D-1 | Filter distinguishes 720p/1080p/4K on a labelled corpus | test + screenshot | C / CG / 🔒 | ⬜ |
| D-2 | Full-disc UI round-trips to the ONE shared rule | one-predicate test + screenshot | C / CG / 🔒 | ⬜ (O-3 first) |
| D-3 | HDR10+ label in Plex + Kometa overlay renders | API query + screenshot | C / CG / 🔒 | 🔨 detection only |
| D-4 | Documentary design decision recorded | decision doc | C / CG / 🔒 | ⬜ |
| D-5 | #192 corrected and verified | `2919cfa`, fact-checked round 10 | C / CG / 🔒 | ✅ |
| D-6 | Scan metrics wired + taxonomy slot + persistence + conservation + CI | wiring commits + injected failure/cancellation tests | C / CG / 🔒 merge | ⬜ base `ce3f10b` ready |

## Track 6 — Ops

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| O-1 | MANIFEST mystery resolved; rollback unblocked | captured elevated run + manifest on disk + verify pass | C preps / script / 🔒 runs | ⬜ wrapper ready |
| O-2 | Preflight-assert fix reviewed + pinned + installed | review round + fresh SHA + elevated run | C staged `c57034e` / CG / 🔒 | ⬜ after O-1 |
| O-3 | Escape-hatch decision | recorded decision | — / — / 🔒 | ⬜ |
| O-4 | Continuity docs reflect verified reality | dated edits | C | ✅ ongoing |
| O-5 | Billing restored; all pending heads CI-attested with executed steps | green run URLs in evidence | 🔒 / C / CG | ⬜ |
| O-6 | Rebase done; dedupe verified; harness+suite at rebased SHA | `agent/hybrid-sweep-rebased`: 90 commits, 0 conflicts, 0 duplicated patches; harness verify 0 at `c48246c`+re-baseline; full suite at head (result in round-13 relay) | C / harness+CI / 🔒 merge follows | ✅ rebase itself; CI leg ⬜ |

## Programme completion
R-16 + N-7 recorded; every row above dispositioned under the five rules; rollback/operating docs match
the deployed system.
