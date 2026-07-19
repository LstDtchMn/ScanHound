# ScanHound overnight engineering report — 2026-07-19

ChatGPT authored every implementation and test. Claude performed Git operations,
conflict resolution, independent review, validation, CI, and parent-versus-child
attribution.

**No merges. No force-pushes. No deployments. Nothing marked ready. `main`
untouched. The PR #3 deployment gate is intact.**

Package integrity: all 16 files verified against `SHA256SUMS.txt` — OK.

---

## 1. New SHAs and draft PR URLs

| PR | Branch | Base | Before | After |
|----|--------|------|--------|-------|
| #5 | `agent/hdencode-structured-outcomes` | `agent/hdencode-detail-pacing` | `c800a3f` | **`3b99985`** |
| #6 | `agent/hdencode-block-cancellation` | `agent/hdencode-structured-outcomes` | `b9c1e85` | **`b321dde`** |
| #7 | `agent/hdencode-source-health` | `agent/hdencode-block-cancellation` | `2abbbe6` | **`26e8ca7`** |
| #8 | `agent/hdencode-lazy-hydration` | `agent/hdencode-detail-pacing` | `c21d7df` | **`d8f36aa`** |
| #12 | `fix/same-volume-trash` | `fix/case-insensitive-dedupe` | `4e2b0c4` | **`8d01846`** |
| #13 | `fix/playwright-production-preview` | `fix/same-volume-trash` | `07dbfb0` | **`de00396`** |

All remain **draft**. Unchanged as required: `main` `58feedf`, PR #3 `397e52d`,
PR #4 `f72a554`, PR #9 `fac474b`, PR #10 `d425eb6`, PR #11 `81e5614`.

Review-only integration branches (pushed, no PRs opened):
`review/int-ci-pr3` `72714c4`, `-pr4` `3a9c21c`, `-pr5` `6ac9a4e`,
`-pr6` `0b7a4e0`, `-pr7` `b63a142`, `-pr8` `be0df36`.

---

## 2. Parent merges and conflict resolutions

| Merge | Result |
|-------|--------|
| #12 → #13 | clean, `2a95e6d` |
| #4 `f72a554` → #5 | **2 conflicts** in `backend/download_service.py` |
| #5 → #6 | clean |
| #6 → #7 | **2 conflicts** in `backend/api/routes/downloads.py` |
| #4 `f72a554` → #8 | clean |
| top CI stack → each of PR #3–#8 | all 6 clean |

### PR #5 conflicts — `scrape_links()`

Both hunks were the documented regression: PR #5 had reintroduced raw-substring
routing while PR #4 carried hostname-only classification.

Resolved to preserve **both** invariants — PR #3/#4 hostname-only gating and
dispatch, PR #5 structured `ScrapedLinks`/`ScrapeDiagnostic` returns:

```python
source_kind = _source_page_kind(url)
if source_kind == "hdencode" and not self.config.get("hdencode_enabled", True):
    diagnostic = ScrapeDiagnostic(
        ScrapeCode.SOURCE_DISABLED, retryable=False, affects_source_health=False)
    return ScrapedLinks(diagnostic=diagnostic)
...
if source_kind == "ddlbase":
    return ScrapedLinks(self._scrape_ddlbase_links(...))
if source_kind == "adithd":
    return ScrapedLinks(self._scrape_adithd_links(...))
```

Verified equivalent gating scope: `_source_page_kind` returns `"hdencode"` as
its default, so the off switch still covers every non-DDLBase/non-Adit-HD page,
exactly as the old `is_hdencode` boolean did — but from a parsed hostname.

### PR #7 conflicts — `downloads.py`

1. Competing imports — kept **both** (`record_scrape_outcome` and
   `ScrapeCode/ScrapeDiagnostic/ScrapedLinks`).
2. PR #7's substring health attribution vs PR #6's `except Exception as exc`.
   Kept both, deliberately leaving the substring form in place so
   `apply_pr7_hardening.py` could convert it to `_source_page_kind` itself
   (which it did).

Both merges were committed only after `git diff --check` was clean, no residual
conflict markers existed anywhere in `backend/` or `tests/`, and
`compileall` passed.

---

## 3. Changed files per commit

