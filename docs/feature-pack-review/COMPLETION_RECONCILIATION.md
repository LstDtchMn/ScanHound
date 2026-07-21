# RSS completion-corrective (63-op) — reconciliation ledger

Base: PR I validated head `a309027c` (branch `agent/hdencode-rss-completion`).
Package: `rss-completion-corrective.zip` (63 ops; expected_head placeholder →
`a309027c`). apply.py guards = head+branch only (no expected_blobs); no
`--skip-head-check` flag exists in this applier.

Per-op anchor pre-check across all 63 ops: **62 matched, 1 missed** (op[6]).
But 5 `replace_between` ops passed the anchor-existence check yet still
produced broken code (their failure mode is span-content, not anchor
existence). Three defects found + fixed:

## Defect 1 — op[6] anchor drift (`replace`, backend/database.py)
- Package anchor `                # ── Versioned migrations ─────…` does NOT
  exist in the real integrated tree (it is a ChatGPT-synthetic-base marker).
- Real tree: `SCHEMA_VERSION = 4` (op[5] bumps → 5), migration section uses a
  `_column_migrations` list + `if current_version < N:` blocks, ending at
  `                # ── Stamp current version ─────…` right before
  `PRAGMA user_version`.
- Fix: re-anchored op[6] to insert the v5 block (hdencode_shadow_cycles /
  hdencode_shadow_misses / index + guarded hdencode_candidates ADD COLUMN
  imdb_id/tmdb_id/discovery_source) immediately before
  `# ── Stamp current version ─` — i.e. AFTER PR E's RSS tables exist (so the
  ALTERs succeed), before the version stamp. Verified: `SCHEMA_VERSION = 5`,
  v5 comment lands at db.py:861, shadow tables + discovery_source present.

## Defect 2 — all 5 `replace_between` ops duplicate their END anchor
- The package's own apply.py uses EXCLUSIVE semantics:
  `text = text[:a] + new + text[b:]`, where `b = find(end)` — so `text[b:]`
  already contains the end anchor.
- But every one of the 5 ops re-emits the end anchor at the tail of `new`:
  - op[7]  db.py get_hdencode_candidate_context → END `def enqueue_hdencode_hydration(...)`
  - op[8]  db.py get_hdencode_rss_readiness (+aggregates) → END `# ── Plex cache ─`
  - op[9]  hdencode_candidate_service _candidate_updates → END `def _size_gb(value):`
  - op[42] hdencode_coordinator priority acquire → END `token = None`
  - op[57] rss/+page.svelte remove evidence()/reasonLabel() → END `onMount(() => {`
- Result under the shipped applier: each end anchor appears TWICE →
  `def enqueue_hdencode_hydration` / `def _size_gb` with no body →
  `IndentationError`. This breaks on ANY base incl. ChatGPT's own; the
  "validated" claim on the package does not hold against its own apply.py.
- Fix: stripped the re-emitted trailing end anchor from each op's new
  (payload files for 7/8/9; inline `new` for 42/57). Applier left
  byte-identical. Post-fix: preflight passes, `git diff --check` clean,
  backend+tests AST OK.

## Defect 3 — stale PR I route-test fixtures (tests/test_rss_routes.py)
- Completion moved unknown-count computation out of the /status route into
  `db.get_hdencode_rss_dashboard_counts()` and added
  `db.get_hdencode_shadow_summary()`, and wired
  `reg.backend.add_shutdown_hook(...)` (real method, app_service.py:807) to
  join RSS hydration threads at shutdown. test_rss_routes.py is PR I's file,
  outside the completion's changed-paths, so its `Db`/`backend` mocks were
  stale → 2 failures (AttributeError on the two missing methods).
- Fix (validation-lane fixture update, no production code touched): added
  `Db.get_hdencode_rss_dashboard_counts` (returns dv=1/identity=1/
  year_conflict=1 mirroring the mock's single unknown candidate) +
  `Db.get_hdencode_shadow_summary` + `backend.add_shutdown_hook` to the mock.

## Defect 4 — completion breaks 12 sibling tests it did not update
The completion changes SHARED backend contracts but its changed_paths do not
include the earlier-layer test files that assert the old contracts. Full broad
after apply: **12 failed, 3659 passed**. All 12 root-caused as stale sibling
tests (NOT production regressions — each new behavior confirmed correct-by-design
against production code); Jesse authorized (dialog) reconciling them in-lane.

- **test_dv_settings (1)** — HARNESS false positive: my broad container had not
  re-copied /work/frontend; the test reads settings/+page.svelte. Passes once
  frontend is staged. No code/test change.
- **test_scan_block_cancellation (4)** — op[21] adds `_last_crawl_request_count`
  to ScannerService.__init__ and op[23] increments it in _fetch_page BEFORE the
  scraper call. The test's `_scanner_shell()` builds via __new__ (skips __init__)
  and lists attrs manually → AttributeError swallowed as a failed fetch →
  scraper.calls==0. Fix: shell sets `_last_crawl_request_count = 0`. Production
  __init__ already sets it — no prod issue.
- **test_hdencode_candidate_service (2)** — op[9] rewrote `_candidate_updates` to
  sparse authoritative-only updates and identity_state="hydrated" (gated on
  payload url+display_title), replacing the old unconditional "exact". Confirmed
  correct: real detail_scraper.scrape_details returns `'url'` (detail_scraper.py
  L388/393), so the gate is satisfied in production. Fix: added `"url"` to the two
  test Detail mocks; assertions "exact"→"hydrated".
- **test_hdencode_rss_primary (3)** —
  (a) DDLBase test: op[29] "RSS-only when cache off" overrides sources=["HDEncode"]
      when rss_active and background_scan_enabled falsy → primary suppresses it →
      0 crawls. Fix: Registry sets `background_scan_enabled: True` (models cache-on,
      the scenario the test actually probes). op[29] is intended.
  (b) shadow-comparison test: shadow crawl calls new real
      `db.record_hdencode_shadow_comparison`; test Db mock lacked it. Fix: added.
  (c) readiness test: op[8] readiness now reads hdencode_shadow_cycles (span first→
      last >=7d, >=20 complete cycles, zero misses, proven request reduction, >=1
      recovery) — not hdencode_ingest_cycles. Fix: rewrote setup to insert
      conforming shadow_cycles; reason string "normal_feeds_unhealthy"→
      "normal_feeds_unhealthy_or_stale". Stricter gate is intended.
- **test_hdencode_rss_shadow (2)** — shadow poll calls new real
  `db.list_hdencode_current_feed_urls`; FakeDb lacked it. Fix: added (+ 
  record_hdencode_shadow_comparison for safety).

Post-reconciliation: the 5 affected files = **44 passed, 0 failed**.

## Verification (base a309027c + completion, reconciled)
- Focused: test_hdencode_rss_completion + test_hdencode_priority +
  test_config + test_rss_routes = **119 passed**.
- Frontend: check 0 errors (350 files), vitest **367 passed** (26 files,
  +status.test.ts), build clean.
- Backend broad: (in progress — recorded on completion).
