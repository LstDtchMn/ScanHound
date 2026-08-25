# Round 28 — provenance

Re-measured 2026-08-24 for this package. Nothing carried forward.

---

## 1. The running container — unchanged, and still not running any of this

```
container : scanhound      image: scanhound:latest
started   : 2026-08-23T12:21:58Z

/app/backend/arms.py  ->  ls: cannot access: No such file or directory
```

**None of rounds 19–28 is deployed.** The listing-claims / arm-registry feature
has never been merged to `main`, so the deployed image has no writer for it. The
"frozen ledger" carried undiagnosed through rounds 24 and 25 was diagnosed in the
round-26 package §3 and is **not an incident** — see that file for the evidence
and positive controls.

The consequence that matters for reviewing this round: **there is no production
positive control for any of it.** The quarantine interlock in particular has run
only in test containers. Its real test is a restart on the actual host, which no
review round substitutes for.

## 2. Git

```
origin/main                                 3c3369d
b8433f1   <- the head you reviewed
origin/fix/round12-attestation-authority    f4feaae   <- this head, PUSHED
local HEAD                                  f4feaae
```

`git merge-base --is-ancestor origin/main HEAD` → **true**. `origin/main` is now
fully merged into the branch, which was not the case at round 26. The branch is
**57 commits** ahead of main.

Four commits since the head you reviewed, all pushed at Jesse's direction:

```
f4feaae  Round 28 package: the round-27 fixes, scoped for review
6e4bd7e  Round 27: all six peer findings, two of them retractions of my own claims
a7f7b13  Declare the three Click'n'Load keys in the expected-keys allow-list
088a9a9  Merge origin/main: the JDownloader Click'n'Load transport
```

So you can read the live tree rather than only the patch. An earlier draft of
this section said these were unpushed; that was true when written and is
corrected here rather than edited away, for the same reason the round-25
provenance correction was struck through in place.

### The merge, since it happened after your review

`origin/main` merged clean: the two sides touched **zero files in common**, so
nothing was resolved and no side was taken. It brought `backend/clicknload.py`
(new), the Click'n'Load config keys, `download_service.py` changes, the retry-card
UI, three diagnostic scripts, and `tests/test_clicknload_fallback_wiring.py`
(17 tests).

Two consequences you should know about:

1. **Main's failing test came with it.** `origin/main` fails one test of its own
   — `test_config.py::TestDefaultConfig::test_default_config_has_no_unexpected_keys`
   — because commits `47fafc5`/`af3a127` added three config keys without the
   allow-list step that `704ebd2` shows is the practice. The branch inherited
   that failure and `a7f7b13` fixes it, mutation-checked so the guard still
   catches an undeclared key (`MUTANT 1 failed / CONTROL 14 passed`).

2. **It corrected a round-26 claim of mine.** Round 26 §1 said
   `tests/test_clicknload_fallback_wiring.py` "does not exist". It exists on
   `main`, 17 tests, and arrived with the merge — all three of my searches had
   been scoped to this branch. The old "11 pre-existing failures" baseline I
   discredited on that basis was a real measurement -- confirmed 2026-08-24, when the THIRD
   file it named, `test_round20_auto_resume_log_once.py`, turned up in an
   unpushed commit on local `main`. All three exist; every claim of mine
   discrediting the baseline was wrong. Details in the round-26 package's §1:
   `3c3369d`'s own message records those tests reaching a live JDownloader and
   being non-deterministic, *"They passed earlier today and failed after JD came
   up"*, which is the 8 clicknload failures it named. The round-26 package's §1
   carries that correction.

## 3. Authority

Pushing, merging, deploying, marking ready and enabling are Jesse's decisions
alone. Nothing here has been merged or deployed and no reviewer guidance can
authorize those steps.

## 4. The suite

```
origin/main  3c3369d                1 failed, 5356 passed, 4 skipped
branch, before round 27  a7f7b13    0 failed, 5805 passed, 4 skipped
branch, this head                   0 failed, 5829 passed, 4 skipped
```

Same method throughout, described in the round-26 package: `git archive` trees
copied WHOLE into containers from one image, pinned test dependencies
(`pytest 9.1.1`, `pytest-asyncio 1.4.0`, `httpx 0.28.1`), bytecode caches
cleared, all in this session.

The +24 are round 27's own regressions: the interlock and its controls, the
close precondition, the strict read and its structural guard, the `/health`
wiring, and the required-keyword tests.

Main's single failure is its own (`04-provenance.md` §2) and `a7f7b13` fixes it
on this branch. **The branch has no failures.**

A green suite is not the point here and I want to say so plainly: every defect in
rounds 26 and 27 was on a failure path that an ordinary suite never exercises.
The inert guard passed every test in the repository. So did the fail-soft
diagnostic. So did the process-local refusal. The suite establishes that nothing
was broken in passing; it establishes nothing about whether the new failure paths
are right, which is what `01-request.md` asks you to attack.

