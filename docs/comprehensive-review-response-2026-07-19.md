# ScanHound comprehensive-review correction & verification report — 2026-07-19

ChatGPT authored the review and all five correction scripts. Claude performed Git
operations, independent reproduction, review, validation, CI, and reporting.

**Not a deployment. Nothing merged, deployed, marked ready, or force-pushed. PRs
#4–#8 not advanced beyond their prescribed corrections. Production state
untouched — the container is still on its pre-existing image.**

Package integrity: all 16 files verified against `SHA256SUMS.txt` — OK.

---

## 1. Executive summary

| Phase | Outcome |
|---|---|
| 1 — PR #3 fail-closed switch | **HELD — child-only regression in the supplied script** |
| 2 — main E2E isolation | Done, PR #14 draft |
| 3 — PR #5 signal sanitization | Done, one residual noted |
| 4 — PR #6 cancellation-aware details | Done, SH-R04 decisively reproduced and fixed |
| 5 — PR #8 preference guard | Done |
| 6 — reproduce broader findings | See §6 |
| 7 — review-only integrations | See §7 |

**SH-R01 is real and I reproduced it before accepting the fix.** **SH-R04 is real
and severe — the legacy path issued 9 requests after cancellation.** **SH-R06 is
real.** ChatGPT's review is substantially correct.

**One blocking defect in the supplied Phase 1 script** stops PR #3 from being
ready to return to the production gate (§4.1).

---

## 2. New SHAs and exact file lists

| PR | Branch | Before | After | Files |
|----|--------|--------|-------|-------|
| #3 | `agent/hdencode-off-switch` | `cb7dc48` | **`cb7dc48` (unchanged — held)** | — |
| #5 | `agent/hdencode-structured-outcomes` | `327a97d` | **`5bea70f`** | `backend/download_service.py`, `tests/test_scrape_outcomes.py` |
| #6 | `agent/hdencode-block-cancellation` | `25b821a` | **`68c8049`** | `backend/detail_scraper.py`, `backend/scrapers.py`, `backend/scanner_service.py`, `tests/test_detail_scraper_pacing.py` |
| #8 | `agent/hdencode-lazy-hydration` | `cee0d23` | **`c1a7f7b`** | `backend/scanner_service.py`, `tests/test_lazy_hydration.py` |
| #14 | `fix/e2e-full-state-isolation` | new | **`be52908`** | `frontend/playwright.config.ts` |

Parent merge SHA: PR #5 → PR #6 = `684b482` (clean, zero conflicts).

PR #4 (`fb99d49`) and PR #7 (`9d29717`) unchanged — no correction was prescribed
for them. Every commit matched its script's declared file list exactly; no
changed-file expansion occurred in Phases 2–5.

---

## 3. Independent reproduction of the review's own findings

I reproduced each blocking finding **before** applying its fix, rather than
accepting the review's assertion.

### SH-R01 — confirmed, 8 of 15 values wrong

Exercising the exact production gate expression
(`not cfg.get("hdencode_enabled", True)`) at PR #3 head `cb7dc48`:

| Value | Expected | Actual |
|---|---|---|
| `"false"` | disabled | **ENABLED** |
| `"False"` | disabled | **ENABLED** |
| `"0"` | disabled | **ENABLED** |
| `"off"` | disabled | **ENABLED** |
| `"no"` | disabled | **ENABLED** |
| `"banana"` | disabled | **ENABLED** |
| `["x"]` / `{"a":1}` | disabled | **ENABLED** |

This is exactly the operator-deception path the review describes: the config file
reads `false` and HDEncode scrapes anyway. Real, high severity, correctly called.

### SH-R04 — confirmed, and worse than a theoretical race

High-fidelity harness: 10 worker threads, 3 limiter slots, cancel while waiters
are blocked, count requests whose start timestamp is after the stop timestamp.

```
TRIAL A (legacy, no stop callback):
  requests before cancel: 1   total: 10   AFTER cancel: 9
```

**Nine requests were issued after cancellation.** With the corrected slot: zero.

### SH-R06 — confirmed at source

