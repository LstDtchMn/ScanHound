# ScanHound — Completion Contract, rev 3.2 (SELF-CONTAINED, EXACTLY BOUND)

**Date:** 2026-08-06 · **Author:** Claude · **Reviewer:** ChatGPT (round-14) · **Arbiter:** Jesse
**Supersedes:** rev 3.1 (`2026-08-05-completion-contract-rev3.1.md`, committed at `32d90e097e1ebd573a1a21bab3b5d8db3b9de212`).
Round-13 contract defects fixed here: every binding is an exact, full-precision reference on the
branch of record (no ellipsized hashes, no "-series", no "post-X commit"); the suite evidence row
cites a COMMITTED artifact, not a relay; R-1/R-5 statuses reflect the production-path corrections
this revision accompanies.
**Branch of record:** `agent/hybrid-sweep-rebased`. **Code head bound by this revision:** `70939859e620e8398ba95b6eb53c33abef1c308d`
(the round-13 closure series, the audit fix, and the round-14 R-4 binding). Base `main@7adb17bd8661633cade99b4ed7ca98bab3a9c8eb`;
**106 commits ahead, 0 behind** — re-counted with `git rev-list --count` at this head, superseding the
stale 96 in the first draft of this revision.

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
| R-1 | Authority end-to-end: upsert guard, every consumer follows the carried verdict, all 17 protected fields proven at BOTH layers (sink preservation AND production emission or explicit retraction) | guard fix `3916ea7d3ed3cf2cd02996515700de024d71701b`; results consumers + ambiguous bookmark `3089f6fbf7ecb7beb620b4dd0f8db590edc019cd`; 17-field sink parameterization `3089f6fbf7ecb7beb620b4dd0f8db590edc019cd`; round-13 closure: episode_end + hevc production producers `89d4fb60bdcb82b1be630498faedd0e9632aa1f8`, title_year retraction pinned + layer separation `b4707b766c788652a3c19e16ad55e63d39a0910b`; emission suite tests/test_detail_hydration_composition.py 28/28 at `b4707b766c788652a3c19e16ad55e63d39a0910b`; adapter mutations discriminate (harness run: 9/9 corrected→PASS defective→FAIL, exit 0); audit fix `0bd1d525d5ab28cf5f7a4718feb6bfbe5c5e7320` — hydration no longer wrote the absent-year sentinel 0 over a real feed year (found by the program audit in this same adapter; the round-13 emission suite had not asserted description_year on the TV path), mutation case added | C / CG round-14 / 🔒 | 🔨 |
| R-2a | Identity engineering + real-function corpus | CLOSED round 11. Rebased bindings: `7170348ffa6eb51ef49c32c605321ea5d905a41c` (shared identity module), `9e029ffea220c68af40e379889717c6ec926256e` (frontier binding), `5363bcaccfecbe7272d6445236a0702260e4bf4a` (corpus measurement + policy), `43da8aa4d7bfb5958fe26d2be02b5b7f4326081b` (actual-canonicalizer pass). Snapshots, full: `69fb7c2cbbcfd904233b043fd3b7e2897046d74996260556ec813015d89d7c06` (structural), `662202ab1aa191bece978dbb2de9b6395cb8cdbe57a68406d81d4a80d2aa0d08` (functional) | C / CG round-11 / 🔒 | ✅ |
| R-2b | Same functional corpus pass on the PINNED deployed build before the window | corpus JSON at the deployed digest | C / CG / 🔒 window gate | ⬜ post-deploy BY DESIGN |
| R-3 | Seam unified; single year authority by construction; harness attested at the bound head | delegation `39d61e39d0f8652efabf77a9497e1bfd6708a776`; consumers-follow-verdict + harness provenance check `e44fca3848aa323e2171fb381261b2b0f96c7fda`; dimension policy narrowed `a097a0aa8feed9c3bde05575818871c58b62562a`; HEVC vocabulary joined the shared grammar `0148f22fe8584c52708fcc79cd0c6eefe1f7f455` (constant only — no parse output changed, verified empirically); harness verified at the `7d062cc5216a7881fed775a1866746ce98f74a77` tree: old=`c1715297621128baa1253f857d929d88b854d0b7` new=`0148f22fe8584c52708fcc79cd0c6eefe1f7f455`, 71 cases / 40 identical / 31 divergences, every one matching the committed expected file, exit 0; re-baseline `7d062cc5216a7881fed775a1866746ce98f74a77` (glued-range fix RESTORES old episode_number; only the round-11 episodes-count divergence remains — reviewed, recorded); CI machine run ⬜ (billing) | C / harness+CG / 🔒 | 🔨 |
| R-4 | Versioning per the ratified model, with the detail/grammar dependency ENFORCED: grammar stamps `f7e271dacaede58f48dd5a686a22c1c616f86373`, consequences `3b510b82fa3c60ffb3981a82f92b67c33bb58870`, offline reparse `23d9d177e9ac5a5bbfd75dc85f225cbbb0d0b142`, cache heal + real two-handle race `12c6c618b0e377be016ae6e711e5dd151bf4fad6`+`3089f6fbf7ecb7beb620b4dd0f8db590edc019cd`; round-14: DETAIL_PARSE_VERSION is COMPOSITE (capability+GRAMMAR_VERSION) so either authority moving invalidates completed rows automatically — **accepted round-15**, bound to `70939859e620e8398ba95b6eb53c33abef1c308d`. **The round-14 feed-repair claim was REFUTED by round-15 and is withdrawn:** `_FEED_ONLY_ON_COMPLETED = ("title_year",)` assumed a field is detail-authoritative whenever the adapter *can* emit it, but `_candidate_updates` omits any field a given payload lacks and the sink COALESCEs, so authority is per ROW — nearly every protected field can remain feed-owned on an individual completed row, and the pass re-derived ONE field while stamping the whole feed leg current. Reproduced against the code before fixing. round-15 replacement `eabcf9205ee49cbf2c831d43b19ddd5035ad4b98`: the sink records which protected fields THIS payload supplied (`detail_authority_fields`), and the repair re-derives every field that row's detail did NOT claim. Three guards, each pinned: a NULL/undecodable claim set repairs NOTHING (unknown is not empty — treating it as empty would overwrite detail facts it cannot see); coupled groups (media-type triple, size text/gb, HDR evidence/formats, season/episode/episode_end) move together or not at all; and a row whose detail owns every protected field is NOT stamped, because nothing was re-derived | R-4 suite + round-14 tests at `70939859e620e8398ba95b6eb53c33abef1c308d`; round-15 sparse-detail discrimination suite at `eabcf9205ee49cbf2c831d43b19ddd5035ad4b98` — 7 tests in `tests/test_completed_row_feed_authority.py`, built on the SPARSE detail payload the review specified precisely because the existing rich-detail fixture cannot expose the defect (nearly every field in it becomes detail-owned): feed-owned fields heal on a grammar change; detail-owned fields are untouched; coupled members cannot diverge; a row owning nothing feed-side is not stamped; unknown claim set repairs nothing; claiming one group member claims the group; the sink records exactly what the payload supplied | C / CG round-15 / 🔒 | 🔨 |
| R-5 | EXECUTED final-consumer suite; the listing composition is ONE production function | part 1 `976380b556dd608953229678f2ca5a7d85e9b4bf` 20/20; part 2 `c48246c4253bbf82a991216ca050f9f0ca557da2` 12/12 (found+fixed the inert provisional gate, same commit); round-13 closure `5b7220e60f06a1fecc97785e2444f413e6111272`: `resolve_listing_media_type` extracted, called by `_process_posts` AND the rescan route (which previously skipped the composition entirely — every rescanned item silently defaulted 'ambiguous'; fixed + route-tested), cross-path suite executes THE function against real parse_feed verdicts incl. the DETAIL-override case on both real paths; composition mutations (drop DETAIL / drop TITLE) discriminate in the committed harness | C / CG round-14 / 🔒 | 🔨 |
| R-6 | Gate wired; demotion restores the safety net end-to-end, durably visible; mutation case committed | wiring `393dbd6d2426963bceea87a216a78a1075c53a9a`; F1 fix `12c6c618b0e377be016ae6e711e5dd151bf4fad6`; F1 mutation case committed `ee0cab7d40bd532f6cc5e4c747161315ea18684b`; full harness discrimination run at the `7d062cc5216a7881fed775a1866746ce98f74a77` tree: 9/9, exit 0 | C / CG round-14 / 🔒 | 🔨 |
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
| N-1 | B1 limitations note (no rate inference from pre-ledger rows) | `b015754430f7e981cb400a48d24a85c9aafd530f` | C / CG / 🔒 | ✅ |
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
| S-2 | Brattain = integration base; ONE combined merge (round-10 order) | revised doc `f6fb68dfb758ea89bea3606301fccfc9de287b11` | C / CG / 🔒 | ✅ doc; execution ⬜ |
| S-3 | Nine-item gate closed on the combined branch | per-item executed evidence | C / CG / 🔒 deferrals | ⬜ |
| S-4 | 10×full-suite zero-flake ON THE MERGE SHA | manifest at merge SHA (method proven 10/10 at `6f22f3d7fd25b9b48e452b58cfb1709b340c9633`) | C / manifest / 🔒 | ⬜ |
| S-5 | threadleak-fail + injected-leak self-test pre-merge | plugin + red/green | C / CG / 🔒 | ⬜ |
| S-6 | notifications opt-in migration (ratified) | migration + test | C / CG / 🔒 | ⬜ |

