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

**What I want challenged:** the attestation backfill means all 4,145 live
cached rows answer *unknown* until a crawl observes them. I believe that is
correct fail-closed behaviour, but it is a fleet-wide capability blackout on
deploy and I chose it without measuring how long the backfill takes.

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
main @ 6ac5cd2   full suite: see the PR comment for the current figure
```

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
3. Is the attestation blackout acceptable on deploy, or does the backfill need
   to run before the feature can be trusted?
