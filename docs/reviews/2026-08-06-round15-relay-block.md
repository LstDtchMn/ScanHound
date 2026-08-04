# Round 15 relay block (Jesse: paste the fenced block to ChatGPT)

```
Peer review round 15 -- the two round-14 opens are closed. Read the
artifacts, not any summary; if you find yourself reviewing a summary,
STOP.

Repository: LstDtchMn/ScanHound
Branch: agent/hybrid-sweep-rebased
Head: 717f759aaa95b7874dc791a820d68ccbb4d62bf2 (plus this relay commit)
Base: main @ 7adb17b -- 106 commits ahead, 0 behind (re-counted)
Prior reviewed head: 9ff626eac6f593e6635df4506a7823a2ed799330

You closed Q1 in round 14. Two things were open; both are fixed, and
you were right about both.

1. R-4 -- THE DEPENDENCY IS NOW MECHANICAL, NOT REMEMBERED.
   You caught that my own comment claimed a grammar change "already
   reaches detail rows because the stamps then differ too" while
   nothing in code made that true. Confirmed: DetailScraper delegates
   year selection, season/episode/range, size, resolution/dimension and
   the HEVC vocabulary to release_grammar, so a grammar bump changes
   what detail extraction produces -- and every completed row still
   compared equal against a FIXED detail stamp and was never refetched.

   DETAIL_PARSE_VERSION is now composite:
       DETAIL_CAPABILITY_VERSION + "+" + GRAMMAR_VERSION
   Either authority moving invalidates completed rows automatically,
   while a detail-only bump still leaves feed_parse_version alone --
   the decoupling round 13 wanted, now enforced.

   Second half of your finding, on completed rows' retained feed facts:
   they were excluded from the wholesale stale sweep for a good reason
   (that pass overwrites every parsed field and would destroy the
   detail facts the authority model protects), but excluding them
   entirely left the fields detail never supplies frozen at the OLD
   grammar's parse forever, since the upsert's CASE guards also stop a
   later poll touching them. _reparse_completed_feed_only() now
   re-derives exactly those, offline through the same shared
   composition, and re-stamps feed_parse_version. The set is named in a
   constant (_FEED_ONLY_ON_COMPLETED) with its reasoning; it is
   title_year today, verified against _candidate_updates' emitted keys.

   All five tests you specified: composite stamp composes both
   authorities; a grammar-ONLY bump invalidates and requeues a
   completed row; its feed facts are re-derived rather than skipped;
   the narrow pass touches no detail-authority field; autonomous action
   stays denied while either leg is stale. The pre-existing R-4
   idempotency test asserts the result dict EXACTLY -- updated
   deliberately for the new counter rather than loosened.

2. CONTRACT REV 3.2 -- corrected on every point you raised.
   Every abbreviated evidence binding is now the full 40-character SHA
   (36 of them, expanded with git rev-parse, not by hand). The
   ahead-count is re-measured: 106 at this head, not the stale 96. The
   R-4 row is rewritten for the mechanical binding. O-6 cites the new
   artifact below.

3. EVIDENCE, and a correction I am volunteering because it affects how
   much you should trust the number: my first attempt at the suite
   artifact RAN IN THE SAME CONTAINER as tests/tools/mutation_check.py,
   which temporarily rewrites source files to prove each regression
   test discriminates. That result was DISCARDED, not cited -- evidence
   produced while the source under test is being mutated is not
   evidence. The committed artifact is a clean-room rerun with the
   container running nothing else and source verified to carry zero
   mutation markers first:

     docs/reviews/evidence/2026-08-06-full-suite-717f759.txt
     4793 passed / 0 failed / 4 skipped / exit 0

   Differential harness at this tree: exit 0, 71 cases / 40 identical /
   31 divergences, every one matching the committed expected file.
   Mutation harness (run separately, afterwards): 10/10 corrected->PASS
   / defective->FAIL, exit 0.

Still open and declared, unchanged: CI attestation (O-5, billing),
R-7 formal sign-off, R-8..R-11, R-2b post-deploy.

MERGE-ORDER NOTE, per your own round-14 point: category-switch is
approved and audit-fixes is in round 2. Both advance main and both
overlap this branch (results.py; database.py + rename/service.py +
tests/test_rename_service.py). This branch will be rebased onto the
resulting main and re-validated as the exact combined tree before R-7,
with the contract and artifact rebound to that SHA.

Verdicts requested:
Q1 Does R-4 close -- is the composite stamp the right mechanical
   binding, and is _FEED_ONLY_ON_COMPLETED the right treatment for a
   completed row's feed facts (versus reparsing them wholesale)?
Q2 Does contract rev 3.2 now satisfy its own exact-binding rule?
Q3 With those closed, is this branch gated only by the combined-tree
   rebase, CI and R-7?
```