## Track 4 — Security

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| C-1 | Pinned scanning + exact-fingerprint suppression + weekly baseline + crafted-repo proof both directions | `c64e591db2dae54ba85519c04778cdbc3aacbf38` + `637889e38d5f1708c967ffb94f79a9067d3d1c3a` + comment repair `5d417b7c71a98a77b42cff549b255cbc16b33194` (all on `agent/security-track-c`, head `5d417b7c71a98a77b42cff549b255cbc16b33194`) | C / probes+CG / 🔒 merge | 🔨 |
| C-2 | Procedure: correct mechanism + operator roles + preventive tracking | `c64e591db2dae54ba85519c04778cdbc3aacbf38`; role wording UNCONFIRMED | C / CG / 🔒 | 🔨 built/pending |
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
| D-5 | #192 corrected and verified | `2919cfa0622c33c7b58bd75162e17348dfa6a377`, fact-checked round 10 | C / CG / 🔒 | ✅ |
| D-6 | Scan metrics wired + taxonomy slot + persistence + conservation + CI | wiring commits + injected failure/cancellation tests | C / CG / 🔒 merge | ⬜ base `ce3f10bb3c99f60bb0a801221b6edd191b635319` ready |
| D-7 | Category switch always consults the server cache; live mode is a scan-time overlay, not a lock; scan streams unaffected; empty selection crosses the API as the named sentinel; scan activity follows the backend-observed lifecycle | separate branch `agent/category-switch-cache-fix` @ `b5dd04b497a497fff03bbf6e27ae575edcf39803` (round-2 fixes) + `3190b0dce5af6256f53553160883ab3906bf462b` (relay): red-first suites both rounds (3-fail then 6-fail discrimination), vitest 402/0, svelte-check 0 errors, build 0, backend sentinel contract + empty-means-all mutation kill; ChatGPT round-2 verdict pending | C / CG + vitest / 🔒 | 🔨 |

