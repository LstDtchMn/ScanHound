# Round 12 relay block (Jesse: paste the fenced block to ChatGPT)

```
Peer review round 12 -- NARROW re-verdict round. Read the artifacts in the
repo, not any summary; if you find yourself reviewing a summary, STOP.

Repository: LstDtchMn/ScanHound (public, GitHub connector)
Branch: agent/hybrid-sweep-implementation
Head: 5268bcf (plus this relay commit)   Base: main @ 7adb17b
(merge-base 7cc5275; rebase is O-6, queued directly behind this round)

Every round-11 finding and required-sequence item 1-7 is closed. Verify:

F1 (P0, demotion): BackgroundScanner follows the EFFECTIVE cycle mode;
   demoted cycles durably carry demoted_from + promotion_gate_blockers;
   behavioral scan_once test proves listing crawl AND shadow comparison run
   under demotion; mutation check (propagation disabled -> exit 1).
F2 (P1, cache heal): conflict-upsert writes derived_state='current' (the
   re-derivation boundary); red-first end-to-end heal + the deterministic
   no-unheal race property your contract row demanded.
F4: DetailScraper CALLS select_release_year (perturbation test: patched
   selector -> scraper obeys). Commit 3a51fce.
F5: results.py consumers follow the carried verdict -- intentional tested
   precedence (explicit category > verdict > DECLARED-LIMITATION season
   fallback for ambiguous only, display-only); bookmark keys from the
   verdict, imdb short-circuit unchanged; old mis-derived keys orphan by
   design. Commit e5d996f, red-first (5 failed pre-fix).
Q5 width: dimension policy NARROWED per Jesse popup -- width==2160 removed
   (2160x900 and portrait 2160x3840 are unknown), docstring states the
   actual policy. Commit 9d5df4c, red-first.
Harness: expected-file provenance validated (old-SHA mismatch -> exit 2,
   proven both directions). Security branch: spliced comment repaired,
   measurement retained.
Item 6 (R-5): tests/test_r5_boundary_suite.py (627bab6) -- the EXECUTED
   suite: cross-path contract-field equivalence through the real RSS parser
   and real deployed facade over five matrix shapes; results, auto-action,
   packaging, cache-visibility groups; 20/20. It refuted two of our OWN
   inventory assumptions (S104 + route-conflict resolve to confirmed TV by
   title authority -- by design), recorded as an executed finding in-suite.
Item 7: contract REV 3 (5268bcf) -- Tracks 2/5/6 as full rows, statuses
   live to this head, S-4 re-run-at-merge, R-7 formal-sign-off distinction,
   rebase as O-6.

Full suite at head: 4764 passed / 0 failed / exit 0 (12:25 wall).
Still open and NOT claimed: R-1's 17-field survival-test parameterization
(your hardening ask -- lands with the rebase batch), rebased-head machine
attestation (billing), R-7 formal sign-off, R-2b post-deploy.

Verdicts requested:
Q1 Findings 1/2/4/5 + Q5: closed?
Q2 R-5: does the executed suite satisfy the exit criterion?
Q3 Contract rev 3: acceptable as the definition of done?
Q4 Anything besides the declared open items blocking O-6 rebase ->
   R-7 sign-off -> R-8..R-11?
```