| Commit | Files |
|--------|-------|
| `8d01846` Persist discoverable trash roots | `backend/rename/fileops.py`, `tests/conftest.py`, `tests/test_rename_core.py` (+224) |
| `de00396` Make E2E authentication deterministic | `frontend/playwright.config.ts` (+15) **— partial, see §7.1** |
| `3b99985` Harden structured scrape outcome routing | `backend/download_service.py`, `backend/api/routes/downloads.py`, `tests/test_scrape_outcomes.py` |
| `b321dde` Expand confirmed-block cancellation coverage | `tests/test_scan_block_cancellation.py` |
| `26e8ca7` Harden source health attribution | `backend/api/routes/downloads.py`, `backend/api/routes/sources.py`, `backend/download_service.py`, `backend/source_health.py`, `tests/test_source_health.py` |
| `d8f36aa` Fail open on uncertain listing upgrades | `backend/scanner_service.py`, `tests/test_lazy_hydration.py` |

Every commit matched the handoff's expected file list exactly.

---

## 4. Validation results

### PR #12 — root and uid 1000

Reproduction shape matters. The original defect needed **both** a non-root uid
and a separately-mounted `/tmp`; a default container passes either way.

| Environment | Focused | Full `test_rename_core.py` + `test_rename_service.py` |
|---|---|---|
| root, `--tmpfs /tmp` | 27 passed | 228 passed, 1 failed* |
| uid 1000, `--tmpfs /tmp` | 27 passed | 228 passed, 1 failed* |
| root, same-device `/tmp` (CI shape) | 27 passed | **229 passed** |
| uid 1000, same-device `/tmp` (CI shape) | 27 passed | **229 passed** |

\* `test_trash_root_for_derives_from_source_anchor_not_data_dir`
(`assert '/tmp/.scanhound-trash' == '/.scanhound-trash'`). **Attributed:** fails
identically on parent `4e2b0c4` without the follow-up. It is an artifact of my
tmpfs harness making `/tmp` its own mount, which changes what
`_trash_root_for` walks to. Not a regression, and not present in CI's shape.

### PR #12 — direct API checks (uid 1000, pristine container)

Real unprivileged deep-root placement, no monkeypatched root list. The mount
root `/.scanhound-trash` was refused, the fallback selected
`/tmp/.scanhound-trash`, and after clearing the runtime set to simulate a
restart:

```
[PASS] used root persisted in index
[PASS] all_trash_roots() finds used root after restart sim
[PASS] list_trash_entries() sees the entry
[PASS] restore_trash_entry() ok, content intact, trashed copy gone
[PASS] delete_trash_entry() ok
[PASS] sweep_trash() removed the entry
[PASS] runtime set covers root when index unwritable
[PASS] same-process restore still works with an unwritable index
[PASS] corrupt index: non-.scanhound-trash path rejected, valid one accepted
[PASS] symlinked registered root rejected
[PASS] stale (deleted) registered root skipped without raising
```

### PR #13 and the full workflow

`npm run check` 0 errors (3 pre-existing a11y warnings), `npm run build` clean.

`CI=1 npx playwright test` — **18/18 passed in 19.5s** with no environment
variables set by me, on a host whose real database **has** a password. Isolation
evidence without printing credentials: the E2E backend logged
`Recovered 0 download history entries` (the real database has 1268) and never
connected to Plex; the data directory was
`%TEMP%\scanhound-playwright-36696-1784453775204`, confined to the OS temp dir.

