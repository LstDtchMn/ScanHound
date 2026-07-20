# ScanHound feature-pack — PR C Stage A round-4 validation

**Reviewer:** Claude. **Date:** 2026-07-20.
**Package** `scanhound-pr-c-stage-a-round4.zip` SHA-256
`acb4737534dca98ecaa5d1b08e2c30950ad5477def92db811bd61a88484a72e1` — verified; nested
checksums OK. Target `agent/hdencode-traffic-coordinator` @ `8a48382`. 32 ops. Env: container
Py3.12. No merge/deploy/force-push/ready/production/sentinel.

## Round-4 fixed the two named residuals — but broke the broad suite

**Focused set (the 7 required files) is now green:**
- `--preflight-only` writes nothing; 32 ops apply; `git diff --check` clean; undetection strings
  3→0; `_detail_source_kind` restored; py_compile ok.
- 7-file focused set = **51 passed** (×2 deterministic). `test_detail_scraper_pacing.py` = **9/9**.
  DDLBase/Adit-HD bypass the coordinator and return real results; spoofed/malformed URLs still
  fail closed into the coordinator; constructor gate confines `cloudscraper.create_scraper(` to
  `hdencode_transport.py` only (0 in the 6 other owned modules) and correctly ignores `rt_scraper.py`.

**But the broad suite regresses — FP-C-3 (blocks commit).**
`pytest -q` (all minus test_api_routes) = **113 failed, 3479 passed** (×2 deterministic):

| File | Failed |
|---|---:|
| tests/test_scrapers_extended.py | 59 |
| tests/test_detail_scraper.py | 26 |
| tests/test_scrapers.py | 19 |
| tests/test_download_service.py | 8 |
| tests/test_fileops_dedupe.py | 1 |

All are `scrape_details(...)` (and the download path) returning **None** →
`result["res"]` / `result["hdr"]` `TypeError: 'NoneType' object is not subscriptable`, or
`assert result is not None`. Example (`test_scrapers_extended.py` alone) = **59 failed, 36 passed**.

**Root cause (recurring — 4th round):** PR C makes `scrape_details`/scanner/download depend on the
process-wide coordinator being explicitly configured. Round 4 made the 7 PR-C-owned test files
install a fresh coordinator, so they pass — but the 5 OTHER existing test files that call these
functions do NOT install one, so they get `None`/failures. These files test core scraping/detail/
download parsing that ships in production, so they pass at baseline `8a48382`; PR C's coordinator
dependency breaks them. PR C's own README requires `pytest -q` (the full suite) to be green; it is not.

**Required fix (break the per-round cycle):** give the coordinator a **lazily-initialized safe
default** so `scrape_details`/scanner/download work with no explicit configuration — in production
and in every existing test — instead of requiring each caller/test to install one. (Equivalently, a
root `tests/conftest.py` autouse fixture that installs a default coordinator for the whole suite, the
way PR A's conftest fix resolved the analogous global-lock problem.) Then run PR C's full README
7-file focused set AND `pytest -q` against a real checkout before resubmitting — the broad suite is
where all four rounds' residuals have surfaced.

## Stage status (unchanged except PR C)

Green & committed: PR #14 `932cefb`, PR #17 `3e60c24`, PR D `2145ef6`, **PR A `4bfe5ca`**,
**PR B `4d678bd`**. Validated green (commit deferred to assembly): PR #15-fs, PR J, PR E.
**PR C is the sole remaining Stage-A blocker** (now 4 rounds, all coordinator global-state).
It gates the RSS runtime-base (C+E → F/G/H/I) and Stage B integration. main untouched.

## Verdict

**FEATURE PACK ACCEPTED WITH REQUIRED FIXES** — one more PR C with a lazily-defaulting coordinator,
validated on `pytest -q` (not just the 7 owned files); then RSS F/G/H/I, Stage B, final review, and
the environment-gated evidence.