`backend/download_service.py:1310` was
`signals.extend(f"iframe:{src}" for src in captcha_frames[:5])` — the complete
iframe URL including query string, published through `to_dict()["signals"]`.

---

## 4. Concrete defects found in the supplied scripts

### 4.1 BLOCKING — Phase 1 introduces a child-only regression

`apply_pr3_fail_closed_switch.py` makes `validate_config()` unconditionally set
the key:

```python
cleaned["hdencode_enabled"] = source_enabled(cleaned, "hdencode_enabled", missing_default=True)
```

So `validate_config({})` now returns `{"hdencode_enabled": True}` instead of
`{}`, breaking:

```
FAILED tests/test_config.py::TestValidateConfigEdgeCases::test_empty_config_returns_empty
        result = validate_config({})
        assert result == {}          # tests/test_config.py:461-463
```

That test exists on `main` and passes there — a textbook child-only regression,
which the handoff's own stop rules say to halt on. The script edited
`tests/test_config.py` (+28 lines) but did not update this expectation.

Full Phase 1 run: **2 failed, 541 passed** (the second failure is a Chrome-launch
error in `test_api_routes.py::TestDownloads::test_download_batch_valid`, an
offline-container limitation, not a code defect — CI has Chrome).

**I did not fix this and did not commit Phase 1.** PR #3 remains at `cb7dc48`.
Two directions, ChatGPT's call:

- update the test's expectation (validate_config now always normalizes the safety
  flag — arguably correct and self-documenting); or
- only normalize when the key is explicitly present, preserving the
  non-additive contract — downstream gates already pass
  `missing_default=True`, so behaviour would be identical.

### 4.2 Phase 1 leaves 2 of 11 gates on raw truthiness

The script updated 9 sites but not:

```
backend/api/routes/scanner.py:351   and not reg.config.get("hdencode_enabled", True)
backend/api/routes/scanner.py:394   and not reg.config.get("hdencode_enabled", True)
```

These are the **rescan and auto-grab** paths — both named in review question 6.

**Not currently exploitable**, and I verified why rather than asserting it:

- `load_config()` always ends in `validate_config()`, which normalizes the flag;
- the settings API is typed `Optional[bool]`, and pydantic coerces `"false"`→`False`,
  `0`→`False`, and **rejects** `"banana"` and `["x"]` with `ValidationError`.

But `settings.py:300` does `reg.config.update(real_updates)` with no
re-validation, and `save_config()` does not re-validate either — so the "always a
real bool" property is an upstream invariant, not a structural guarantee at these
two call sites. That is precisely the pattern SH-R01 exists to eliminate. Should
be closed for consistency.

### 4.3 Phase 3 residual — non-URL iframe src is echoed verbatim

`_challenge_iframe_signal()` sanitizes real URLs correctly, including credentials
(`https://user:passw0rd@evil.example.com/path?tok=SECRET` →
`iframe:challenge@evil.example.com`). But a src that is not URL-shaped is passed
through as the "hostname":

```
"not a url at all"  ->  iframe:challenge@not a url at all
```

`captcha_frames` come from page-controlled `iframe src` attributes, so
arbitrary source-controlled text can still reach a public field. Low severity
(lowercased, no path/query), but it is unsanitized echo. Suggest constraining the
host to a hostname character class and falling back to `unknown`.

Also minor: a protocol-relative src (`//challenges.cloudflare.com/...`) resolves
to `@unknown` rather than the real hostname — fidelity loss, not a leak.

---

## 5. Answers to every review question

### Phase 1 — PR #3 fail-closed switch

1. **Any explicitly-present non-boolean able to enable?** No. Tested 18 values
   (`"false"`, `"true"`, `"0"`, `"1"`, `"yes"`, `"no"`, `"off"`, `"banana"`, `0`,
   `1`, `0.0`, `1.0`, `None`, `[]`, `["x"]`, `{}`, `{"a":1}`) — only real `True`
   enables. `source_enabled()` is `config.get(key) is True`.
2. **Corrupt/unreadable config forces disabled?** Yes — verified with genuinely
   malformed JSON through the real `AppService.load_config()`:
   `hdencode_enabled` → `False` (`app_service.py:857`).