**Dispatched `Tests` on PR #13 — run
[29681915627](https://github.com/LstDtchMn/ScanHound/actions/runs/29681915627):**

| Job | Result |
|-----|--------|
| `test (3.11)` | **success** — 3768 passed, 4 skipped |
| `test (3.12)` | **success** — 3768 passed, 4 skipped |
| `frontend` | **success** — 25 unit files, **18/18 E2E in 15.3s** |

This is the **first fully green workflow in the project's recent history**, and
the first time the 3.12 job has ever completed rather than being cancelled.

### HDEncode module validation

| PR | Command | Result |
|----|---------|--------|
| #5 | off-switch + pacing + outcomes + download service | 200 passed, **2 failed** (pre-existing) |
| #6 | cancellation + outcomes + pacing | **22 passed** |
| #7 | source health + outcomes + cancellation | 22 passed, **1 failed** (see §7.2) |
| #8 | lazy hydration + pacing + background + off switch | **48 passed** |

PR #5's two failures are `test_download_item_force_bypasses_*` —
`ModuleNotFoundError: No module named 'PySide6'`. **Attributed:** both fail
identically on unmodified `origin/main`. Pre-existing, unrelated.

PR #7's `tests/test_api_routes.py` could not be completed locally — it hangs in
my offline container (a known property of that module, no network mocking). CI
runs it successfully, so CI is the authority there.

### Cross-version compile (real interpreters, not the host)

| Branch | py3.11 | py3.12 |
|--------|--------|--------|
| `fix/same-volume-trash` (#12) | OK | OK |
| `fix/playwright-production-preview` (#13) | OK | OK |
| #5, #6, #7, #8 | **FAIL** | OK |
| `review/int-ci-pr3 / -pr5 / -pr7 / -pr8` | **OK** | OK |

The HDEncode branches' 3.11 failure is the original bug, unchanged:

```
backend/api/routes/rename.py:604
    (f', {result['skipped']} already tracked' if ...)
SyntaxError: f-string: f-string: unmatched '['
```

They branch from `main`, which still carries it; only PR #9 fixes it. The
integration branches compile on both versions precisely because they include
the CI stack. **This is direct evidence that the CI-stabilization stack is a
hard prerequisite for the HDEncode stack**, independent of any CI run.

### Integration branch CI

All six review branches were patched with `fail-fast: false` (review-only, so
both Python jobs finish) and dispatched. **All six runs completed**; every
`frontend` job succeeded; every backend job failed on both 3.11 and 3.12.

Crucially, the failures are *only* the two already-attributed blockers — no new
or unexpected failure appeared anywhere:

| Integration branch | 3.12 result | Failing tests |
|---|---|---|
| `int-ci-pr3` | 1 failed, 3779 passed, 4 skipped | config allowlist (§7.3) |
| `int-ci-pr4` | 1 failed, 3786 passed, 4 skipped | config allowlist (§7.3) |
| `int-ci-pr5` | 1 failed, 3795 passed, 4 skipped | config allowlist (§7.3) |
| `int-ci-pr6` | 1 failed, 3801 passed, 4 skipped | config allowlist (§7.3) |
| `int-ci-pr7` | 2 failed | config allowlist (§7.3) **+** source-health contradiction (§7.2) |
| `int-ci-pr8` | 1 failed | config allowlist (§7.3) |

Both Python jobs completed on every branch, confirming `fail-fast: false` did
what it was set for. The rising pass counts (3779 → 3801) track each HDEncode
layer's added tests, all of which pass.

The practical reading: **once the two blockers are fixed, the whole combined
project is expected to be green.** Every HDEncode layer integrates cleanly with
the CI-stabilization stack; nothing in PRs #4–#8 introduces a failure of its
own.

---

## 5. Answers to the review questions

### PR #12 — trash discovery

1. **Registered before bytes move?** Yes. `_record_trash_root(root)` is inserted
   before `dedupe_dest()` and the move, closing the placement-to-index window.
2. **Every successful placement discoverable?** Yes. All three success paths in
   `_trash()` (atomic rename, same-volume `shutil.move`, app-data fallback)
   return via `_finish_trash_move`, which calls
   `_record_trash_root(os.path.dirname(bucket))`.
3. **Index write fails — same-process rollback still works?** Yes.
   `_TRASH_ROOTS_RUNTIME.add()` happens inside the lock *before* the persistence
   attempt, and `OSError` is caught and logged. Verified: scenario 5 passed with
   an unwritable index path.
4. **Corrupt index inject a non-`.scanhound-trash` path?** No.
   `_normalize_registered_trash_root` requires an absolute path whose final
   component casefolds to exactly `.scanhound-trash`. Verified with an index
   containing a plain directory, a relative path, `""`, `None`, and an int.
5. **Symlink roots rejected on every load?** Yes — the `os.path.islink` check
   lives in the normalizer, which runs on both persisted and runtime entries
   every time `_load_registered_trash_roots()` is called, not once at write time.
6. **Persistence atomic, thread-safe, harmless?** Yes. `threading.RLock`,
   write-to-temp + `flush` + `os.fsync` + `os.replace`, temp cleaned in
   `finally`, all failures swallowed with a warning. It runs after the move has
   already completed, so it cannot affect the move's outcome.
7. **Overwrite rollback finds a deeper root via `all_trash_roots()`?** Yes —
   `roots.update(_load_registered_trash_roots())`. This is the actual fix: the
   three previously failing `test_rename_service.py` restore-safety tests now
   pass, in CI and locally at both uids.
8. **List/delete/sweep after restart?** Yes — verified individually after
   clearing the runtime set (scenarios 1, 3, 4).
9. **Tests avoid the user's real index?** Yes. `tests/conftest.py` gains an
   `autouse=True` fixture monkeypatching both `_TRASH_ROOTS_INDEX` (to
   `tmp_path`) and `_TRASH_ROOTS_RUNTIME` (to a fresh set) for **every** test.
10. **Stale roots skipped safely?** Yes — `all_trash_roots`,
    `list_trash_entries` and `sweep_trash` all tolerated a registered root whose
    directory no longer exists, without raising.

### PR #13 — E2E authentication

1. **Does `--no-auth` set an empty nonce and `SCANHOUND_ALLOW_OPEN=1` before
   `create_app()`?** **Not applied — this is a production auth regression.** See
   §7.1. The E2E goal is met by the Playwright `webServer.env` alone.
2. **Configured password still authoritative?** Yes, unchanged: `has_password`
   still drives `auth_required` regardless of open mode.
3. **Normal startup still fail-closed?** Yes — because the `__main__.py` change
   was **not** applied, `SCANHOUND_ALLOW_OPEN` remains unset by default and the
   documented `docker/entrypoint.sh` contract still holds.
4. **Preconfigured nonce unchanged in normal mode?** Yes (untouched code path).
5. **Unique temporary config/DB/data dir per spawned backend?** Yes.
   `E2E_DATA_DIR = join(tmpdir(), \`scanhound-playwright-${process.pid}-${Date.now()}\`)`
   is exported as `SCANHOUND_DATA_DIR`, `SCANHOUND_DB_DIR`, `APPDATA` and
   `LOCALAPPDATA`. Empirically confirmed: 0 recovered history rows vs the real
   1268, and no Plex connection.
6. **CI uses `vite preview`, local uses `vite dev`?** Yes — measured from
   Playwright traces: CI mode served 0 `@vite/client` and 74 `_app/immutable`
   references; local mode served 4 and 0.
7. **`CI=1` never reuses existing servers?** Yes —
   `reuseExistingServer: !process.env.CI` is `false` under CI.
8. **`/auth/status` reports `setup_required: false`?** Yes, by way of
   `SCANHOUND_ALLOW_OPEN=1` scoped to the test webServer only.
9. **All E2E render intended routes, not `/login`?** Yes — 18/18 locally and
   18/18 in CI; previously 12 failed with the fallback title `App | ScanHound`.
10. **Temp paths confined to the OS temp dir?** Yes — `node:os.tmpdir()`.

### PR #5 — structured outcomes

1. **Path/query bypass?** No. All routing goes through
   `_url_matches_domain`, which parses with `urlparse`, takes `.hostname`
   (dropping credentials and port), lowercases, strips a trailing dot, and
   matches `host == d or host.endswith("." + d)`. `?next=https://ddlbase.com`
   cannot influence it.
2. **Disabled HDEncode returns `source_disabled` before Selenium?** Yes — the
   gate precedes `_ensure_selenium()` and any driver acquisition.
3. **DDLBase/Adit-HD hosts and subdomains still routed independently?** Yes;
   subdomains are matched by the `.endswith("." + d)` arm.
4. **Every empty outcome carries a diagnostic?** Yes for the paths this commit
   touches.
5. **Batch exceptions surface `scrape_exception`?** Yes —
   `downloads.py` now builds `ScrapeCode.SCRAPE_EXCEPTION` instead of `links = []`.
6. **Local failures excluded from source health?** Yes —
   `affects_source_health=False` on `BROWSER_LAUNCH_FAILED`, `SOURCE_DISABLED`
   and `SCRAPE_EXCEPTION`.
7. **Can a title containing "Captcha"/"Access Denied" false-positive?** No, and
   this is the substantive improvement: the blanket `"captcha" in low` marker
   over the whole document is gone. Detection now requires a *technical* marker
   (`cf-chl`, `challenges.cloudflare.com`, `turnstile`, `hcaptcha`, `recaptcha`)
   anywhere, or a specific phrase in `driver.title`, or visible body text. A
   release named `Captcha.2024.1080p` no longer trips it.
8. **Classification based on strong evidence?** Yes — technical markers, title
   evidence, visible body text, or an actual captcha iframe.
9. **Can diagnostics disclose sensitive data?** **Residual risk, minor.**
   `ScrapeDiagnostic.to_dict()` emits `message`, which is `detail or
   _MESSAGES[code]`, and the new batch handler sets
   `detail=f"Batch link scrape failed: {exc}"`. That reaches API consumers via
   `downloads.py:253` and `:306`. Exception text can embed local paths (e.g. a
   chromedriver path). This mirrors a pre-existing pattern
   (`BROWSER_LAUNCH_FAILED` already did this), so it is not a new class of
   problem, but it is worth a deliberate decision. `signals` is safe — it
   carries only `type(exc).__name__`.
10. **Retry/recycle paths release locks and driver state?** The touched paths
    return inside the existing `with self._driver_lock:` / `finally` structure,
    which is preserved. Not independently stress-tested.

### PR #6 — cancellation

1. **Threshold exactly three consecutive 403/429/503?** Yes, per the added tests.
2. **HTTP 200 resets it?** Yes — explicitly covered by a new test.
3. **404/500 avoid shared cancellation?** Yes — non-block statuses are ignored.
4. **Only queued futures cancelled?** Consistent with the design; the added
   tests assert queued-future cancellation while in-flight work completes.
5. **Explicit stop doesn't poison the next source/scan?** Per the design note,
   one `run_scan` invocation handles one source type and the background scanner
   invokes sources separately, so a stop is scoped to the active invocation.
6. **Stop event reset per background source invocation?** Yes.
7. **Incomplete-crawl purge protection preserved?** Yes — untouched by this
   commit and still covered by `test_background_scanner.py`.
8. **Can a health/DB write failure block the stop flag or `break`?** The PR #7
   hardening makes health advisory and fail-open, which removes the DB-write
   path as a blocker. I did not fault-inject this specific interleaving.
9. **Scan-slot and `_running` released on every exception path?** Structurally
   yes (`finally` blocks retained); not exhaustively fault-injected.

Items 4, 8 and 9 are **reviewed by reading, not proven by execution** — stated
plainly rather than claimed as verified.

### PR #7 — source health

1. **Hostname-only attribution at every call site?** Yes. Both the single and
   batch sites now use `_source_page_kind(...) == "hdencode"`; the repo-wide
   sweep in §6 found no remaining substring-based routing decision.
2. **Advisory failure can't break `/sources`?** Yes — `get_source_health()` is
   wrapped in `try/except` that logs and falls back to `{}`, with the comment
   "Health is advisory."
3. **Success clears streak/cooldown without erasing history?** Yes. The upsert
   sets `state='healthy'`, `reason_code=NULL`, `consecutive_failures=0`,
   `cooldown_until=NULL` and updates `last_success_at`, while
   **`last_failure_at` is deliberately absent from the SET list**, so the
   historical failure timestamp survives.
4. **Expired/malformed cooldown projected as degraded?** Yes.
   `effective_health_state()` returns `degraded` for an expired timestamp, a
   missing one, or one that fails `fromisoformat` — without a DB write.
5. **Reachable-empty clears a stale block?** Yes — and this is the source of the
   test contradiction in §7.2.
6. **Local browser/DNS/launch failures can't blame the source?** Yes, via
   `affects_source_health=False`.
7. **Health writes bounded to one row per source?** Yes —
   `source TEXT PRIMARY KEY` with `ON CONFLICT(source) DO UPDATE`.
8. **Restart/concurrent-write tests preserve timestamps and counts?** Covered by
   the package's own tests, which pass apart from the §7.2 contradiction.
9. **Can reason codes leak sensitive content?** Reason codes are a closed enum;
   the leak surface is `detail`/`message`, see PR #5 answer 9.

### PR #8 — lazy hydration

1. **Omitted DV token suppress a real DV upgrade?** No — missing DV evidence
   under an active DV rule now fails open and hydrates.
2. **Missing size suppress a size upgrade?** No — same fail-open treatment.
3. **HEVC/HDR10+ decided without detail metadata?** No — those also hydrate.
4. **Best owned quality used, not the latest row?** Yes — comparison is against
   best owned quality via the Plex index.
5. **Malformed/uncertain/unmatched/exception paths hydrate?** Yes — the function
   returns `True` (hydrate) on every uncertain branch; it is fail-open by
   construction.
6. **Non-HDEncode sources never filtered?** Yes, first statement in the gate:
   `if post_info.get("source") != "hdencode": return True`.
7. **`4K` vs `2160p` normalization mismatch?** Handled by the shared release
   parser rather than raw string comparison.
8. **Exact-URL history the only unconditional shortcut?** Yes —
   `if url and url in self.download_history` is the single unconditional skip.
9. **Active user rules decide sufficiency?** Yes — the gate consults active
   upgrade rules, not hard-coded defaults.
10. **Still useful, metrics honest?** The optimization survives (48/48 tests
    pass); I did not measure the real-world avoided-request rate, and the
    conservative fail-open checks will reduce it by design.

---

## 6. Repository-wide source-routing audit

Every occurrence of `ddlbase.com`, `adit-hd.com`, `hdencode.org` in
`backend/**/*.py`, classified:

**Safe parsed-hostname matching (control flow)**
- `download_service.py:109,111` — `_url_matches_domain()` via `urlparse.hostname`
- `detail_scraper.py:50,52` — `host == "…" or host.endswith(".…")`
- `api/routes/scanner.py:106-108` — `_is_hdencode_url()` on a normalized parsed
  hostname with `www.` stripped

**Fixed source URL construction (no classification)**
- `config.py:532` (`base_url` default), `scanner_service.py:417`
- `scanner_service.py:556-567` — the six DDLBase/Adit-HD source definitions
- `sources/hdencode.py:24`, `sources/ddlbase.py:51`, `sources/adithd.py:30-31`
- `scanner_service.py:714-716` — absolutizing a relative `href`; the branch is
  keyed on the internal `source_id`, **not** on URL text, so it cannot be
  spoofed by page content

**Display / logging / comments only**
- `downloads.py:157`, `download_service.py:1448`, `sources/adithd.py:3`

**Defects: none.** After the PR #5 and PR #7 hardening, no control-flow decision
anywhere in the backend routes on a raw URL substring.

**Advisory-system isolation:** health writes are wrapped so failures cannot
break `/sources`; `affects_source_health` keeps local faults out of source
state; notifications are emitted over WebSocket after the fact. No advisory
path gates scraping, delivery, cancellation, or API availability.

---

## 7. Concrete defects, with attribution

### 7.1 BLOCKER — `apply_pr13_no_auth_contract.py` opens the production API

The script rewrites `backend/api/__main__.py` so `--no-auth` sets
`SCANHOUND_ALLOW_OPEN=1` process-wide. **`docker/entrypoint.sh:51` launches the
production container with exactly that flag:**

```
exec python -m backend.api --host 0.0.0.0 --port 9721 --no-auth
```

`entrypoint.sh:39-50` documents the opposite contract in prose — "`--no-auth`
only disables the desktop-sidecar nonce … it does NOT disable the app's own
password gate" — which the SH-H01 remediation established deliberately.

Measured on a credential-free database with no token presented:

```
current behaviour        GET /settings -> 401   (fail closed)
with the proposed patch  GET /settings -> 200   (fully open on 0.0.0.0)
```

That is exactly the state the 2026-06-29 credential-wipe incident produced. The
script's own new test `test_no_auth_enables_the_existing_open_escape_hatch`
codifies the unsafe behaviour.

**Action taken:** the `backend/api/__main__.py` half was **not applied** and
`tests/test_api_entrypoint.py` was **not created**. The Playwright
`webServer.env` half *was* applied and is sufficient on its own — 18/18 locally
against a password-protected host database, and 18/18 in CI. Disclosed in the
`de00396` commit message and in the PR #13 body.

**Needed from ChatGPT:** a corrected script that isolates E2E without touching
the CLI's production contract.

### 7.2 BLOCKER — PR #7 package contains two contradictory tests

`tests/test_source_health.py` now asserts opposite outcomes for the same input
(a `REQUESTED_HOST_MISSING` diagnostic carrying `affects_source_health=False`):

| Test | Origin | Result |
|------|--------|--------|
| `test_reachable_empty_page_clears_stale_blocked_state` | added by the script | **passes** |
| `test_non_health_affecting_diagnostic_does_not_change_state` | pre-existing, untouched | **fails** |

The new reachable-empty semantics is the stated intent, so the pre-existing test
encodes the superseded contract. Which contract governs is a semantics decision,
so **neither test was edited**. Committed with the failure documented in
`26e8ca7`'s message and the PR body.

### 7.3 BLOCKER — PR #3 has a latent failing test, and it is the deployment-gate PR

`tests/test_config.py::TestDefaultConfig::test_default_config_has_no_unexpected_keys`

```
AssertionError: Unexpected keys in _DEFAULT_CONFIG: {'hdencode_enabled'}
```

PR #3 adds `hdencode_enabled` to `backend/config.py` (2 occurrences) but never
adds it to the test's expected-key allowlist (0 occurrences on that branch).

**Parent-versus-child, executed:**

```
origin/main (58feedf)                     -> 1 passed
origin/agent/hdencode-off-switch (397e52d) -> 1 failed
```

This is **PR #3's own defect**, not an integration artifact. It has been latent
since the branch was created because CI never completed a 3.12 job on it — 3.10
and 3.11 died at the f-string `SyntaxError` during import, and fail-fast
cancelled 3.12. The `review/int-ci-pr3` run is the first execution that ever got
far enough to observe it, and it failed on both 3.11 and 3.12 with
`1 failed, 3779 passed, 4 skipped`.

Because PRs #4–#8 all descend from PR #3, every integration branch inherits it.

**This matters for the release gate:** PR #3 is the PR that deploys alone. It
should not be deployed with a known-failing test in its own config surface.

### 7.4 Two package scripts had stale markers

- `apply_pr13_no_auth_contract.py` — "isolated E2E data directory" marker
  expected a `playwright.config.ts` comment that PR #13 had already reworded;
  found 0, aborted before writing (correct fail-closed behaviour).
- `apply_pr5_hardening.py` — its "hostname-only source gate"/"dispatch" markers
  expected the *unresolved* PR #5 substring text, i.e. it assumed the merge
  would be resolved in PR #5's favour and then hardened. I had already resolved
  to the hostname-only form.

**Action taken:** for PR #5 I aligned my merge resolution to the script's exact
canonical text (commit `21a7629` + a small follow-up) so `replace_or_confirm`
could confirm it and apply the remaining six hardening steps unmodified. For
PR #13 I transcribed the config changes exactly as the script specified.
Both are disclosed rather than silent.

### 7.5 Minor — diagnostic `detail` reaches API consumers

See PR #5 answer 9. Not fixed; recorded for a decision.

### 7.6 Environment-only, not defects

- `test_trash_root_for_derives_from_source_anchor_not_data_dir` fails under a
  tmpfs `/tmp`; fails identically on the parent (§4).
- `test_download_item_force_bypasses_*` fail with `ModuleNotFoundError: PySide6`;
  fail identically on `origin/main` (§4).
- `tests/test_api_routes.py` hangs in my offline container; passes in CI.

---

## 8. Confirmation of protections

- **No merges** into any production branch; the only merges performed were the
  prescribed parent-into-child merges and the review-only integration merges.
- **No force-pushes** — every push was fast-forward.
- **No deployments.** Nothing was built or restarted; the running container is
  untouched.
- **No PR marked ready** — all 11 remain draft.
- **`main` unchanged** at `58feedf`; the working checkout is clean.
- **PR #3 `397e52d`, PR #4 `f72a554`, PR #9 `fac474b`, PR #10 `d425eb6`,
  PR #11 `81e5614`** all unchanged.
- **Deployment gate intact:** PR #3 deploys alone, HDEncode disabled, and one
  complete real background-scan cycle must show zero HDEncode listing, detail,
  Selenium and pipeline-search traffic before PRs #5–#8 advance. Nothing in this
  report advances that gate — and §7.3 gives a concrete reason to fix PR #3
  before it is deployed.

---

## 9. Recommended order

1. **PR #3** — allowlist `hdencode_enabled` in `tests/test_config.py` (§7.3).
   Blocks the deployment gate.
2. **PR #13** — corrected `__main__.py` script, or formally drop that half (§7.1).
3. **PR #7** — decide which source-health contract governs (§7.2).
4. Then re-run the integration branches.

The CI-stabilization stack (#9 → #13) is otherwise **green end to end** and is a
hard prerequisite for the HDEncode stack, which cannot even compile on Python
3.11 without it.
