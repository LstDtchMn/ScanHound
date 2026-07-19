# SH-R03 durable-trash review — HELD, 3 defect classes

Branch `fix/durable-trash-transaction`, stacked on accepted SH-R02 `70dca70`.
Applied cleanly (4 files, compiles, whitespace clean) but **not committed**.

Result: **5 failed, 244 passed** — identical as root and as uid 1000.

## A. Three of the new tests never run — harness bug

```
tests/test_trash_durability.py::test_resolve_keep_plex_trash_failure_is_not_success
tests/test_trash_durability.py::test_resolve_keep_plex_archive_failure_restores_download
tests/test_trash_durability.py::test_overwrite_trash_preparation_failure_changes_neither_file

E  AttributeError: property '_db' of 'RenameService' object has no setter
   tests/test_trash_durability.py:208  ->  service._db = db
```

`RenameService._db` is a read-only property. The `_bare_service()` helper assigns
to it, so all three abort in setup. **The new SH-R03 acceptance criteria are
therefore currently unproven** — these are exactly the tests that would
demonstrate the new transaction order works.

## B. `test_keep_plex_trash_failure_still_archives` — deliberate contract change

```
tests/test_rename_service.py:2350   assert out["ok"] is True   ->   False
```

This is **evidence the correction works**, not a defect. The old test pins the
superseded unsafe contract (archive proceeds and reports success even when
trashing the download failed). The correction deliberately reports failure
instead. The test name itself — "still archives" — encodes the behaviour being
removed.

Needs a formal supersede + rewrite to assert the new contract, same shape as the
PR #7 source-health contract test. Not fixed here.

## C. `test_trash_exdev_never_cascades_to_appdata` — root preference changed

```
tests/test_rename_core.py:579
  expected trash under  .../volume-trash
  actual  trash under   .../  (volume root)
log: "crossed a mount boundary ...; using verified copy + unlink at /.scanhound-trash"
```

Verified, so this is not overstated:

```
_TRASH_ROOT (app-data)  = /data/.local/share/scanhound/trash
_trash_root_for('/')    = /.scanhound-trash
is /.scanhound-trash app-data?  False
```

**No app-data cascade** — the safety property that originally blocked PR #12
still holds, and the destination remains same-device. What changed is that the
EXDEV path now selects the volume-root trash rather than the deeper writable
ancestor this test pins.

Design call for ChatGPT: is volume-root selection intended under the new
transaction order, or should deeper-ancestor preference be retained? Either the
implementation or the test should move — not both, and not by the reviewer.

## Not a regression

`test_trash_exdev_never_cascades_to_appdata` and
`test_keep_plex_trash_failure_still_archives` both pass on the SH-R02 parent
`70dca70`, so all five failures are attributable to this package.
