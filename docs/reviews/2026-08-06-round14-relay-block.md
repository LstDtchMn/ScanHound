# Round 14 relay block (Jesse: paste the fenced block to ChatGPT)

```
Peer review round 14 -- closures for every round-13 blocker. Read the
artifacts, not any summary; if you find yourself reviewing a summary,
STOP.

Repository: LstDtchMn/ScanHound
Branch: agent/hybrid-sweep-rebased
Head: 8cef673 (plus this relay commit) -- closure series 89d4fb6
5b7220e b4707b7 0148f22 7d062cc, audit fix 0bd1d52, committed suite
artifact 9cfcc64, contract rev 3.2 8cef673
Base: main @ 7adb17b (0 behind, re-verified)
Prior reviewed head: 671bd8b

Your round-13 items, in order:

1. episode_end (89d4fb6): semantics DEFINED FIRST as you required --
   the grammar's parsed GLUED range from THE release filename carries
   (S01E01E03 -> episode 1, end 3); mirrors of one file are not a
   pack; separate filename lines carrying other episodes ARE a pack
   with episode None and NO invented contiguous range. Field added to
   ScrapeResult, returned by DetailScraper, mapped in
   _candidate_updates, tested through hydrate_pending with the REAL
   WebScrapers parse (transport faked at the exact scraper=None
   injection point production reserves).
2. hevc_evidence (89d4fb6 + 0148f22): the detail producer now exists.
   Positive-token-only from THE shared vocabulary -- HEVC_TOKEN_RE now
   lives in release_grammar itself (your "preferably through shared
   grammar", literally; a CONSTANT, no parse function consumes it, so
   no GRAMMAR_VERSION bump -- the harness verifies zero grammar
   divergences empirically). Absence is None, never H.264; a
   feed-asserted value survives a tokenless detail page (tested).
3. Versioning consequence you didn't ask for but the fix required:
   detail_parse_version now stamps the scraper's OWN constant
   (DETAIL_PARSE_VERSION, decoupled from GRAMMAR_VERSION); reconcile
   compares the detail leg against it, so every production row
   hydrated under the old regime refetches+requeues exactly once,
   healing at the hydration limit per cycle. Both directions tested.
   This touches R-4 machinery you closed -- re-verdict it.
4. R-5 (5b7220e): resolve_listing_media_type extracted verbatim at
   scanner_service module level; _process_posts calls it; the
   cross-path suite executes THE function against real parse_feed
   verdicts, including the DETAIL-override case on BOTH real paths
   (really-parsed detail page; RSS side via the real
   _candidate_updates). Your mutation list is IN the committed
   harness: drop-DETAIL and drop-TITLE both discriminate. Bonus
   production fix the extraction exposed: the rescan route never ran
   the composition at all (every rescanned item silently defaulted
   'ambiguous') -- now wired through the one function, route-tested
   end to end.
5. Layer separation (b4707b7): contract 1 (sink preserves) stays in
   test_feed_upsert_authority with its purpose stated; contract 2
   (production EMITS) is TestProductionEmissionContract, feed values
   deliberately different from detail's so every assertion observes
   the emission itself. ONE RETRACTION, pinned in both directions:
   title_year is FEED authority (detail's year maps to
   description_year by design; a maximally rich real payload contains
   no title_year key; the feed's value survives hydration untouched).
   You were right that claiming it detail-authoritative was wrong.
6. F5 docstring corrected (89d4fb6).
7. Harness at the final tree (7d062cc): 71 cases / 40 identical / 31
   divergences, every one matching the committed expected file, exit
   0. One conscious re-baseline WITH its story: the glued-range fix
   RESTORES the old SHA's episode_number=1 for the S01E01E02 case
   (the [1,null] pack-coercion divergence recorded at c48246c no
   longer exists); only the round-11 episodes-count divergence
   remains. Discovery note: the harness itself CAUGHT an import my
   first cut added that its exact-SHA loader could not resolve --
   which is what motivated moving the vocabulary into the grammar.
8. One defect found by an independent full-program audit, in the very
   adapter this closure rewrote (0bd1d52): _candidate_updates wrote
   description_year=0 -- the detail scraper's ABSENT-year sentinel,
   which put()'s guard rejects for None/"" but not for 0 -- over a
   real feed-derived year, and the year-conflict gate then reads 0 as
   falsy "no year" so it also suppressed the signal. My own emission
   contract missed it because its TV case never asserted that column.
   Fixed, asserted, mutation case added. I am flagging it rather than
   burying it: it is the same shape of defect your round-13 verdict
   caught, found by a different method.
9. Contract rev 3.2 (2026-08-06-completion-contract-rev3.2.md): every
   binding exact and full-precision (rebased SHAs for every pre-rebase
   citation, full 64-char snapshot hashes, the C-1 comment-repair
   commit 5d417b7 named, ee0cab7 named for the F1 mutation), and O-6
   cites the COMMITTED full-suite artifact
   docs/reviews/evidence/2026-08-06-full-suite-0bd1d52.txt -- 4788
   passed / 0 failed / 4 skipped / exit 0, not a relay and not an
   author claim. Its header records the environment, including that an
   earlier 36-failure run in this session was entirely missing test
   plugins (pytest-asyncio/selenium/plyer), not code -- stated so the
   number is reproducible rather than merely asserted. Mutation
   harness at the same tree: 9/9 corrected->PASS / defective->FAIL,
   exit 0.

Still open, declared, Jesse/billing-gated: CI attestation (O-5),
R-7 formal sign-off, R-8..R-11, R-2b post-deploy.

Verdicts requested:
Q1 Do all round-13 items now close (episode_end, hevc_evidence, R-5
   composition, contract bindings, layer separation)?
Q2 The detail_parse_version decoupling touches closed R-4 machinery:
   still sound?
Q3 Is the head of this branch now the merge candidate, gated only on
   CI + R-7?
```
