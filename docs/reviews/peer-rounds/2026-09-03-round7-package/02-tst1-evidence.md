# TST-1 evidence — 2026-09-03

Host: development workstation (Windows), worktree `C:\Users\NLSur\AppData\Local\Temp\fix-tst1`, branch `fix/tst1-suite-trash-isolation` off `main` @ `0a2751d`.

## 1. Pre-fix state of the real root (measured before any change)

```
C:\.scanhound-trash: 400 buckets, 873 files, 0.2 MB
oldest bucket: 2026-08-09 20:33   newest: 2026-09-03 08:26
X:\.scanhound-trash absent   V:\.scanhound-trash absent   P:\.scanhound-trash absent
```

## 2. Guard shown to FAIL (copy, redirect removed, guard kept)

Files: test_rename_core, test_rename_service, test_apply_conflict_strategy, test_trash_durability, test_api_rename, test_suite_trash_isolation.

```
2 failed, 346 passed, 1 skipped, 16 errors in 82.24s
teardown ERRORs: 17 (16 per-test + 1 session)
tests the guard named as writers: 15
    tests/test_rename_core.py::TestFileOps::test_hardlink_falls_back_to_copy_across_filesystems
    tests/test_rename_core.py::TestFileOps::test_trash_moves_into_source_volume_bucket_without_data_dir_copy
    tests/test_rename_service.py::TestApplyProgressBroadcast::test_cross_device_copy_broadcasts_speed_and_eta
    tests/test_rename_service.py::TestConflictSignal::test_overwrite_apply_clears_conflict_signal
    tests/test_rename_service.py::TestConflictSignal::test_overwrite_apply_clears_size_signal
    tests/test_rename_service.py::TestConflictSignal::test_overwrite_apply_place_file_failure_clears_stale_conflict_signal
    tests/test_rename_service.py::TestConflictSignal::test_overwrite_db_write_failure_also_restores_trashed_original
    tests/test_rename_service.py::TestConflictSignal::test_overwrite_restore_failure_surfaces_loud_error
    tests/test_rename_service.py::TestConflictSignal::test_overwrite_restores_trashed_original_on_place_file_failure
    tests/test_rename_service.py::TestReplaceLibraryDupAndKeepPlex::test_keep_plex_archives_and_trashes_download
    tests/test_rename_service.py::TestReplaceLibraryDupAndKeepPlex::test_replace_library_dup_restores_library_on_place_failure
    tests/test_rename_service.py::TestReplaceLibraryDupAndKeepPlex::test_replace_library_dup_restores_when_restore_path_is_not_persisted
    (+3 more in test_rename_service)
real roots named: C:\.scanhound-trash
entries the demo added and were removed again: 6
    C:\.scanhound-trash\20260903-204453 ... 20260903-204605
real roots back to pre-demo state: YES
```

The two FAILED tests are the new isolation tests that assert the redirect; with the redirect removed they must fail, and did.

## 3. Adversarial read (Opus, read-only) between the first cut and the commit

Four defects found and closed: root-watch test Linux-unsafe; in-bucket mtime test vacuous; ancestor walk unpinned; silent fallback. While fixing the second, measured on this host: a bucket's mtime as reported by scandir did NOT change after a file was created inside it (before and after both 1000000000 ns). The snapshot therefore lists one level inside each bucket and uses no mtimes.

## 4. With the final fixture (worktree, redirect + guard)

Trash-related files (8): `471 passed, 1 skipped in 105.06s`.

Guard shown to FAIL, re-run on the final code (copy, redirect removed, guard kept, same six files):

```
2 failed, 348 passed, 1 skipped, 13 errors in 99.79s
teardown ERRORs: 14 (13 per-test + 1 session)
tests the guard named as writers: 12
real roots named: C:\.scanhound-trash
entries the demo added and were removed again: 7
real roots back to pre-demo state: YES
```

First full-suite run (the fixture before the adversarial round, same redirect): `1 failed, 5449 passed, 5 skipped in 963s`; the guard fired 0 times across the whole suite. The one failure, `test_dv_host_scan.py::test_post_rows_direct_success_delivers_key`, aborted on a local socket (WinError 10053) and does not touch any trash path; see the re-check below.

Re-check of that test on the final code: passes alone (3 of 3), with its file (37 passed), and in the final full run.

Full suite, final code: `5452 passed, 5 skipped, 1 warning in 1269.75s (0:21:09)`, exit 0, guard fired 0 times. The extra five minutes over the first run is the guard listing inside each of the 400 stale buckets before and after every test.

## 4. What the guard does not do

It never deletes from a real root. The 400 pre-existing buckets remain until the owner removes them.
