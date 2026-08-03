# Round 9 relay block (Jesse: paste the fenced block to ChatGPT)

```
Peer review round 9 — read the artifacts in the repo, not any summary of them.
If you find yourself reviewing a summary, STOP and say so.

Repository: LstDtchMn/ScanHound (public, GitHub connector)
Branch: agent/hybrid-sweep-implementation
Head: c2cb3d4
Base: main @ 7cc5275

Scope of this round, in order:
1. Round-8 P0 closures: commits 604be2e, 09a335f, b1825f1 (tri-state media
   type persisted through cache and hydration; unresolved-after-hydration is
   terminal). CI green on all three SHAs.
2. docs/reviews/2026-08-03-completion-contract.md (6fdfa81) — the completion
   contract: binary exit criteria + owners for every track. Attack vagueness:
   any criterion you could argue is satisfied without the work being real.
3. docs/reviews/2026-08-03-canonical-url-inventory.md (c2cb3d4) — seven-item
   gate item 2. Verify the measurements you can (the SQL is quoted), then
   attack: (a) the proposed closure criteria in §5 — sufficient? (b) the P0 in
   §4 — we claim entry.link is an AttributeError swallowed at
   hdencode_rss_service.py:339 and the symmetry test's FakeEntry masks it;
   confirm against the tree. (c) the unbound sweep-ledger canonicaliser —
   which form should the frontier key use, A (feed, trailing slash) or B
   (listing/shadow, stripped)? We lean A with a versioned shared module.

Tests: CI run on head + three local full-suite runs 4660/4664/4668 passed,
0 failed, 4 xfailed (the four DetailScraper-gap strict xfails, deliberate).

Questions requiring a verdict:
Q1 Round-8 P0s: closed or not?
Q2 Contract: acceptable as the definition of done (subject to Jesse's batch)?
Q3 Inventory: does item 2 close once §5's five criteria land, or is more needed?
Q4 The #191 P0 fix plan (real-dataclass failing test first): correct?
```
