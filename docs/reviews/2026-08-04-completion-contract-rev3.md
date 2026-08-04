> ## SUPERSEDED 2026-08-05 by `2026-08-05-completion-contract-rev3.1.md`
> Round-12 Q3: self-containment, exact SHAs, degrouped R-8..R-16, honest
> statuses. Retained as the rev-3 record.

# ScanHound — Completion Contract, rev 3

**Date:** 2026-08-04 (post-round-11) · **Author:** Claude · **Reviewer:** ChatGPT (narrow re-verdict
requested) · **Arbiter:** Jesse
**Supersedes:** rev 2 (`2026-08-04-completion-contract-rev2.md`). Round 11 accepted rev 2's five
contract-wide rules but required (a) EVERY item as a full row — Tracks 2/5/6 were prose — and
(b) statuses current to the reviewed head. Rev 3 does both. The five rules (anti-proxy evidence,
three-role separation, artifact binding, deferral schema, valid negative outcomes) carry over verbatim
and bind every row below.

Status legend: ✅ done+evidence · 🔨 built, verdict pending · ⬜ open · 🔒 Jesse.
Head at writing: post-`627bab6` (round-11 findings 1/2/4/5 fixed, Q5 policy narrowed, R-5 executed
suite landed, harness provenance check added).

## Track 1 — RSS promotion

| ID | Exit criterion | Evidence (executed) | Builds/Verifies/Approves | Status |
|---|---|---|---|---|
| R-1 | Authority end-to-end: upsert guard + every consumer follows the carried verdict | 17-field guard suite; results.py consumers fixed red-first (7/7 + 72/72); round-12 confirm + parameterize the survival test over ALL 17 fields (round-11 hardening ask, ⬜) | C / CG+suites / 🔒 | 🔨 fixes landed post-verdict |
| R-2a | Identity engineering + real-function corpus | closed BY round 11 | — | ✅ CLOSED |
| R-2b | Same functional corpus pass on the PINNED deployed build before the window | corpus JSON at the deployed digest | C / CG / 🔒 window gate | ⬜ blocked on deploy, BY DESIGN |
| R-3 | Seam unified; single year authority BY CONSTRUCTION; harness attested on rebased head | delegation + perturbation test (`3a51fce`); provenance-checked harness verify exit 0; rebased-head machine run ⬜ | C / harness+CG / 🔒 | 🔨 code complete; attestation pending rebase+CI |
| R-4 | Versioning per the ratified model incl. cache heal + no-unheal race property | 14/14 R-4 suite incl. end-to-end heal + deterministic no-unheal (`findings 1+2` commit); round-12 confirm | C / CG / 🔒 | 🔨 fixes landed post-verdict |
| R-5 | EXECUTED consumer-boundary suite over the round-11 matrix | `627bab6`: 20/20 — cross-path contract-field equivalence, results, auto-action, packaging, cache visibility; inventory `691468a` closed the enumeration subtask | C / CG / 🔒 | 🔨 landed post-verdict |
| R-6 | Gate wired; demotion RESTORES the safety net end-to-end, durably visible | behavioral scan_once test + mutation check; demoted cycles carry demoted_from + blockers | C / CG / 🔒 | 🔨 finding-1 fix landed post-verdict |
| R-7 | Formal recorded sign-off at gate closure (approved-in-principle ≠ final) | recorded sign-off against final semantics + artifact | — / — / 🔒 | ⬜ |
| R-8..R-11 | Rebase → merge → ONE pinned build → deploy that digest | merge SHA; imagetools digest; container == digest; no duplicated cherry-picks | C preps / CI / 🔒 ×4 | ⬜ blocked on rounds+billing |
| R-12..R-16 | Bootstrap → 7-day window → Phase A grade → Phase B → combined decision | window log, digest checks both ends, committed verdicts | C / CG / 🔒 | ⬜ |

## Track 2 — Rename safety (now full rows)

| ID | Exit criterion | Evidence (executed) | B/V/A | Status |
|---|---|---|---|---|
| N-1 | B1 historical-limitations note: no rate inference from pre-ledger rows | committed doc `eec7f2e` | C / CG / 🔒 | ✅ |
| N-2 | B3 old-fail/new-pass + fault injection runs in CI with a discrimination check | CI run URL green incl. mutation step | C / CI / 🔒 | ⬜ (CI billing-blocked) |
| N-3 | B5 capability probe: per-volume matrix (renameat2/hardlink/errno/collision/fsync), scratch-only, errno-detected | probe output committed with negative controls | C executes / CG / 🔒 authorises | ⬜ |
| N-4 | B5 copy-only rehearsal on real storage, hashes verified | rehearsal log + hash table | C / CG / 🔒 authorises | ⬜ |
| N-5 | B6 restart/reconciliation invariants under real container interruption, in the separate pinned rename-test container only | interruption test log | C / CG / 🔒 authorises | ⬜ |
| N-6 | B7 one sacrificial backed-up file: source/dest/hash/ledger/DB/Plex/restart all verified | run log, all seven checks | C / CG / 🔒 authorises | ⬜ |
| N-7 | Rollout-or-continued-freeze decision (both valid) | decision doc | — / — / 🔒 | ⬜ |

