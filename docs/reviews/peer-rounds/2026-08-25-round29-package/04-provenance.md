# Round 29 — provenance

Re-measured 2026-08-25. Nothing carried forward.

---

## 1. The running container — still none of this

```
container : scanhound      image: scanhound:latest (built 2026-08-22T17:10:25Z)
```

`/app/backend/arms.py` is absent. **None of rounds 19–29 is deployed.** The
feature has never been merged to `main`, so the deployed image has no writer for
it. The "frozen ledger" carried through rounds 24–25 was diagnosed in the
round-26 package §3 and is not an incident.

Consequence for this round specifically: **the quarantine interlock has never run
outside a test container.** Every claim about it is measured in containers built
from `git archive` trees. Its real test is a restart on the actual host, which no
review substitutes for.

The container restarted at `2026-08-25T00:51:52Z` during this session. Same
image, same code, and the DV port binding and `SCANHOUND_DV_INGEST_KEY_SHA256`
both survived — the recreate hazard from 2026-08-11/12 held this time, because
the pinned recovery compose and the working tree are now semantically identical
(174 lines each, zero diff).

## 2. Git

```
origin/main                                 3c3369d
b8433f1   <- the head your round-28 review read as "previously reviewed"
4b24eca   <- the head you reviewed this round
this head                                   d096885
```

`origin/main` is fully merged into the branch. Three PRs are open against main
and **unmerged**, all verified green independently:

```
#96  fix/config-allowlist-keys        main's one failing test  -> 5357 passed
#97  fix/queue-auto-resume-log-spam   ~43,000 log lines/day
#98  fix/kometa-dv-badge-mirror       a doc that caused a shipped defect
```

### Two things the patch carries that you have not seen

`4b24eca` predates them, so `02-code-changes.patch` includes both. Jesse held
them back from round 28 deliberately and asked that they go into the next round,
which is this one.

**The swallowed-failure lint** — `scripts/lint_swallowed_failures.py`,
`tests/test_lint_swallowed_failures.py`, 11 annotations in `backend/`, one CI
step. This is the Layer-1/Layer-2 static check from your round-27
recommendation. **Its design questions are written up separately in
`docs/reviews/peer-rounds/LINT-swallowed-failures-review-request.md`** — six
things I want attacked, including the name-matching heuristic I am least happy
with and the fact that its acceptance test validates only against defects I
already knew about.

**The `rename apply()` fix** — `backend/rename/service.py`. A `status="failed"`
write wrapped in `except Exception: pass`, where two callers discard the return
value. Safe today because the write records the failure durably; if that write
also failed, the failure vanished. Now logged, with `status_recorded` on the
returned dict. Adding `apply` to the lint's critical-name list was measured
rather than guessed: 16 new defects in a file built on rollback handlers, so it
stays out.

## 3. Authority

Pushing, merging, deploying, marking ready and enabling are Jesse's alone.
Nothing here has been merged or deployed. `gh pr merge` is additionally blocked
for me by this environment's permission classifier, so those three PRs require
Jesse regardless.

## 4. The suite

```
origin/main  3c3369d                     1 failed, 5356 passed, 4 skipped
branch, before this round  97847c6       0 failed, 5841 passed, 4 skipped
branch, this head          d096885       0 failed, 5853 passed, 4 skipped
```

Same method throughout: `git archive` tree copied WHOLE into a fresh container
from one image, pinned test dependencies (`pytest 9.1.1`, `pytest-asyncio
1.4.0`, `httpx 0.28.1`), bytecode caches cleared, same session.

Main's single failure is its own and #96 fixes it.

The +12 are this round's regressions: seven for the watchdog check function, six
for delivery and dedup, minus the removed source-inspection test.

**A green suite proves little here and I want to keep saying so.** Every defect
in rounds 26–28 was on a failure path an ordinary suite never exercises. The
inert guard passed. The fail-soft diagnostic passed. The process-local refusal
passed. All three M28-2 paths passed. The suite establishes that nothing broke in
passing; the failure-injection probes in `03-evidence.md` are what establish the
failure paths behave.

`scripts/lint_swallowed_failures.py backend/` exits 0 with 11 suppressions —
down from 12, because `_clear_quarantine_pending()` no longer absorbs.
