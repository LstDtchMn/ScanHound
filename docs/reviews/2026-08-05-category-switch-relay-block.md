# Category-switch fix relay block (Jesse: paste the fenced block to ChatGPT)

```
Peer review -- SMALL STANDALONE FIX, independent of the round-13 thread.
Read the artifacts, not any summary; if you find yourself reviewing a
summary, STOP.

Repository: LstDtchMn/ScanHound
Branch: agent/category-switch-cache-fix
Head: 327d305 (plus this relay commit)
Base: main @ 7adb17b

One commit. User-reported bug: open the app (deck shows the last scan's
TV items), switch to 4K/Remux, list stays empty -- while the live DB
held 1623 fresh 4k / 184 remux cached rows (all last_seen within 38
min). Root cause: onMount's GET /results locks pagedMode=false ("live
mode"); a category chip then only client-filters the in-memory array;
the debounced refetch bails in live mode and nothing ever flips back.

Fix (Jesse-ratified design): toggleCategoryFilter -- the single writer
of categoryFilter -- exits live mode and refetches the server cache
when toggled outside a running scan. scanActive is mirrored in from
scanner.ts's scanState via setScanActive (that direction avoids an
import cycle). Mid-scan toggles keep the old client-side behavior (the
stream owns the deck). Plus one latent-bug fix the single-fetch proof
surfaced: the _filterKey debounce cleared its pending timer only on
the paged path; the clear now precedes the mode check.

Evidence: red-first discrimination run -- fix reverted (inert stub so
imports resolve): 3 failed / 117 passed, exit 1; the paged-debounce
test passes, pinning the unchanged axis. Green at 327d305: vitest 396
passed / 0 failed / exit 0; svelte-check 0 errors; vite build exit 0.
Declared limitation: browser-level (clicked-in-UI) check is
post-deploy -- the prod backend sits behind CF Access; the chip ->
toggleCategoryFilter wiring is unchanged code.

Full record: docs/reviews/2026-08-05-category-switch-live-mode-fix.md
(includes the proposed contract row D-7 for rev 3.2 and two side
findings routed elsewhere: rescan-item category poisoning -> its own
task; rss_primary deck starvation -> promotion-program input).

Verdicts requested:
Q1 Is the live-mode exit correct and complete (any category-switch
   path that still dead-ends, any state the scanActive gate misses --
   e.g. scheduled scans streaming in with scanState still 'idle')?
Q2 Is the debounce always-clear change safe (any caller relying on a
   pending refetch surviving a live-mode filter change)?
Q3 Any objection to merging this independently, ahead of the round-13
   branch? Measured at this head: main...agent/hybrid-sweep-rebased
   touches 105 files, NONE under frontend/ -- zero overlap with this
   fix's four files. Verify if you doubt the measurement.
```
