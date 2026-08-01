# A4 — the parity check cannot be built as specified, and the reason matters

**Date:** 2026-08-01 · **Plan item:** 0.1 (R6) · **Status:** finding, not a fix

R6 proposed a harness asserting that a release discovered by RSS reaches the
**same actionable decision** as the same release discovered by the listing
crawl. Building it turned up something that should be settled before the
qualification window opens: **the two paths share no parsing code and no
decision point.** "Identical decision" is not currently a comparable property,
because only one of the two paths produces a decision at all.

Everything below was verified against `agent/hybrid-sweep-implementation`.

---

## Three facts

**1. The candidate pipeline is fed exclusively by RSS.**

`hdencode_candidates` has exactly one writer repo-wide:

```
backend/database.py:1448          INSERT INTO hdencode_candidates (
backend/database.py:1419          def ingest_hdencode_feed(...)      <- the only definition
backend/hdencode_rss_service.py:346   self.db.ingest_hdencode_feed(...)  <- the only call
```

Nothing on the listing side writes a candidate row. `HDEncodeTrafficCoordinator`
is a rate-limit and priority coordinator, not an ingestion merge point.

**2. The two paths parse release titles with different code.**

`parse_release_title` is defined at `backend/sources/hdencode_feed_parser.py:180`
and called from exactly one place, `:142`, inside the feed parser. The listing
path never touches it — `backend/sources/hdencode.py:193-194` uses its own
`extract_resolution()` and `extract_size()`.

So resolution, size, year, season and media-type are each derived twice, by two
independent implementations, from the same underlying release title.

**3. The shadow comparison compares URLs, and only URLs.**

`compare_shadow` (`backend/hdencode_shadow.py:79`) reduces both sides to
canonical URL sets and computes `duplicate` / `feed_only` / `listing_only`. A
relevant miss is a listing URL absent from RSS. Nothing downstream of the URL is
compared — not the parsed fields, not the classification, not the action.

---

## Why this matters more than a missing test

A4 says a URL arriving is insufficient, and that the candidate must reach the
same actionable decision the listing path produced. Today the evidence stops at
fact 3: we know the same URLs arrive. We have never compared what happens next,
and there is no code path that would notice a divergence.

**This is a known-real failure mode on this project, not a hypothetical.** The
full-disc `[BD]` defect was exactly it: full-disc releases have no `Filename`
field, the listing side learned to exclude them, and the RSS side did not — an
asymmetry that lived in the difference between the two extraction
implementations. `#191` fixed that one instance by giving both sides a shared
`is_full_disc_title()`. It did not make the rest of the extraction shared, and
nothing detects the next instance.

**T2 does not cover this.** T2 requires zero actionable candidates left
unresolved at window closure — it checks that the RSS side *resolves*, never
that it resolves the *same way* the listing would have. A release that RSS
classifies as a confident non-match, where the listing path would have flagged
it as a wanted upgrade, satisfies T2 perfectly and is exactly the outcome A4
exists to prevent.

So the gap is not "A4 is asserted but untested." It is **"A4 is asserted, and
the system contains no place where it could be observed."**

---

## What can and cannot be built

**Buildable now, cheaply — bounded field-level parity.** Drive a fixture set of
real release titles through both extraction implementations and assert they
agree on the fields that both produce: resolution, size, year, season, media
type, and full-disc exclusion. This is a genuine test with real
discriminating power — it would have caught the `[BD]` asymmetry — and it needs
no new production code. Roughly the half-day originally estimated.

It does **not** establish A4. It establishes that the inputs to the decision
agree, which is necessary and not sufficient.

**Not buildable cheaply — decision-level parity.** Comparing actionable
decisions requires a listing-side decision to compare against, and there is no
such object. Producing one means either routing listing items into candidate
rows, or extracting the decision logic so both representations can be fed
through it. Both are real production changes to the measured surface, which
means they cannot land during the qualification window (R5) — they would have to
land *before* it, pushing the deploy back by more than a day.

---

## The decision this forces

Three honest options, and this is Jesse's call because it trades calendar time
against the strength of the eventual verdict:

1. **Field-level parity now; decision-level parity is out of scope for this
   window.** Record explicitly that the window tests URL coverage and field
   agreement, *not* decision equivalence, and that A4 is therefore partially
   evidenced. Cheapest, honest, and leaves a named gap.
2. **Build the decision bridge before deploying.** Strongest verdict, delays the
   8-9 day clock by however long the bridge takes, and adds production code to
   the measured surface immediately before measuring it.
3. **Field-level parity now, decision bridge built during the window and
   deployed after it.** The window's verdict covers coverage and fields; the
   decision evidence arrives one window later, before promotion actually
   happens.

Option 3 preserves the clock and still gates promotion on the full evidence,
provided promotion is not treated as automatic on the window passing. It is the
one I would choose, but the tradeoff is real: it means the window can pass while
A4 is still unproven, and the discipline to hold promotion afterwards has to
survive a passing result.

---

## What must not happen

Whichever option is taken, the qualification report must not claim A4 is
satisfied on the strength of T2. If option 1 or 3 is chosen, "RSS candidates
reach the same actionable decision as listing candidates" is an **untested
assumption**, and it must be written that way in the verdict.
