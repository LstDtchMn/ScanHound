# ScanHound — state of play

**Last updated:** 2026-08-10 by Claude (session `e7d059a1`, since archived)

This file exists on `main` so that anyone starting from `main` can see what is outstanding
without having to know which branch to look at. **Read this before starting work on DV
detection, the Turnstile/download-link path, or the queue-recovery policy tests.**

---

## 1. The one thing to know before touching anything

**`main` does not contain the current DV work, and its `backend/rename/dv_detect.py` parser has
a defect that can remove a managed Plex label.**

`_classify()` on `main` tests `"FEL" in sub` as a raw substring *before* looking at the profile,
and returns an authoritative `MEL` for a tokenless `Profile: 7`. So:

| summary | `main` returns | correct |
|---|---|---|
| `Profile: 8 (FEL)` | `fel` | `profile8` |
| `Profile: 7 (NOT FEL)` | `fel` | `unknown` |
| `Profile: 7` | `mel` (authoritative) | `unknown` |

`mel` is a **managed** label: `may_remove = authoritative or not additive_only`, and
`is_authoritative('mel')` is True, so an ambiguous parse can **replace** a real badge. Separately,
`_parse_info()` starts at `LAYER_NONE`, so an unreadable `dovi_tool info -s` — reached only *after*
a non-empty RPU has already proven Dolby Vision exists — returns an authoritative "no Dolby
Vision".

All of this is fixed on `agent/dv-detector-consolidation`. **Do not build on `main`'s parser.**

## 2. Where the work is

| Branch | Head | What it is |
|---|---|---|
| `agent/dv-detector-consolidation` | `e196de4` | DV detector + wrapper + imports, consolidated from two parallel sessions. **4 review rounds, round 4 outstanding.** Suite 4689 passed / 5 skipped |
| `agent/turnstile-consolidation` | `49625ff` | The Turnstile / download-link fold — **instructions only, nothing folded yet** |
| `agent/hdr10plus-design-review` | `8fbac87` | The live-progress wrapper, approved over 3 rounds and verified on a real scheduled run. **This is what the `ScanHound-DVScan` task currently executes** |

Retire when the consolidation lands: `fix/dv-import-cadence`, `fix/dv-scan-live-progress`,
`agent/dv-scan-hang-and-starvation`, `agent/policy-migration-audit` — all subsumed or superseded.
Close `fix/policy-tests-wall-clock`: both its author and the other session confirmed it is
redundant with `a88d541`.

**Merging `agent/dv-detector-consolidation` lands five branches at once** (verified with
`git merge-base --is-ancestor`, not assumed).

## 3. Two operational facts that are easy to get wrong

**The working tree is the deployment surface.** `ScanHound-DVScan` executes
`X:\Docker Apps\ScanHound\scripts\run-dv-scan.ps1` from the working tree, so **checking out a
branch deploys it**. `main` has no such file, so checking out `main` in the main working tree
breaks the scheduled task. **Use a worktree** for anything based on `main`.

**The container cannot read `dv_host.db` while the detector holds it open.** SQLite's WAL index
(`-shm`) needs mmap semantics the Windows bind mount cannot provide, so the read fails with
`disk I/O error` — and `import_dv_host_db()` catches that and returns
`{"imported": 0, "updated": 0}` **behind an HTTP 200**. Isolated with a controlled writer: writer
holding the connection → fails; writer exits → succeeds. This is why the container's `dv_scan` has
been frozen at 466 rows since 2026-07-26 while the host store kept growing.

**So never verify an import with an HTTP 200. Verify it with a known row.**

## 4. Outstanding, needing a person

1. **Round 4** on the DV consolidation — one substantive question: is close/reopen the right layer
   for the WAL problem, or should `dv_host.db` leave WAL entirely (~6 rows/hour workload), or
   should the detector POST its rows in the body so the container never reads the file?
2. **The canary** — existing roots only, no root widening in the same event, and do **not** restart
   the container first (the deliberate startup-baseline rule would adopt the new generation and
   sync nothing).
3. **Plex DV labels have been stale since 2026-07-26.** The fix exists; no canary has run.
4. **The Turnstile fold** — fully specified in
   `docs/reviews/peer-rounds/turnstile-fold-instructions.md`. One branch author offered to peer
   review the result rather than author it.

## 5. Detail lives here

- `docs/reviews/2026-08-10-scanhound-consolidation-map.md` — every current branch, attributed to
  its owning session, with verified ahead/behind and conflict counts
- `docs/reviews/peer-rounds/turnstile-fold-instructions.md` — the fold, port-by-port
- On `agent/dv-detector-consolidation`: `dv-detector-consolidation-round{,2,3,4}.md`,
  `2026-08-10-dv-round-summary.md`, the gate results, and the staged-set revalidation
- `X:\Docker Apps\SCANHOUND-CLOUD-SESSIONS-CATCHUP.md` — the running cross-session log (outside
  the repo)

## 6. The methodological note

Three times during this consolidation a **green test suite was actively concealing a real
defect**: a test that asserted the unsafe `none`; a suite that passed only because of pytest's
collection order; and an HTTP 200 that meant zero rows imported. Each was found by looking at live
output rather than by reasoning about the code.

The related habit, from the other session, while re-verifying a result it had already correctly
predicted: **"predicted by the evidence" and "measured" are different claims.**
