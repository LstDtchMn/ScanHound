# ScanHound feature pack — Stage B validation and final verdict

Reviewer: Claude (git/deploy/real-checkout validation lane).
Integration branch: `agent/feature-pack-integration` head **`6939220`**
(pushed to origin). Assembled from `origin/main` `555e26bc`.

## 1. Assembled commits (all validated + pushed individually first)

| Track | Branch | Head |
|---|---|---|
| RSS + HDEncode + actions | `agent/hdencode-rss-actions` | `b3b5af87` |
| — RSS operations UI (F→G→H→I) | `agent/hdencode-rss-operations-ui` | `a309027c` |
| — RSS completion (63-op) | `agent/hdencode-rss-completion` | `a55b2e59` |
| — action base (completion + PR J) | merge | `96582228` |
| PR #14 e2e-isolation | `agent/…e2e-isolation` | `932cefb` |
| PR #15 no-replace placement | `fix/atomic-no-replace-placement` | `70dca70` |
| PR #15 unsupported-fs fail-safe | `fix/…-failsafe` | `356a0e3b` |
| PR #16 durable trash | `fix/durable-trash-transaction` | `44ea7ba` |
| PR B SH-R04 lifecycle | `agent/trash-lifecycle-transaction` | `4d678bd` |
| PR A single-writer guard | `agent/single-writer-runtime-guard` | `4bfe5ca` |
| PR J public-error boundary | `agent/public-error-boundary` | `52f26535` |