## Track 3 — Shutdown branches

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| S-1 | CI green on both heads | runs 30789418560 + 30789421160 | C / CI / 🔒 | ✅ |
| S-2 | Round-10 order: brattain = integration base, ONE combined merge | revised doc (`f6fb68d`) + CG confirm | C / CG / 🔒 | ✅ doc; execution ⬜ |
| S-3 | Nine-item gate closed on the COMBINED branch (deadline, fencing, admission, startup safety) | per-item executed evidence | C / CG / 🔒 deferrals | ⬜ |
| S-4 | 10 consecutive full-suite runs, zero flakes, ON THE MERGE SHA | manifest at merge SHA (provisional 10/10 at `6f22f3d` stands as method proof) | C / manifest / 🔒 | ⬜ re-run at merge |
| S-5 | `--threadleak-fail` + injected-leak self-test, pre-merge (not deferrable) | plugin + red/green self-test | C / CG / 🔒 | ⬜ |
| S-6 | desktop_notifications explicit opt-in migration (ratified) | migration + test | C / CG / 🔒 | ⬜ queued on integration line |

## Track 4 — Security

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| C-1 | Push/PR scan + weekly full-history; versions pinned; exact-fingerprint suppression (path allowlist retired); crafted-repo proof both directions at the CI-pinned version | probes in `c64e591`+`637889e` commit messages; comment splice repaired | C / probes+CG / 🔒 merge | 🔨 round-12 confirm |
| C-2 | True-positive procedure: correct mechanism, operator roles, preventive-controls tracking | doc + measured no-op note | C / CG / 🔒 | ✅ pending role-wording confirm |
| C-3 | CODEOWNERS/required-review on scanner files + required check | repo settings state | — / — / 🔒 | ⬜ Jesse-side |
| C-4 | Scope-gap declarations (LFS/submodules/binaries) + literal-secret handling notes | doc additions | C / CG / 🔒 | ⬜ |
| C-5 | Gotify token + stale gists: rotate/delete or deferral per schema (reason/risk/trigger) | rotation or recorded deferral | — / — / 🔒 | ⬜ deferred-by-decision, schema fields due at closeout |

## Track 5 — Product items (now full rows)

| ID | Exit criterion | Evidence (executed) | B/V/A | Status |
|---|---|---|---|---|
| D-1 | TV filter distinguishes 720p/1080p/4K on a labelled corpus | test + UI screenshot | C / CG / 🔒 accepts | ⬜ |
| D-2 | Full-disc UI toggle round-trips to the SAME shared rule both paths call | test proving one predicate object + screenshot | C / CG / 🔒 accepts | ⬜ (escape-hatch decision O-3 first) |
| D-3 | HDR10+ label in Plex + Kometa overlay renders | API query + screenshot | C / CG / 🔒 accepts | 🔨 detection wired; labels ⬜ |
| D-4 | Documentary design decision recorded | committed decision doc | C / CG / 🔒 accepts | ⬜ |
| D-5 | #192 spec corrected and verified | rev-5 supersession (`2919cfa`), fact-checked by round 10 | C / CG / 🔒 | ✅ |
| D-6 | Scan metrics: wired call sites + full-disc taxonomy slot + durable persistence + conservation proof + workflow coverage (round-10 hold) | wiring commits + tests vs injected failure/cancellation | C / CG / 🔒 merge | ⬜ rebased base ready (`ce3f10b`) |

## Track 6 — Ops (now full rows)

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| O-1 | MANIFEST.json mystery resolved; rollback unblocked | supervised elevated run's captured log + manifest on disk + verify pass | C preps / verify script / 🔒 runs | ⬜ wrapper delivered, awaiting Jesse |
| O-2 | Installer preflight-assert ordering fix reviewed + pinned + installed | own review round + fresh SHA + elevated re-run | C (`c57034e` staged) / CG / 🔒 | ⬜ review pending; wait for O-1 evidence |
| O-3 | Escape-hatch decision (keep-with-warning vs remove) | recorded decision | — / — / 🔒 | ⬜ |
| O-4 | BACKLOG/TL-032 + catch-up reflect verified reality | dated edits | C / — / — | ✅ maintained continuously |
| O-5 | GitHub billing restored; every pending head CI-attested; run URLs recorded in evidence docs | green runs with executed steps | 🔒 fixes / C reruns / CG | ⬜ |
| O-6 | Final rebase onto current main; cherry-pick dedupe verified; harness + full suite + CI at the rebased SHA; bindings updated | rebase commit + attestations | C / CI+CG / 🔒 merge follows | ⬜ next after round-12 |

## Programme completion
R-16 + N-7 decisions recorded; Tracks 2–6 every row dispositioned per the five rules; rollback and
operating docs match the deployed system. Do-not-promote / stay-frozen remain valid completions.
