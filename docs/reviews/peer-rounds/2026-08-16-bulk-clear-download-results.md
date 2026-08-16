# Peer review — "Clear done" does nothing

**Repository:** `LstDtchMn/ScanHound`
**Branch:** `feat/bulk-clear-download-results`
**Base:** `feat/queue-review-followups` (`2081c62`) — **not** `main`. That base is
the ChatGPT-approved queue branch; if it merges first this rebases cleanly onto
`main`.

Files: `backend/download_service.py`, `backend/api/routes/downloads.py`,
`frontend/src/lib/api/client.ts`,
`frontend/src/lib/components/mobile/MobileDownloadsView.svelte`,
`tests/test_bulk_clear_download_results.py`.

---

## The report

The owner: *"the clear completed items doesn't work at least in mobile view."*

## What it was

`MobileDownloadsView.clearFinished()` looped `removeDownloadResult` over every
finished row and awaited each. Production holds **578 result rows, 563 finished**,
so one tap fires **563 sequential HTTP requests** — and `remove_package` re-reads
*all 578 rows* per call plus its own JDownloader round trip. O(n²) with a network
hop per row.

It also had **no busy/disabled state** (its three sibling buttons do), **no
completion message**, and `catch { }` swallowing every error. So it reads as a dead
button, and navigating away abandons the job part-done.

## The part worth reviewing: why the existing bulk route is a trap

A bulk route already exists — `DELETE /download/results` → `clear_download_results()`
— and wiring the button to it is the obvious one-line fix. **It would be worse than
the bug.** It deletes our rows and tells JDownloader nothing. JDownloader still OWNS the
packages, so the clear is **non-durable**: once the poller needs to persist them
again — a state change, cache invalidation, or a restart — they reappear. "Half
works" is harder to diagnose than "does nothing".

*(Round-2 correction, adopted from the review: "the next poll re-upserts every
package" was too strong. With a hot `_results_cache` an unchanged package can skip
the immediate write — which makes the old route more deceptive, not safer.)*

The `remove_package` docstring and its cache-eviction comment already describe this
resurrection path for the single-row case. The bulk route predates them and never
learned it.

## The fix

`remove_packages(ids)` — `remove_package`'s discipline, once instead of N times:

1. read the rows **once**;
2. hand the whole set to JDownloader in a **single** `remove_links([], uuids)` call
   (it takes a list);
3. delete the rows;
4. evict `_results_cache` / `_uuid_id` / `_best_titles` for each key, or an
   unchanged package still in JD hits `poll_results()`'s unchanged-state skip and
   re-emits the id we just deleted.

**REVISED IN ROUND 2 — see §Round 2.** This first version cleared the rows even
when JD failed, on an idempotence argument. That was wrong: it reports a durable
removal that the next poll undoes. It now fails closed. New route
`POST /download/results/remove-many`. The
caller sends the ids it means, so "finished" stays the client's policy rather than
being re-derived server-side where the two could disagree.

~~Also: `downloaded` now counts as finished.~~ **REVERTED IN ROUND 2** — a failed
`query_links()` also produces `downloaded`, so it cannot distinguish "nothing to
extract" from "we could not look". See §Round 2, MEDIUM 3.

## Tests — 13 in round 1, **27 after round 2**

Including *exactly one* JDownloader call for 563 rows, rows read once rather than
per id, int-not-string uuids, cache eviction, an unreachable JD still clearing, a
DB failure not aborting the rest, and a guard that this never falls back to the
cosmetic table-only clear.

**Backend suite: 5067 passed, 0 failed.**

## Questions

1. **Is `downloaded` really "finished" for the user?** It means the archive
   downloaded and extraction has not succeeded — which could be "extraction is
   about to run" rather than "done". `extracting` is excluded, so the window is
   narrow, but a package waiting on a queued extraction would now be clearable.
   Is that the right call, or should `downloaded` be clearable only after some age?

