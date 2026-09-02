# Scripts round 3 — the deploy script has now been executed

**Branches:** `ops/deploy-and-permission-scripts`, `fix/kometa-design-not-config` (PR #99)
**Previous review:** 2026-08-26, verdict REQUEST CHANGES, three HIGH findings
(OPS-1 reopened, OPS-2 reopened, SR2-1 new), two MEDIUM, one LOW

Every finding was verified against the real code before being touched. **I
refuted none.** Two of them I can now show with measurements rather than
argument, and one of them was live on this machine while you were writing the
review.

---

## Read this first: the asymmetry you named is closed

| | round 2 | round 3 |
|---|---|---|
| deploy engine sections executed | 0 of 7 | **all of them** |
| deploy engine tests | 0 | **12 real-Docker cases, 12 passing** |
| deploy defects proven to FAIL when reintroduced | 0 | **9 of 9 mutants killed** |

No mocks. Nothing replaces `docker.exe`. Every case builds real images and
creates real containers in a disposable Compose project with its own name,
container, image tag and localhost port. The `scanhound` container is never
touched.

## The part that matters most: executing it found two defects reading it did not

Both are exactly the class this review sequence keeps producing, and neither
survived first contact with a real container.

**1. The port assertion was keyed by the wrong port.**
`.NetworkSettings.Ports` is keyed by CONTAINER port. The engine built its key
from the HOST port. ScanHound publishes `127.0.0.1:9721->9721`, so the two
numbers are equal and the check reads correct in production *forever*. The
fixture publishes host→8080, and the first green run reported

```
OK   /health status=ok
PROBLEM  127.0.0.1:49993 is NOT bound
```

A healthy service, declared unbound. In production this would have passed
every time while asserting nothing at all — the exact shape of the grep-for-a-
string identity check you killed in round 1.

**2. My own test harness made CASE A pass vacuously.**
PowerShell variable names are case-INSENSITIVE. The test helper
`Check([string]$name, ...)` shadowed the script-scope `$NAME` — the fixture
container — for the whole body of every case. Every deploy ran against a
container named after the test case. `CASE A` (build failure must not replace
the running container) passed because *both sides of its comparison were
equally null*. It is now `$CaseName` / `$FXNAME`, with a tripwire that refuses
to deploy if any identity stops matching the fixture prefix.

I am reporting the second one because it is the more useful of the two: the
suite you asked for was itself capable of the failure mode it exists to
prevent, and only running it under a changing fixture exposed that.

## OPS-1 was live on this machine

You argued that rejecting every `??` line would still be insufficient because
git-ignored files never appear in `git status` at all. Measured here, 2026-08-26,
at the moment the review arrived:

| in the build context | size | `git status --porcelain` shows it? | `.dockerignore` excludes it? |
|---|---|---|---|
| `frontend/src-tauri/gen` | **1.7 GB** | no — ignored | no |
| `frontend/src-tauri/target` | large | no — ignored | no |
| `chrome.exe` (repo root) | 4.0 MB | yes, as `??` — and round 2 filtered it out | no |
| `backend/sweep` | 73 KB | no — ignored | no |

`COPY frontend/ ./frontend/` and `COPY backend/ ./backend/` both take them.
So `HEAD == T` did not imply `build context == T`, and `expected_sha` in the
round-2 ledger was a false provenance claim. Your recommended fix is what I
built. Verified against the real repository:

```
HEAD in clean worktree : 65e75ef9dfe0d1410658f487ee5ce42b122734db
target                 : 65e75ef9dfe0d1410658f487ee5ce42b122734db
clean worktree status  : empty
frontend\src-tauri\gen         primary=True   clean-worktree=False
frontend\src-tauri\target      primary=True   clean-worktree=False
chrome.exe                     primary=True   clean-worktree=False
backend\sweep                  primary=True   clean-worktree=False
worktree removed       : True
```

Repo on `X:`, worktree on `C:`; cross-volume works and cleans up in a `finally`.

## What each finding became

| ID | Response |
|---|---|
| **OPS-1** | Confirmed and measured (above). Build is now `docker build` from a disposable `git worktree --detach` at the target SHA, whose own status must be empty *including* untracked. The primary checkout is never moved, which also answers question 3. |
| **OPS-1 dry-run** | Confirmed. `-WhatIf` no longer reports `HEAD`; the target is resolved identically in both modes because resolving touches nothing. `-WhatIf` does create and remove a worktree, so the drift gate is genuinely exercised by a dry run. |
| **SR2-1** | Confirmed — the round-2 check ran in §1 against the pre-merge checkout. Now compared against the **target** recipe twice: once as soon as the source exists (before a ten-minute build), and again immediately before activation. |
| **OPS-2** | Confirmed, including the `mount-nas-shares.ps1` reachability path. The build now writes `scanhound:candidate-<sha12>`, never `scanhound:latest`; an explicit assertion fails the run if the recovery tag moved during the build; activation uses a Compose override; promotion happens only after VERIFIED, under `Global\ScanHound-MountNASShares` — the same mutex the recovery task holds. |
| **OPS-5** | Confirmed — the round-2 ledger was populated-before-the-error, not observed-after-it. `Observe-CurrentContainerState` cannot throw and cannot change state, runs from a real `finally`, and reports every field as a value or `UNKNOWN`. |
| **OPS-3 / OPS-4** | Kept as-is; you closed them. OPS-4's port key was nonetheless wrong (above). |
| **testability** | `scripts/deploy-core.ps1` is the engine and takes a config object; `scripts/merge-and-deploy.ps1` is a thin production wrapper holding the real identities. This is your §17 architecture. |

## Answers to your four questions

**1. Does the source SHA need to be in the image?** You said not necessarily,
provided the build context is truly tied to the Git tree — and that the current
chain was not yet sound. Agreed, and that was the actual defect. I fixed the
context rather than adding a label. The chain is now
`clean worktree at T (status empty) -> docker build -> exact image ID ->
running container image == that ID`. I did **not** add durable post-hoc
provenance, so "which SHA produced `sha256:...`?" is still unanswerable after
the terminal closes. Called out below as an open gap rather than pretended
closed.

**2. Build succeeds, activate fails.** You were right that the state is fine
and the *naming* was not. The candidate is now quarantined under its own tag,
so the desired end state is what the fixture actually produces and asserts.

**3. `checkout --detach`.** Removed entirely. The operator's branch is never
touched.

**4. The BOM mutation.** You confirmed the reasoning, and the same principle
bit me again here from the other side. On the first mutation pass, removing the
build-exit-code check did *not* make CASE A fail: the
candidate-does-not-exist guard stops the deploy anyway, so the container is
still not replaced and the outcome CASE A asserted held. Defence in depth made
the case look load bearing when it was not. The fix was to make CASE A assert
the refusal *reason* rather than only the outcome — which is the same lesson as
the BOM mutant, just approached from the opposite direction: there, a validation
gate moved which invariant kills the mutant; here, redundant guards meant no
invariant did. Both say the same thing — mutation adequacy is a property of what
the suite pins, not of what a case is named.

---

## The cases, and the proof they are load bearing

Commit `a70ce6a`. `tests/test_deploy_core_docker.ps1` builds a disposable Git
repo with a bare origin, a tiny HTTP service, its own Compose project and a
pinned recovery copy. `tests/mutate_deploy_core.py` then reintroduces each
reviewed defect and requires the case written for it to FAIL.

```
CONTROL  unmutated                                     exit=0  failures=0

OPS-1: build from the mutable primary worktree ....... KILLED by CASE D
OPS-2: promote the recovery tag on build success ..... KILLED by CASE B
SR2-1: drop the post-resolution drift check .......... KILLED by CASE E
artifact identity: accept any running image .......... KILLED by CASE C
OPS-5: no observation after destructive work ......... KILLED by CASE F
OPS-4: key the port assertion by HOST port ........... KILLED by SEED
OPS-2: take an unshared mutex name ................... KILLED by CASE G
OPS-4: accept any /health answer ..................... KILLED by CASE I
build transport: ignore the build exit code .......... KILLED by CASE A

VERDICT: every reviewed defect is caught by the case written for it
```

Your cases A-F are all there. G, H and I come from your "optional next layer":
mutex contention, exact port, health status. I promoted the mutex one out of
"optional" because the mutex is the mechanism this round introduces, and an
untested lock is not a lock -- CASE G takes the real mutex name from the test
process and requires the engine to refuse.

### The mutant that survived, and what it taught

On the first pass, `build transport: ignore the build exit code` **SURVIVED**.
CASE A asserted only that the container was not replaced -- which stays true
when the exit code is ignored, because the candidate-does-not-exist guard stops
the deploy anyway. The property is defended in depth, so the *outcome* held
while the case's name claimed a *mechanism* it did not pin.

That is your section 13 finding -- a test name claiming more than it proves --
occurring inside the suite written to answer your section 13 finding. CASE A now
asserts the refusal reason, and the mutant dies. I am reporting the survivor
rather than only the corrected table, because the first result is the more
useful one.

## Answers to your remaining structural asks

**Section 16, real Docker fixtures.** Agreed and done. Nothing replaces
`docker.exe`. The fixture uses `python:3.12-slim` (already local, no network)
and a stdlib HTTP server, so a case costs seconds rather than the real
ten-minute build.

**Section 17, testability injection.** `scripts/deploy-core.ps1` holds the
engine and takes a config object; `scripts/merge-and-deploy.ps1` is a thin
production wrapper holding the real identities. `Invoke-DeployCore` returns a
result and never exits, so a test can drive it -- and
`tests/deploy-fixture-runner.ps1` runs it in a child process anyway, because
"exits nonzero" is the operator-facing contract and a verdict string can be
correct while that contract is not.

**Your OPS-2 practical sequence.** Implemented as written, with one addition: on
a non-VERIFIED outcome the wrapper prints the exact recovery-style command that
rolls back, rather than leaving the operator to wait for
`ScanHound-MountNASShares` to do it at an unpredictable moment.

## What this still does not prove

**No durable provenance.** After the terminal closes, "which SHA produced
`sha256:...`?" is unanswerable. The chain is sound *within* a run and
undocumented after it. Your section 5 offered two routes; I took neither,
deliberately, because both belong in a commit allowed to touch the Dockerfile or
add a deployment record, and this one was not.

**The log window is still a window.** It observes three minutes of volume. A
window with no stuck batch reads zero whether or not the fix works. The causal
property belongs in a unit fixture that deliberately creates one, and that
fixture does not exist.

**Neither script has been run against production.** Merging and deploying are
Jesse's alone.

## Where I want to be argued with

**1. Is the reconcile step right?** After promotion the engine runs
`up -d --no-build` with the *plain* target recipe, because the activation
override changed the service's image NAME and the container therefore carries a
Compose config hash the pinned recipe does not reproduce. Left alone, the
deployed container and the recovery recipe would disagree about their own
identity even though the image content is identical. The cost is that Compose
may recreate once more, after verification. I re-check artifact identity
afterwards but do NOT re-run the three-minute log window. Right trade, or should
the candidate be activated some other way entirely?

**2. The mutex is held across the runtime checks.** Roughly three and a half
minutes during which `ScanHound-MountNASShares` (Boot + Logon + 288x/day) cannot
recreate. It uses `WaitOne(0)` and skips the run, retrying five minutes later,
so the cost is a deferred mount recovery. The build is deliberately *outside*
the lock, since it writes only the candidate tag. Right split?

**3. `-WhatIf` now creates and removes a worktree.** A real filesystem side
effect in a dry run. I chose it so the drift gate is genuinely exercised rather
than described. Reasonable, or should a dry run touch nothing at all?

**4. CASE A's defence in depth.** Two independent guards produce the same
outcome. I made the case pin the specific one. An alternative reading is that
the case *should* assert the outcome, and that the mutant was correctly
survived. I do not think so, but it is a real choice and I would like it
challenged.


---

# The other two findings, and why they took longer than the deploy script

OPS-7, SR2-3 and SR2-2 are closed, but not in one round each, and the pattern is
the most useful thing I can hand you from this session.

## The permission script: four rounds, and the fix kept reintroducing its own defect class

| round | what closed | what the next verifier found |
|---|---|---|
| `7d01549` | OPS-7 commit primitive, SR2-3 exit codes | handler claimed "settings.json is UNCHANGED" without measuring it |
| `9569159` | measured failure reporting, ReadOnly refusal, missing `allow` key | an access error reported as a finding about CONTENT |
| `0b1f1fd` | tri-state readability, failure-mode axis | branch coverage detected only SELF-DECLARED branches |
| `6f37e66` | AST arm walk, random-line sampling | four branch shapes still survived |
| `14f9637` | closure by prohibition | a fifth shape: `Set-Variable verdict` |
| `3ffac8d` | that shape closed; LIMITS corrected | -- |

Three separate times the fix closed a defect class one layer up and reopened it
one layer down. Round 3's handler was written to stop asserting unmeasured
state, and asserted unmeasured state. Round 5's branch guard was written to stop
depending on self-description, and depended on self-description. Round 6's LIMITS
section was written to stop the file overstating its reach, and overstated its
reach.

### Two findings from that sequence you should have regardless of the review

**`Move-Item` carried the temp file's ACL onto `settings.json`.** Not in your
review, found while implementing it. The old commit moved the candidate ONTO the
destination, so the surviving file inherited the sibling temp file's ACL and any
explicit ACE on `settings.json` was silently discarded -- by the script whose
entire job is managing an authorization file. Measured both ways: after
`File.Replace` the marker ACE survives, after `Move-Item -Force` it does not.
Creation time is preserved by both (NTFS tunnelling), so it is not a
discriminator; the ACL is.

**A privilege escalation was invisible to the suite.** Nothing asserted that a
PLAIN grant does NOT add the deploy rules. Mutating that one line to always
grant them left the suite green. Worth noting how the measurement moved: it was
23 passed / 0 failed when first measured, and by the time it was re-measured the
suite had grown a fresh-user case that killed it INCIDENTALLY, on an unrelated
axis. Both numbers were honest at the time they were taken. The named assertion
was added anyway, because an accidental kill is not coverage.

### Where the permission script's guarantee now stops

The branch-coverage check is bounded and has been defeated five times. Every
extension of it is a record of a defeat, not a claim of completeness -- and the
test module now says exactly that. It does not recognise a verdict reached
through `Invoke-Expression`, a `[ref]` handle, a closure, a dot-sourced file, or
a function the handler calls. Those are named as uncaught rather than defended
against, deliberately.

## SR2-2: the runbook was the least dangerous surface carrying the defect

Your finding was about the 4K runbook. Fixing it turned up the same stale
vocabulary on four surfaces that matter more:

* **the caption under the "Sync Plex labels" button** -- the last thing an
  operator reads before a destructive click -- said "Only these four labels are
  managed, your own labels are never touched". Both halves false: the badges are
  `DV8`/`DV5`, and `MANAGED` is nine. It understated what the sync can strip by
  five labels, the dangerous direction.
* **`scripts/stage_fel_write.py`** built its ROLLBACK SNAPSHOT from the same
  hardcoded four-label list, so Gate 4's promise that the operation "can be
  reversed exactly" was false -- the snapshot silently omitted five labels while
  still reporting success.
* **`scripts/blast_radius2.py`**, whose only job is measuring what that button
  will touch, under-measured it.
* **`backend/config.py`** described the hourly unattended sync as
  "ADDITIVE-ONLY -- it never removes a managed label". Executed: a matched
  authoritative title returns `removed=['DV FEL','DV7']` and issues two real
  `pm.remove_label` calls. And `app_service.py` dropped `result["removed"]` from
  the hourly log, so the only trace of an unattended destructive write never
  mentioned the destruction.

All now derive from `dv_labeler.MANAGED` rather than restating it.

### Where I disagree with the review

You asked for a test that fails if any live doc reintroduces `DV P8` or `DV P5`.
Taken literally that is the wrong invariant and would make the runbook newly
misleading in the other direction. Those names are IN `MANAGED` via
`RETIRED_LABELS` precisely so the sync REMOVES them; they will appear in the dry
run's removals. An operator told they no longer exist reads a working migration
as a fault. The invariant implemented is that a retired name never appears as
CURRENT vocabulary, and the runbook now names them in their own labelled
"retiring" block and tells the operator to expect them.

### And where the vocabulary guard stops

After three rounds of a reviewer defeating it with new wordings, we stopped
widening it. It catches written-out retired names, prefix-factored slash-runs,
and explicit wrong counts adjacent to "label(s)". It does NOT catch an
understating ENUMERATION with no count word -- which is the shape the original
defect actually took. That, and six other known-uncaught shapes, are listed in a
LIMITS section and each is pinned by an executable test so the list cannot rot.

A guard that overstates its reach is worse than a narrow one that is honest
about it. That is the through-line of this whole sequence, and it is why the
guard now says what it cannot do.

---

# The thing I most want your view on

**The deploy engine converged in one round. The two prose-shaped loops took four
each and were still producing defects when we stopped.**

Same reviewer, same rigour, same standard of evidence. The difference is that
the deploy engine's failure states can be EXECUTED -- build fails, activation
fails, wrong image runs, recipe drifts, mutex contended -- so a fixture either
reproduces the failure or it does not, and a mutation either dies or it does
not. Nine of nine reviewed defects were shown to fail the case written for them,
and the one that survived taught us something real about defence in depth.

The permission handler's branch coverage and the vocabulary guard are both
assertions about TEXT. Nothing executes them. Every round produced a plausible
guard, and every round an adversarial reader found a shape it missed -- five
times and three times respectively. The guards are better now, but the honest
description of their state is "bounded, defeated N times, limits documented",
not "closed".

If that generalises, the rule is: **spend the qualification budget where a
machine can run the failure, and where it cannot, buy an honest statement of
limits instead of a better detector.** We stopped both text loops on that
reasoning rather than on fatigue. I would like to know whether you think that is
the right call, or whether there is a way to make a prose invariant executable
that we did not find.


---

# Consolidated evidence

Everything below was re-measured on the final tree rather than carried forward
from the round it was produced in.

## Deploy engine -- `ops/deploy-and-permission-scripts`

```
tests/test_deploy_core_docker.ps1        12 passed, 0 failed
tests/mutate_deploy_core.py              9 mutants, 9 KILLED, control clean
                                         before and after, script restored
```

Real Docker throughout: a disposable Git repo with a bare origin, a tiny stdlib
HTTP service, its own Compose project, container, image tag and localhost port.
`python:3.12-slim`, already local, no network. Nothing replaces `docker.exe`.

## Permission script

```
tests/test_claude_permissions_script.ps1  57 passed, 0 failed
tests/mutate_claude_permissions.py        29 declared mutants
                                          KILL=23  PROBE_KILL=2
                                          PROBE_SURVIVE=3  SURVIVE_BY_DESIGN=1
                                          "every declared mutant behaved as
                                          expected", exit 0
RANDOM-LINE KILL RATE                     6 of 12  (seed 20260826)
```

That last number is the important one and it is published rather than tuned. It
samples executable lines that NO mutant in the file names, mutates each by line
number, and reports what happens. Six of twelve is the honest state of the suite
outside the lines your findings pointed at. Three of the declared mutants are
PROBE_SURVIVE -- gaps we decided to declare rather than close, each with the
reason written beside it.

## Kometa / SR2-2

Committed on `sr2-2-work`, which branches from PR #99's head.

```
tests/test_dv_label_vocabulary.py + tests/test_dv_hdr10_label.py
                                          71 passed
scoping proof (backend/config.py)
  CONTROL                                 15 passed
  qualification deleted, phrases parked
  in a comment at end of file             1 failed, 14 passed
  restored                                sha256 identical, 15 passed
```

## Repository state

`ops/deploy-and-permission-scripts` was 8 commits behind `main` and carried one
inherited failure -- `test_config.py::test_default_config_has_no_unexpected_keys`,
which `main` had already fixed in `f004143`. Merged; zero file overlap between
the two sides, no conflicts, and that test is now 103 passed.

## What has NOT been done

Neither script has been run against production. `scanhound` has been up
throughout and was never touched: 26+ hours uptime, `/health` `status: ok`.

Merging, deploying, changing permissions and enabling anything remain Jesse's
alone. Nothing in this package authorizes any of them.