Merge topology (from `git log --graph`): main → action head → PR #14 →
(#15→#16→PR B) → #15-failsafe → PR A → Stage-B gates. Only ONE merge conflict
across the whole assembly: `backend/rename/fileops.py` (PR A single-writer ×
PR B SH-R04 restructure), resolved by grafting `require_writer_lock()` onto 11
trash-mutation entry points on HEAD's structure (the 10 PR A guarded + the new
SH-R04 `repair_trash_transactions`, per §5 invariant #7).

## 2. Validation matrix — EXECUTED (all green)

| Gate | Command / scope | Result | Env |
|---|---|---|---|
| Full backend suite | `pytest -q --ignore=tests/test_api_routes.py` on `6939220` | **3729 passed, 4 skipped, 0 failed** | Python 3.12.13, root, `scanhound:latest` container |
| Frontend typecheck | `npm run check` | 0 errors (353 files) | host node |
| Frontend unit | `vitest run` | **373 passed** (28 files) | host node |
| Frontend build | `npm run build` | clean | host node |
| Whitespace/conflict | `git diff --check` | clean | — |
| Stage-B cross-seam gates | `test_feature_pack_integration.py` | **3 passed** | 3.12 |
| File-safety cross-seam | runtime_lock/trash_durability/trash_lifecycle/fileops_dedupe | **35 passed** | 3.12 |
| Migration (additive) | SCHEMA_VERSION 4→5→6; CREATE TABLE IF NOT EXISTS + guarded ALTER only; recovery on service init | verified additive, zero destructive ops | 3.12 fresh + reopen |
| Constructor / off-switch / lifecycle / coordinator priority | test_hdencode_constructor_gate / off_switch / priority / coordinator | passed (in broad) | 3.12 |
| Public-error boundary (SH-R09) | test_public_error_boundary + client.errors.test | passed | 3.12 + node |
| DNS-pinned TLS review | code review of `hdencode_feed_client._PinnedHTTPSConnection` | PASS — pinned-IP socket wrapped with `create_default_context` (check_hostname=True, CERT_REQUIRED) + `server_hostname`=approved host; SSRF IP rejection; 2 MiB bounded gzip; hostname verification NOT weakened | code review |

## 3. Validation matrix — ENVIRONMENT-GATED (NOT run; reported honestly)

These require an environment this reviewer cannot provide. They are **not**
simulated and remain open:

- **Python 3.11** — the validation image is Python 3.12-only; the 3.11 floor
  is unverified here.
- **Root AND UID 1000** — all runs were as root in the container; UID-1000 is
  unverified.
- **Playwright production-build occupied-port / state isolation** — no browser
  runner available.
- **Production-schema migration, interrupted restart, old-image reopen,
  rollback** on the real production DB — only the fresh-DB additive path and a
  clean reopen were exercised.
- **Seven calendar days + ≥20 valid RSS shadow comparison cycles with zero
  relevant misses, measured request reduction, and recovery evidence** — the
  readiness gate is code-correct and unit-proven, but the operational shadow
  window has not elapsed.
- **Jesse-authorized CIFS/NTFS filesystem sentinel** — not authorized; not run.

## 4. Requirement traceability (area → executed evidence)

- **MP-10 lifecycle generation guard / stale-lifespan safety** → Stage-B
  `test_feature_pack_integration` (3 pass) + test_rss_routes lifespan tests.
- **B1–B4 HDEncode resilience** → test_hdencode_coordinator / off_switch /
  priority / constructor_gate / rss_foundation (in broad).
- **SH-R09 public-error boundary** → test_public_error_boundary + client
  boundary unit; capture_public_exception on scrape/rename/downloads.
- **Action lifecycle / idempotency / cancellation / recovery / post-submit
  uncertainty** → test_hdencode_actions + test_hdencode_action_database
  (restart recovery: retrieving_links→queued, submitting→needs_review).
- **Source-post duplicate detection / capability correction** → test_hdencode
  actions + test_source_hdencode (no DIRECT_LINKS/CLOUDFLARE_BYPASS claim).
- **§5 invariant #7 single-writer over trash** → require_writer_lock grafted
  onto 11 entry points; test_runtime_lock (10) + trash suites (25).
- **Invariants #1/#8 no-replace + unsupported-fs fail-safe** → PR #15 +
  failsafe merged; test_trash_durability, fileops no-replace tests.
- **Auto-grab disabled by default; primary gated on readiness** →
  `hdencode_rss_auto_grab_enabled=False` default; test_rss_routes primary
  block-until-ready; test_feature_pack_integration primary_not_ready → 0 req.

## 5. Package reconciliations performed (disclosed to ChatGPT)

Every ChatGPT-authored package required real reconciliation its "validated"
label did not cover — all root-caused as intended-behavior / stale-test with
**zero production regressions**. Ledgers: `COMPLETION_RECONCILIATION.md`,
`ACTIONS_RECONCILIATION.md`.

- Completion (63-op): op[6] synthetic-anchor drift; 5 `replace_between` ops
  duplicate their END anchor under the package's own exclusive `apply.py`
  (un-appliable on ANY base); 12 sibling tests broken by shared-contract
  changes (all stale-test).
- Action/DNS (18-op): op[9] synthetic-anchor drift; recovery wired at service
  init not DB init (2 own-tests at wrong layer); feed-client wholesale replace
  dropped `validate_feed_url`; source-capability-correction test updates.

## 6. FINAL VERDICT

The complete feature pack is assembled on `agent/feature-pack-integration`
(`6939220`) and passes every gate this reviewer can execute: full backend
suite (3729/0 on Python 3.12), frontend typecheck/unit/build, `git diff
--check`, Stage-B cross-seam gates, file-safety cross-seam suites, additive
migration, security/lifecycle gates, and a code-level DNS-pinned-TLS review
confirming hostname verification is intact. No production regression was
found in any track.

Acceptance still depends on environment evidence that cannot be produced here
and must not be simulated: Python 3.11, UID 1000, Playwright isolation,
production-schema migration/rollback, the seven-day ≥20-cycle RSS shadow gate,
and the Jesse-authorized filesystem sentinel.

### **FEATURE PACK REQUIRES ENVIRONMENT EVIDENCE**

No merge, deploy, RSS-primary/auto-grab enablement, or production change has
been made; the integration branch is draft. The single remaining code item to
adjudicate before merge is the `fileops.py` single-writer × SH-R04 cross-seam
resolution (§1), which warrants an independent adversarial read since it was a
manual integration merge.
