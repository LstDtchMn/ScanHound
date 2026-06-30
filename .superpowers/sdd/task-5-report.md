### Task 5 Report: `POST /rename/jobs/{id}/rematch-preview`

**Status:** IMPLEMENTED ✓

---

## TDD Evidence

### RED (before implementation)
```
$ python -m pytest tests/test_api_rename.py -v -k rematch_preview
FAILED tests/test_api_rename.py::TestRenameApi::test_rematch_preview_does_not_mutate_db
  AssertionError: assert ('new_filename' in {'detail': 'Method Not Allowed'})
FAILED tests/test_api_rename.py::TestRenameApi::test_rematch_preview_library_unconfigured_flag
  KeyError: 'library_configured'
FAILED tests/test_api_rename.py::TestRenameApi::test_rematch_preview_library_configured
  KeyError: 'library_configured'
3 failed, 17 deselected in 2.36s
```

### GREEN (after implementation)
```
$ python -m pytest tests/test_api_rename.py -v -k rematch_preview
tests/test_api_rename.py::TestRenameApi::test_rematch_preview_does_not_mutate_db PASSED
tests/test_api_rename.py::TestRenameApi::test_rematch_preview_library_unconfigured_flag PASSED
tests/test_api_rename.py::TestRenameApi::test_rematch_preview_library_configured PASSED
3 passed, 17 deselected in 2.07s
```

### Full regression suite
```
$ python -m pytest tests/test_api_rename.py tests/test_rename_service.py -v
94 passed, 1 warning in 11.19s
```
(1 warning = pre-existing httpx/starlette TestClient deprecation warning — NOT new)

---

## Files Changed

1. **`tests/test_api_rename.py`** — Added 3 new tests to `TestRenameApi`:
   - `test_rematch_preview_does_not_mutate_db`: seeds a job, calls preview, confirms DB row unchanged
   - `test_rematch_preview_library_unconfigured_flag`: confirms `library_configured=False` + `warning` set when no movie library configured
   - `test_rematch_preview_library_configured`: confirms `library_configured=True`, `destination_path` under lib root, `warning=None`

2. **`backend/rename/service.py`** — Added `rematch_preview()` method after `rematch()`. It:
   - Fetches job from DB (read-only, no update)
   - Calls `_tmdb_client().details()` (same as `rematch()`)
   - Extracts title/year/season/episode with same override logic as `rematch()`
   - Reuses `self._lib_set()` shared helper for `library_configured` flag + label
   - Calls `_naming.build_target()` via same path as `rematch()`
   - Sets `dest=None` and `warning` message when library not configured
   - Returns `{new_filename, destination_path, library_configured, warning}`
   - NEVER calls `db.update_rename_job()` — no DB mutation

3. **`backend/api/routes/rename.py`** — Added:
   - `RematchPreviewRequest` Pydantic model (same shape as `RematchRequest`)
   - `POST /jobs/{job_id}/rematch-preview` route after `rematch_job`

---

## Self-Review

- **No DB mutation**: `rematch_preview()` has zero `update_rename_job` calls. Confirmed by the no-mutation test.
- **Shared helper**: uses `self._lib_set(mtype, job.get("resolution"))` — no duplicated config logic.
- **Error handling**: TMDB failure returns clean `{"library_configured": False, "warning": "Could not fetch TMDB details"}` — no unhandled 500.
- **Job-not-found**: returns clean dict with `"warning": "Job not found"` — no HTTPException needed (mirrors the rematch() dict approach but without the `ok` key since this is preview, not action).
- **No new warnings**: the 1 existing deprecation warning is pre-existing, not introduced here.

## Concerns / Notes

- The route returns a dict directly (no HTTPException on missing job), consistent with the brief's response shape spec. The frontend should check `warning` to detect a not-found job.
- The brief's code snippet uses `self._cfg.get("auto_rename_tv_library")` and `bool(self._movie_root(...))` directly instead of `self._lib_set()`. The implementation uses `_lib_set()` as required by the binding constraints — this is the correct approach.

## Fix: preview no-500 hardening + dedup request model + fail-safe tests

### `build_target` behaviour on a null/empty root

