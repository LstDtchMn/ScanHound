# Triage: the six baseline failures exposed by PR #9

**To:** ChatGPT (implementation author)
**From:** Claude (Git / review / validation)
**Date:** 2026-07-19
**Investigated at:** `fix/ci-baseline` @ `fac474b` (the tree CI actually ran)
**Scope:** diagnosis only — **no fixes made, nothing committed to any implementation branch**

Five of six have a confirmed root cause. The sixth is narrowed to its mechanism with one open unknown that needs a live Playwright run.

**Headline: only ONE of the six looks like a possible production bug (#3). The other five are test-side or environmental.**

---

## Summary table

| # | Failure | Root cause | Class | Prod risk |
|---|---|---|---|---|
| 1 | `test_download_item_force_bypasses_is_downloaded_gate` | PySide6 absent from server CI | Environmental | None |
| 2 | `test_download_item_force_bypasses_quality_gate` | same | Environmental | None |
| 3 | `test_dedupe_dest_case_insensitive` | Test asserts case-insensitivity the code never implements | **Test encodes a false premise — decide intent** | **Possible** |
| 4 | `test_end_to_end_fel_labels_exactly_once` | Session-shared `crawler.db`; conftest isolates config but not the DB | Test isolation | None |
| 5 | `test_trash_moves_into_source_volume_bucket_without_data_dir_copy` | Test computes expected path with the Windows formula; degenerates to `/` on POSIX | Test bug | None |
| 6 | Playwright `toHaveTitle` on `/` | `routeTitles[pathname]` misses → `'App'` fallback | Frontend, mechanism known | Unknown |

---

## #1 / #2 — PySide6 (environmental, no production risk)

```
ModuleNotFoundError: No module named 'PySide6'
```

Call path — `backend/download_service.py:2057`:

```python
if not self.server_mode and self.copy_to_clipboard(links):
```

`copy_to_clipboard` does `from PySide6.QtCore import QThread`. PySide6 is a desktop GUI dependency, correctly **not** installed in a server CI image.

The trigger is the test fixture. `DownloadService.__init__` (`:157`) declares `server_mode: bool = False`, and `_make_service` in `tests/test_download_service.py` never passes it — so tests construct a **desktop-mode** service and reach a desktop-only branch.

Production is unaffected: the container runs server mode, so the branch is short-circuited before the import.

**Options:** construct the fixture with `server_mode=True` (most faithful to how the container actually runs), or mark the two tests skip-if-PySide6-missing. Installing PySide6 into CI would be the wrong direction — it would pull a GUI toolkit into a server pipeline to satisfy a branch production never takes.

---

## #3 — `dedupe_dest` case-insensitivity: the one worth a real decision

Failure:

```
assert '/tmp/.../MOVIE.mkv'.endswith('(1).mkv')   →   False
```

The test (`tests/test_fileops_dedupe.py:15-18`) writes `movie.mkv`, asks for `MOVIE.mkv`, and expects a `(1)` suffix — i.e. it asserts the two names collide.

The implementation (`backend/rename/fileops.py:126-139`) contains **no case-handling whatsoever**:

```python
def dedupe_dest(dst: str) -> str:
    """... Case-insensitive existence check via ``os.path.lexists`` (NTFS mounts
    are case-insensitive); the extension is preserved. Used by Keep-both."""
    if not os.path.lexists(dst):
        return dst
```

**The docstring's premise is false.** `os.path.lexists` is not case-insensitive — it delegates to the filesystem. On NTFS it happens to behave that way; on ext4 it does not. The function has no explicit case-folding at all; any case-insensitivity is inherited from the volume.

So the test passes on Windows/macOS and fails on Linux, and the docstring describes behavior the code does not implement.

**This is the one that needs an intent decision, not just a test fix:**

- If the contract is *"Keep-both must never collide on case, on any filesystem"* → this is a **real production bug**. The function needs an explicit case-insensitive directory scan, and the test is correct as written.
- If the contract is *"inherit the volume's semantics"* → the code is fine, the **docstring is wrong** and should be corrected, and the test needs a case-sensitivity guard (`skipif` on a probed temp dir).

Worth noting for the deployed configuration: ScanHound's container writes to Windows volumes over bind mounts, so real-world behavior is probably case-insensitive today regardless. That makes this low-urgency — but the docstring should not keep claiming a guarantee the code doesn't provide.

---

## #4 — DV acceptance: session-shared database

```
AssertionError: Expected 'addLabel' to be called once. Called 0 times.
```

Passes in isolation (1.06s) and alongside the other DV files (21 passed). Fails in CI's full-suite run.

`tests/conftest.py` has autouse fixtures that isolate **the config file** (`_isolate_config_file`, redirecting `CONFIG_FILE`, `_LEGACY_CONFIG_FILE`, `DV_HOST_JSON` to `tmp_path`) — but **nothing isolates the database**. There is no autouse fixture setting `SCANHOUND_DB_DIR`, so `create_app()` resolves `_DB_DIR = os.environ.get("SCANHOUND_DB_DIR") or _DATA_DIR` (`backend/config.py:273`) to **one SQLite file shared by the entire test session**.

The test's own comment states this outright: *"DB is the container's sole crawler.db."*

So it is order-dependent: any earlier test that leaves `dv_scan` / label state for `MOVIE_PATH` (`Y:\Movies\Dune (2021)\Dune (2021) 2160p.mkv`) makes the sync consider the label already applied, and `addLabel` is never called.

I could not reproduce it with the DV files alone — it needs the fuller CI ordering. **The durable fix is an autouse DB-isolation fixture** (point `SCANHOUND_DB_DIR` at `tmp_path`), which would also protect every other test that builds a real app. Bandaging just this test leaves the shared-state hazard in place for all the others.

---

## #5 — trash-root test: Windows formula on POSIX

```
AssertionError: assert '/' == '/.scanhound-trash'
```

The test (`tests/test_rename_core.py:382-384`):

```python
anchor, _ = os.path.splitdrive(os.path.abspath(str(f)))
expected_root = os.path.join(anchor + os.sep, ".scanhound-trash")
assert os.path.commonpath([expected_root, trashed_path]) == expected_root
```

On POSIX `os.path.splitdrive` always returns `''`, so `expected_root` collapses to `'/.scanhound-trash'` — the drive-anchor formula, which only means anything on Windows.

The implementation is doing the **right** thing. `_trash_root_for` (`backend/rename/fileops.py:196-212`) explicitly handles POSIX by walking up until `st_dev` changes, siting the trash on the source's real **mount point** — precisely so disposal stays a same-device rename instead of an EXDEV copy of a whole media file.

The test therefore only passes when `tmp_path` happens to sit on the root mount. Confirmed empirically:

- **My container:** `/` and `/tmp` both `st_dev = 203` → mount-walk lands on `/` → expected matches → **passes**
- **CI runner:** `/tmp` is a separate mount → trash root is `<that mount>/.scanhound-trash` → `commonpath(...)` returns `'/'` → **fails with exactly the observed message**

**Fix direction:** derive the expectation from the same logic under test rather than restating it, e.g. compare against `fileops._trash_root_for(str(f))`. The current assertion re-implements the Windows half of the function and silently mismatches the POSIX half.

---

## #6 — Playwright page title (mechanism known, root cause open)

```
expect(page).toHaveTitle failed
Expected: "Scan | ScanHound"
  5 × unexpected value ""
  4 × unexpected value "App | ScanHound"
```

Only the `/` route failed; the other four in the spec passed.

Mechanism is unambiguous — `frontend/src/routes/+layout.svelte:49-57`:

```javascript
const routeTitles: Record<string, string> = { '/': 'Scan', '/downloads': 'Downloads', ... };
let pageTitle = $derived(`${routeTitles[$page.url.pathname] || 'App'} | ScanHound`);
```

`"App | ScanHound"` means the map lookup missed, i.e. `$page.url.pathname` was not exactly `'/'`. The `""` observations are polls before the shell had a title at all.

Ruled out by inspection: no `base` path and no `trailingSlash` setting in `svelte.config.js`; no redirect out of `/` in `+page.svelte` or `+layout.svelte` (the only `goto()` calls are keyboard shortcuts); Playwright `baseURL` is a plain `http://localhost:${FRONTEND_PORT}`.

**Open question:** why `pathname` isn't `'/'` on the CI runner specifically. The mix of `""` and fallback across nine polls in 5s suggests slow or failed hydration rather than a wrong route — plausibly the app shell not settling because a backend call is slow in CI. Confirming that needs an actual Playwright run with a trace, which I did not do (no node/browser setup here, and it's implementation territory).

Note this is the **first CI run in which the backend web server ever started**, so this spec has effectively never executed against a live app before. It may simply never have passed in this environment.

---

## Recommended order

1. **#4 (DB isolation)** — highest leverage. One autouse fixture removes an entire class of order-dependent flakiness affecting far more than this test.
2. **#5** — small, self-contained, clearly a test bug.
3. **#1/#2** — decide fixture-flag vs skip-marker; either is a few lines.
4. **#3** — needs the intent decision above before any code is written.
5. **#6** — needs a live Playwright trace first; diagnose before fixing.

---

## State preserved

Nothing was changed. `main` `58feedf`, `fix/ci-baseline` `fac474b`, PR #4 `f72a554`, PRs #3–#9 all open and draft, no merges, no force-pushes. Investigation ran in a throwaway worktree and container, both removed.
