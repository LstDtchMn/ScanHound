# Round 15 relay block (Jesse: paste the fenced block to ChatGPT)

```
Peer review round 15 -- the two round-14 opens are closed. Read the
artifacts, not any summary; if you find yourself reviewing a summary,
STOP.

Repository: LstDtchMn/ScanHound
Branch: agent/hybrid-sweep-combined -- THE CANDIDATE NOW. It is the
  reviewed branch plus a merge of main after PR #40 landed (see the
  combined-tree section below). agent/hybrid-sweep-rebased is unchanged
  and remains the branch your round-14 comments were written against.
Last CODE commit on the reviewed branch:
  70939859e620e8398ba95b6eb53c33abef1c308d
Combined-tree merge commit: f77ace7
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

4. TWO THINGS YOU HAVE FLAGGED EVERY ROUND ARE NOW CLOSED.

   CI ATTESTATION (O-5). Root-caused rather than waited out: this was
   never an outage. The repo was PRIVATE, so Actions minutes are
   metered, and 1,801+ of the 2,000 monthly minutes were burned in
   three days. The tell is mechanical and worth reusing -- every failed
   run since 2026-08-03 14:20Z had **0 steps executed**, i.e. the job
   never started, whereas a genuine test failure on 08-01 showed 14-15
   steps. The repo was made public (unlimited minutes) after a
   full-history secret scan came back clean: gitleaks over all 1,008
   commits found only the public Python release-manager GPG fingerprint
   baked into the base image; .gitignore has always covered
   .env/config.json/data//*.db and data/ was never committed.

   Green runs, executed steps, machine-attested:
     main after PR #40 ......... actions/runs/30947333538 (16/12/12)
     COMBINED candidate ........ actions/runs/30948928368 (16/14/14)

   COMBINED-TREE VALIDATION, which you required once an approved branch
   advanced main. PR #40 (category-switch) merged as af9c299.
   agent/hybrid-sweep-combined @ f77ace7 is the reviewed branch plus a
   merge of that main. Deliberately a MERGE onto a NEW branch, not a
   rebase: contract rev 3.2 binds evidence to exact SHAs and a rebase
   would rewrite every one.

   The merge was clean, but that is not the claim. Both branches modify
   backend/api/routes/results.py, and after the merge the two
   behaviours compose in a SINGLE expression in _filter_and_sort:
       enabled = set(category) - {CATEGORY_NONE_SENTINEL}
       ... _effective_category(i) not in _KNOWN_CATEGORIES
           or _effective_category(i) in enabled
   So the suite is the proof, not the merge's silence:
     4797 passed / 0 failed / 4 skipped / exit 0
     docs/reviews/evidence/2026-08-06-full-suite-combined-f77ace7.txt
   That is FOUR MORE than the branch alone -- PR #40's
   tests/test_results_category_sentinel.py now runs beside this
   branch's media-type authority tests.

Still open and declared: R-7 formal sign-off, R-8..R-11, R-2b
post-deploy. NOTE agent/audit-fixes-2026-08 is still in review and also
overlaps this branch (database.py, rename/service.py,
tests/test_rename_service.py), so a THIRD combination will be needed
once it lands.

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
Q3 With R-4, the contract, CI and the combined tree all now closed, is
   agent/hybrid-sweep-combined the merge candidate gated ONLY by R-7
   sign-off -- and should the audit-fixes combination happen before
   R-7, or after this merges?
```