`build_target` in `backend/rename/naming.py` does NOT raise on a null or empty `movie_root`/`tv_root`. It falls through to `_destination()` which calls `os.path.join("", folder)` — returning a relative path (e.g. `"Real Title (2022)"`) rather than an absolute one. No exception is thrown. The real problem was that this relative path would be returned as `destination_path` to the caller instead of `None`, and the `not lib_set` guard below it correctly nulls it out — but only if the guard fires. The defensive `try/except` wrapper added around `build_target` handles the (currently hypothetical) case where a future change to `build_target` could raise.

### Changes made

1. **`backend/api/routes/rename.py`** — Eliminated `RematchPreviewRequest` duplicate class. Replaced with type alias `RematchPreviewRequest = RematchRequest`. Route behaviour is identical.

2. **`backend/rename/service.py`** — Wrapped the `_naming.build_target(...)` call in `rematch_preview()` in a `try/except Exception` block. On any exception it returns the clean 4-key dict with `library_configured=False`, `destination_path=None`, and `warning="Could not build target filename"` — never lets an exception escape to the caller.

3. **`tests/test_api_rename.py`** — Added 3 fail-safe no-500 guarantee tests:
   - `test_rematch_preview_job_not_found_clean_200`: job id 99999 → HTTP 200, `warning="Job not found"`, `library_configured=False`, `destination_path=None`.
   - `test_rematch_preview_tmdb_error_clean_200`: monkeypatches `_tmdb_client` to raise → HTTP 200, `library_configured=False`, warning set.
   - `test_rematch_preview_unconfigured_library_has_new_filename`: exercises the unconfigured path with a successful TMDB fetch so `build_target` is actually called with an empty movie root. Asserts HTTP 200, `library_configured=False`, `destination_path=None`, AND `new_filename` is non-null (the would-be name is still produced).

The pre-existing `test_rematch_preview_library_unconfigured_flag` test already exercises the unconfigured path but did NOT assert that `new_filename` is present — the new test closes that gap.

### Test run

```
$ python -m pytest tests/test_api_rename.py -v
============================= test session starts =============================
...
tests/test_api_rename.py::TestRenameApi::test_list_empty PASSED
tests/test_api_rename.py::TestRenameApi::test_health_reports_capabilities PASSED
tests/test_api_rename.py::TestRenameApi::test_status_defaults PASSED
tests/test_api_rename.py::TestRenameApi::test_list_and_status_filter PASSED
tests/test_api_rename.py::TestRenameApi::test_jobs_flags_destination_conflict PASSED
tests/test_api_rename.py::TestRenameApi::test_jobs_recommends_keeper_for_duplicate PASSED
tests/test_api_rename.py::TestRenameApi::test_dv_scans_empty_and_shape PASSED
tests/test_api_rename.py::TestRenameApi::test_dv_scan_folder_requires_folder PASSED
tests/test_api_rename.py::TestRenameApi::test_dv_scan_folder_starts PASSED
tests/test_api_rename.py::TestRenameApi::test_apply_unknown_job_is_400 PASSED
tests/test_api_rename.py::TestRenameApi::test_apply_then_undo_via_api PASSED
tests/test_api_rename.py::TestRenameApi::test_delete_job PASSED
tests/test_api_rename.py::TestRenameApi::test_llm_test_endpoint PASSED
tests/test_api_rename.py::TestRenameApi::test_poster_url_built_when_poster_path_set PASSED
tests/test_api_rename.py::TestRenameApi::test_poster_url_null_when_no_poster_path PASSED
tests/test_api_rename.py::TestRenameApi::test_dv_layer_joined_when_dv_scan_exists PASSED
tests/test_api_rename.py::TestRenameApi::test_dv_layer_null_when_no_dv_scan PASSED
tests/test_api_rename.py::TestRenameApi::test_rematch_preview_does_not_mutate_db PASSED
tests/test_api_rename.py::TestRenameApi::test_rematch_preview_library_unconfigured_flag PASSED
tests/test_api_rename.py::TestRenameApi::test_rematch_preview_library_configured PASSED
tests/test_api_rename.py::TestRenameApi::test_rematch_preview_job_not_found_clean_200 PASSED
tests/test_api_rename.py::TestRenameApi::test_rematch_preview_tmdb_error_clean_200 PASSED
tests/test_api_rename.py::TestRenameApi::test_rematch_preview_unconfigured_library_has_new_filename PASSED

============================== warnings summary ===============================
  StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.

======================== 23 passed, 1 warning in 8.60s
```