3. **`SCANHOUND_HDENCODE_ENABLED=false` overrides persisted true?** Yes → `False`.
4. **`=true` intentionally enables?** Yes → `True`. Truthy set is
   `{"1","true","yes","on"}`; `"banana"`, `""`, `"FALSE"`, `"0"`, `"off"` all → disabled.
5. **Missing-key compatibility unchanged?** Yes — missing → `missing_default`
   (`True` by default, `False` when requested).
6. **Do all paths use strict evaluation?** **No — 2 of 11 do not.** See §4.2.
7. **DDLBase/Adit-HD independent?** Yes — HDEncode fail-closed touches no other
   source key.
8. **Env override present before service construction?** Yes — applied inside
   `load_config()`'s `env_overrides`, before `validate_config()` and before
   services are built.

### Phase 2 — main E2E isolation (PR #14)

- **Paths inside the unique temp dir?** Yes. And I verified the review's premise:
  `_get_data_dir()` (`backend/config.py:231`) reads `LOCALAPPDATA` / 
  `expanduser('~')` and **never** `SCANHOUND_DATA_DIR` — so the review was right
  that `HOME` is the effective POSIX control, and the fix adds it.
  (`XDG_CONFIG_HOME`/`XDG_DATA_HOME` are set but inert here — harmless.)
  `_DATA_DIR` is computed at module import, so the env must be set on the spawned
  process, which it is.
- **Real HOME password/config not read?** Yes — HOME is redirected before import.
- **Live server on 9721 causes a loud conflict?** **Yes — demonstrated
  accidentally.** Running local mode immediately after a CI run, before ports were
  released, produced `Timed out waiting 30000ms from config.webServer` instead of
  silently attaching. From a clean start it passes. That is the required
  fail-loud behaviour working.
- **CI still production preview / local still Vite dev?** Yes, both.
- **All 18 E2E pass in both modes?** Yes — CI 18/18 (27.2s), local 18/18 (26.4s).

### Phase 3 — PR #5 signals

**Complete audit of every `ScrapeDiagnostic.signals` producer** (19 sites):

| Producer | Content | Safe? |
|---|---|---|
| `type(e).__name__` ×6 | exception class name only | ✅ |
| `code` | enum value | ✅ |
| `"access_control_present"/"absent"`, `"large_zero_anchor_document"` | fixed literals | ✅ |
| `matched_network` | closed literal allow-list (`"err_"`, `"dns_probe"`, …) matched against body | ✅ |
| `challenge_markers` | closed literal allow-list (`"checking your browser"`, …) | ✅ |
| `requested_host_present:<bool>` | boolean | ✅ |
| `iframe:` | **was full URL — now `marker@hostname`** | ✅ except §4.3 |

No signal can carry URL credentials, path, query, fragment, local path, token, or
raw exception text — with the one non-URL echo caveat in §4.3. Internal logs
retain full evidence (`diagnostic.message` still returns `detail or public_message`).

### Phase 4 — PR #6 cancellation

1. **Semaphore waiter makes no request after cancel?** Yes — 0 post-stop requests
   vs 9 on the legacy path.
2. **Pacing-clock waiter makes no request?** Yes — and this is the *strongest*
   evidence: real spacing is 2.0s, so in the corrected trial every worker was
   still on the pacing clock at cancel time and **all** correctly issued nothing.
3. **Retry backoff stops before next request?** Yes — `_interruptible_sleep(5.0, …)`
   with cancellation returns in 0.000s by raising `_DetailRequestCancelled`.
4. **In-flight request finishes safely?** Yes — cancellation is checked before
   acquiring/issuing, never mid-request.
5. **DDLBase/Adit-HD inherit the limiter or callback?** No — `_hdencode_request_slot`
   appears only in `backend/detail_scraper.py` (definition + 2 internal uses).
6. **Legacy callers retain pacing?** Yes — `stop_requested=None` keeps the original
   semaphore + one-shot sleep; a 0.2s legacy sleep still took 0.200s.
