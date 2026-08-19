# Review round — normalized-path collision fix (dv_labeler)

**Date:** 2026-08-17
**Scope:** `backend/rename/dv_labeler.py`, `backend/app_service.py`,
`backend/api/routes/rename.py`, `tests/test_dv_path_collision.py`,
`tests/test_dv_conflict_alert.py`
**Round 2** — responds to the peer review of `c1bbac4`, which returned
REQUEST CHANGES with one MEDIUM blocker.

> **Round 1's §4 has been retracted and replaced.** Not softened — the central
> claim was wrong in a way neither I nor the reviewer caught, and the corrected
> version is in §4 below.

---

## 0. Response to round 4

Round 4 approved the design outright — conflicts stay derived, the narrow
endpoint is right, no cache, no persisted table, no component-test dependency —
and withdrew the CI caveat. It left **M1 (MEDIUM)**, **I1 (INFO)** and
**T1 (TEST)**. All three are addressed.

### M1 — the reconnect refresh ignored the retry policy next door

Verified before fixing: `loadDvConflicts()` was a one-shot with a silent catch,
fired from the same reconnect moment as `resyncAfterReconnect()` — which
documents the WS-up/REST-not-ready race, retries once after
`RESYNC_RETRY_DELAY_MS` (2000, `renames.ts:515`), and raises a toast when both
attempts fail. Mine did neither. Preserving the old value on failure is right;
the bug is that the preserved value can be the **stale zero** left by a missed
alert, which renders as "nothing needs attention" and suppresses the very badge
that would have prompted the user to look.

`resyncDvConflictsAfterReconnect()` now retries once at the same delay, then —
if both attempts fail — keeps the old value **and** raises a warning toast.
Deliberately **not** folded into `fetchResyncSnapshot()`, since that would take
the whole rename-job resync down with a DV status failure.

Tests, including the two you specified:

* first read throws → second returns 2 → store ends at 2, **two** fetches, no
  toast (recovered, so nothing to report);
* both fail **while the preserved value is the stale zero** → value stays 0 and
  a warning IS raised — the discriminating case, because silence there is
  indistinguishable from a healthy library;
* both fail with a known nonzero → the real warning is never retracted;
* **negative control**: first read succeeds → exactly one fetch, no toast, so
  "retry once" cannot silently become "always two requests".

### T1 — the rendering test, and a correction

I told you there was no component-test harness, so a rendering test would need a
new dependency. **That was wrong, and the error is mine.** I searched for tests
that mount components, found none, and reported the general absence — while
`frontend/playwright.config.ts`, four specs under `frontend/tests/e2e/`,
`@playwright/test`, `npm run test:e2e` and a CI job running Chromium were all
present. I had even seen the `tests/e2e` directory in a listing earlier the same
session. A narrower query than the claim I drew from it.

Added `tests/e2e/shared/dvConflictBadge.spec.ts` — **no new dependency**, and it
runs under both the desktop and mobile projects:

* a conflict is visible while the panel is still `aria-expanded="false"`;
* opening the panel re-reads the narrow endpoint (inventory says 0,
  `/dv-conflicts` says 2, badge appears) and the badge **survives collapsing** —
  which is exactly what would fail if it lived in the panel body;
* **negative control**: a clean library shows no badge at all.

### I1 — wording

Corrected in §0a below. `onMount()` does fetch the inventory; what became cheap
is the additional attention-state refresh.

## 0a. Response to round 3

Round 3 closed the round-1 MEDIUM, round-2 L1 and round-2 L2, and accepted the
server-side half of M1. It left **one MEDIUM: the frontend never re-reads the
derived state.** Both halves verified before fixing:

* `resyncAfterReconnect()` refreshes rename jobs, rename status and applying
  jobs — **confirmed, no DV refresh**. And `POST /rename/dv-host-rows`, the
  durable ingest, emits no `dv:scan_done`, so nothing else nudges it either.
* The conflict card sits outside the inventory guard but **inside
  `{#if dvOpen}`, and `dvOpen = $state(false)`** — confirmed. My round-3 claim
  that it "appears regardless" was half right and the wrong half mattered: a
  correctly-loaded page still showed nothing until the panel was expanded.