2. **Should the JD removal failing change the return?** Today the rows clear and
   the response says `removed: N` regardless. That is deliberate (idempotence,
   matching the single-row path) but it means the UI can report success while JD
   still holds the packages, and the next poll will bring them back. Better to
   surface a partial state?

3. **Unverified frontend.** This environment has no `node_modules`, so the
   Svelte/TS half is typechecked in CI only, and the UI is behind a login gate I do
   not have credentials for. Worth a specific look at
   `MobileDownloadsView.svelte`.

4. **A type lie found in passing, not fixed here.** `DownloadResult.id` is declared
   `number`, but `download_service` has a path where `row["id"]` stays `None` if
   every id-recovery attempt fails. The new code guards with `r.id != null`. Should
   the type be `number | null`, or should the backend refuse to emit an id-less row
   at all?

---

## Round 2 — all three MEDIUMs closed

Each was verified against the code before being acted on.

### MEDIUM 1 — the stale-snapshot race: **fixed**

A results **epoch**. A poll captures it immediately before its JD snapshot and
`poll_results` refuses to persist if it changed underneath; a removal that actually
deleted something advances it under a short lock. No lock is held across a network
call, as you specified. A removal that deleted nothing does **not** advance it, so an
unrelated no-op cannot discard a healthy poll.

Also fixed alongside it: the first version evicted the caches for **every** targeted
row, including ones it deliberately kept. That would push the next poll into the
cache-miss branch and have it rewrite exactly the rows we chose not to delete —
performing the resurrection by hand. Eviction is now scoped to what was actually
deleted.

### MEDIUM 2 — success reported for a failed removal: **fixed, fail-closed**

`remove_links`'s return is now checked. An explicit `False` is a refusal; `None`
stays success, because `removeLinks` is a void action and demanding truthiness would
fail every real call — the mirror mistake.

If JD did not positively succeed, the **known-JD rows are kept**, `jd_removed: false`
and `durable: false` are returned, and the UI says *"Could not clear"* rather than a
success message. Orphans with no JD side are still dropped. A DB read failure now
returns a failure instead of being folded into "already gone", and a partial delete
sets `ok: false` with `errors`.

You were right that the tests did not constrain this contract at all: **the double
returned `None` on its success path**, so neither direction was pinned. It now takes
an explicit `returns` and there are cases for `False`, `None` and `True`.

### MEDIUM 3 — `downloaded` as "finished": **reverted**

Verified: a failed `query_links()` sets `links_observed = False` and leaves
`child_links` empty, and the state classifier ignores that distinction, so a fully
downloaded package falls through to `downloaded`. Taking the conservative option —
`FINISHED` is back to `['extracted', 'failed']`.

Worth noting the signal already exists: `links_observed` was introduced by your
2026-08-13 round precisely to separate "absence of observation" from "observation of
absence", and it reaches the provenance write but not the state field. Making it
reach the state is the real fix and is **not** on this branch.

### LOW — `Clearing…` during Pause/Resume/Stop: **fixed**, dedicated `clearing` flag.

### One more, found by the suite rather than by either of us

Reading the epoch from `poll_results()` made a missing epoch field raise — so any
`DownloadService` not built through `__init__` turned **every poll into a FAILURE**,
surfaced as "JDownloader poll failing". An accounting field raising a liveness alarm.
The helpers are now self-initialising, with a test that a bare service still polls.
This codebase already has the rule (a log write under fail-fast once killed a job at
line 1); I re-learned it in a new place.

**Tests: 13 → 27.** Full suite green.

### Not done

- The `poll -> remove -> poll -> assert absent` lifecycle test you asked for. The
  epoch unit tests pin the mechanism, but not the end-to-end promise, and I would
  rather say so than imply coverage I have not written.
- `remove_package()` (single) still lacks the epoch discipline. It has the same race;
  I did not want to change the single-row path in the same commit that changes the
  bulk one.
- Splitting `DownloadResult` into persisted/live types.
