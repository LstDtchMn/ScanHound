# Round 5 — both blockers closed, and a question about whether one of these models is wrong

**Base:** `main` @ `0a2751d`
**PRs:** #101 `f634a17` (ready) · #94 `d04ab63` (draft) · #100 `8c51df0` (draft)
**Previous:** Round 4, REQUEST CHANGES — R4-101-1 (HIGH, production blocker) and
R4-94-1 (HIGH, union loss). Both closed. Neither closed cleanly on the first try.

You have not seen any of the eight commits since the heads you reviewed.

---

# The question I actually want answered

Ask this before the correctness pass, because it may change what the correctness
pass should look for.

**#94 is now four fixes deep, and every single fix contained a defect the next
round found:**

```
R4-94-1  carry the cached verdict into media_type
             -> the route then laundered its OWN verdict back in as DETAIL
                evidence; a provisional verdict cleared itself on a second
                rescan that observed nothing
R4-94-2  stop the laundering
             -> conflict suppression became ORDER-DEPENDENT; a conflict
                recorded after a rescan did not suppress, one recorded before
                did
R4-94-3  make suppression order-independent
             -> a rescan wrote category_attested=False onto rows where the key
                was ABSENT, permanently withdrawing them from the only writer
                that can ever attest them
R4-94-4  make the third state representable
             -> ?
```

Each fix was verified. Each verification was adversarial and found the *next*
defect, not the one it was checking. That is either a model converging, or a
model that is wrong.

The recurring shape: **`category_attested`, `is_tv`, `media_type`,
`media_type_provisional` and `category_conflict` are five fields carrying
overlapping authority about one question — what kind of thing is this release,
and who says so.** Every defect in the chain has been one of those fields being
written by something that did not observe it, then read by something that
treated it as observation.

So: is the right move to keep fixing, or to collapse those five into one
explicit provenance-carrying value? I cannot get a useful answer from my own
verifiers — they share my assumptions about the model. You do not.

If your answer is "the model is wrong", say so plainly and I will stop patching
it.

---

# PR #101 — the deploy path

## R4-101-1, your production blocker: promotion is now a transaction

You said `scanhound:latest` was promoted before the final plain-recipe container
was qualified, and not restored on failure — so the recovery namespace stopped
meaning "last verified image", and the playbook's rollback claims were false.

Correct, and I had written a comment *arguing against* demoting the tag. That
comment is gone; your counter is in its place.

The transaction is now the shape you specified: `old_latest = recovery_tag_before`
→ candidate qualifies → tag moves *provisionally* → plain-recipe final activation
→ pass commits it, fail restores the prior tag **while the recovery mutex is
still held** → observer reports what actually serves.

Verified independently, at the real consumer rather than structurally:
`mount-nas-shares.ps1` takes the same mutex with `WaitOne(0)` from line 247 to
1025, so it cannot recreate inside the window; the pinned compose names
`scanhound:latest`, so a reverted tag makes the recovery recreate a genuine
rollback.

**Your required regression exists and passes:** old latest = A, candidate = B,
candidate checks pass, latest temporarily becomes B, final reconcile fails,
result != VERIFIED, latest == A again — then the recovery recipe is executed and
asserted to restore A.

## What my own verifier then found in that fix

**R4-101-2 (HIGH):** `Test-StorageFailureObserved` classified a *consequence* as
a storage failure. Promotion requires zero problems and zero unknowns at the
candidate phase, and the host proof is a `Stop-Deploy` gate — so on **every**
post-promotion failure, host and candidate read `probed / 0` while the dead
final container makes `nas_final_reason='not-running'`. The wrapper printed
*"DO NOT RECREATE YET. A STORAGE proof failed"* two lines below printing the
proof that the sources were fine, and demoted the genuine one-command rollback
to step two — on the most likely NOT-VERIFIED shape, the one the runbook is
written for.

Fixed, then driven exhaustively: **54,000 cases** (6 promotion states × 10 reason
shapes per phase × liveness flags) and **1,440 rollback cases**. No alarm whose
stated reason contradicts the proofs beside it; no missed alarm; never both
alarm and plain-rollback text. 10 oracles shown to fail.