**Fixed as the three-part delta requested:**

1. `loadDvConflicts()` runs on WebSocket reconnect (`connection.onReconnect`).
2. It runs when the DV panel is opened — via the header toggle and via
   `dolbyVision()`, the StatusDashboard entry point, which both set `dvOpen`.
3. A `N need attention` badge renders in the panel **header**, so it is visible
   while collapsed.

**Cost — took the recommendation as given.** No cache: the point of this state
is that it is always recomputable, and a cache would trade that for an
invalidation problem. Instead the preferred option, `GET /rename/dv-conflicts`,
backed by a new `db.get_dv_layer_rows()` reading only `path, dv_layer`, and
`/dv-scans` now uses that same narrow read for its conflict field rather than a
second seven-column unpaged query.

> **Correction (round 4, I1).** An earlier draft of this section and the round-4
> commit message said "the inventory stays lazy". That is not literal:
> `onMount()` still calls `loadDvScans()`, so the 500-row inventory is fetched
> on page load exactly as before. What became cheap is the **additional
> attention-state refresh** on reconnect and panel-open, which no longer drags
> the inventory along. The commit message cannot be corrected without rewriting
> a pushed commit, so it is corrected here.

**Documentation cleanup** done: the alert docstring and the sync log comment now
say the alert is the CHANGE notification and `current_conflicts()` is the
record, rather than implying the alert is the only reporting path.

### A correction I owe you, on evidence

Two things I told you were wrong, and both caused you to discount real evidence:

* **The repository is PUBLIC**, not private. I wrote "private" in all three
  requests without checking. My own memory had been corrected on this on
  2026-08-05 and I carried the stale value anyway.
* **CI has been green on every head you reviewed.** Runs are push-triggered,
  which is why a PR/combined-status lookup found nothing:

```
c1bbac4  round 1  success
64e2ba6  round 2  success   (frontend, test 3.11, test 3.12)
11b2989  round 3  success   (frontend, test 3.11, test 3.12)
```

`gh run list --branch agent/dv-path-collision-fix` reproduces it. So the
same-session baseline numbers were independently corroborated the whole time,
and the "author-supplied rather than confirmed" caveat in rounds 2 and 3 rested
on my error rather than on absent evidence.

### Test evidence this round

* 5 new frontend store tests (`dvConflicts.test.ts`): the narrow endpoint is
  hit rather than the inventory; a stale tab holding 0 recovers the real count;
  a failed refresh keeps the last known value instead of silently retracting a
  warning; the negative control that it still clears on genuine resolution; and
  truncation surfaced so a capped sample is never read as the whole set.
* 2 new backend tests: the endpoint derives current state and **asserts it never
  touches the paged inventory**; and it is safe before the DB exists.
* `svelte-check`: 366 files, **0 errors** (3 warnings, all pre-existing, in
  files this branch does not touch). `vitest`: 416 passed across 32 files.

**Open question back to you, rather than a unilateral call.** You asked for "one
visible collapsed-state test". There is **no component-test harness in this
repo** — all 32 frontend test files are pure logic with a `fetch` spy, and
nothing mounts a component. So that test needs `@testing-library/svelte` added
first.

I have not done that, because I am not sure it earns its place: the defect was
in *state recovery*, which is now tested at the store; the rendering half is a
single `{#if $dvConflicts.count}` in the panel header, and `svelte-check` (0
errors) confirms it compiles against the typed store. Introducing a new
dependency and a testing pattern the project has never used, to assert a
two-line conditional, looks like the tail wagging the dog.

Against that: the duplicate/best-version feature queued next is largely UI, so
a harness would not be single-use, and "it compiles" is not "it renders" — which
is close to the distinction this whole branch has been about.

**Your call — I will add it if you think the rendering assertion is worth the
harness.** If you would rather it stayed as-is, say so and I will record the
gap explicitly rather than let it read as covered.

## 0a. Response to round 2

