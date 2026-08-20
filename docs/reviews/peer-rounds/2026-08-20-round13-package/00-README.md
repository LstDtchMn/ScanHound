# Round 13 review package — attestation authority (round-12 remediation)

**Self-contained.** Everything needed to review this round is in this directory.
You should not have to fetch code from the branch — and on 2026-08-16 a round was
lost because the connector reached the right SHA and returned no file contents,
so the full diff travels with the package rather than being linked.

## Identity

```text
repository    LstDtchMn/ScanHound
branch        fix/round12-attestation-authority
code head     64815c5     the last commit touching backend/ or tests/
branch head   see 04-provenance.md    later commits are documentation only
base          main @ 6ac5cd2, 0 behind
working tree  clean
deployed      NOTHING. The running container predates all media-kind work.
```

## Contents

| File | What it is |
|---|---|
| `01-request.md` | **Start here.** The round-13 request: what was found, what was fixed, what is deliberately NOT fixed, and the question I want answered. |
| `02-code-changes.patch` | The complete diff of `backend/` and `tests/` against `main`. `git diff origin/main...HEAD`. |
| `03-evidence.md` | Every command run, with results: reproduction-before-fix, suite figures against a like-for-like `main` control, and mutation results in both directions. |
| `04-provenance.md` | Exact SHAs, file hashes, and how each figure was produced — including a figure I got wrong first and why. |

## What this round is about, in three sentences

Round 12 found that `category_attested=True` was granted without evidence
sufficient to prove "checked clean", which is a false-positive **destructive**
authorization rather than a feature outage. It is fixed, along with two related
defects and one that neither the review nor I had named. The part I could **not**
close is stated plainly in `01-request.md` rather than papered over: my gate
earns the type-coverage half of the claim and does not earn the depth half.

## If you only read one thing

The section in `01-request.md` titled **"NOT DONE, stated rather than implied"**.
The fix deliberately leaves the media-kind capability switched off — no caller
passes `attest_coverage=True` — and the question of what coverage model would
actually justify turning it back on is the decision I am asking you to make.
