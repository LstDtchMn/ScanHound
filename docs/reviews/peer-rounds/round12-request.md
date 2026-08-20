# ScanHound Peer Review Request — Round 12: the post-merge state of `main`

**Repository:** `LstDtchMn/ScanHound`
**Head under review:** `main @ 6ac5cd2`
**Nothing is deployed.** The running container predates all of this.

## Why this round is different

Previous rounds reviewed branches. This one reviews **`main` itself**, because
four PRs landed in quick succession, they interacted, and one produced a real
conflict I resolved on my own judgement.

Round 11's findings were closed on branches you had already read. What you have
**not** read is my *implementation* of several of them, and what the merges did
when combined.

## What merged, in order

```text
#95  fix/rss-history-keyed-on-release-url   re-delivered the stranded #90
#93  fix/carry-is-tv-not-rederive           Q8 + the rescan fix
#91  feat/consume-media-kind-in-ui          the semantic consumer
#92  feat/queue-records-category            batched-grab media kind
```

#95 existed because #90 was merged into #89's *branch* 14 seconds after #89
merged to `main`, so its content never arrived. GitHub reported #90 as merged.
Proven by content rather than ancestry: `git grep -l media_kind origin/main`
returned **zero files**.

**Every PR now targets `main` directly**, so that cannot recur.

## THE THREE COMMITS NOBODY HAS REVIEWED

This is the part I most want read. All three are in the
destructive-authorization path.

### 1. `1407ea4` — my implementation of M1a and M1b

You specified the approach; you have not seen how I built it.

**M1a — durable retraction.** You were right that a conflict discovered later
did not revoke an already-persisted `downloads.media_kind`. I reproduced it end
to end before fixing:

```text
T0  cache 4k clean  -> verified_media_kind = movie -> persisted movie
T1  later crawl sees it in a TV listing
                    -> verified_media_kind = None   (correct refusal)
T2  downloads.media_kind = movie                    (STALE, and what the UI reads)
T3  status-only write with None -> movie preserved by COALESCE
```

Implemented as `retract_download_media_kind(urls, reason=...)`, a named
operation that only ever erases — because, as you put it, `None` had acquired
two meanings and one value cannot carry both.

**M1b — attestation, not absence.** Two halves. The crawl now records a
classification claim BEFORE any skip decision, so an already-cached release
(the entire deployed corpus) can still be marked conflicted. And absence of
`category_conflict` no longer reads as "checked and clean": three states now,
with legacy rows attested the next time a conflict-aware crawl observes them.

### MEASURED AFTER WRITING THIS, AND IT IS WORSE THAN I SAID

I originally flagged the attestation change as "a blackout for an interval I
have not measured". I measured it. **It is not an interval — it is permanent for
most of the library.**

```text
cached rows                        4178
all of which have a usable category today
a crawl observes per cycle        ~150   (early-stop fires at page 1-2)

re-observed within 1 day            276   (6.6%)
re-observed within 7 days           744   (17.8%)
NOT re-observed in 7 days          3434   (82.2%)
```

The crawl early-stops the moment it reaches cached content, so it sees roughly
the same ~150 most-recent releases every cycle and never goes deeper. My
backfill attests only what the crawl OBSERVES. Therefore **~82% of the corpus
would never be attested, and would answer `unknown` forever.**

Every one of those 4,178 rows has a working category right now.

So the change as built trades a real, working classification for permanent
unknown across most of the library, in order to close a risk whose size I have
not measured: how often a legacy row's release actually appeared in two
listings. The old crawler discarded that evidence, so I cannot measure it
retrospectively.

**I do not think this should deploy as-is, and I would rather you told me which
way to resolve it than have me pick.** The options I see:

```text
A. attest the existing corpus once, at deploy
   -> restores capability immediately
   -> but that IS "absence means attested clean", which you rejected

B. deep-crawl to observe every cached URL
   -> principled, uses listing evidence as you suggested
   -> but releases that have paged off the listing are unreachable, so it
      cannot cover the older tail at all

C. ship it and accept the loss
   -> fail-closed and honest, and 82% of the feature goes dark permanently

D. attest existing rows but mark them a WEAKER grade -- usable for grouping,
   never for a destructive action -- and let observation upgrade them
   -> keeps display behaviour, keeps the destructive gate closed
   -> more machinery, and a third state to reason about
```

D is where I would lean, because it separates "good enough to show" from "good
enough to destroy on". But it is a design decision on a safety boundary you
raised, so I would rather have your ruling than my instinct.

### 2. `6a458d7` — the rescan fix

You found that `/scan/rescan-item` derived the TV signal from `source_category`,
which is the source NAME (`"HDEncode"` on all 4,145 rows) and therefore always
False. Worth stating plainly: **that line was added by the #93 fix itself.** A
change whose entire premise is "stop re-deriving a fact that was already known"
re-derived it again, one route over, from a field that never held it.

**My first test for it was vacuous** — it reimplemented the classification in a
local helper, so a mutation restoring the bug killed nothing. The logic is now
extracted as `rescan_classification()` and the test imports it.

### 3. `67de532` — the merge resolution, written minutes before this request

#93 and #95 both fixed the same route from different angles, so they conflicted:

```text
#93   the extracted helper, and the TV signal it recovers
main  preservation of a recorded classification conflict
```

Taking either wholesale would have dropped the other. `rescan_classification()`
now returns all three values and the route consumes all three. **No one has
reviewed this, including me with fresh eyes.**

## What is live TODAY, and what deploying changes

The running container has **none** of the media-kind work — `downloads.media_kind`
does not exist there — but it **does** have the destructive Keep-best action,
authorized from `identity_kind` derived from `season is not None`. Unverified,
not conflict-aware.

So deploying replaces a filename/season-derived authorization with a
server-verified, conflict-aware, fail-closed one. Everything unreviewed makes
authorization **stricter**.

That is my argument for deploying, and it is also exactly the kind of claim I
would like checked, because it is a claim about code you have not read.

## Verification

```text
main @ 6ac5cd2   35 failed / 5286 passed / 4 skipped
```

Identical failure set to the clean-`main` baseline, so the four merges
introduced no interaction failures.

Baseline for comparison is 35 failed (32 network-dependent, plus
`test_source_hdencode`, `test_notifications`, `test_hdencode_off_switch`).

Mutation evidence for each fix, in the direction of the original defect:

```text
retraction becomes a no-op                    kills 3
absent attestation treated as clean           kills 1
conflicts recorded only for indexed posts     kills 2
rescan TV signal from source_category         kills 3
rescan category from source_category          kills 6
```

## What I got wrong this round, stated rather than buried

- **I claimed I1 fixed. It is not**, and both implementations overruled one of
  the two branches. It is unresolved on #94 and needs your ruling.
- **My reason-code enum is not built**, and my claim that all refusal causes are
  logged was overstated: conflict and client-mismatch are; a missing scan row or
  unrecognised category is not.
- **The grab-time resolver measurement is not done.** You are right that 53/664
  is not the user-impact number.

## The question for this round

Is `main @ 6ac5cd2` safe to deploy? Specifically:

1. Does the M1a/M1b implementation actually do what round 11 asked, or does it
   only appear to?
2. Is the merge resolution in `67de532` correct — does the route consume all
   three values coherently?
3. **The attestation question, restated after measuring it:** the change makes
   ~82% of the cached corpus permanently unknown, not temporarily. Which of
   A/B/C/D above, or something I have not thought of?
