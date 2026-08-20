# Repository state

**What this is:** the one place that says what is open, what is live, and what
each branch is for. Read it before starting work.

**Why it exists:** on 2026-08-19 an hour was spent re-deriving a finding that
already existed in `backend/sweep/gate.py`, on a branch nobody remembered — and
the same insight had *also* reached `main` by a third route. Three independent
paths to one conclusion. Branch names and merge status had both proved
unreliable, so there was no cheap way to check.

**Maintenance rule:** update this file in the same commit that opens, closes or
merges a PR. **A stale version of this file is worse than no file**, because it
invites exactly the confident-wrong answer it exists to prevent. If you cannot
verify a row, delete the row rather than leave it guessed.

**Last verified:** 2026-08-19, after round 10 (against `gh pr list` and `git for-each-ref`).

---

## Open PRs

**Last verified:** 2026-08-20, after peer-review round 11.

| PR | Branch | Head | State |
|---|---|---|---|
| **#95** | `fix/rss-history-keyed-on-release-url` | `1407ea4` | **Re-delivers #90 to main.** #90 was merged into #89's *branch* 14s after #89 merged to main, so its content never arrived. M1a/M1b/LOW all fixed here. |
| **#94** | `agent/hybrid-sweep-rebased` | `7a50443` | DRAFT. Hybrid sweep, 0 behind main. R-3 harness run. I1 unresolved. |
| **#93** | `fix/carry-is-tv-not-rederive` | `6a458d7` | Q8 closed + rescan fixed. Reviewer APPROVED at the earlier head. |
| **#92** | `feat/queue-records-category` | `2a953cc` | L1/L2 closed. Retargeted to `main`. |
| **#91** | `feat/consume-media-kind-in-ui` | `08b0e75` | Consumer sound. Retargeted to `main`. |
| **#61** | `design/rss-readiness-gate` | — | Evidence record; its design lives in #94. |
| **#59** | `docs/dv-detector-enable-runbook` | — | Blocked on two Jesse-only gates. |

**Every PR now targets `main` directly.** They previously targeted each other,
which is how #90 stranded — merging a PR whose base is a branch puts the content
in that branch, and GitHub only auto-retargets when the base is **deleted**.

**How to check a merge actually landed** (ancestry lies after a squash):

```bash
git grep -l "<a symbol the PR added>" origin/main -- backend/
```

Zero files means it stranded. That is what caught #90.

---

## #94 — the hybrid sweep, exactly where it stands

**Integrated and largely repaired.** Head `fc7760c`.

| | failures |
|---|---:|
| clean `main` baseline | 35 |
| at the merge (`77db9f2`) | **115** (80 net new) |
| after the readiness/window work | **52** (17 net new) |
| after the shadow/miss cluster | *see the PR* |

### The pattern behind every regression

**The sweep's 118 commits and 16 review rounds never ran against main's tests,
because those tests do not exist on that branch.** Every regression found here
was a main-side test the sweep had never seen — `test_rematch_cache_stop_flag`,
`test_hdencode_readiness_integrity`, `test_round7/8_discrimination`,
`test_miss_resolution_rule`, `test_shadow_miss_validity`,
`test_shadow_provenance_paths`.

Sixteen rounds of review cannot catch what is not there to run. That is an
argument for merging sooner, not for reviewing harder.

### Three traps for anyone touching this branch

1. **`05_shadow_evidence.py` is a second PRODUCTION implementation** of
   readiness that the collector reads, kept independent so the two corroborate.
   Updating one side without the other breaks the corroboration silently — its
   own header warns about exactly that, and it caught me anyway.
2. **A signature can accept a parameter its body ignores.** The window was
   grafted onto main's summary and only **one query in six** carried the scope.
   Grep for the parameter, then check every query.
3. **Consumers switch on outcome STRINGS.** Adding three new outcomes without
   teaching `get_hdencode_miss_resolution` about them made guarded cycles stop
   blocking — the same "right and unreachable" failure its own comment already
   records from 2026-08-07.

### Still open

- The contract's 🔨 rows are attested against **pre-merge SHAs** and are stale.
  Re-attestation has not been done.
- The app validates the miss count against rows; the mirror sums the stored
  count. They agree on consistent cycles and diverge exactly when the evidence
  contradicts itself. Unsettled; recorded in the parity fixture.

---

## Live in the running container

- **Version-count badges** — backfill completed 2026-08-19 16:51: 15,250 titles
  seen, **1,029 labels added, 0 write failures**. Watermark advanced.
- **DV auto-sync** — running; 1,536 matched, 44 labels added on 08-19.
- **RSS shadow** — 585 cycles over 28 days. Not promoted; still shadow-only.

---

## Branches with no PR

**76 of them.** Full classification in
[`docs/reviews/branch-audit-2026-08-19.md`](reviews/branch-audit-2026-08-19.md).
The short version:

**Real unfinished work — decide, do not just carry:**

| branch | date | what |
|---|---|---|
| `agent/audit-fixes-pass2` | 08-05 | 47 code files |
| `agent/hybrid-sweep-{implementation,combined}` | 08-03/04 | superseded by #94's branch |
| `feat/item-history-sheet` | 08-16 | per-item history, data half |
| `feat/queue-declared-semantics` | 08-16 | declare `affected_scope` per ScrapeCode |
| `fix/dv-import-cadence` | 08-10 | import DV results during the walk |

**Known dead:** `agent/turnstile-classification` — one of its own commit
messages says *"this branch is NOT the base"*.

**Trap worth remembering:** `feat/rss-coverage-canary` is **docs only** despite
the `feat/` prefix. A prefix is not evidence.

---

## How to check state, rather than assume it

Merge status by *name* is worthless and by *ancestry* is incomplete — a branch
can sit unmerged while its fix shipped by another route.

```bash
git merge-base --is-ancestor origin/<branch> origin/main   # content on main?
git diff --stat origin/main...origin/<branch>              # zero = delivered elsewhere
gh pr view <n> --json state,headRefName,baseRefName        # never infer from the branch
```

For anything claimed to be deployed, verify **inside the container** — merged is
not deployed:

```bash
docker exec scanhound python -c "import backend.<module>; print(backend.<module>.__file__)"
```
