# Completion contract — re-attestation after merging main (0a2751d)

**Date:** 2026-08-28 · **Author:** Claude · **Supersedes:** rev3.5
**Head:** `3f4d000` on `agent/hybrid-sweep-rebased` · **Base:** `main @ 0a2751d`, **0 behind**

## Why rev3.5's frozen head moved

rev3.5 froze the branch at `7a50443` pending a verifier. The branch then fell
59 commits behind while #59/#61/#84/#93 and the collision fix landed on main.
The owner ruled: re-attest the PR whole against current main, under a brief
merge freeze on the overlapping subsystems. Moving the head is his
instruction, not another self-inflicted staleness.

Two commits sit on top of `7a50443`:

- **`7fb6819`** — merge of `origin/main @ 0a2751d`. Three files conflicted
  (`backend/api/routes/scanner.py`, `backend/scanner_service.py`,
  `tests/test_scanner_service.py`); `backend/database.py` auto-merged. Every
  conflict was resolved as a UNION after enumerating what each side adds; the
  merge commit message documents each hunk, including the one place main's
  *lines* were not kept (`_match_against_plex`'s inline `is_tv`/`season`) and
  the verification that its *guarantees* survive under the tri-state selector.
- **`3f4d000`** — reconciliation of the semantic overlap the full suite
  surfaced (8 merge-adjacent failures, none visible in the textual
  conflicts):
  1. `_media_item_from_dict` now feeds main's recorded `is_tv=True` into the
     legacy-row resolver as DETAIL-authority TV evidence — without it, main's
     decided verdict was silently discarded and such rows were refused.
     **Shown to fail first**: 2/6 tests fail with the line stashed, 6/6 pass
     with it.
  2. `tests/test_scanner_carries_is_tv.py` now drives main's six guarantees
     through the production reconstruction path. **Five keep their meaning.
     One changed sides, visibly**: `test_neither_signal_means_movie` →
     `test_neither_signal_is_refused_not_guessed`. A zero-evidence item is
     refused (`MEDIA_TYPE_UNRESOLVED`), not matched against the film
     library. That is PR #94's reviewed round-13 fix; keeping main's
     assertion would pin the defect the PR removes. **This is the single
     behaviour change in the merge that the R-7 sign-off should know about.**
  3. `tests/test_active_listing_path_gap.py`'s source marker retargeted to
     the union's `_post` dict; same guarantee, now also pinning
     `all_posts.append(_post)`.

## What was RE-RUN today, at `3f4d000`

Environment: host Windows, Python 3.12.9, pytest 9.0.2 (same environment as
rev3.5's runs; the 2026-08-06 evidence was a clean-room container run).

| Row | Evidence | Result today | rev3.5 said |
|---|---|---|---|
| R-1 | `test_detail_hydration_composition.py` + `test_feed_upsert_authority.py` | **33 passed** (19 + 14) | 33 passed |
| R-3 | `scripts/r3_differential_harness.py` | **`old=c17152976 new=3f4d00039 cases=71 identical=40 differing=31` · every divergence matches the committed expected file · exit 0** | same counts at `7a50443` |
| R-4 | `test_completed_row_feed_authority.py` | **12 passed** | 12 passed |
| R-5 | `test_listing_membership_authority.py` | **14 passed** | 14 passed |
| R-1/R-6 | `tests/tools/mutation_check.py` | **10/10 DISCRIMINATE · 0 survived · exit 0** | 10/10 |
| bundle | `qualification/scripts/selftest.py` | **ALL SELFTESTS PASSED** | pass |
| bundle | `SHA256SUMS` | **0 mismatches** | 0 mismatches |

The worktree was verified clean (`git status` empty) after
`mutation_check.py` and before the full suite, so the suite ran against
exactly `3f4d000` — the 2026-08-06 evidence file records why that check
matters.

Full suite at `3f4d000`: **1 failed / 6066 passed / 5 skipped (17:59). The one failure, `test_dv_host_scan.py::test_post_rows_direct_success_delivers_key`, passes in isolation and with its whole file (37/37) — an ordering/concurrency flake, not a defect at this head. Other suites were running on this machine throughout, per the merge-freeze arrangement.**
Full suite at `main @ 0a2751d`, same machine, same interpreter, same session:
**1 failed / 5441 passed / 5 skipped (16:53). Its one failure, `test_api_routes.py::TestErrorContracts::test_export_csv_no_results_returns_400`, likewise passes in isolation on BOTH trees — the same flake class.**
Delta: **+625 passing over main, zero reproducible failures on either side.** (The first full run at the bare merge commit `7fb6819` failed 8 — six `test_scanner_carries_is_tv`, one marker test, one ordering flake — which is what `3f4d000` reconciles; that run is why the reconciliation commit exists.)

## What is INHERITED from the original attestation (NOT re-run)

- **R-3 reference corpus and expected-divergence file** — the 71-case corpus
  and `r3-expected-divergences.json` are committed definitions; today's run
  re-executed the harness against them but did not re-derive them.
- **Live measurements** — `2026-08-05-per-row-authority-live-measurement.txt`
  and the shadow-evidence collections were taken against the live DB on their
  stated dates. Nothing live was touched today.
- **The review-round history** (rounds 1–16, I1 disposition, Q2 fixes) —
  positions taken then stand; nothing was re-argued.
- **R-2b, reason-code enum, grab-time resolver measurement** — still open,
  exactly as rev3.5 states.
- **I1 (guard precedence)** — still deliberately UNRESOLVED, status quo
  shipped, recorded in `tests/test_round11_guard_precedence.py`.

Merged-is-not-deployed still applies: nothing here says anything about the
running container.

## The commitment, renewed

No further commits to this branch until the R-7 sign-off. If anything must
change, this document is superseded and says so.