Round 2 returned REQUEST CHANGES: **M1 MEDIUM** (the unattended alert can be
lost permanently), **L1** (uncapped path list on the wire), **L2** (stale
round-1 comments still present despite the doc claiming otherwise). All three
are addressed.

**M1 — and the reviewer was right about the shape of it.** The alert marks a
conflict set "seen" *before* attempting delivery. The in-app broadcast targets
whoever is connected at that instant and raises nothing when that is nobody, and
no outbound channel is configured, so a conflict appearing while no tab was open
was announced to an empty room and then permanently deduped. The reviewer also
spotted that the scheduled sync only runs when `get_latest_dv_scan_at()`
advances, so there is not even an hourly retry.

That is the same distinction this branch exists to enforce — *the function ran*
is not *the result arrived* — applied one level further out than I applied it.

Fixed by making an unresolved conflict **current state rather than only an
event**, per the reviewer's preferred shape. The key property is that conflicts
are **derived, not stored**: they are a pure function of the `dv_scan` rows, so
`current_conflicts()` recomputes them on demand. Nothing to persist, replay, or
let drift. `/rename/dv-scans` returns them, and the Renames DV panel shows a
persistent "N file(s) need attention" card — deliberately outside the inventory
guard, so it appears whether or not any scan counts exist, and regardless of
what happened to any notification. The event/dedup path is unchanged and still
does its job for *changes*; it is simply no longer the only way to find out.

The regression test the reviewer asked for is
`test_unresolved_conflict_is_discoverable_after_the_alert_reached_nobody`: it
runs the alert with no notifier and a broadcast nobody receives, fires it twice
so the dedup engages, and then asserts the conflict is *still* discoverable.

**L1** — `wire_safe_sync_result()` caps `layer_conflict_paths` at the route,
i.e. at the boundary that transmits, adding
`layer_conflict_paths_truncated`. The exact set stays in the returned summary
because the alert's dedup needs it, and `layer_conflicts` remains the exact
count so a client is never misled about scale.

**L2** — the stale comments are gone: the "311 keys collide today" log comment,
the test docstring claiming "all 311 colliding keys" and that `unknown` "strips
the badge" (it does not — it fails to add or converge one), and the docstring
still saying a conflict "is reported as 'unknown'".

**On §8, the `0x800710E0` lead — the reviewer's correction is accepted.** The
installer sets and asserts `-MultipleInstances IgnoreNew`, which *prevents*
scheduled overlap rather than causing it, and `0x800710E0` is
`HRESULT_FROM_WIN32(ERROR_REQUEST_REFUSED)` — consistent with a trigger being
refused, not with a second scanner having started. My duplicate-spelling
mechanism does not follow from it. I had marked it inferred; it is weaker than
that framing implied and should not be pursued as stated.

## 0b. Response to round 1

| # | Required change | Status |
|---|---|---|
| 1 | Multipart conflict hole: a true conflict must not be maskable by a positive sibling | **Fixed** — `LAYER_CONFLICT` sentinel + `pick_layer` rule 0 (§3) |
| 2 | End-to-end multipart test: conflict + clean positive sibling | **Added** — 2 tests, both part orders, plus a regression control (§5) |
| 3 | Annotation loop must not select a conflicted path | **Fixed** — local guard in `sync_labels` (§3) |
| 4 | Correct stale "311 all correct" comments | **Fixed**, and they were replaced with something different again — see §4 |
| 5 | Preserve the measurement command + collision-size histogram | **Added** (§4); histogram **confirms** the reviewer's inferred invariant |

The blocker was real and I reproduced it before fixing it. It is also a repeat
of the failure this project keeps logging: I traced the conflict state through
the consumer for a **single-part** title, found `may_remove=False` and
`matched=False`, and stopped. `pick_layer` runs its positive-rank loop *before*
its `unknown` handling, so on a multi-part title a clean sibling made the whole
title authoritative and re-opened both label removal and the `rating_key`
back-write.

---

## 1. The bug

All three indexes in `dv_labeler` were built with last-write-wins in a loop:

