# ScanHound feature pack — Stage B validation and final verdict

Reviewer: Claude (git/deploy/real-checkout validation lane).
Integration branch: `agent/feature-pack-integration`, assembled from
`origin/main` `555e26bc`. Head lineage (all pushed):

```text
Current draft branch head: 2915e3a   (evidence/report documentation)
Latest code-tested SHA:    a6b4a7b   (final runnable closure; unrestricted 3974/0)
Original Stage-B SHA:      6939220   (initial full-matrix integration; 3729/0)
```

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
| Single-writer graft (fileops.py) | independent adversarial STATIC review | **GRAFT SOUND** (see §5b) | code review |
| Migration (additive) | SCHEMA_VERSION 4→5→6; CREATE TABLE IF NOT EXISTS + guarded ALTER only; recovery on service init | verified additive, zero destructive ops | 3.12 fresh + reopen |
| Constructor / off-switch / lifecycle / coordinator priority | test_hdencode_constructor_gate / off_switch / priority / coordinator | passed (in broad) | 3.12 |
| Public-error boundary (SH-R09) | test_public_error_boundary + client.errors.test | passed | 3.12 + node |
| DNS-pinned TLS review | code review of `hdencode_feed_client._PinnedHTTPSConnection` | PASS — pinned-IP socket wrapped with `create_default_context` (check_hostname=True, CERT_REQUIRED) + `server_hostname`=approved host; SSRF IP rejection; 2 MiB bounded gzip; hostname verification NOT weakened | code review |

## 3. Validation matrix — ENVIRONMENT-GATED (reported honestly)

> NOTE: this section is the ORIGINAL Stage-B snapshot. Since then, UID 1000 and
> the Python 3.11 non-browser suite were executed and are GREEN — see §5d for
> the current, authoritative classification. The items below that remain open
> are Playwright, the full prod-migration matrix, the 3.11 browser subset, the
> 7-day shadow, and the sentinel.

- **Python 3.11** — non-browser suite now GREEN (3804/0, §5d); the
  browser-backed scraper subset is still PENDING (needs Chromium/ChromeDriver).
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

## 5b. Adversarial review of the one manual merge (fileops.py graft)

An independent skeptical review of the single-writer × SH-R04 resolution
returned **GRAFT SOUND**:
- all 11 `require_writer_lock()` calls are the first executable statement of
  their function (verified line-by-line);
- every externally-reachable trash mutation is covered — directly (the 11) or
  transitively (SH-R04 helpers `_reserve/_remove_reserved_trash_record`,
  `_begin/_complete/_clear_trash_operation`, `_restore_no_replace`,
  `_atomic_write_json` are called only from guarded functions; the api-route,
  service, and app_service entry points reach mutation only through guarded
  functions);
- no read/discovery path is over-guarded, and startup ordering is correct
  (`app_service.py:435` acquires the lifetime lock BEFORE the maintenance pass
  that calls `repair_trash_transactions`/`sweep_trash`);
- `require_writer_lock()` **raises** (fails closed), never blocks — no deadlock
  risk on a lock-free path.

**Important honesty note surfaced by the review:** the 3729/0 backend run does
NOT by itself validate this graft. `tests/conftest.py`'s autouse
`_unlocked_fileops_for_tests()` fixture bypasses `require_writer_lock()`
suite-wide, so the test suite would pass even with a misplaced guard. Guard
correctness therefore rests on (a) the adversarial STATIC analysis above and
(b) `test_runtime_lock` (10 pass), which exercises the lock mechanism itself
across processes. This should be re-confirmed by a human reviewer.

Also flagged (pre-existing, unrelated to the graft): a duplicate
`_fsync_directory` definition (lines ~125 and ~443) where the second shadows
the first — worth a follow-up cleanup, does not affect the invariant.

## 5c. Peer-review round (ChatGPT) — two runnable issues, both resolved

ChatGPT peer-reviewed this closeout and raised two runnable issues + a doc
correction. All addressed on head **`6e62cde`** (Python 3.12.13, root/uid=0,
`scanhound:latest`, pytest 9.1.1).

