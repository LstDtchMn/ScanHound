# Claude independent adversarial review — ScanHound Master Plan (2026-07-19)

Reviewer: Claude. Author: ChatGPT. Read-only. Nothing implemented, merged,
deployed, or altered. Package integrity: all 7 files verified against
`SHA256SUMS` — OK.

Baseline reverified against the live repo, not the plan's prose:

- `main` = `555e26bc65a6e6474eb63fdfb6e025a41255dea9` — **matches**.
- All 10 open-PR head SHAs in EVIDENCE_INVENTORY §2 — **match exactly**
  (#3 `f3c2f0c`, #4 `fb99d49`, #5 `64663e6`, #6 `da15768`, #7 `8a48382`,
  #8 `c1a7f7b`, #14 `be52908`, #15 `70dca70`, #16 `44ea7ba`, #17 `bf07697`).
- MP-05 topology **independently confirmed** by ancestry:
  `#4` IS an ancestor of `#8`; `#5`, `#6`, `#7` are NOT. PR #8 forks from #4.

Every empirical code claim I could reach was checked against the actual tree at
these SHAs. Results below.

---

## Verdict table

| Plan item | Verdict | Evidence | Required change | Blocks |
|---|---|---|---|---|
| Core rec 1 — single access coordinator | ACCEPTED | 5 distinct HDEncode paths confirmed (§5.3); boolean off-switch must gate all | Add a constructor-level enforcement test (below) | HDEncode stack release |
| Core rec 2 — tri-state RSS evidence, separate from boolean media models | ACCEPTED | `matching.py:687` `web.get('dovi', False)`; `:697` `web_loses_dv` confirmed | none | RSS classification |
| Core rec 3 — atomic feed validator+candidate transaction | ACCEPTED | risk is real; §4.6 design is correct | crash-test must use real sqlite, not a mock (Q15) | RSS ingest |
| MP-01 boolean DV coercion | ACCEPTED | `matching.py:687/697-698/788` — silence→false→`web_loses_dv`→scoring | none beyond core rec 2 | RSS-primary |
| MP-02 non-atomic validator/candidate | ACCEPTED | design inference; §4.6 resolves it | validator DB-resident + written last in same txn (plan already says) | RSS ingest |
| MP-03 50-item shallow feeds | ACCEPTED WITH CHANGE | qualification: `movies_all` ~12 h; quality feeds are subsets | state deepest-quality-feed depth bound + residual class → listing fallback (Q4) | RSS-primary |
| MP-04 fragmented access | ACCEPTED | live paths confirmed: `download_service` selenium, `detail_scraper`, source plugins | enforcement TEST as a gate, not just routing | HDEncode release |
| MP-05 PR #8 wrong base | ACCEPTED | ancestry: #4 ancestor, #5/#6/#7 not | rebuild (PR D), not rebase | #8 merge |
| MP-06 health precedence / weak success clears block | ACCEPTED | my PR #7 matrix showed reachable-empty `blocked→healthy` | define evidence strength + cooldown-expiry + which outcomes may clear | coordinator completion |
| MP-07 restore/delete not durable | ACCEPTED (severity nuance) | `fileops.py:903` rename-then-manifest non-fatal; `:953` unlink-then-manifest non-fatal; both return `ok:True` | note: bookkeeping/false-success, NOT media loss; SH-R04 (PR B) | "full lifecycle durable" claim + Auto-rename hold |
| MP-08 process-local locks | ACCEPTED | design inference | lifetime lock file, fail-loud on contention (Q12) | production safety sign-off |
| MP-09 CIFS/NTFS unqualified | INSUFFICIENT EVIDENCE | no probe run | honest guarantee: no-replace YES / fsync-durability NO on CIFS (Q13); gate on sentinel | Auto-rename hold removal |
| MP-10 late-worker after teardown | INSUFFICIENT EVIDENCE | my #17 test covered stale-refs, NOT late-worker-publish | reproduce late worker first; add generation token only if it reproduces (Q10) | #17 final approval |
| MP-11 no IDs / year conflict | ACCEPTED | byte-level: 0 `tt`, 0 imdb.com, 0 `<a href>`; 7/50 year disagreements | store both years, desc-year first | conflict classification |
| MP-12 excerpt truncation | ACCEPTED | ADDENDUM_FIELD_PARITY: every desc ends `[…]` | `description_truncated`/`mediainfo_complete` flags | fetch minimization |
| MP-13 parser traps | ACCEPTED | 4 traps confirmed live | dedicated RSS parser + regression suite | RSS parser merge |
| MP-14 feed URL SSRF/XML | ACCEPTED | probe already enforces allowlist/IP/redirect/2 MiB/DTD-reject | reuse the qualified probe's guards | RSS networking |
| MP-15 dormant evasion WebDriver | ACCEPTED WITH CHANGE | `hdencode.py:449-453` obscuring opts; **dormant** (no caller); live path clean | **REMOVE the options, do not "route" them**; see below | control-stack sign-off |
| MP-16 partial candidates ≠ cache | ACCEPTED | §5.4 confirmed | separate candidate/feed tables | RSS persistence |
| MP-17 Auto-rename still enabled | ACCEPTED | re-verified live this session: `auto_rename_enabled=True`, method `move` | Jesse toggle | immediate |
| MP-18 README stale (MediaScout) | ACCEPTED | operational; low | doc fix pre-release | doc gate |

---

## 1. Blocking findings

Nothing in the plan is *wrong* enough to reject. Four items must be tightened
before the work they gate proceeds:

**B1 — MP-15 phrasing lets evasion survive relocation.** The obscuring options
at `hdencode.py:449-453` (`--disable-blink-features=AutomationControlled`,
`excludeSwitches: enable-automation`, `useAutomationExtension: False`) hide the
`navigator.webdriver` signal. MP-15 says "remove/deprecate **or route through
DownloadService/coordinator**." The disjunction is the problem: routing them
through a coordinator would centralize the evasion, not remove it. Required: the
options are **deleted outright**. The coordinator may own pacing, host policy,
and the off-switch; it must not carry browser-undetection flags. This is the
plan's own Network Invariant #6 — I am closing a contradiction between that
invariant and MP-15's looser wording. Good news that lowers the stakes: the path
is **dormant** (no caller, no dynamic dispatch), and the **live** download path
(`DownloadService._ensure_selenium`) carries **none** of these flags. So this is
deleting latent code, and the active path is already non-evasive.

**B2 — MP-04 needs enforcement, not just routing.** Routing all five paths
through the coordinator is necessary but not self-proving. Required gate: a test
that monkeypatches every direct `webdriver.Chrome`, `_ensure_selenium`, and HTTP
client constructor to raise unless a coordinator authorization token is present,
then runs discovery/detail/grab and asserts zero unauthorized construction.
Without this, a future path silently re-bypasses.

**B3 — MP-07 blocks the "durable lifecycle" claim and the Auto-rename hold, and
must land before hold release.** Confirmed: restore/delete move bytes first,
update the manifest after, tolerate manifest failure as non-fatal, and still
return `ok:True`. Severity nuance the plan should record: this is a
**bookkeeping / false-success** defect (a ghost manifest record and a lie in the
return value), **not** a media-loss defect like SH-R02/R03 — the user's file is
already safe (restored, or intentionally deleted). It therefore does **not**
block deploying #15/#16, but it does block declaring the trash lifecycle
transactional and must be corrected (PR B) before the Auto-rename hold is lifted.
The plan's Phase 2 sequences this correctly; I'm asking only that the severity be
stated precisely so #15/#16 are not held hostage to it.

**B4 — MP-09/Q13 CIFS guarantee is unresolved and gates the hold.** Honest
position: on Windows `os.rename` refuses an existing destination and on CIFS
`os.link` EEXIST or the fail-safe path can still provide **no-replace**; but
**durability** (crash-consistency of the manifest via directory fsync) **cannot**
be guaranteed on CIFS and is degraded on Windows. So a crash on a CIFS mount can
lose a just-written manifest record even though no-replace held. This is
INSUFFICIENT EVIDENCE until the prepared sentinel runs on the actual mount. The
plan already gates Auto-rename hold removal on it (MP-09) — correct.

---

## 2. High-risk non-blockers

- **MP-06 health precedence.** My own PR #7 matrix showed a reachable-but-empty
  response drives `blocked → healthy`. That is intended for genuine
  reachable-empty, but the risk is a *misclassified* block page (a challenge
  rendered as an empty listing) or a *stale* 200 clearing a *fresh* block. The
  coordinator's health model needs evidence-strength + epoch + cooldown-expiry so
  a weak/old success cannot clear a strong/recent block.
- **MP-10 late worker.** #17 clears the 13 lifespan refs and I validated three
  clean lifespans — but I did **not** test a worker that exceeds the 2-3 s join
  and publishes afterward. That reproduction must exist before #17 final sign-off.
- **MP-03/Q4 catch-up completeness.** Quality-feed union ≈ all-feed union only
  *within each feed's depth*. A release class matching none of the enumerated
  quality feeds, or an outage longer than the deepest quality feed, still needs
  the limited listing fallback. Document the bound; don't imply the quality feeds
  are total.

---

## 3. Simplifications

- **Single-writer (PR A): a lifetime lock file is enough.** This is a single-node
  personal deployment (one container). Per-mutation cross-process locking is
  over-engineering; a lock file acquired at startup, released at exit, failing
  loudly if another writer holds the DB/trash state, covers the only real risk
  (accidental double-start). Q12: process-wide lifetime lock is sufficient.
- **Do not build a new matching engine from scratch if the claim-aware
  `CandidateDecisionEngine` can wrap the existing rule set.** Q6: keep it
  *separate* from `matching.py` until hydration (correct), but the size/resolution
  rules in `matching.py` are sound — the only unsafe part is the boolean DV/HDR
  coercion. Reuse the rules; replace only the evidence layer.
- **Claim enum is already right; don't over-model.** `asserted|negated|unknown`
  + `hdr_formats[]` set covers DV/HDR/HEVC including SDR-as-negated and paired
  variants (Q5). No additional states needed.

---

## 4. Missing tests (with the exact seam each must hit)

RSS:
- Crash after **each** statement inside the real `BEGIN IMMEDIATE` — using an
  actual sqlite connection with real COMMIT points, **not** a mocked DB (Q15);
  assert rollback leaves the prior Last-Modified so the feed refetches.
- 304 path asserts **zero** candidate-table mutation.
- Concurrent duplicate poller: two ingest cycles for the same feed; assert
  idempotent upsert by `canonical_url` PK, no double-insert.
- Outage rollover: simulate N-hour gap > `movies_all` depth; assert catch-up via
  quality feeds reconstructs the missed set, and a class matching no quality feed
  triggers the listing fallback exactly once.
- Parser mutation tests for all four live traps: `S01E03E04` (episode before
  pack), en-dash size `–`, `HDR10P` == `HDR10+`, `\bDV\b`/`DoVi`/`Dolby.Vision`.
- Unknown-DV through the **complete** matching/Compare path: assert `unknown`
  never produces a downgrade recommendation and never scores as `negated`.

HDEncode coordinator:
- Constructor-authorization test (B2) — the load-bearing enforcement gate.
- Cancellation injected during **each** wait: semaphore, pacing clock, retry
  backoff, DNS, browser wait — assert no request starts after the stop timestamp
  (extends the PR #6 pattern I already validated).
- Domain block on one operation halts lower-priority operations across all
  transports.
- DDLBase/Adit-HD independence: assert their throughput is unaffected by an
  HDEncode block (the coordinator must not serialize other sources).
- Source-health persistence failure does not alter the primary scrape result
  (I validated this on PR #7; re-assert through the coordinator).

Lifecycle:
- Late worker exceeds teardown join and attempts to publish (Q10 / MP-10) — the
  missing reproduction.
- Stale worker DB write after DB close raises cleanly, does not corrupt.

File ops:
- Restore publication then manifest failure → assert either full completion or a
  detectable incomplete-transaction marker, never a silent `ok:True` with a ghost
  record (the MP-07 correction target).
- Delete unlink then manifest failure → same.
- Second process attempts the same manifest → single-writer guard rejects.
- **Real** sentinel on the actual production mount (CIFS/NTFS), Jesse-authorized.

---

## 5. Corrected dependency graph

The plan's graph is sound. One ordering correction and one addition:

```
main 555e26b
  ├─ PR #14 E2E isolation ─────────────┐
  ├─ PR #17 lifecycle ── late-worker ──┤→ refreshed main   (independent, early)
  │        repro (Q10) gate
  │
  ├─ PR #15 no-replace ─→ PR #16 durable disposal
  │            └─ PR A single-writer  ← MOVE EARLIER: lands WITH file-safety deploy,
  │                                      not after (it gates the same hold)
  │            └─ PR B SH-R04 restore/delete ─→ CIFS sentinel ─→ Auto-rename hold release
  │
  └─ PR #3 off-switch ─→ zero-traffic proof
         └─ #4 ─ #5 ─ #6 ─ #7  (linear)
                └─ PR C coordinator (+ DELETE evasion, not route)  ← B1/B2
                     └─ PR D claim-aware evaluator (rebuild #8)
                          └─ PR E RSS foundation (fixtures only)
                               └─ PR F shadow (atomic ingest)
                                    └─ PR G classification/hydration
                                         └─ PR H RSS-primary (flag)
```

Change vs plan: PR A (single-writer) is drawn under file-safety and gating the
same Auto-rename hold, so it is not deferred behind the HDEncode stack.

## 6. Corrected PR/branch strategy

Agree with the plan's dispositions with three edits:

1. **PR C** acceptance must add: "the automation-obscuring WebDriver options are
   **deleted**; no coordinator code path sets `disable-blink-features`,
   `excludeSwitches`, or `useAutomationExtension`." (B1)
2. **PR C** acceptance must add the constructor-authorization enforcement test.
   (B2)
3. **PR #17** must not be marked final until the late-worker reproduction (Q10)
   is run; add the generation token **only if it reproduces** — do not add
   speculative machinery.

Everything else — retain #3–#7 rebased around the coordinator, rebuild #8 as
PR D, keep #14/#17 independent, #15→#16 stacked, RSS as E→F→G→H — is correct.

## 7. Final recommendation

The plan is architecturally correct, its empirical claims verify against the real
tree, and it integrates the RSS qualification faithfully (tri-state, no layer
inference, atomic ingest, four parser traps, dual-year, truncation). The required
changes are tightenings, not redesigns: delete (don't relocate) the dormant
evasion flags; make coordinator enforcement a tested gate; state MP-07's severity
precisely so it doesn't block #15/#16; resolve the CIFS guarantee by probe; and
reproduce the late-worker case before adding a lifecycle token.

Two honest limits on this review: I did **not** run the CIFS sentinel (needs
Jesse's mount authorization) or the late-worker reproduction (needs building),
so MP-09 and MP-10 are marked INSUFFICIENT EVIDENCE rather than accepted.

---

# MASTER PLAN ACCEPTED WITH REQUIRED CHANGES
