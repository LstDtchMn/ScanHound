# Audit-fixes relay block (Jesse: paste the fenced block to ChatGPT)

```
Peer review -- STANDALONE AUDIT FIXES, independent of the hybrid-sweep
and category-switch branches. Read the artifacts, not any summary; if
you find yourself reviewing a summary, STOP.

Repository: LstDtchMn/ScanHound
Branch: agent/audit-fixes-2026-08
Head: 440682d (plus this relay commit)
Base: main @ 7adb17b

Context: a decomposed full-program audit (nine subsystem readers over
database, scanner core, RSS pipeline, API routes, rename/fileops,
DV/HDR/Kometa, frontend, infra, security) produced 15 critical/high
candidate findings. This branch carries only the ones I verified
against the code MYSELF, quoting it, before changing anything. The
remainder are still in adversarial verification and are NOT in this
branch.

Two defects, both on main today:

1. backend/rename/dv_detect.py -- DV detection failures were reported
   as an authoritative "no Dolby Vision". tempfile.mkstemp pre-creates
   the RPU output file at zero bytes and dovi_tool writes it only on
   success, so every genuine failure (read error on the media mount,
   truncated file, demux error) leaves rpu_size == 0. The empty-file
   test ran BEFORE the error discrimination, so those returned
   {layer: 'none', error: None} -- the exact inverse of what the
   surrounding comment says the code does. Reachability is the point:
   the media sits on 9p/SMB bind mounts where dovi_tool read failures
   are an expected event, and the verdict is persisted to dv_scan and
   consumed by the labeler. Now "no DV" is positive-only: a clean exit,
   or dovi_tool itself saying "no RPU"/"not found". Everything else is
   'unknown' with the error preserved.
   Test-quality note worth your attention: the existing hard-error test
   passed only because it used rpu_size=5 -- a state dovi_tool never
   leaves behind on failure -- so the suite had no power on the axis
   the bug was on. Three cases added for the shapes that actually
   occur.

2. backend/api/routes/watchlist.py:296 -- the Trakt import called
   mgr.add_item(); WatchlistManager has no such method (it is add(),
   backend/watchlist.py:290). Every item raised AttributeError into the
   per-item `except Exception`, so the endpoint always answered HTTP
   200 with imported: 0 and imported nothing. The regression test
   spec's its mock deliberately: a bare MagicMock invents add_item and
   would keep the test green against the defect.

Evidence: red-first -- all three new tests fail on the unfixed tree
(3 failed / 44 passed) with pre-existing tests green throughout; green
at 440682d -- 363 passed / 0 failed / exit 0 across the dv_detect,
dv_labeler, dv_host_scan, watchlist, watchlist-route and api-route
suites in a throwaway container. CI remains billing-blocked, so this is
author-attested local evidence (declared).

Verdicts requested:
Q1 dv_detect: is "no DV" positive-only the right semantics, and does
   the new branch ordering miss any failure shape (e.g. should a clean
   exit WITH a nonempty RPU but unparseable info also be 'unknown' --
   it currently is; confirm)?
Q2 Should the 'unknown' results now produced on mount errors trigger a
   retry/backoff anywhere, or is leaving them for the next host run
   correct?
Q3 Any objection to merging this ahead of the other two branches?
   Overlap: none with agent/hybrid-sweep-rebased; none with
   agent/category-switch-cache-fix.
```
