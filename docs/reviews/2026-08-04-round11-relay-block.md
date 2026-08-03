# Round 11 relay block (Jesse: paste the fenced block to ChatGPT)

```
Peer review round 11 — read the artifacts in the repo, not any summary.
If you find yourself reviewing a summary, STOP and say so.

Repository: LstDtchMn/ScanHound (public, GitHub connector)
Branch: agent/hybrid-sweep-implementation
Head: b823220 (plus this relay-block commit on top)
Base: main @ 7adb17b (merge-base still 7cc5275; the final rebase is queued
behind this round per your own item 10)

CI CAVEAT unchanged: account billing still blocks Actions; every commit
after the cutoff is locally evidenced with unmasked exits and committed
artifacts. Green-run URLs follow in-thread the moment billing returns.

EVERY round-10 required-sequence item except the rebase is DONE. In your
order:

1. Q3 P0 (feed-upsert authority): FIXED test-first per your exact recipe --
   2638b24. All 17 detail-authority fields guarded behind
   hydration_state='completed'; feed-only facts still refresh; pre-hydration
   control green. Your recipe's invalidation half lands via R-4 (below).
   ONE REFUTATION of round 10: your Q7 note said no 10/10 S-4 manifest was
   at the reviewed tip -- docs/reviews/evidence/2026-08-03-s4-flake-manifest.txt
   (run10, completed 18:17:38Z) landed in eec7f2e, an ANCESTOR of 66764d5.
   Verify and amend.
2. Hermetic differential harness: scripts/r3_differential_harness.py
   (c2cbfc5) -- exact-SHA worktrees, subprocess isolation, embedded 71-case
   corpus, committed expected-divergences file, nonzero on unexplained OR
   vanished diffs. Discrimination proven twice: --new f172d1f (the flawed
   R-3) exits 1; the item-3 fixes were flagged as exactly four new
   divergences before the conscious re-baseline (be01638). Re-run it.
3. Shared-behaviour fixes (2981d52 + ratified follow-ups): episode
   glued-suffix constraint (S01E01FOOBAR is a name; x264/x265/h264/h265
   still glue); right-bounded size labels; span/list episode counting;
   blank-Filename rejection. RATIFIED BY JESSE via popup and implemented:
   rightmost-plausible release year as the SINGLE year authority
   (select_release_year -- RSS reader, SourceBase, DetailScraper,
   metadata_start all consult it; 'Blade Runner 2049 2017' = 2017
   everywhere, pinned by an EXECUTED cross-path oracle, not a mock);
   1080i as a real token folding to 1080P; 3840-wide scope crops = 4K
   recorded as product policy. Expected-divergence file re-baselined per
   change; full suites green at each step.
4. R-2 closure (your Q1 split): R-2a evidence complete -- the corpus tool
   now runs the ACTUAL canonicalizers (355e164): every stored URL a fixed
   point (0 rejected / 0 noncanonical / 0 collisions over 3,350 distinct),
   the NAMED bridge post_to_listing_identity measured directly (119/120
   shadow with the 1 known genuine absence; 117/152 exclusions with the 35
   known out-of-population), DISTINCT added, fresh snapshot sha 662202ab….
   The frontier-identity override is REMOVED (the stamped-lie defect you
   flagged). R-2b remains the pinned-deploy window gate, by design.
5. R-5 consumer inventory (691468a): 27 ranked boundaries, full structured
   record in evidence/; verified negatives bound R-4's scope (Kometa +
   rename decisions + Phase A instruments consume no derived facts;
   candidate-details payload is WRITE-ONLY). Two NEW defect candidates for
   THIS round's verdict: results.py:35 and :578 still derive TV-ness from
   `season is not None` -- the exact forbidden reconstruction.
6. R-4 COMPLETE per your Q5 model, three commits: 7f23bd0 (GRAMMAR_VERSION
   + dedicated version/staleness columns + three stamped write boundaries),
   607ebef (reconciler: refetch_required + requeue for hydrated mismatches,
   cache skip-set exit + parse-cache generation bump, auto-action stale
   exclusion RAISE-based -- our own test caught the first version silently
   passing stale rows), b318df0 (reparse_feed_facts = THE shared
   composition used by live ingest AND the reconciler; stale non-hydrated
   rows heal offline -- proven on a real S0MEGRP season-0 artifact; anti-
   drift test: reparse of a fresh row is a no-op).
7. R-6 WIRED per your Q5 boundaries (c8f359b): mode admission 409s with
   blocker lists; per-cycle DEMOTION to shadow on gate invalidity (polling
   never gated, never a silent skip -- your fail-dangerous point);
   auto-grab at the queue entry; BOTH real rename admission sites;
   parser_version always from the RUNNING grammar so a recorded verdict
   cannot vouch for a parser it was not measured against; a parser bump
   closes an open gate (tested). tests/tools/gate_pass.py makes every
   open-gate test assumption explicit.
8. Contract rev 2 (f6fb68d, docs/reviews/2026-08-04-completion-contract-rev2.md):
   your Q7 loopholes closed IN the tables -- five contract-wide rules,
   R-2a/R-2b split, three-role columns, deferral schema, S-1 pointing at
   the green runs. Q7 re-verdict requested.
9. S-2 revision per your Q4 (same commit): brattain = integration base, no
   standalone merge; threadleak fail-mode + the ratified notifications
   opt-in migration pre-merge.
10. Security round 2 on agent/security-track-c (c64e591 + 637889e):
    anchored-then-RETIRED path allowlist -- exact-fingerprint
    .gitleaksignore suppression (probe: a PAT inside a reviewed evidence
    file now REPORTS; history clean on fingerprints alone); action + both
    checkout steps pinned by SHA; GITLEAKS_VERSION pinned (our AKIA claim
    was INVERTED for CI -- 8.24.3 flags it); comments + SARIF artifact
    disabled; weekly full-history baseline job; .gitleaksignore fix in the
    procedure; measured limitation documented: rule-scoped suppression
    (targetRules / [[allowlists]]) is non-functional under [extend] on
    8.24.3-8.28.0. CODEOWNERS/required-check remains Jesse-side.
11. Full suite at head: 4731 passed / 0 failed (12:04 wall), exits
    unmasked, plus per-slice subset evidence in every commit message.

Questions requiring verdicts:
Q1 Gate items: do R-1 (with the Q3 fix), R-2a, R-3, R-4, R-5, R-6 now
   close? List anything short.
Q2 The two season-is-not-None defect candidates (results.py:35, :578):
   confirm and we fix test-first under the standing authorization.
Q3 Contract rev 2: acceptable as the definition of done?
Q4 The S-4 manifest refutation: amend your Q7 note?
Q5 Ratified-behaviour audit: any objection to the implemented year/1080i/
   scope-crop/demotion semantics as landed (all declared above)?
Q6 What, if anything, blocks the final rebase + Jesse's merge sequence
   (R-8..R-11) once CI attests?
```
