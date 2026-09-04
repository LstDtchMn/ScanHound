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

## 4. Round 8 (peer review REQUEST CHANGES, narrow) — 2026-09-04

Owner action recorded: on 2026-09-04 Jesse authorised and Claude verified-then-deleted the historical residue (400 timestamp-named buckets, 873 files, none over 1 MB, no other entries). Test code deleted nothing.

Changes (implemented by a Sonnet lane from a written spec, adversarially read by an Opus lane, verified by the supervisor):

- R8-TST1-1: the ordinary fixture also replaces `fileops.all_trash_roots` with an isolated discovery: the derivation applied to tmp_path and the app-data fallback, each kept only if under tmp_path (by construction, not by audit), plus the already-isolated registered roots. Regression test simulates POSIX with a sentinel mount and proves the three default-roots mutators never probe it (both the bare and the abspath'd form, and the probe list must be non-empty); a marked counterpart proves the real function would have surfaced it.
- R8-TST1-2: bucket snapshot records (name, type, size) per direct child plus a sha256 of the first 1 MiB of a non-symlinked `manifest.json`. No recursion, nothing else hashed.
- R8-TST1-3: under the plain marker the six mutators raise; `mutators="allowed"` opts back in for the one test that legitimately trashes under a tmp mount.
- Supervisor's finding during mutation: the implementer's proof test passed `__file__` to `_trash`; with the raise removed it moved its own module into the real root. Now a tmp file.
- The Opus read's six findings (vacuous half of the probe assertion on Windows; unbounded, symlink-following digest; isolation by audit; unmatched raise; two comment corrections) all closed.

Mutants on a whole-tree copy (each killed by exactly the test that claims it; the copy's one real-root write under mutant D was removed again by the script, root restored):

```
A2 discovery real + first assertion disabled   KILLED  (probe assertion alone)
A  all_trash_roots left real                   KILLED
B  manifest digest constant                    KILLED
C  child size zero                              KILLED
D  marked-branch raise removed                  KILLED  (+ guard fired: 2 errors)
control                                         16 passed
```

Trash-related files (8): `477 passed, 1 skipped in 53.47s`.

HOST VERIFIED full suite on `3f32681` (the qualification the reviewer asked for):

```
real root before: absent           2026-09-04T11:59:38Z
5458 passed, 5 skipped, 1 warning in 824.24s (0:13:44)   exit=0
guard firings: 0
real root after:  absent           2026-09-04T12:13:31Z
```

Timing: 13:44 against 21:09 for the run that listed inside the 400 stale buckets on every test, and 16:03 for the very first run before the guard looked inside buckets at all.

## 5. Round 8 closure (R8R-TST1-4) — 2026-09-04

Reviewer closed R8-TST1-1/2/3 and R8-DOC-1; one final narrow change: registered roots were re-added to the isolated discovery unfiltered. Now all three candidate kinds (derivation on tmp_path, app-data fallback, registered roots) pass one under-tmp_path predicate. Regression registers a never-created external root through `_record_trash_root`, proves the registry reads it back, the isolated discovery drops it, and repair/sweep/empty with default roots never probe it; a marked read-only counterpart proves the real discovery would have surfaced it.

```
MUTANT F: registered roots unfiltered -> KILLED | 1 failed, 17 passed  (exactly the regression)
unmutated copy: 18 passed
real root entries added by this run and removed: []  | real root exists now: False
```

Eight trash-related files: `479 passed, 1 skipped in 63.67s`. CI on `6ae62dc` (ubuntu-latest): 5454 passed on Python 3.11 and 3.12, frontend green. CI VERIFIED.

## 6. What the guard does not do

It never deletes from a real root. The 400 pre-existing buckets remain until the owner removes them.
