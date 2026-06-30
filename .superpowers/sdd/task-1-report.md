# Task 1 Report: Add `poster_path` column to `rename_jobs`

## What was implemented

Added `poster_path TEXT` (nullable) to the `rename_jobs` table via two coordinated changes in `backend/database.py`:

1. **CREATE TABLE block** (line ~365): inserted `poster_path TEXT` after `imdb_id TEXT`, so all fresh installs get the column automatically.
2. **`_column_migrations` list** (line ~432): appended `'ALTER TABLE rename_jobs ADD COLUMN poster_path TEXT'`, so existing installs receive the column on the next `init_db()` call. The existing `duplicate column` guard in the loop makes this fully idempotent.

Created `tests/test_rename_poster_migration.py` with two tests verifying both paths.

---

## Ambiguity resolution

The brief's stub used `dm._connect()` and `dm.init_database()`. The real API (confirmed from `backend/database.py`) is:
- Connection accessor: `get_connection()` (public method)
- Schema bootstrap: `init_db()` (called by `__init__`)

Test was written with the real names. Also used an isolated temp-file DB path (matching the conftest `db_manager` pattern) instead of the default `DB_PATH` to avoid touching any real database.

---

## TDD Evidence

### RED — column absent

**Command:** `python -m pytest tests/test_rename_poster_migration.py -v`

**Output:**
```
FAILED tests/test_rename_poster_migration.py::test_fresh_db_has_poster_path_column
  AssertionError: assert 'poster_path' in {'new_filename', 'original_path', ...}
FAILED tests/test_rename_poster_migration.py::test_rerunning_migrations_is_a_noop
  AssertionError: assert 'poster_path' in {'new_filename', 'original_path', ...}
2 failed in 0.76s
```

**Why expected:** The column did not yet exist in either the CREATE TABLE statement or the migration list.

### GREEN — column present

**Command:** `python -m pytest tests/test_rename_poster_migration.py -v` (after both edits)

**Output:**
```
tests/test_rename_poster_migration.py::test_fresh_db_has_poster_path_column PASSED
tests/test_rename_poster_migration.py::test_rerunning_migrations_is_a_noop PASSED
2 passed in 0.38s
```

### Regression suite

**Command:** `python -m pytest tests/test_api_rename.py tests/test_rename_service.py -v`

**Output:** `78 passed, 1 warning in 14.14s` — zero regressions.

---

## Files changed

- `backend/database.py` — two edits: CREATE TABLE `rename_jobs` + `_column_migrations` list
- `tests/test_rename_poster_migration.py` — new file (2 tests)

---

## Self-review findings

- The `_RENAME_FIELDS` tuple in `DatabaseManager` (line ~1409) does NOT include `poster_path`. This tuple gates which fields `create_rename_job` and `update_rename_job` will accept. Task 1's scope is schema only; `_RENAME_FIELDS` should be updated in a later task when the service layer actually writes `poster_path`. No action needed here.
- No concerns about the idempotency guard — the `"duplicate column"` string match in the except clause correctly handles SQLite's exact error text (`table rename_jobs already has a column named poster_path`).
- Test uses `tempfile.mktemp()` (not `mkstemp`) for brevity; the file is cleaned up in a finally block. Acceptable for a migration test.

## Concerns

None. Task is fully self-contained and clean.

---

## Fix: mktemp → mkstemp fixture

**Issue found by task reviewer:** `test_rename_poster_migration.py` used `tempfile.mktemp()`, which is deprecated and racy and emits a `DeprecationWarning`. The file also had two identical copy-pasted `finally:` cleanup blocks (`dm.close()` + `os.unlink()`).

**Fix applied to `tests/test_rename_poster_migration.py`:**

1. Removed the `_make_dm()` helper that called `tempfile.mktemp()`.
2. Introduced a `tmp_db_path` pytest fixture using `tempfile.mkstemp()` — creates the file, closes the fd immediately, yields the path, and unlinks in teardown (swallowing `OSError`).
3. Refactored both test functions to accept `tmp_db_path` as a parameter, construct `DatabaseManager(db_path=tmp_db_path)` directly, and use a single `try/finally` with only `dm.close()` (file cleanup now handled by the fixture).

All test logic and assertions are identical to before; only the temp-file handling and cleanup are changed.

**Command:** `python -m pytest tests/test_rename_poster_migration.py -v`

**Output:**
```
============================= test session starts =============================
platform win32 -- Python 3.12.9, pytest-9.0.2, pluggy-1.6.0
...
collected 2 items

tests/test_rename_poster_migration.py::test_fresh_db_has_poster_path_column PASSED [ 50%]
tests/test_rename_poster_migration.py::test_rerunning_migrations_is_a_noop PASSED [100%]

============================== 2 passed in 0.54s ==============================
```

Result: **2 passed, 0 warnings** — suite is pristine.
