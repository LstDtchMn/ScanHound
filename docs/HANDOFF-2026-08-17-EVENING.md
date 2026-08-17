# ScanHound handoff — 2026-08-17, evening

Successor to `HANDOFF-2026-08-17.md`, which remains valid for everything before
tonight. **Its §2A was corrected in place** — the original claim there was wrong
and so was my first replacement; see §3 below.

Written uncommitted, deliberately, so the reviewer's SHA could not go stale
mid-round. Committed once the merge landed.

---

## 1. Where things stand

| | |
|---|---|
| Branch | `agent/dv-path-collision-fix` |
| Status | **MERGED to `main` as `ec94b0c`** (2026-08-17). Approved head `839f2ed` verified an ancestor of `origin/main`, as is the entrypoint fix `d08c989`. **NOT DEPLOYED — that is the next action, §4** |
| Base | `f64f677` (`main`, unchanged tonight) |
| Reviews | **5 rounds — round 5 is APPROVED.** Rounds 1–4 each returned REQUEST CHANGES on something real; all findings CLOSED. One accepted non-blocking INFO, see §2b |
| Suite | 5,109 passed / 35 failed vs same-session unmodified baseline 5,067 / 35 — **failure sets byte-identical** |
| Frontend | `svelte-check` 366 files, 0 errors · `vitest` 416 passed / 32 files |
| CI | **green on all five heads** — `c1bbac4`, `64e2ba6`, `11b2989`, `61965a9`, `839f2ed` (test 3.11, test 3.12, frontend). The frontend job runs type-check, build, Vitest **and Playwright**, so the new collapsed-badge e2e spec is confirmed by CI as well as locally. Runs are **push-triggered**, which is why a PR/combined-status lookup finds nothing — use `gh run list --branch <branch>` |

The 35 failures are pre-existing on unmodified `main` — 32 are `test_network.py`
tests that cannot reach the internet from the container.

