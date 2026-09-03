# Round 7 — a complete review of five weeks, and five fixes that came out of it

**Base:** `main` @ `0a2751d`
**Package:** `docs/reviews/peer-rounds/2026-09-03-round7-package/` on branch `review/2026-09-02-complete-review` — the review document (`00-complete-review.md`), and in `patches/` a full diff against `main` for every branch named below. Nothing in this package is merged or deployed.
**Previous:** Round 6 (design), APPROVE DIRECTION / REQUEST CHANGES. Your six design findings are acknowledged and not yet implemented; the V6/V7 bridge you asked for landed on #94 and was verified (3/12 → 0/12, closure 37/77 → 0/77).

---

## What this round is

Not a PR. The owner asked for a complete review of everything since 2026-08-01: 410 commits, 43 merged PRs, three open PRs, and the branches from the last two days. Nine review lanes, each running its area's tests and attacking the claims that would hurt most if false, each re-executed by an adversarial verifier. The document is the deliverable; the five fixes are what the owner asked to have done immediately.

## What we want from you

1. **The report's severity ordering (section 1).** You have not seen this code run either, but you have the evidence per finding. Is anything rated too high, too low, or missing from the top?
2. **The five fixes, as patches.** Each is a separate branch from `main` with its own verifier report in the PR. The one we are least sure of is **#105** (stall-report hold scope): it narrows `human_required` as a side effect, and its verifier found an unexercised belt-half in `_refresh_batch_locked`. Say whether that narrowing is correct or a second defect.
3. **#103 (undo).** The fix excludes the trash entry undo just made from the overwrite-restore search by its exact trash path. The alternative was scoping the restore to an entry recorded on the job at apply time. We chose the exclusion because it cannot regress when two entries share an `original_path`; argue against it if you can.
4. **The resilience change (#102 + the task half of #101), reviewed as new code.** Six defects found and fixed the same day (section 2). Two are still open by design: "verified" is identity, not writability; and refused TV jobs are not retried on their own when the share returns. Are either of those acceptable to ship?
5. **Section 3, claims that did not survive.** Ten of them, in our own documents and PR titles. Which of these should change how we write the next ones?

## What we are NOT asking

Not asking whether to merge, deploy, or enable anything; those are the owner's. Not asking you to re-derive the round-6 design; that is queued behind this. Not asking about the Gotify token; the owner has decided to leave it until ScanHound is back.

## Evidence boundary

You can read every branch, every PR and its CI. You cannot execute the Windows/PowerShell suites, the Docker fixture, or anything against the stopped container. The document says, per finding, what was executed and by whom. Two of the nine lanes ran on a cheaper model and one of those returned placeholder text on its first attempt (section 7); it was re-run on a stronger model and its verifier's report is included.

No merge, deployment, permission change or enablement is authorized by this review.