**Issue 1 — no-exclusion backend run.** `tests/test_api_routes.py` is runnable,
not env-gated. Ran the COMPLETE suite with no `--ignore`:
`pytest -q --deselect <3 network tests>` → **3964 passed, 4 skipped, 3
deselected, 0 failed** (514 s). The only tests that cannot run are
`TestDownloads::{test_download_valid_request, test_download_with_all_fields,
test_download_batch_valid}` — pre-existing integration tests that POST real
download requests to JDownloader/scrape with NO network mock; they require a
live JDownloader + internet the sandbox lacks. They are not stale-test
mismatches and not feature-pack code; the proper fix is a download-service mock
(test-infra follow-up, out of scope). NOTE: a first no-ignore pass with
`--timeout-method=signal` reported 8 failures, but 5 of those
(`test_download_service` scrape tests) were SIGALRM artifacts — they pass
normally (8/8). The `--ignore` was never needed for a code reason.

**Issue 2 — hydrated identity semantics.** Both claims confirmed with evidence
and fixed (see the "Identity safety" commit):
- `classify_candidate` promoted `{exact,high,hydrated}` → `exact` when no Plex
  match existed (raw `hydrated` became `exact` with no identity). Now promotes
  to `exact` only for prior `exact`/`high`, or a HYDRATED candidate that is
  also identity-confirmed via `_identity_is_confirmed()` (external id, or a
  complete non-conflicting tuple: movie `clean_title+year`, TV
  `clean_title+season+episode`; season packs / year conflicts need an explicit
  external id). Un-hydrated parsed title+year and raw `hydrated` stay
  unresolved.
- Auto-grab gate (`_validate_auto_action`) now accepts only `{exact, high}`.
- `tests/test_hdencode_identity_promotion.py` (14 tests) covers all 7 requested
  cases; identity/classify/action regression set = 62 passed. No sibling test
  was silently changed; auto-grab stays disabled by default + Jesse-gated.

**Documentation correction (accepted) — head lineage.**
- `6939220` — code-tested full-matrix head (backend broad 3729/0, frontend,
  gates). This is the authoritative code-tested integration.
- `05729f5`, `2d6ade3` — closeout/reconciliation DOCUMENTATION only (same code
  as `6939220`).
- `6e62cde` — adds the identity-safety CODE fix, validated by the complete
  no-ignore suite **3964/0** (3 network tests deselected) + the 62-test
  identity regression set. Frontend unchanged from `6939220` (backend-only fix).

## 5d. One Big Push — final runnable closure + environment gates

Executed the "One Big Push" plan Phases 0–4. **Code-tested SHA `a6b4a7b`**
(closure applied on `479299f`, then committed).

**Phase 1 — final runnable closure** (`scanhound-stage-b-final-runnable-closure`,
SHA-256 `4af08062…78c4002`, verified before apply; head + all 3 blob guards
matched exactly — authored against the real pushed head):
- year provenance: hydrated detail-page year persists as `description_year`;
  `title_year` (RSS parse) preserved → independent year sources, so the
  conflict check blocks promotion meaningfully;
- `_identity_is_confirmed` accepts either non-conflicting year source for movies;
- `TestDownloads` autouse fixture no-ops `BackgroundTasks.add_task` → the 3
  previously-deselected download-route tests now RUN with no scrape/JDownloader;
- `test_hdencode_year_provenance.py` — year provenance + conflict regressions
  (real SQLite);
- `test_fileops_writer_guard_contract.py` — AST contract asserting all **11**
  trash-mutation entry points guard-first, plus fail-closed dynamic tests with
  the global `_unlocked_fileops_for_tests` bypass DISABLED (closes the §5b
  honesty gap — the guards now have executable coverage).

**Phase 2 — unrestricted validation:**

| Gate | Command | Result |
|---|---|---|
| Full backend, NO `--ignore`/`--deselect`/signal-timeout | `pytest -q` | **3974 passed, 4 skipped, 0 failed** | Py 3.12.13, root |
| Frontend | check / vitest / build | 0 errors · **373 passed** · clean | node v24.14.0, npm 11.9.0 |
| `git diff --check` | — | clean |

**Phase 4 — environment gates (attempted now):**