7. **Callback exception fails closed?** Yes — `_is_cancelled()` returns `True` on
   exception, so a broken observer produces **no traffic**.
8. **Semaphore capacity always released?** Yes — `free=3, capacity=3` after all
   trials including cancelled ones.
9. **Fake-clock pacing tests deterministic?** Yes — 50 passed.

**Positive control (my own addition):** the stop-aware slot with a never-true
callback completed 3/3 requests in 4.0s, confirming the fix does not over-block
normal traffic.

### Phase 5 — PR #8 preference guard

- **Can a download-history branch skip while HEVC/HDR10+ metadata is unknown and
  the preference is active?** No — the guard returns `True` (hydrate) when either
  `pref_hevc` or `pref_hdr10plus` is set.
- **Active rules rather than hard-coded defaults?** Yes — reads
  `self.config.get("pref_hevc", False)` / `pref_hdr10plus`.
- **Non-HDEncode sources unaffected?** The guard sits inside the download-history
  sibling branch of the lazy-hydration path introduced by PR #8.
- **Is exact-URL successful history still the only unconditional no-detail path?**
  Yes — `scanner_service.py:792-793`,
  `if url and url in self.download_history: return False`, remains the sole
  unconditional skip.

---

## 6. Reproduction of the three broader findings

Method: three independent reproduction agents in disposable containers, each
followed by an **adversarial verifier** told to refute it and to default to
"refuted" unless it could confirm the evidence was real executed output. All
three verifiers independently re-ran the harnesses in their own fresh containers.

**All three findings: REPRODUCED. All three verifiers: could not refute.**

### SH-R02 — destination replacement race: REPRODUCED, and worse than the review states

Executed with `--tmpfs /tmp` so `/` and `/tmp` are distinct devices. A competing
writer creates `dst` inside the real window: `place_file()` calls
`os.makedirs(os.path.dirname(dst))` at `fileops.py:902`, i.e. **between** the
`os.path.lexists(dst)` check at `:900` and every publish primitive.

```
CASE move_thread   method='move'      dst: *** VICTIM BYTES DESTROYED ***
CASE move_proc     method='move'      dst: *** VICTIM BYTES DESTROYED ***  (external process)
CASE copy_thread   method='copy'      dst: *** VICTIM BYTES DESTROYED ***
CASE hardlink      method='hardlink'  FileExistsError -- victim intact
CASE symlink       method='symlink'   FileExistsError -- victim intact
```

No exception, no warning, a return value indistinguishable from success, and the
victim's bytes exist nowhere afterward. The external-process case rules out a
same-process/GIL artifact.

**The escalation — this reaches the default config on your actual topology.** The
reproduction first framed the blast radius as limited because `automatic=True`
forces `move`→`hardlink`, and `os.link` is O_EXCL at the syscall level. The
verifier disproved that mitigation: **`os.link` only fails `EEXIST` on the same
filesystem.** With downloads and library on different volumes — exactly
ScanHound's layout since the TV pipeline moved to `V:` — the default
`auto_rename_move_method="hardlink"` **always** hits `EXDEV` at `fileops.py:911`,
falls back to `_copy_verify_atomic`, and publishes with `os.replace` at
`fileops.py:117`, which clobbers. Reproduced **on stock defaults with zero
monkeypatching**.

Exposed set: user-confirmed `move`, `method="copy"`, **and default `hardlink` on
every cross-device placement**. The `.part` sidecar widens the window rather than
narrowing it — the check-to-publish gap scales with file size, minutes for a
large remux.

### SH-R03 — trash manifest durability and concurrency: REPRODUCED (both halves)

(a) Manifest write failure after the move (injected `ENOSPC`):

```
_trash() raised          : None            <-- swallowed
source still in library  : False           <-- ALREADY MOVED
manifest.json            : *** ABSENT ***
restore_trash_entry -> {'ok': False, 'error': 'No manifest record for this entry'}
```

The move is at `fileops.py:459`; the manifest write at `:412` via
`_finish_trash_move` — strictly after. The `except OSError` swallow is deliberate
("best-effort"), but `restore_trash_entry` **hard-refuses** any entry without a
record (`fileops.py:612`), so best-effort loss becomes permanent loss.

