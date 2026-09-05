# Round 7 package — 2026-09-03

Self-contained. Everything a reviewer needs is in this directory; nothing depends on a link that can go stale.

| file | what it is |
|---|---|
| `00-complete-review.md` | the review of everything since 2026-08-01: nine lanes, each verified; findings ordered by cost to the owner; claims that did not survive; what survived; the five fixes; open PRs; lessons |
| `01-request.md` | what we are asking this round, and what we are not |
| `provenance.txt` | every branch this package covers: name, head SHA, commit date, PR number |
| `patches/r7-undo-restores-its-own-trash.patch` | PR #103 — full diff against `main` |
| `patches/r7-checker-three-queue-keys.patch` | PR #104 |
| `patches/r7-stall-report-hold-scope.patch` | PR #105 |
| `patches/r7-dv-scan-abort-names-the-real-cause.patch` | PR #106 |
| `patches/r7-checker-delivery-failure-visible.patch` | PR #107 |
| `patches/share-identity-guard.patch` | PR #102 — the app-side write guard, including the six round-7 fixes |
| `patches/101-recovery-task-degraded-mode.patch` | the recovery-task half of PR #101: the script, its harness and its pin, against `main` |
| `patches/r7b-refuse-blind-rss-primary.patch` | PR #108 (round 7b/7c) |
| `patches/r7b-one-reveal-one-source-observation.patch` | PR #109 (round 7b/7c) |
| `patches/tst1-suite-trash-isolation.patch` | PR #110 (round 8) — the suite never writes into a real volume trash root |
| `01b-round8-request.md` | what round 8 asks |
| `02-tst1-evidence.md` | TST-1: pre-fix host state, guard shown to fail, suites |
| `patches/tst2-queue-order-dependence.patch` | PR #111 — TST-2: every test owns its own HDEncode coordinator |
| `03-tst2-evidence.md` | TST-2: reproduction, bisection, the leaked state with proof, mutant, suites |
| `patches/hde6-coordinator-context-reset.patch` | PR #112 (stacked on #111) — HDE-6, full diff against `main` |
| `patches/hde6-only-against-111.patch` | the same PR's own diff, against #111's branch |
| `04-hde6-evidence.md` | HDE-6: investigation with file:line, change, tests, mutants incl. the masking finding, adversarial read, suites |
| `patches/hde4-reveal-accounting.patch` | PR #113 (stacked on #109) — HDE-4 reveal accounting, full diff against `main` |
| `patches/hde4-only-against-109.patch` | the same PR's own diff, against #109's branch |
| `05-hde4-evidence.md` | HDE-4: investigation with file:line, what was built, tests, mutants, adversarial read, suites |
| `patches/hde5-only-against-113.patch` | PR #114 (stacked on #113) — HDE-5 docstrings, docs-only diff against #113's branch |
| `06-hde5-evidence.md` | HDE-5: the stale claims replaced (each with the contradicting code line), the docs-only proof, the truth-check |

**How to read it.** Start with section 1 of the review (the table). Every row names the lane that found it, the verifier's re-execution, and its status. The patches are complete diffs, not excerpts: apply any of them to `main` @ `0a2751d` and run the tests the PR names.

**Evidence boundary.** The container was stopped for the whole window; the Docker deploy suite was not run by any review lane. Each finding says what was executed.

No merge, deployment, permission change or enablement is authorized by this package.
| `07-week-review-request-2026-09-05.md` | the whole-week review request (2026-08-31 to 09-05): every branch with full SHA, the integrated-stack merge rehearsal and full-suite result, the questions only a stack review can answer |
