# Category-switch fix relay, round 2 (Jesse: paste the fenced block to ChatGPT)

```
Peer review round 2 -- category-switch fix. Both blockers from your
CHANGES-REQUIRED verdict are fixed and behaviorally tested. Read the
artifacts, not any summary; if you find yourself reviewing a summary,
STOP.

Repository: LstDtchMn/ScanHound
Branch: agent/category-switch-cache-fix
Head: b5dd04b (plus this relay commit)
Base: main @ 7adb17b
Prior reviewed code commit: 327d305

Blocker 1 (empty selection): the empty set now crosses the API as an
explicit sentinel -- category=__none__, named CATEGORY_NONE_SENTINEL
in backend/api/routes/results.py and subtracted from the enabled set,
so every known category hides while unknown/'search' items keep their
always-show behavior. Named contract test:
tests/test_results_category_sentinel.py (sentinel hides knowns/keeps
unknowns; sentinel inert when mixed; omitted param still means
no-filter; _csv carries it). Honesty note: the raw behavior was
ALREADY accidentally correct on main (any unknown token filters to the
empty known-set), so the red axis is the regression, not the feature:
a mutation run injecting the empty-means-all "cleanup" fails the named
contract test (1 failed / 3 passed). Your three requested tests all
exist: live-mode last-category-off (asserts __none__ on the wire),
paged-mode last-category-off (debounced fetch asserts __none__), and
the server-side hides-all-knowns contract.

Blocker 2 (remote/scheduled scans): activity now follows the
backend-observed lifecycle, all four of your required mechanisms:
scan:progress adopts 'running' when idle (never overriding
'stopping'); handleScanResult sets the activity flag as the stream
backstop; scan:complete/scan:error clear it EXPLICITLY in scanner.ts
(a streamed-only scan never flips scanState, so the mirror alone
cannot clear it) plus handleScanComplete clears it in results.ts; and
reconcileScanActivity() (exported) queries api.scanStatus at startup
and on every reconnect -- covering both joining mid-scan (falsely
idle) and the missed-completion stuck-flag case. Your required
behavioral test exists: scanState 'idle', simulated scheduled-scan
stream, toggle stays live with zero cache requests, completion
re-arms the exit. Plus: scan:progress-only (nothing streamed yet) and
reconnect-reconcile variants, driven through the recorded WS-handler
mock and the real scanner store.

Nonblocking note closed as well: the live exit now drops the live rows
at the flip, so a failed cache request shows the loadError state
rather than rows contradicting the selected chips. Tested (rejected
fetch -> paged, results empty, loadError true).

Evidence at b5dd04b: the six new tests are red at the prior head
(6 failed / 120 skipped, exit 1) and green here -- vitest 402 passed /
0 failed / exit 0; svelte-check 0 errors; vite build exit 0; backend
subset (sentinel contract + full test_api_results.py) 62 passed /
exit 0 in a throwaway container. CI remains billing-blocked
(author-attested local evidence, declared).

OVERLAP CHANGE you should re-verify: this round adds
backend/api/routes/results.py and tests/test_results_category_sentinel.py
to the branch. agent/hybrid-sweep-rebased also edits results.py, so
round-1's zero-overlap measurement no longer holds. The regions
differ (_filter_and_sort sentinel block vs _effective_category/
bookmark work) and the new test file is overlap-free, but combined-
tree validation after this merge is now mandatory rather than
precautionary -- consistent with what your round-13 verdict already
requires for the rebased branch.

Verdicts requested:
Q1 Do both blockers now close?
Q2 Any objection remaining to merging this ahead of hybrid-sweep,
   given the now-nonzero (small, disjoint-region) results.py overlap?
```
