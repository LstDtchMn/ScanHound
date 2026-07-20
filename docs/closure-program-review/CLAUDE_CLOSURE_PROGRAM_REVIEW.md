# Claude pre-implementation review — ScanHound Complete Feature-Pack Closure Program

Reviewer: Claude. Author: ChatGPT. Read-only. Nothing implemented, merged,
deployed, force-pushed, marked ready; no sentinel run; no production change.

Document SHA-256 `17bac71a…d5b7a39c` — **verified**.

Baseline reverified against the live repo (not chat):

- `main` `555e26b`, and all 11 §3 kickoff SHAs **match exactly**, including
  **PR #17 at `3e60c24`** (the MP-10 fix I built and validated last cycle).
- PR #8 topology **confirmed**: `#4` is an ancestor, `#7` is not — "incorrectly
  forked from #4" is accurate.
- RSS foundation branch **empty at `555e26b`**; all three §4 evidence branches
  present (`hdencode-rss-qualification` `125cdf7`, `master-plan-review`
  `dae68b8`, `pr17-lateworker-probe-defect` `679da80`); all open PRs draft.

The code seams underpinning this program were verified in my two prior reviews
(matching.py boolean-DV `:687/:697`, restore/delete non-atomicity `:903/:953`,
dormant-and-live-path-clean evasion `hdencode.py:449-453`, health precedence,
PR #8 fork). Those findings are correctly incorporated below.

---

## Verdict table

| Plan item | Verdict | Evidence | Required change | Blocks impl? |
|---|---|---|---|---|
| §3 kickoff SHAs / topology | ACCEPTED | all 11 SHAs match; #8-from-#4 confirmed | none | no |
| PR #17 = `3e60c24` incl. MP-10 fix | ACCEPTED | verified head + landed last cycle | none | no |
| Scope: 4 master-plan changes incorporated | ACCEPTED | B1 Phase 3B "delete…must not be relocated"; B2 constructor-auth test; B3 §2/Phase 2 "not #15/#16 blocker"; B4 Phase 2E two guarantees | none | no |
| Scope: MP-06/08/11/12/13/14/16/18 | ACCEPTED | health precedence 3C; single-writer PR A; dual-year P7; truncation P5/P7; 4 parser traps P5; SSRF/XML P5; partial-cache Track D + P8; README Track G | none | no |
| **Scope: SH-R09 raw-exception exposure** | **ACCEPTED WITH CHANGE** | WS bodies (`downloads.py:117/217`, `rename.py:456/612/650/705`) + HTTPException detail → `client.ts:72` → toasts; PR #5 sanitized only ScrapeDiagnostic and **deferred this** | scope or explicitly record in KNOWN_LIMITATIONS — do not orphan | **yes** |
| Branch topology auditable (12-deep #3→I) | ACCEPTED WITH CHANGE | linear #3→#4→#5→#6→#7→C→D→E→F→G→H→I | document the rebase/reconciliation procedure; flatten where deps allow | no |
| Integration-candidate strategy (Phase 9) | ACCEPTED | one candidate, exact commits, migrations from prod schema, one final diff | none | no |
| Testing reaches real seams | ACCEPTED | constructor-auth on real constructors; crash-injection on real SQLite; single-writer via separate processes; UID 1000; CIFS on real mount | none | no |
| CIFS sentinel classified operational | ACCEPTED WITH CHANGE | correct that "which mode CIFS is in" is operational | but the **fail-safe code** must be a Phase-2 blocker, unit-tested w/ simulated unsupported-fs, independent of the sentinel | **yes** (code, not sentinel) |
| 7-day shadow classified operational | ACCEPTED | code completes; observation is a deploy gate | none | no |
| PR #8 transfer/supersede | ACCEPTED | §7.1 + Phase 4: close only after equivalent + tests transferred | none | no |
| Two-review workflow | ACCEPTED | pre-impl + final acceptance, with targeted verification allowed | none | no |
| Final review package (Phase 10, 14 files) | ACCEPTED WITH CHANGE | traceability, invariant audit, migration report, runbooks, sentinel, checksums | REQUIREMENT_TRACEABILITY must map every MP-##/invariant to **executed** evidence + an SH-R09 line | no |
| Single-writer covers trash state | ACCEPTED | §2C names "trash mutation" | ensure lock wraps ALL trash-root/manifest entry points, not just DB | no |
| Trash 8-state machine | ACCEPTED | §2D allows "state machine OR journal", "not complexity for its own sake" | prefer minimal recoverable design | no |
| DB migration additive/rollback | ACCEPTED WITH CHANGE | new tables (Track D); rollback "additive" | MIGRATION_REPORT must assert additive-only, no destructive ALTER, old image tolerates new tables | no |

---

## 1. Blocking plan defects

**BD-1 — SH-R09 exception-boundary exposure is unscoped and would be orphaned.**
My master-plan review (SH-R09) and ChatGPT's own PR #5 residual commit
("reported rather than broadened here") both confirmed raw exception text still
reaches clients by two routes the program does not cover: WebSocket notification
bodies (`downloads.py:117`, `:217`; `rename.py:456/612/650/705` — `{"body":
str(e)}`) and `HTTPException(detail=f"…{e}")`, which the frontend api client
rethrows verbatim (`client.ts:72`) into toasts. Track C's "sanitized
diagnostics" is the `ScrapeDiagnostic` path only. The closure definition (§9)
requires that "no draft work item… remains orphaned or ambiguously superseded";
an identified privacy/info-leak that is neither fixed nor formally deferred
violates that. **Required:** either add a bounded exception-boundary hardening
item to Track G (a centralized public-error mapping — log raw detail with a
correlation id, return stable closed messages), or record it explicitly in
`KNOWN_LIMITATIONS.md` as accepted-deferred with rationale and blast-radius.
This is "blocks implementation" only in the sense that the program's scope must
resolve it before freeze — not that it blocks any single PR.

**BD-2 — CIFS fail-safe must be a Phase-2 code blocker, not folded into the
operational sentinel.** The sentinel (which no-replace/fsync mode the production
mount actually provides) is correctly operational. But the CODE response —
invariant 5.1.8 "unsupported filesystem guarantees fail safely and visibly" —
must be implemented and unit-tested against a *simulated* unsupported filesystem
in Phase 2, **before** the sentinel runs. Otherwise a negative sentinel result
on a real mount meets code that assumed the guarantee held, risking the exact
silent loss SH-R02 exists to prevent. Make "fail-safe on absent no-replace/
dir-fsync, proven by a simulated-unsupported-fs unit test" an explicit Phase-2
exit criterion, decoupled from the sentinel.

## 2. Missing scope

- **SH-R09** (above).
- Nothing else material is missing. I traced MP-01…MP-18, the four required
  master-plan changes, and every §5 invariant into the tracks; all are present.
  Already-shipped features (mobile, pipeline, renames redesign, skipped-manager,
  flat-folders) are correctly out of scope — they are merged to `main`, not part
  of the active reliability/HDEncode/RSS pack.

## 3. Dependency / branch corrections

- **The #3→I stack is 12 deep** (5 existing + C,D,E,F,G,H,I). No force-push +
  the integration candidate mitigate it, but a deep draft stack is fragile:
  rebasing any lower PR shifts everything above, and per-PR incremental review
  degrades with depth. **Required:** document the exact rebase/reconciliation
  procedure (order, conflict handling, how each PR's incremental diff is
  re-verified after a parent moves). **Consider flattening:** evaluate whether
  PR E (RSS foundation, fixture-only, no live network) must base on PR D, or
  whether the claim-aware evidence model can be a shallower shared base so RSS
  foundation does not inherit the entire coordinator stack. F (live polling)
  genuinely needs C; E may not.
- PR A base = `main`, PR B base = PR #16 — correct. Confirm PR A's lock is
  acquired before **every** trash-root/manifest mutation entry point (the
  maintenance sweep touches trash), not only before `DatabaseManager`.

## 4. Simplifications

- The 8-state trash machine (`trash_prepared…repair_required`) is more than a
  single-node app likely needs. The program already permits "journal OR state
  machine" and warns against complexity-for-its-own-sake — hold it to that:
  prefer the minimal design that recovers deterministically at every byte/
  bookkeeping boundary (a fsync'd intent record + idempotent completion may
  suffice with fewer states).
- No other over-engineering. The coordinator, tri-state evidence, atomic
  transaction, and integration candidate are all proportionate.

## 5. Missing test seams

The matrix is strong. Add:

- **SH-R09 assertion:** a test that a WS notification body and an HTTPException
  detail contain no raw local path / URL query / driver internal (drive the real
  route, force an exception carrying a sentinel secret, assert it is absent from
  the client-visible payload). Only if BD-1 is scoped as a fix.
- **CIFS fail-safe unit test** with a monkeypatched "renameat2 ENOSYS + os.link
  EXDEV + no dir-fsync" filesystem, asserting placement raises safely with the
  source intact (BD-2).
- **Migration additive-only assertion:** apply the RSS migration to a copy of
  the current production schema, then confirm the pre-migration image opens the
  DB without error (new tables ignored) — proves rollback tolerance.
- **Stale-worker across the coordinator:** the MP-10 late-worker test proves the
  lifecycle guard; add one proving a stale worker also cannot issue an HDEncode
  request through the coordinator after its lifespan ends (ties Track A ↔ C).

## 6. Corrections to the final review package

Phase 10's 14 deliverables are comprehensive. Strengthen:

- `REQUIREMENT_TRACEABILITY.md` must map every MP-01…MP-18, each of my four
  required master-plan changes, and each §5 invariant to the **executed** test
  that proves it (command + result + SHA), not to "implemented." Add an explicit
  SH-R09 row (fixed or deferred).
- `DATABASE_MIGRATION_REPORT.md` must state additive-only, no destructive ALTER,
  and old-image tolerance, with the assertion test from §5.
- `SECURITY_AND_INVARIANT_AUDIT.md` must include the SH-R09 disposition and the
  "no browser-undetection flags anywhere" grep result (not just the enforcement
  test).

## 7. Implementation-ready verdict

The program is complete, correctly incorporates the accepted master plan + my
four required changes + the validated MP-10 fix, has an accurate baseline, sound
topology, a real-seam test matrix, and correctly classified operational gates.
Two items must be resolved before the plan is frozen: **SH-R09 must be scoped or
formally deferred (BD-1)**, and **the CIFS fail-safe must be a Phase-2 code
blocker independent of the sentinel (BD-2)**. The stack-depth, traceability, and
migration-report items are required tightenings, not redesigns. None rejects the
program.

Two honest limits: I did not run the CIFS sentinel (needs Jesse's mount
authorization) and did not re-execute the full suite here (that is the Phase-9/10
final-acceptance job) — this is a plan review, and those remain the operational
and final-acceptance gates the program itself defines.

---

# CLOSURE PROGRAM ACCEPTED WITH REQUIRED CHANGES