(b) Same-second bucket collision, verified against the real unpatched clock
(`_trash_bucket_name()` × 3 → identical strings; 1-second precision at
`fileops.py:129-130`):

```
bucket 20260719-120000: Alien (1979).mkv, Aliens (1986).mkv, manifest.json
manifest.json: 1 record(s)      <-- two files, one record
restore 'Alien (1979).mkv' -> {'ok': False, 'error': 'No manifest record ...'}
```

Two files gone from the library, one restore record — and the manifest is not
even corrupt, just valid JSON silently missing an entry.

**Verifier correction to the attributed vector:** "batch applies make same-second
disposals normal" is wrong — `_bulk_lock` (`service.py:2069`) admits one bulk run
and applies are queued sequentially. The genuinely reachable vectors are the
**unlocked, directly HTTP-reachable sync routes**: `resolve_keep_plex`
(`service.py:1805`, route `rename.py:353`), `undo` (`service.py:1725`, route
`rename.py:342`), and `/trash/restore` + `/trash/delete` (`rename.py:526/543` →
unlocked `_load_manifest`/`_save_manifest` at `628/675`). Two rapid Compare-modal
"Keep Plex" resolutions in the same second reproduce the permanent orphan. **A
fix must cover those callers, not just the apply path.**

### SH-R07 — lifecycle teardown: REPRODUCED, but I had the mechanism wrong

Reproduced: `AppService.shutdown()` joins with `timeout=3` (`app_service.py:777`,
`:785`), `BackgroundScanner.stop()` with `timeout=2.0`
(`background_scanner.py:65`); all daemon threads, so an expired join is silently
ignored and the DB closes anyway — teardown returned at **3.002s with the
maintenance thread still executing**, and `db.conn` went from `None` back to a
live connection. `_run_maintenance_pass` has **six sequential stages, none
checking `_maintenance_stop` between them**. The registry shutdown event is
**stale `True` across lifespans**.

**Correction to my own earlier reports.** I twice attributed the CI
`test_select_all_empty` flake to "a maintenance-loop thread mutating global state
concurrently." **That is not the mechanism.** With the real 3600s interval the
loop is `while not self._maintenance_stop.wait(interval)`, so shutdown makes the
thread exit *cleanly* — measured "maintenance thread alive after teardown: False"
on the unmodified interval. Orphaning needs a pass in flight, which needs ≥1 hour
uptime — never true in CI.

The verifier reproduced the byte-identical traceback with **zero harness
modifications**. The real chain is synchronous, on MainThread:

```
main.py:440-462 _teardown_services never clears registry._rename_service
  -> next _init_services calls backend.startup() BEFORE `reg.db = backend.db`
  -> app_service.py:566 runs _run_maintenance_pass() synchronously
  -> stage 4 reaches the stale registry._rename_service, whose _db is now None
  -> AttributeError: 'NoneType' object has no attribute 'list_rename_jobs'
```

**Fix implication:** the review's headline recommendation — cooperative stop
checks between maintenance stages — **would not fix the CI failure**. The fix is
to clear `registry._rename_service` (and siblings) in `_teardown_services`,
and/or set `reg.db` before `backend.startup()`.

Also corrected: production creates exactly **one** lifespan per process
(`main.py:583`, `__main__.py:25-26`, no reload/workers), so the second-lifespan
consequences are a test-architecture problem, not a production runtime one.

### SH-R09 — client-visible raw exceptions: REPRODUCED

**19 first-order sites** in `backend/api/routes/` (10 WebSocket payloads + 8
`HTTPException` details + 1 second-order at `rename.py:718`), plus 9 second-order
cases. There is **no** custom exception handler, traceback filter, or detail
sanitizer anywhere in `backend/api/`; FastAPI serializes `detail=` verbatim and
`ws_manager.broadcast_sync` (`ws.py:68`) does no scrubbing.

Verified end-to-end through the real app with TestClient (real auth and CORS
middleware, real serialization):