```python
for r in rows:
    p = normalize_path(r.get("path"), mappings)
    if p:
        idx[p] = r.get("dv_layer")        # last row wins, silently
        norm_to_path[p] = r.get("path")
```

## 2. The rule

Depends only on the SET of layers observed, so it is permutation-invariant:

```
exactly one distinct authoritative layer (+ any failures) -> that layer
no authoritative layer at all                             -> the failure value
two or more DIFFERENT authoritative layers                -> LAYER_CONFLICT
```

`none` is authoritative (the detector ran, found no DV). `unknown`/NULL is a
failed detection and is never evidence. `last_seen_at` is not used to arbitrate
(a failed scan preserves the old layer while advancing the timestamp), and
`_LAYER_RANK` is not used (it ranks parts of one title, not observations of one
file). All three call sites share `_index_by_normalized_path`.

## 3. The conflict state — changed per review

Round 1 emitted `unknown` for a conflict. **The reviewer was right that this is
wrong**, and the argument that convinced me is the one I had not considered:
`unknown` and "contradiction" are non-authoritative in *different* ways.
`pick_layer` deliberately lets a sibling part's positive finding beat an
`unknown` — "one part proving DV proves it for the title" — which is correct for
a failed scan and wrong for evidence that disagrees with itself, because the
contradicting file could be *any* of its claimed layers, including one that
outranks the sibling.

Now:

* `LAYER_CONFLICT = "conflict"`, its own value.
* `_NON_EVIDENCE = frozenset({LAYER_DETECTION_FAILED, LAYER_CONFLICT})` — one
  set consumed by `is_authoritative`, `desired_label`, `desired_labels` and
  `reconcile_movie`, because updating four guards out of five is precisely how
  this hole was created.
* `pick_layer` gains **rule 0**: a conflicting part poisons the title, checked
  *before* the rank loop.
* `sync_labels`' annotation loop skips a conflicted path. Rule 0 already makes
  that unreachable; the guard is local so the invariant does not depend on
  `pick_layer` continuing to hold the line.

The ordinary `unknown + positive -> positive` behaviour is unchanged and now has
its own explicit regression control.

## 4. Measurement — round 1's §4 was wrong, and so was the handoff's

Round 1 said the deployed code was *currently wrong on 335 keys*. The handoff
before it said all colliding keys *happen to resolve correctly*. **Both are
snapshots of a coin toss.**

`ORDER BY last_seen_at DESC` looks like a tie-break and is not one:

```
rows (source='scan')                 6,948
distinct last_seen_at values             5
   5,975 rows   '2026-08-17 12:32:09'
     970 rows   '2026-08-17 12:32:08'
       1 row    '2026-07-25 16:45:35'
       1 row    '2026-07-25 15:47:37'
       1 row    '2026-07-22 19:08:19'
```

A bulk rescan stamps everything within one second, so 6,945 of 6,948 rows sit in
two tie groups and the "winner" is whatever order the sorter emits among equals
— which shifts as rows are added. Re-running the identical old-vs-new comparison
twenty minutes later, after 11 rows were appended, the count of keys whose layer
changed went **335 → 0**.

So the defect is not a wrong answer. It is an **arbitrary** one:

```
normalized keys                                       4,725
keys with >1 row                                      2,223
max rows per key                                          2   <-- see below
keys where a real layer and a failed scan coexist       346   <-- order decides
   of those, whose real layer produces a badge          180
keys with two DIFFERENT real layers (true conflict)       0
```

**Collision-size histogram** (the reviewer inferred every group must have
exactly two rows from `rows - keys == colliding keys`, and asked for
confirmation — it holds):

```
  1 row  : 2,502 keys
  2 rows : 2,223 keys
  max    : 2
  rows - keys = 2,223 = sum(group_size - 1) = keys with >1 row   [consistent]
```

**Consumer-level effect, measured as a bound.** The real
`sync_labels(dry_run=True)` run three times against the live Plex server —
identical except for row order, library fetched once and reused, no writes:

