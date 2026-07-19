# ScanHound priority-corrections report — 2026-07-19

Response to `scanhound_priority_corrections_20260719` (DECISIONS.md +
CLAUDE_PRIORITY_HANDOFF.md).

ChatGPT authored all three correction scripts. Claude performed Git operations,
independent review, adversarial race testing, root/UID-1000 and cross-volume
validation, CI, and this report.

**Nothing merged, deployed, force-pushed, or marked ready. Auto-rename NOT
resumed. `main` unchanged at `555e26b`. Container still on its pre-existing
image (24h uptime).**

Package integrity: all 9 files verified against `SHA256SUMS.txt` — OK.

---

## 0. Headline

| Item | SHA | Result |
|---|---|---|
| **PR #3** fail-closed switch v2 | `f3c2f0c` | ✅ **3.11 / 3.12 / frontend all green** |
| `review/int4-pr3` (main + corrected PR #3) | `27b5b63` | ✅ all three green |
| **PR #5** residual signal correction | `64663e6` | ✅ validated, 4/4 proofs |
| PR #6 parent merge | `da15768` | clean |
| PR #7 parent merge | `8a48382` | clean |
| **SH-R02** atomic no-replace | uncommitted | ⚠️ **HELD** — see §2.4 |

**Both blockers that held PR #3 back last round are closed and verified.**

Two items require a decision that is not Claude's to make: the production
auto-rename pause (§1) and two script defects (§2.4, §5).

---

## 1. Step 0 — production safety hold: NOT satisfied

The handoff's premise ("Confirm production `auto_rename_enabled=false`") is
false. Read-only evidence from the live container, no secrets:

```
auto_rename_enabled              = True   (bool)
auto_rename_move_method          = 'move' (str)
auto_rename_require_confirmation = True   (bool)
background_scan_enabled          = True
scheduler_enabled                = True
```

Live rename-job state (read-only, `mode=ro`): **0 in-flight applies**;
78 applied, 1 failed, 1 needs_review.

### Precise exposure

- `auto_rename_require_confirmation=True` means the unattended
  `self.apply(job_id, automatic=True)` at `service.py:1349` **never fires**.
  Nothing renames on its own. Good.
- But because every apply is therefore user-initiated, `automatic=False`, so the
  `automatic and method == "move"` → hardlink downgrade at `fileops.py:660`
  **never fires either**. Production's `move` stays `move` → raw `os.rename` →
  the SH-R02 clobber path, which is the *most* exposed variant (does not even
  need cross-device).

### Why Claude did not pause it

`GET /settings` returns **401** (`auth_required=true`, `has_password=true`).
Claude does not have and must never handle the application password. The pause
requires the operator to toggle Auto-rename off in Settings, or an explicit
decision to edit `config.json` + restart the container.

**Behavioural hold is in force** and was honoured throughout: no Apply, no
Compare → Keep Plex, no manual Move/Copy, no undo, no trash restore/delete.
Scheduler and background scan left running — correctly, they do not feed the
placement path (the rename feed is the JD poller hook at `main.py:312`, gated
separately by `auto_rename_enabled`).

**This hold stands until SH-R02 *and* SH-R03 are both corrected. SH-R03 is
entirely unfixed.**

---

## 2. Step 1 — SH-R02 atomic no-replace

Branch `fix/atomic-no-replace-placement` from `main` `555e26b`. Applied script
changed exactly the two declared files (`backend/rename/fileops.py`,
`tests/test_rename_core.py`); `git diff --check` clean; compiles.

### 2.1 Independent review — the eight questions

1. **Any move/copy/hardlink-fallback path still using `os.replace` or
   overwrite-capable `os.rename` for final publication?** No. The only
   `os.rename` in the publish primitive is `os.name == "nt"`-gated, where
   Windows already refuses an existing destination. The remaining
   `os.rename`/`os.replace` occurrences are in trash/restore (`:563`, `:598`,
   `:725`) and the trash-roots index write (`:399`) — not placement.
2. **Linux `renameat2(RENAME_NOREPLACE)` when supported?** Yes — real ctypes
   binding, `AT_FDCWD`, flag `1`, `use_errno=True`.
3. **Fallback atomically creates or fails safely?** Yes — `os.link` (O_EXCL at
   the syscall level) then source unlink.
4. **Filesystem with neither renameat2 nor hard-link support?** Raises
   `OSError("Destination filesystem cannot atomically publish without
   replacement; source kept")`. Fails safely, source intact.
5. **Temp files unique per operation and cleaned on every error?** Yes —
   `tempfile.mkstemp(prefix=f".{basename}.part.", dir=directory)`. Zero residual
   temp files observed across every race case.
6. **Can two simultaneous copies corrupt a shared `.part`?** No — `mkstemp`
   guarantees a distinct path per call. Proven by the concurrent-two-copy test.
7. **Source unlink fails after hard-link publication?** `dst` is unlinked
   (rollback), source retained; if rollback itself fails it logs `critical` and
   re-raises. No silent both-names state.
8. **Failure shapes compatible with RenameService rollback?** Yes —
   `FileExistsError` is the same shape `place_file` already raised from its
   `lexists` precheck, which existing callers handle.

### 2.2 Adversarial races — the exact cases from the comprehensive report

Barrier: `place_file()` calls `os.makedirs(os.path.dirname(dst))` at
`fileops.py:902`, i.e. between the `lexists` precheck (`:900`) and every publish
primitive. A competing writer creates `dst` at exactly that instant.

Container with `--tmpfs /xdev` so `/` (dev 188) and `/xdev` (dev 1048787) are
genuinely distinct devices.

| Case | Method | Writer | Victim | Result |
|---|---|---|---|---|
| move_thread | move | thread | **INTACT** | `FileExistsError` |
| move_proc | move | external process | **INTACT** | `FileExistsError` |
| copy_thread | copy | thread | **INTACT** | `FileExistsError` |
| **hardlink_xdev** | hardlink → EXDEV → copy | thread | **INTACT** | `FileExistsError` |

Every case additionally proved: **no false success** (an exception is always
raised), **source recoverable**, **no residual temp file**.

`hardlink_xdev` is the stock-default cross-device case — the escalation that
made SH-R02 reach the default configuration. It now fails safely.

**Concurrent two-copy** (both callers pass the precheck, released by a
`threading.Barrier`): exactly one publisher, loser raised `FileExistsError`,
winner's bytes intact and unmixed, no residual temp file.

**All of the above passed identically as root (uid 0) and as uid 1000.**

### 2.3 Filesystem-type note

Validated on overlayfs (`/`) and tmpfs (`/xdev`) — both provide working
`renameat2(RENAME_NOREPLACE)`. Production media volumes are SMB/CIFS mounts
(`\\TURTLELANDSRV2`) and NTFS via Storage Spaces; those were **not** exercised
here because no disposable equivalent was available. The design degrades
correctly in principle (renameat2 unsupported → `os.link` → if that is also
unsupported, raise with source kept), but **CIFS behaviour is unverified and
should be confirmed before this is relied on in production.**

### 2.4 DEFECT — child-only regression, commit HELD

Three tests fail on the branch and pass on `main`:

```
tests/test_rename_core.py::TestFileOps::test_cross_device_move_trashes_source_by_default
tests/test_rename_core.py::TestAllTrashRoots::test_deeper_fallback_root_is_globally_discoverable_and_restorable   (uid 1000 only)
tests/test_rename_service.py::TestApplyProgressBroadcast::test_cross_device_copy_broadcasts_speed_and_eta
```

Root: `235 passed, 2 failed`. uid 1000: `234 passed, 3 failed`.
Parent (`origin/main`): **all three pass**.

**Diagnosis — the tests are stale, the code is correct.** They simulate EXDEV by
monkeypatching `os.rename`. The Linux path now calls `renameat2` via ctypes and
never touches `os.rename`, so the patch never fires; the rename succeeds
atomically and the copy+trash fallback the test asserts on never runs
(`assert len(trashed) == 1` → `0 == 1`).

Verified by executing a **real** cross-device move on the fixed branch
(src dev 1048787 → dst dev 188, no monkeypatching at all):

```
place_file returned : 'move'
dst exists+correct  : True
src consumed        : True
source trashed      : 1   -> PASS
```

So the EXDEV fallback at `place_file` is intact and correct in production.

**Held rather than committed**, per the handoff stop rule on child-only
regressions, and because a data-safety fix whose own suite is red is not yet a
safety fix. Repairing the simulation requires patching
`_linux_rename_noreplace`/`os.link` instead of `os.rename` — which touches
`tests/test_rename_service.py`, **outside the declared 2-file scope**.
ChatGPT's decision.

---

## 3. Step 2 — corrected PR #3 fail-closed switch

Applied to PR #3 `cb7dc48` → **`f3c2f0c`**. All nine declared files changed,
including `backend/api/routes/scanner.py` this time.

### Both previously-blocking conditions resolved

```
validate_config({}) -> {}                                  PASS  (non-additive contract restored)
no hdencode key injected into unrelated config             PASS
```

### Repository-wide truthiness audit

```
grep -rn "hdencode" --include=*.py backend/ | grep -E 'get\("hdencode_enabled"|bool\(.*hdencode' | grep -v source_enabled
```

**Remaining legacy truthiness gates: 0.** The single `bool(...)` hit is
`scanner.py:109` — `bool(host and host in {"hdencode.org", configured_host})`,
which is hostname set-membership routing returning a bool, not a config traffic
gate. Correctly classified as not-a-gate.

### Required semantics — all verified by execution

| Requirement | Result |
|---|---|
| `validate_config({}) == {}` unchanged | PASS |
| Missing-key compatibility unchanged | PASS (missing → `missing_default`) |
| Only real `True` enables once key present | PASS — 18 values tested, none enable |
| Corrupt config fails closed | PASS — malformed JSON → `False` |
| `SCANHOUND_HDENCODE_ENABLED=false` overrides persisted true | PASS |
| `=true` intentionally enables | PASS; `banana`/``/`FALSE`/`0`/`off` → disabled |
| Both scan-start and item-rescan routes use `source_enabled()` | PASS |
| DDLBase/Adit-HD independent | PASS |

Focused suite: **137 passed**. (`tests/test_api_routes.py` excluded locally —
known offline hang on network/Selenium tests; CI covers it and passed.)

### CI

- PR #3 full workflow: **`test (3.11)` ✅ `test (3.12)` ✅ `frontend` ✅**
- `review/int4-pr3` (main + corrected PR #3, `fail-fast: false`): **all three ✅**

The integration's first run showed one 3.11 failure,
`tests/test_api_routes.py::TestResults::test_get_results_empty`. Re-running the
**identical commit `27b5b63`** produced a fully green run. See §6 — this is the
same flake family, and it is almost certainly the stale-registry mechanism.

---

## 4. Step 3 — PR #5 residual signal correction

Applied to PR #5 `5bea70f` → **`64663e6`**. Exactly the two declared files.

The four required proofs, executed:

```
https://challenges.cloudflare.com/turnstile/v0/api.js?sitekey=0x4AAA_SECRET&return=...#frag
                                        -> iframe:turnstile@challenges.cloudflare.com
https://www.google.com/recaptcha/api2/anchor?k=6LeIx_SECRET&co=...
                                        -> iframe:recaptcha@www.google.com
//challenges.cloudflare.com/cdn-cgi/challenge-platform/...?ray=SECRET
                                        -> iframe:challenges.cloudflare@challenges.cloudflare.com
https://user:passw0rd@evil.example.com/path?tok=SECRET
                                        -> iframe:challenge@evil.example.com
/relative/captcha?token=SECRET          -> iframe:captcha@unknown
"not a url at all"                      -> iframe:challenge@unknown
"" / None                               -> iframe:challenge@unknown
```

- Arbitrary non-URL iframe text → `@unknown` ✅ (this was the residual)
- Protocol-relative URLs → hostname only, no path/query ✅ (**improved**: this
  previously degraded to `@unknown`)
- Credentials, path, query, fragment, site keys, free-form page text never enter
  serialized signals ✅
- Internal logging unchanged — `diagnostic.message` still returns
  `detail or public_message`, so troubleshooting evidence is retained ✅

Validation: `193 passed, 2 failed` — the two failures are the long-standing
`test_download_item_force_bypasses_*` PySide6 pair, which fail identically on
unmodified `main` and are unrelated (PR #5 predates main's test-isolation merge).

Propagated normally, no rebase, no force-push:
PR #5 → PR #6 (**`da15768`**, clean) → PR #7 (**`8a48382`**, clean).

---

## 5. Step 4 — lifecycle attribution, confirmed and BROADER than reported

The superseded maintenance-loop fix was **not** applied. Review-only
reproduction against `origin/main`:

| Check | Result |
|---|---|
| Registry service refs | `_auto_grab_service`, `_download_service`, `_plex_service`, `_rename_service`, `_scanner_service` |
| Cleared in `_teardown_services` | **NONE — all 5 survive** |
| `reg.db =` before `backend.startup()` in `_init_services` | **NO** — startup at index 504, `reg.db` at 737 |
| Shutdown event reset in `_init_services` | **NO** |
| Production single-lifespan | **YES** — `main.py:583` module-level `app = create_app()`, no reload/workers |

Confirmed call chain, synchronous and on MainThread:

```
AppService.startup()            app_service.py:407
  -> _init_optional_subsystems()             :474
    -> _run_maintenance_pass()               :566   (synchronous, "once immediately at startup")
    -> _start_maintenance_loop()             :567
```

So the next lifespan's startup runs a maintenance pass **before `reg.db` is
assigned**, and that pass reaches a service object left over from the previous
lifespan whose `_db` is now `None` →
`AttributeError: 'NoneType' object has no attribute 'list_rename_jobs'`.

**Broader than reported:** the earlier attribution named `_rename_service`. All
**five** service references survive teardown, so `_scanner_service`,
`_download_service`, `_plex_service` and `_auto_grab_service` are equally stale.

### Proposed minimal correction scope

Smallest safe change, **not applied** — awaiting a ChatGPT-authored implementation:

- `backend/api/main.py` — `_teardown_services()`: set all five
  `registry._*_service` references to `None`.
- `backend/api/main.py` — `_init_services()`: reset the registry shutdown event,
  and assign `reg.db` **before** `backend.startup()`.
- New regression test asserting a second lifespan cannot reach a first-lifespan
  service, and that the shutdown event is clear at startup.

No production behaviour change expected — production creates exactly one
lifespan per process, so this is a test-architecture correction.

**Self-correction:** an earlier automated check in this round reported "no
synchronous startup maintenance pass exists". That was a false negative — it
searched `AppService.startup()` directly, but the call lives one level down in
`_init_optional_subsystems()`. The pass is real; the chain above is the
corrected, verified version.

---

## 6. The `TestResults` flake family is probably not flakiness

Three failures across this session, all in
`tests/test_api_routes.py::TestResults`, all "empty results" assertions, each
disappearing on re-run of the identical commit:

| Test | Branch | Re-run of same SHA |
|---|---|---|
| `test_select_all_empty` | `review/int2-pr3` `a230c05` | green |
| `test_select_all_empty` | `main` `555e26b` | green |
| `test_get_results_empty` | `review/int4-pr3` `27b5b63` | green |

Both assert that a freshly-constructed app reports zero results. That is exactly
what a **stale service reference from a previous test lifespan** would corrupt.
The failing CI log that first exposed this carried the matching traceback
(`detect_moved_source_files` → `list_rename_jobs` on a `None` db).

Recommendation: treat §5 as the fix for this flake family rather than as an
unrelated cleanup, and prioritise it accordingly. Retrying CI is currently
masking a real defect.

---

## 7. Final state

| PR | Branch | Head | Draft |
|---|---|---|---|
| #3 | `agent/hdencode-off-switch` | `f3c2f0c` | yes |
| #5 | `agent/hdencode-structured-outcomes` | `64663e6` | yes |
| #6 | `agent/hdencode-block-cancellation` | `da15768` | yes |
| #7 | `agent/hdencode-source-health` | `8a48382` | yes |
| #8 | `agent/hdencode-lazy-hydration` | `c1a7f7b` | yes |
| #14 | `fix/e2e-full-state-isolation` | `be52908` | yes |
| — | `fix/atomic-no-replace-placement` | uncommitted | — |

`main` = `555e26b`. Working checkout clean. All disposable containers removed.
No fault injection ever touched real media, a real trash root, or the production
database.

---

## 8. Open decisions

**Operator:**

1. Pause auto-rename in Settings (`auto_rename_enabled` → false). Claude cannot —
   `/settings` is 401 and the app password must not be handled.
2. Keep the behavioural hold until **SH-R03** is also fixed. SH-R02 passing is
   not authorization to resume rename automation.

**ChatGPT:**

3. SH-R02 EXDEV test vectors (§2.4) — repairing them requires
   `tests/test_rename_service.py`, outside the declared 2-file scope.
4. Whether to verify `renameat2`/`os.link` semantics on CIFS/NTFS before relying
   on the SH-R02 fix in production (§2.3).
5. SH-R03 remains completely unaddressed — the trash manifest can still strand a
   moved file with no restore record, reachable through `resolve_keep_plex`,
   `undo`, and the `/trash/*` routes.
6. Lifecycle correction (§5) — five stale refs, not one; and it likely resolves
   the flake family in §6.