```
settings.py:450 -> 502 {"detail": "HTTPSConnectionPool(host='api.themoviedb.org', ...
                   /3/configuration?api_key=TMDBKEY_deadbeefcafe1234567890 ..."}  LEAKS
discord webhook -> leaks 'WEBHOOK-SECRETTOKEN' verbatim on post-DNS failure
```

Verifier qualifications, all worth carrying into a fix:

- TMDB/OMDb key disclosure is conditional on a **network-layer** failure (DNS
  outage, refused connection, TLS error), not on an invalid key — a bad key
  returns a clean `TMDB returned HTTP {status}`. Still genuinely reachable.
- `_validate_outbound_url` (`settings.py:21-43`) is the inverse: it turns DNS
  failure into a clean 400, so webhook-token leaks occur on *post-resolution*
  failures. It is also **not applied to the tmdb/omdb channels at all**.
- `scanner.py:226` is a wire-level disclosure only — the consumer
  (`stores/scanner.ts:118-123`) reads just `grabbed`/`total`, so it never renders.
- One frontend substring dependency exists (`stores/server.ts:60`) but matches a
  **client-generated** string from `client.ts:40`, so the "frontend does not
  depend on backend error text" conclusion holds.

---

## 7. Review-only integration results

Built on current `main` (`555e26b`) with `fail-fast: false`. No production PR was
retargeted merely to run CI.

| Branch | Contents | 3.11 | 3.12 | frontend |
|---|---|---|---|---|
| `fix/e2e-full-state-isolation` (PR #14) | main + E2E isolation | ✅ | ✅ | ✅ |
| `review/int3-pr5` `f84ee19` | main + corrected PR #5 | queued | ✅ | ✅ |
| `review/int3-pr6` `d782956` | main + corrected PR #6 | queued | ✅ | ✅ |
| `review/int3-pr8` `066d7ec` | main + corrected PR #8 | queued | ✅ | ✅ |
| `review/int3-pr7pr8` `589a579` | main + corrected #7 + #8 | queued | ✅ | ✅ |

All five merged cleanly — zero conflicts. **PR #14 is fully green on all three
jobs.** Every completed job is green; the remaining 3.11 jobs are queued behind
GitHub Actions concurrency and none had failed at time of writing.

A `main + corrected PR #3` integration was **not** built, because Phase 1 is held
(§4.1) — there is no corrected PR #3 to integrate.

### 7.1 Urgent, and outside the PR stack

**SH-R02 should be treated as a live production risk, not a queued follow-up.**
It is reachable on the **default** `auto_rename_move_method="hardlink"` whenever
source and destination sit on different volumes — ScanHound's normal layout.
Until it is fixed, keep **auto-rename paused** and avoid concurrent applies. This
is independent of the HDEncode work and does not belong in PR #3.

---

## 8. Protections

- No merges; no force-pushes; nothing marked ready; nothing deployed.
- `main` unchanged at `555e26b`.
- PR #3 unchanged at `cb7dc48` — Phase 1 deliberately held.
- PRs #4 (`fb99d49`) and #7 (`9d29717`) untouched.
- All fault injection ran in disposable containers; no real media library, trash
  root, or production database was touched.
- Container still on its pre-existing image (22h uptime) — no deployment.

---

## 9. Final recommendation

**PR #3 is NOT yet ready to return to the controlled production gate.**

Not because the review's fix is wrong — SH-R01 is real, and the correction's
strict semantics, corrupt-config fail-closed, and environment override all
verify correctly. It is blocked on two things ChatGPT should close:

1. **§4.1** — the child-only `test_empty_config_returns_empty` regression must be
   resolved (one decision, one line).
2. **§4.2** — the 2 remaining raw-truthiness gates in `backend/api/routes/scanner.py`
   should use `source_enabled()`, so the off switch does not depend on an
   upstream invariant at the rescan/auto-grab entry points.

Once both are closed and PR #3's full workflow is green, the deployment-gate
preconditions in the review's own checklist remain appropriate — with the
addition that **auto-rename must stay paused** until SH-R02/SH-R03 are corrected.
