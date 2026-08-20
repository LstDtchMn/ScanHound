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

| PR | Branch | Base | State | What it does |
|---|---|---|---|---|
| **#94** | `agent/hybrid-sweep-rebased` | `main` | **DRAFT** | Hybrid listing sweep. 118 commits, 16 review rounds, stalled 08-05 at contract row R-7. Integrated 08-19 (`b92ad92`) — **suite NOT clean, see below.** Evidence not re-attested either; every 🔨 row cites pre-merge SHAs. |
| **#93** | `fix/carry-is-tv-not-rederive` | `main` | **APPROVED** (round 10) | Carry `is_tv` instead of re-deriving it from `season is not None`. Q8 closed: a recorded season now outranks an `is_tv=False`. Head `ac10d02`. |
| **#92** | `feat/queue-records-category` | #91 | round 10 L1+L2 **closed** | Records media kind for batched grabs. Signature test now binds per call; migration test builds a real old schema. Head `2a953cc`. |
| **#91** | `feat/consume-media-kind-in-ui` | #90 | consumer OK, was blocked by M1 | Deletes `isCanonicalSeasonName`; authorizes from the wire. Head `08b0e75`. |
| **#90** | `feat/record-media-kind-at-ingest` | #89 | round 10 M1 **closed both halves** | The SERVER now answers what kind a release is; the client may only contradict. Crawl records classification conflicts instead of first-source-wins. Head `3e94f34`. |
| **#89** | `fix/rss-history-keyed-on-release-url` | `main` | ready | One history row per release, not per file-host mirror. |
| **#61** | `design/rss-readiness-gate` | `main` | ready | **Evidence record, not a design.** Its hybrid already exists in #94's `sweep/gate.py`. Carries the 28-day shadow measurements. |
| **#59** | `docs/dv-detector-enable-runbook` | `main` | ready | Runbook for enabling the DV host detector. Blocked on gates 1 and 4 — both Jesse-only. |

**Merge order for the media-kind stack is forced:** #89 → #90 → #91 → #92.

---

## Round 10 peer review — the finding worth remembering

> **package provenance != media-kind provenance**

Knowing which release a package came from does not certify what KIND of thing it
is. The stack had been treating `identity_source = provenance` as if it did.

Two mechanisms that were wrong, both verified before being fixed:

1. **The kind came from the client.** `DownloadRequest.category` is unvalidated
   and filled by the frontend from `ScanResult.category`, so a value that
   authorizes a DESTRUCTIVE overwrite made a round trip through the client.
2. **The server's own category was first-source-wins.** One `seen_post_urls` set
   spans every source and the movie listings crawl before TV Packs, so a release
   in both was silently recorded as a movie.

**Trap for anyone touching this:** `background_scan_cache.source_category` is the
source NAME (`"HDEncode"` on all 4,084 rows), not the crawl category. The
category lives in the row's `data` JSON.

**Open question back to the reviewer:** the conflict is recorded at the crawl and
the lookup declines, rather than adding `media_kind_source` /
`media_kind_conflict` to the wire as proposed. Equivalent for safety, not for
diagnosis — downstream cannot tell conflict from unscanned from disagreement.

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
