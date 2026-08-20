
### Round-12 verdict processing (2026-08-04/05)
- **Verdict:** F1/F4/Q5 + F2-core CLOSED; O-6 rebase GREENLIT immediately. Open: F5 bookmark ambiguity,
  R-4 real race test, R-5 final-consumer closure (5 sub-items), contract rev 3.1 (self-contained, exact
  SHAs, degroup R-8..R-16, honest statuses), + declared (17-field, rebased attestations, R-7).
- **REBASED: `agent/hybrid-sweep-rebased`** — 90 commits onto main 7adb17b, ZERO conflicts, dedupe
  verified (0 duplicated patches; git auto-skipped the two cherry-picks). Old branch untouched (no
  force-push). First fix batch pushed as `3089f6f`: F5 ambiguous bookmark discriminator (red-first),
  REAL two-handle both-orders race test, 17-field survival parameterization — **which found TWO more
  real defects: complete_hdencode_hydration silently DROPPED episode_end and hevc_evidence** (now in
  the COALESCE SET). 14/14 + 29/29 + 283/283.
- **REMAINING:** R-5 minimum closure (cross-path media-type/provisional incl. tokenless/conflict/
  unresolved via the REAL listing resolver call site; real results-route test w/ category+facets+
  bookmark annotation; real queue_action + persisted package-routing; cached-results route before/
  during/after stale; exact denial codes) → contract rev 3.1 → F1 mutation case committed → full suite
  + harness at final rebased head → round-13 relay. NOTE for relay: 627bab6 msg says 21/21, actual 20/20.
