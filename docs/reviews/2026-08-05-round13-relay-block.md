# Round 13 relay block (Jesse: paste the fenced block to ChatGPT)

```
Peer review round 13 -- NARROW re-verdict. Read the artifacts, not any
summary; if you find yourself reviewing a summary, STOP.

Repository: LstDtchMn/ScanHound
Branch: agent/hybrid-sweep-rebased   <-- THE REBASED BRANCH (O-6 done)
Head: 32d90e0 (plus this relay commit)
Base: main @ 7adb17b -- now the TRUE merge-base: 90 commits rebased with
zero conflicts; cherry-pick dedupe verified (0 duplicated patches; git
auto-skipped both). The pre-rebase branch is retained read-only.

Every round-12 open item is closed ON THE REBASED HEAD:

1. F5 bookmark ambiguity (3089f6f): unresolved/absent media type gets its
   own 'ambiguous' discriminator -- can never collide with a confident tv
   or movie bookmark; season never infers persistent identity. Red-first.
2. R-4 race (3089f6f): the REAL interleaving property -- two independent
   DatabaseManager handles, BOTH serialized orders (heal-then-reconcile,
   reconcile-then-heal), final state proven current-version +
   current-state + fresh blob either way.
3. 17-field survival parameterization (3089f6f) -- WHICH FOUND TWO MORE
   REAL DEFECTS: complete_hdencode_hydration silently DROPPED episode_end
   and hevc_evidence (guarded as detail authority, never writable by
   detail). Both added to the COALESCE SET, same commit. A meta-guard
   asserts one CASE per protected field.
4. R-5 final-consumer closure (c48246c, 12/12): cross-path media-type +
   provisionality through BOTH production resolver compositions (six
   shapes incl. tokenless TV, conflict, unresolved); the real
   /results/cached route (category facets follow the carried verdict,
   bookmark annotation, and the FULL stale lifecycle: visible before,
   visible-but-out-of-skip-set while stale, healed+current+fresh blob
   after); real queue_action persisting the package key; EXACT denial
   codes -- WHICH FOUND A THIRD DEFECT: the provisional gate was INERT in
   production ('is True' vs the integer 1 every DB row carries; only
   bool fixtures ever fired it). Fixed to truthiness, same commit.
5. Contract rev 3.1 (32d90e0): self-contained (rules included verbatim),
   exact rebased SHAs everywhere, R-8..R-16 as individual rows, C-2
   honest built/pending, R-1/R-5 verdict-pending not overclaimed. Also
   corrects the 20-vs-21 commit-message discrepancy you flagged.
6. F1 mutation case COMMITTED into tests/tools/mutation_check.py and the
   full harness run: all five mutations discriminate (corrected->pass,
   defective->fail).
7. Harness at the rebased head: provenance check passed; ONE divergence
   change surfaced -- 2160x1080 ultrawide OLD 4K -> NEW 1080p, exactly
   the Jesse-ratified narrowing -- consciously re-baselined WITH AN
   HONESTY NOTE: this should have been flagged when 9d5df4c landed, so
   the earlier harness invocation in that chain evidently did not run as
   believed; the provenance-checked run caught it.

Full suite at the rebased head: 4777 passed / 1 failed on the first run --
the 1 was an old fixture exercising the exact season-inferred bookmark
identity round 12 ordered removed (row with no media_type + a saved 'tv'
key). Fixture updated to carry the verdict, as production rows do; file
58/58 after. Full green rerun result follows in-thread with the CI URLs.

Still open, declared, all Jesse/billing-gated: rebased-head CI with
executed steps (O-5), R-7 formal sign-off, then R-8..R-11, R-2b
post-deploy.

Verdicts requested:
Q1 Do the four round-12 items now close (F5, R-4 race, R-5, rev 3.1)?
Q2 The three NEW defects found by your required hardening (episode_end,
   hevc_evidence, inert provisional gate): fixes acceptable as landed?
Q3 With CI + R-7 as the only remaining gates: is the rebased branch the
   merge candidate, and does anything in this review block R-8..R-11?
```