**Two MEDIUMs, both mine:**

*A suite whose verdict moved with the checkout.* `core.autocrlf=true`, so
`git worktree add` checks the tests out as CRLF — and .NET's `(?m)$` matches
before `\n` but not `\r\n`. Measured:

```
my main checkout   entirely LF     -> 34 passed, 0 failed
fresh worktree     entirely CRLF   -> 33 passed, 1 failed
```

So every "34/0" I reported for that case was true of one tree and false of a
clean clone or a CI runner. Fixed and re-run **in the CRLF worktree**, where it
had failed: 34/0.

*A stale alarm.* The promotion journal was never cleared when the `docker tag`
itself failed — `Require-Native` throws before `promoted` is set, and the revert
returned early on exactly that. Every later run would have reported an
interrupted promotion that never happened.

## Your SR3-7 reopen, and the playbook

`PLAN ONLY nothing was changed` → `PLAN ONLY - no production state changed.`

And the playbook no longer claims the run redeploys "the commit already
running" — you were right that the repo cannot prove that, and that #101's own
merge advances `main`. It now says: intended to redeploy current main with no
intentional runtime-feature change, and tells the operator to verify the SHA and
whatever provenance actually exists.

It also stopped saying "last verified image". What is restored is
`recovery_tag_before` — and today's `scanhound:latest` was built by hand, outside
this path, with no record of what proved it. **Prior image, not verified image.**

---

# PR #94 — the four-fix chain

**R4-94-1** carries the cached verdict into `media_type`. Verified through the
real HTTP route in both directions.

**R4-94-2** stops the route's own verdict re-entering as observation. A
131-case fixpoint sweep over all 128 seed shapes passes here and fails at the
parent.

**R4-94-3** makes conflict suppression order-independent. The verifier's own
3-position ordering matrix (288 shapes × 3 arms, 2,592 real route steps):

```
                          before this fix    after
order-dependent cells            20            0
attestation losses              432            0
```

**R4-94-4** (this round) — a rescan must not decide the attestation state of a
row that never had one. `category_attested` is three states and the third lives
in the **key**: absent means never checked, and `attest_scan_categories` skips
any row where the key is present, because it is a one-time backfill.
`_media_item_to_dict` dumps every field, so a rescan persisted `False` and
permanently withdrew the row from the only writer that can reach it:

```
control (no rescan): key absent -> attest=1, get_scan_category='tv'
after ONE rescan:    key False  -> attest=0, get_scan_category=None
```

Two tests from R4-94-3 had **pinned that as correct**. One asserted
`_persisted()["category_attested"] is False` directly beneath a docstring saying
"a rescan observes nothing … so it must not create the flag either". Writing
`False` *is* creating it. Both corrected; the replacement asserts the harm is
gone — after a rescan the row must still be **attestable**.

Full suite: **6139 passed, 0 failed, 5 skipped.**

---

# PR #100 — desktop retirement

Your two README corrections are in: "hourly, additive-only" now states the real
reconciliation semantics, and "delivery verification" is qualified for
Click'n'Load's deliberate `delivered_unconfirmed`. No code blocker was found in
your skim and none has been introduced.

---

# Evidence boundary

You can read all three PRs and their CI. #101 and #100 are green; #94's checks
were still running when this was written.

You **cannot** execute the Windows/PowerShell suites, the Docker fixture, the
live-NAS differential, or the 54,000-case wrapper drive. Those are
author-reported, measured on the owner's host. Say which conclusions depend on
them.

**A caution about author-reported numbers, learned this round.** The #94 fix
lane reported 6126 passed / 0 failed. On this host the same tree gave
**17 failed** — 15 of which passed in isolation and were ordering flakes, and 2
of which were real. Same code, different answer, twice in one session. Treat any
single green number in this document as environment-shaped unless it says it was
re-measured here.

The deploy script has still **never run against production**.

No merge, deployment, permission change, or enablement is authorized by this
review. Those remain Jesse's alone.