| run | matched | would add | would remove | titles gaining a label |
|---|---|---|---|---|
| OLD, adverse order (failed row last) | 764 | 0 | 0 | **0** |
| OLD, favourable order (real layer last) | 1,079 | 72 | 7 | **30** |
| **NEW, order-independent** | **1,079** | **72** | **7** | **30** |

Asserted in the run: `NEW == FAVOURABLE` is true, `NEW == ADVERSE` is false. The
candidate lands on the good outcome by construction rather than by luck.

So an unlucky sort order costs **72 labels across 30 titles** — 14 FEL, 10
Profile 8, 5 MEL, 1 HDR10 (*A Shot in the Dark*, *Alien: Romulus*, *Babe*,
*Lucy*, *One Battle After Another* among them) — and drops 315 titles out of
`matched` altogether.

An earlier single run comparing old and new on the *current* row order showed no
difference whatsoever. That is exactly why the bound is the right thing to
report: it caught the coin on a favourable landing.

### Still not established

How many titles are *visibly* missing a badge in Plex at any given moment. Round
1 estimated ~38 from `dv_scan.rating_key`; **that estimate was withdrawn**
because `plex_metadata_scan.py:301` also writes `rating_key` on `source='scan'`
rows, so it is not labeller provenance. The reviewer independently confirmed the
withdrawal. Since the underlying count is unstable anyway, the useful figure is
the 346/180 bound, not a point estimate.

### Reproducing

Scripts are preserved outside the repo (they read the live DB and Plex, both
read-only, `mode=ro`). Method that matters: **import the deployed function and
the candidate and run both on the same row list in one process** — not a
re-implementation of either.

## 5. Test evidence

`tests/test_dv_path_collision.py` — 18 tests, every case asserted in **both**
orders. New in round 2:

* conflicted part + clean `profile5` sibling → no add, no remove,
  `matched=False`, no `rating_key` write, in both part orders
