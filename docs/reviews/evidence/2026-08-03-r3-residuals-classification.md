# R-3 rework — residual differential classification (post-rework harness run)

Baseline: OLD = pre-unification DetailScraper (f172d1f~1). NEW = tree after
rework commits 8606d8c + a86e5e2. Harness: 71 cases, 45 identical, 26
differing — full output in 2026-08-03-r3-harness-post-rework.txt.
NOTE for re-runners: the harness loads module snapshots from the session
scratchpad; refresh new_detail_scraper.py and release_grammar_real.py from
the tree before trusting a re-run (the first re-run measured a stale copy).

**The three review regressions are GONE from the diff list** (old and new now
agree): 'x264-S0MEGRP' is a movie on both sides; 'Movie Title (2020)' titles
match; '2001.A.Space.Odyssey.1968' titles/years match.

All 26 residuals fall into six DECLARED families:
1. Episode-guard widening (underscore/paren/string-start/glued-codec S01E01
   forms now parse as TV) — improvements, shared-grammar semantics.
2. S104-family title cuts (title now cut at the ambiguous token; typed
   claims unchanged: is_tv stays False).
3. Size units: KB no longer parses; TB now does; grammar GiB values now
   computed correctly for ranking.
4. Resolution: page-line vocabulary now the shared token set (720p/4K/UHD)
   plus the named dimension bridge; the leftover substring override is gone,
   so an explicit page value is no longer beaten by filename fragments
   ('GRP1080', 'x2160z'); bare '1080i'/'2160'-substrings alone yield '?'.
5. Year guards: '20201' and '1920x1080' no longer produce years; where no
   metadata token exists at all the raw filename remains the title (honest,
   not inferred).
6. Pack episode counting through the grammar (glued E-lists now counted).