**Next action: MERGE (Jesse's), then DEPLOY — §4.** The review track is closed;
nothing is outstanding on the branch.

> **Accepted, non-blocking (round 5 INFO).** The Vitest cases call
> `resyncDvConflictsAfterReconnect()` directly, so they pin the helper's
> semantics but not the one-line `connection.onReconnect()` wiring. A future
> edit swapping that callback back to `loadDvConflicts()` would not be caught.
> The wiring is correct at this head and was inspected. Closing it properly
> needs a way to fire `reconnectHandlers`, which is a private Set inside the
> store closure — i.e. new test infrastructure, which the reviewer explicitly
> said not to add. Asserting on source text instead would be the
> "test that asserts on a docstring" anti-pattern. **Left open deliberately.**

---

## 2b. ROUND 4 — ADDRESSED at `839f2ed`. Send round 5.

Verdict was **REQUEST CHANGES, 1 MEDIUM + 1 INFO + 1 TEST.** The design was
accepted: conflicts stay derived, the narrow endpoint **approved**, no cache, no
persisted table, no component-test dependency. **All three items are now fixed
and verified** — detail below, and in §0 of the review doc. Nothing here is
outstanding; it is kept as the record of what was done and why.

### M1 (MEDIUM) — the reconnect refresh ignores the retry policy next door

```ts
connection.onReconnect(() => { loadDvConflicts(); });   // one-shot, silent catch
```

`resyncAfterReconnect()` **in the same module** documents the exact race this
hits — after a backend restart the WebSocket upgrade path and plain HTTP behind
the reverse proxy do not become available in lockstep — and therefore retries
once after `RESYNC_RETRY_DELAY_MS` (= 2000, `renames.ts:515`), then raises a
toast if both attempts fail. My DV refresh fires from the same reconnect moment
and does neither.

Surviving failure sequence:

```
tab holds count 0  ->  WS drops while conflict A appears  ->  alert missed and
deduped  ->  WS reopens  ->  loadDvConflicts() runs  ->  REST not yet routable
->  throws  ->  catch{} keeps the stale 0  ->  no retry, no warning, and the
collapsed badge stays absent, so the user has no reason to open the panel
```

That is M1 reproducing one step further out. Preserving a known nonzero value on
failure is right; the bug is that the preserved value can be the stale zero.

**Fix (reviewer's preference):** a dedicated DV reconnect helper that retries
once after `RESYNC_RETRY_DELAY_MS`, then keeps the old value **and visibly
reports that DV attention state may be stale**. Deliberately NOT folded into
`fetchResyncSnapshot()` — that would couple DV status failure to the whole
rename snapshot.

**Required tests** (the first is the discriminating one):

```
stale count 0 -> first fetch throws -> second returns 2 -> store ends at 2
both attempts fail -> old state preserved AND failure surfaced
```

### T1 (TEST) — the rendering test, and I was wrong about the harness

I told the reviewer there was no way to test rendering without adding
`@testing-library/svelte`. **That was wrong.** The repo already has Playwright:
`frontend/playwright.config.ts`, `frontend/tests/e2e/` (layout, navigation,
sheets, routes specs), `@playwright/test` in devDependencies, `npm run test:e2e`,
and **CI already installs Chromium and runs it** — the frontend job on `61965a9`
passed type-check, build, Vitest *and* Playwright.

I searched for *component* tests, found none, concluded rendering was untestable,
and never followed up the `tests/e2e` directory I had already seen in a listing.

So add the collapsed-badge assertion with **zero new infrastructure**:

```
intercept /rename/dv-scans -> conflicts.count = 2
open /renames
assert the DV panel is aria-expanded=false
assert "2 need attention" is visible while collapsed
```

Optionally also exercise the narrow endpoint: `/dv-scans` returns 0,
`/dv-conflicts` returns 2, open the panel, expect 2, collapse, assert the badge
persists.

**Do not add `@testing-library/svelte` for this.** If a component harness is
wanted for the UI-heavy 2B work, add it deliberately there.

### I1 (INFO) — wording

The round-4 commit says the 500-row inventory "stays lazy". Not literal:
`onMount()` still calls `loadDvScans()`, so the inventory is fetched on page
load. What became cheap is the **additional attention-state refresh** on
reconnect and panel-open. Fix the wording; no code change.

### Also settled in R4

* The endpoint/cost design is **approved** — `get_dv_layer_rows()` narrow query,
  no cache. Deterministic O(n) recomputation beats an invalidation problem at
  this scale.
* Alert path: **no regression**. Dedup on the exact set intact; the route-side
  WebSocket cap does not mutate the server-side result the alert uses.
* **The CI caveat is withdrawn.** The reviewer independently confirmed the repo
  is public and that `c1bbac4`, `64e2ba6`, `11b2989` all passed, and noted the
  earlier rounds were wrong to describe those heads as lacking CI evidence.

## 2. What the branch does

A file can have two `dv_scan` rows under different spellings (drive letter vs
UNC, separators, case). Every index in `dv_labeler` was built with last-write-
wins in a loop, so which row won was decided by sort order.

* **One shared aggregator** (`_index_by_normalized_path`) feeds all three index
  sites. The rule depends only on the SET of layers seen, so it cannot depend on
  order. `none` is authoritative; `unknown`/NULL never outvotes a real finding.
* **`LAYER_CONFLICT` is its own state**, not a reuse of `unknown`, and
  `pick_layer` checks it *before* the positive-rank loop. Reusing `unknown` was
  R1's blocker: on a multi-part title a clean sibling silently overrode the
  contradiction and re-enabled label removal.
* **`_NON_EVIDENCE`** holds both non-authoritative states as one set, because
  updating four guards out of five is exactly how that hole was created.
* **Conflicts are derived, never stored.** `current_conflicts()` recomputes from
  the rows; `GET /rename/dv-conflicts` serves it. That is what makes a missed
  alert survivable — R2/R3's blocker was that the alert marked a set "seen"
  before delivering, and delivery is best-effort.
* **The alert** dedups on the SET of paths (not the count — same count with a
  different file is news), delivers in-app, and cannot take the sync down.

## 3. The measurement, wrong twice — read this before quoting any number

The original handoff said the colliding keys "all resolve to the real layer,
accidentally". I replaced it with "335 are currently wrong". **Both were
snapshots of a coin toss.**

`ORDER BY last_seen_at DESC` looks like a tie-break and is not one: a bulk
rescan stamps everything within a second, so the live table holds **6,948 rows
across 5 distinct timestamps, 5,975 sharing one value**. Re-running the
identical comparison 20 minutes later, after 11 rows landed, the changed-key
count went **335 → 0**.

The stable figures:

```
normalized keys                                      4,725
keys with >1 row (every group exactly 2)             2,223
keys where a real layer + a failed scan coexist        346   <- order decides
   of those, whose real layer produces a badge         180
keys with two DIFFERENT real layers (true conflict)      0
```

And measured at the consumer, running the real `sync_labels(dry_run=True)`
against live Plex three times, differing only in row order:

```
OLD, adverse order      764 matched,   0 labels applied
OLD, favourable order 1,079 matched,  72 labels / 30 titles
NEW                   1,079 matched,  72 labels / 30 titles
```

**So the defect was never a wrong answer — it was an arbitrary one.** The fix is
worth 72 badges across 30 movies on an unlucky ordering, and removes the
variance entirely.

## 4. Deploy (after merge — the steps are verified, not remembered)

The compose trap is currently **disarmed**: the pinned
`C:\ProgramData\ScanHound\deploy\docker-compose.yml` and the working-tree copy
are byte-identical (SHA256 `7DDFAF72…5ECE2D51`, 10,618 bytes), both carrying the
ingest key and `9721:9721`. Nothing in this branch touches compose.

The real risk is that those compose edits are **uncommitted**, and `git stash`
has swallowed them before.

1. Fingerprint before any git op: `git status --short docker-compose.yml`,
   `grep -c '9721:9721' docker-compose.yml`, `sha256sum docker-compose.yml`.
2. Merge, then `git checkout main && git pull --ff-only`.
3. **Re-run step 1 and compare** — must still be ` M`, `1`, same hash.
4. `docker compose up -d --build` — takes >10 min, start it and leave it.
5. Verify in the CONTAINER, not by exit code:
   `docker exec scanhound sh -c 'grep -c LAYER_CONFLICT /app/backend/rename/dv_labeler.py; grep -c current_conflicts /app/backend/rename/dv_labeler.py; grep -c "NEVER A GATE" /entrypoint.sh'`

That third grep matters: **`d08c989` (the entrypoint fix) is on main but NOT in
the running container** — verified, `/entrypoint.sh` dated Aug 16 22:01 has zero
matches. Deploying this branch also ships it, which removes the window where the
container reports "Up" with an empty log and nothing answering.

**Observed directly on 2026-08-17**, restarting the container after an E2E run:
`docker inspect` reported `running` immediately, but `/health` did not answer for
**~160 seconds**. Not a fault — it is the pre-fix entrypoint gating startup on
the browser-lock cleanup over the 9p mount. If you restart ScanHound before
deploying, expect roughly three minutes of "up but dead" and do not chase it.

**Expect the "needs attention" card NOT to appear.** There are 0 true conflicts.
Its absence is correct, not a failure.

## 5. Queued, in the order agreed

> **Sequence confirmed with Jesse, 2026-08-17 evening: close round 4 → round 5
> (delta) → he merges → deploy.** Not a suggestion to re-litigate; start at 0.

0. **Close round 4 (§2b)** — the retry-and-surface fix, its two tests, the
   Playwright collapsed-badge assertion, and the "stays lazy" wording. Small and
   fully specified. Then send round 5 (delta only) and, on approval, merge.
1. **Deploy** (§4).
2. **2B — duplicate/best-version labelling**, the feature originally asked for.
   Unblocked now. Bitrate is the discriminator (resolution differs in only 6% of
   the 1,029 multi-version movies); match at VERSION level, never by
   `rating_key` alone.
3. **Gotify.** ScanHound currently has **zero** outbound notification channels —
   headless container disables plyer; Discord/Slack/Pushover/generic webhook all
   unset; `NotificationManager._channels == []`. Needs a dedicated channel
   (header auth so the token never rides a URL, correct payload, 0–10 priority
   mapping), not a settings entry. Scoped in §5b of the review doc.
4. ~~Open with the reviewer: component-test harness?~~ **Settled in R4** — use
   the existing Playwright e2e harness; do NOT add `@testing-library/svelte`.

## 5c. Every open item, with its state CHECKED (2026-08-17 evening)

Prompted by Jesse asking whether the unfinished list had actually been reviewed.
It had not — the session went into 2A and its review rounds. So each item below
was checked against reality rather than recited. Source: `HANDOFF-2026-08-17.md`
§2C and its branch table.

| Item | State | Checked how |
|---|---|---|
| **2A** collision fix | **BUILT**, 5 rounds, at `839f2ed`, unmerged | this document |
| **2B** duplicate/best-version labelling | **Open, now unblocked** (2A was its prerequisite) | — |
| 118 `dv_scan` rows = one file twice | **Mitigated, not cleaned.** The aggregator now resolves them deterministically, so cleanup is optional rather than a prerequisite | 2A's design |
| 70 items, same size+mtime, different mount roots | **Open, unresolved.** Do not merge on stored metadata | untouched |
| `feat/item-history-sheet` `1d9632f` | **Confirmed unmerged.** Still needs `ATTEMPT_HISTORY_TRUSTED_FROM` repointed off midnight before it ships, or it renders pre-fix fabricated failures as real | `git merge-base --is-ancestor` vs `origin/main` |
| `feat/queue-declared-semantics` `9706fa0` | **Confirmed unmerged**, unreviewed | same |
| `feat/source-hold-surface` | **Confirmed merged** (`77d2a70` on main) | same |
| Single-row `remove_package()` epoch race | **OPEN — and the only actual live bug on this list.** The bulk path was fixed; the single-row path kept the race | untouched tonight |
| Hold-card UI partition has no automated test | **Open, but its blocker was imaginary.** I assumed no UI harness existed; Playwright works and is now proven in use (§2b/T1). The four browser cases are writable today | proven by running the new spec |
| `otherErr=1814` | **Open.** Jesse's own correction stands: the monitor watches a task that is Disabled and cannot run, so find what it actually reads before treating the number as meaning anything | his commit `6db893a` |
| `ScanHound Qualification Evidence` task, exit 3 | **Open, untouched** | — |
| `ScanHound-MountNASShares` failing | **Open.** Observed `LastTaskResult=2` tonight; the guard cries wolf while the container's `/library/tv` is fine. Root cause unfixed | `Get-ScheduledTaskInfo` |
| **Notification channels** | **NEW tonight.** ScanHound has ZERO working outbound channels; Gotify agreed as the fix (§5, item 3) | probed the live config |

**The pattern worth carrying:** the list was accurate about *what* the items
were and unreliable about *where they stood* — the same failure as the memory
audit in §7. Check state; do not recite it.

## 6. What went wrong tonight, and the one lesson

**Four times I wrote code that was correct and reached nobody:**

1. a conflict state a sibling part silently overrode (peer review found it);
2. an alert sent through a `NotificationManager` with zero configured channels;
3. the same alert aimed at `AppService.notification_manager`, an instance
   nothing ever calls `configure_from_dict` on — permanently channel-less;
4. an alert broadcast to whoever happened to be connected, while marking the
   state "seen" before delivering.

A send through zero channels, a broadcast to zero sockets, and a real delivery
are **indistinguishable at the call site** — all return without error. Ask who
RECEIVES a thing, not whether the function ran. Where an event is best-effort,
back it with state that can be re-read.

**And a fifth, different shape: I claimed a capability did not exist without
looking.** I told the reviewer there was no way to test rendering without a new
dependency. The repo has had Playwright all along — config, four e2e specs,
`@playwright/test` installed, and CI running it on every push. I had even seen
the `tests/e2e` directory in a listing earlier the same evening. I searched for
one specific thing (component tests), found none, and reported the general
absence. **A negative claim needs a positive control just as much as a
measurement does** — "I looked for X and didn't find it" is not "X isn't there".

**Two measurements retracted** — the 335, and a "~38 titles missing a badge"
estimate that rested on `dv_scan.rating_key` implying a past label pass (it does
not; `plex_metadata_scan.py:301` writes that column too).

**And a fact I got wrong all night:** I told the reviewer the repo was PRIVATE in
three consecutive requests. It is **public**, and CI runs green on every push —
so twice they wrote a paragraph explaining they could not treat my test numbers
as independently confirmed, on the strength of my error. `gh api repos/... --jq
.visibility` before every handoff; never carry it from memory.

## 7. Memory was audited too

117 pointers, all verified resolving, index rebuilt. Five corrections, each
checked against reality rather than reasoned about: repo visibility; CI state;
the Windows Terminal issue is **not** resolved (its own file says never call it
fixed without Jesse confirming); the Kometa DV tag set **is** deployed (verified
in the container); the July audit remediation **is** deployed (its commits are
ancestors of the running image).

**Six of seven "unmerged/not deployed" claims in memory were false.** The trap
worth carrying: `agent/dv-scan-hang-and-starvation` is genuinely unmerged, but
its commit `48cbd53` shipped by another route. **Never infer a fix's state from
its branch's state — check the commit, then check the container.**