| Track | Result |
|---|---|
| **A — Python 3.11 (non-browser suite)** | **PASS** — core modules import on 3.11.15; non-browser suite **3804 passed / 0 failed** (fresh `python:3.11-slim` + `requirements-docker.txt` + pytest-asyncio). |
| **A — Python 3.11 (browser-backed scraper subset)** | **PASS** — built `scanhound:py311` (the production Dockerfile with the runtime base changed to `python:3.11-slim-bookworm`, so it carries the same apt-provisioned matched browser: **Chromium 150.0.7871.124 + ChromeDriver 150.0.7871.124**) and ran the three suites: `test_scrapers` + `test_scrapers_extended` + `test_rt_scraper` = **170 passed / 0 failed** on Python 3.11.15. With the 3804 non-browser tests, **Python 3.11 is a full pass**. |
| **B — UID 1000** | **PASS** — full unrestricted suite as `1000:1000` = **3974 passed / 0 failed**, identical to root; app init, runtime-lock acquisition, trash transactions, and RSS/action recovery all correct as non-root. |
| **C — Playwright production E2E** | **PASS** — `npm run test:e2e` (production `preview` build + host backend `python -m backend.api --no-auth` in an isolated `SCANHOUND_E2E_DATA_DIR`, `reuseExistingServer:false` + `--strictPort`, desktop + mobile projects) = **18 passed / 0 failed**. A Windows-only teardown artifact (`EBUSY unlink crawler.db-wal` during temp-dir cleanup) was logged AFTER all tests passed — a Windows open-file-unlink limitation, not a test or state-isolation failure (isolation worked: a unique temp data dir per run). |
| **D — production-schema migration matrix** | PARTIAL — the additive path (SCHEMA v4→5→6, `CREATE TABLE IF NOT EXISTS` + guarded `ALTER`, zero destructive ops) and a clean reopen are proven in-suite. The full matrix (byte-for-byte production-DB copy, interrupt-during-migration, old-image reopen, documented rollback) requires a production-DB snapshot Jesse provides; the running production instance was not touched. |

## 6. FINAL VERDICT

The complete feature pack is assembled on `agent/feature-pack-integration`
(latest code-tested SHA `a6b4a7b`, the final runnable closure) and passes every
gate this reviewer can execute: the COMPLETE backend suite with NO exclusions
(no `--ignore`, no `--deselect`, no signal timeout) = **3974 passed, 0 failed**
on Python 3.12; frontend typecheck/unit/build; `git diff --check`; Stage-B
cross-seam gates; file-safety cross-seam suites; the additive migration path;
security/lifecycle gates; and a code-level DNS-pinned-TLS review confirming
hostname verification is intact. ChatGPT independently reviewed `a6b4a7b`:
**CODE CLOSURE ACCEPTED**, no new code defect. No production regression was
found in any track.

Environment gates executed and GREEN: **UID 1000** (3974/0), **Python 3.11 —
full** (3804 non-browser + 170 browser scraper on `scanhound:py311` with
Chromium/ChromeDriver 150), and **Playwright production E2E** (18/0). Only three
items remain, all requiring a production-DB snapshot or Jesse's explicit
authorization (must not be simulated):

- the full **production-schema migration** matrix
  (interrupt-during-migration / old-image reopen / documented rollback) — needs
  a byte-for-byte production-DB snapshot; the running instance was not touched;
- the **seven-day ≥20-cycle RSS shadow** gate (calendar-bound);
- the **Jesse-authorized filesystem sentinel**.

Every runnable and browser-backed validation gate is now green. The verdict
remains bounded by the calendar/operational gate and the production-data and
sentinel authorizations:

### **FEATURE PACK REQUIRES ENVIRONMENT EVIDENCE**

No merge, deploy, RSS-primary/auto-grab enablement, or production change has
been made; the integration branch is draft. The one manual integration merge
(the `fileops.py` single-writer × SH-R04 graft) has been independently
adversarially reviewed — **GRAFT SOUND** (§5b) — with the caveat that the test
suite bypasses the lock via an autouse fixture, so a human should re-confirm
the static analysis before merge. A pre-existing duplicate `_fsync_directory`
definition (§5b) is worth a follow-up cleanup.
