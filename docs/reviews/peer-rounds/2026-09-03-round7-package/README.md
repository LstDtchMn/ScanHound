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

**How to read it.** Start with section 1 of the review (the table). Every row names the lane that found it, the verifier's re-execution, and its status. The patches are complete diffs, not excerpts: apply any of them to `main` @ `0a2751d` and run the tests the PR names.

**Evidence boundary.** The container was stopped for the whole window; the Docker deploy suite was not run by any review lane. Each finding says what was executed.

No merge, deployment, permission change or enablement is authorized by this package.