## Track 6 — Ops

| ID | Exit criterion | Evidence | B/V/A | Status |
|---|---|---|---|---|
| O-1 | MANIFEST mystery resolved; rollback unblocked | captured elevated run + manifest on disk + verify pass | C preps / script / 🔒 runs | ⬜ wrapper ready |
| O-2 | Preflight-assert fix reviewed + pinned + installed | review round + fresh SHA + elevated run | C staged `c57034ec47a8990c2a510d3d2e9f16b421774361` / CG / 🔒 | ⬜ after O-1 |
| O-3 | Escape-hatch decision | recorded decision | — / — / 🔒 | ⬜ |
| O-4 | Continuity docs reflect verified reality | dated edits | C | ✅ ongoing |
| O-5 | Billing restored; all pending heads CI-attested with executed steps | green run URLs in evidence | 🔒 / C / CG | ⬜ |
| O-6 | Rebase done; dedupe verified; harness + full suite at the bound head with COMMITTED artifacts | rebase: **106 commits ahead of `main@7adb17b`, 0 behind** (re-counted at this head; the 96 in the first draft was stale); harness verify exit 0 at this tree (71 cases / 40 identical / 31 divergences, all matching the committed expected file); mutation harness 10/10 corrected→PASS defective→FAIL exit 0; full-suite output COMMITTED at `docs/reviews/evidence/2026-08-06-full-suite-7093985.txt` (code head attested `70939859e620e8398ba95b6eb53c33abef1c308d`; artifact blob `68dc01f6d9449d7d83442dc6f50d71708cc6e072`, added by `717f759aaa95b7874dc791a820d68ccbb4d62bf2` — the filename abbreviates the SHA, so both bindings are given here rather than inferred from it) — **4793 passed / 0 failed / 4 skipped / exit 0**, run CLEAN-ROOM (an earlier attempt overlapped the mutation harness, which rewrites source, and was discarded rather than cited); CI leg ⬜ (billing) | C / harness+CI / 🔒 merge follows | ✅ rebase+local attestation; CI leg ⬜ |

## Programme completion
R-16 + N-7 recorded; every row above dispositioned under the five rules; rollback/operating docs match
the deployed system.