* the same with a clean `fel` sibling
* `pick_layer` returns `LAYER_CONFLICT` regardless of part order
* **regression control**: ordinary `unknown` + positive sibling → still positive
* **negative control**: a clean multi-version title still labels normally
  (1,029 of this library's movies have more than one version)

**Mutation-tested, one mutant per half of the fix**, each applied alone:

```
M1  collapse emits 'unknown' again (round 1's design)  -> 4 tests fail,
                                    including both multipart end-to-end tests
M2  pick_layer rule 0 disabled                         -> 3 tests fail
restored                                               -> 55 pass
```

M1 *is* the reviewed-out bug, reproduced and caught.

Full suite: run in a throwaway container from `scanhound:latest` with the source
tree copied in, against a same-session `HEAD` baseline built identically.
Baseline: 35 failed / 5,067 passed. The 35 are pre-existing and the failure sets
are **byte-identical** (32 are `test_network.py` tests that cannot reach the
internet from the container).

## 5b. Conflict alert (owner decision, this round)

Round 1's open question — *is it right that one contradicting part suppresses an
otherwise-labelable title?* — was put to the owner. His answer: **keep it
hands-off, and tell me when it happens.**

`sync_labels` now returns `layer_conflict_paths` alongside the count, and the
unattended hourly sync raises a HIGH-priority alert.

**On two channels, and checking that was not optional.** The obvious
implementation — `NotificationManager.send_notification()` — would have reached
**nobody on this deployment**. Probed against the live config:

```
desktop_notifications  True     <- but plyer's Linux backend is disabled in a
                                   headless container (no gdbus/notify-send),
                                   notifications.py:143
discord_webhook        empty
slack_webhook          empty
pushover_token/user    empty
webhook_url            empty
NotificationManager channels: []          <- send_notification is a no-op
```

So the alert broadcasts on the in-app websocket — the channel
`app_service`'s own unmapped-Plex-path check already uses, which needs no
configuration and surfaces in the ScanHound UI.

**And the second channel had to be repointed.** The first attempt sent through
`AppService.notification_manager`. That instance is constructed bare at
`app_service.py:552` and *nothing ever calls `configure_from_dict` on it*, so
its channel list is permanently empty — it could never deliver regardless of
configuration. The instance that IS configured from config is the registry's
`NotificationBridge` (`NotificationBridge.configure` → `configure_from_dict`),
so the alert now uses `registry.notifications.send(...)`. Same class of mistake
as the one being guarded against, one level down, and only visible by asking
which object actually holds a channel.

Neither channel can take the sync down; a failure on one still attempts the
other, and a missing bridge is survivable (startup order is not guaranteed).

This is the `.ps1.NEW` shape from the handoff: correct code that cannot run,
failing silently while reading as coverage. It is only caught by asking whether
the thing is *delivered*, not whether it is *called*.

**The manual sync path needed it too.** `/rename/dv-sync-labels` reports one
summary line and has no alert. A conflicted title moves none of
matched/added/removed, so a run that silently skipped files was
indistinguishable from one with nothing to do. The summary now names them, and
the string-building moved to a module-level `dv_sync_summary_body()` so it is
testable without driving the route's background thread — a threaded end-to-end
test for a display string would be flaky for no gain.

**Dedups on the SET of paths, and that is load-bearing in both directions.** A
conflict does not self-heal — the two rows keep disagreeing until a rescan
resolves them — so an unconditional alert fires every hour forever. But a
*count*-based guard has the opposite failure: it goes silent on precisely the
pass where one file resolves and a different one starts conflicting. Both are
pinned by tests, and both are mutation-proven:

```
M1  dedup on the count      -> test_alert_fires_again_when_a_DIFFERENT_file_conflicts fails
M2  dedup removed           -> test_alert_does_not_repeat_for_the_same_set fails
M3  in-app broadcast killed -> 3 delivery tests fail (the no-op state above)
restored                    -> 20 pass
```

**A phone alert is NOT in this branch, deliberately.** The in-app alert works
with no setup and there are 0 conflicts live, so nothing is currently going
unreported. Reaching the owner's Gotify server is agreed as the next piece of
work, scoped separately so it does not ride along on a correctness fix. Two
things established while scoping it, neither yet addressed:

* `GenericWebhookChannel` posts `Notification.to_dict()`
  (`id/type/title/message/priority/data/timestamp`). Gotify expects
  `title/message/priority`. It may tolerate the extras; unverified against a
  real server.
* Gotify's token conventionally rides the URL query string. That is a
  credential in a URL; its `X-Gotify-Key` header form is preferable, and
  `configure_from_dict` exposes no custom headers for the generic webhook —
  so a small code change is needed, not just a settings entry.

A dedicated `GotifyChannel` (correct payload, correct 0–10 priority mapping,
header auth) looks better than widening the generic one. The token itself stays
with the owner, entered through ScanHound's own settings.

Clearing is silent but re-arms, so the same file conflicting again after a
rescan fixed it is reported. A notifier that raises, or is absent entirely,
cannot propagate: the label work has already succeeded when the alert runs, and
a dead channel must not stop the watermark advancing (the shape of the
2026-08-12 logging-as-hard-dependency outage).

## 6. Open for this round

1. Does rule 0 belong in `pick_layer`, or should the conflict set be threaded
   into `reconcile_movie` separately? Rule 0 is simpler and keeps one verdict
   type flowing through the existing code. The *behavioural* half of this
   question — one contradicting part suppressing an otherwise-labelable title —
   is settled: the owner chose hands-off plus an alert (§5b). What remains is
   whether the plumbing is right, given rule 0 makes `pick_layer` consult a
   value it cannot itself produce.
   Also worth a look: `layer_conflict_paths` is unbounded in principle (capped
   only by the colliding-key count, currently 2,223 max, 0 in practice) and
   rides the `dv:sync_done` websocket payload on dry runs. The alert's `data`
   is capped at 50; the summary list is not.
2. `min()` on the raw path is now documented as *a deterministic representative
   row, not the newest and not the filesystem's preferred spelling*, per the
   round-1 suggestion. Annotating **all** equivalent winner rows was considered
   and deliberately not done — no consumer requires it.
3. The seed-conflict log now states what the report actually renders
   (`seed_conflict_live_<layer>`) rather than claiming a generic "unverified"
   state, per the round-1 note.
