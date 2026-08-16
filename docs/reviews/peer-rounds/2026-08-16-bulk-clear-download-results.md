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
the bug.** It deletes our rows and tells JDownloader nothing; `poll_results()`
re-upserts every package JD still holds, so the list empties and returns on the next
poll. "Half works" is harder to diagnose than "does nothing".

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

Idempotent like the single path: an unreachable JD still clears the rows, because
the user asked for them to go. New route `POST /download/results/remove-many`. The
caller sends the ids it means, so "finished" stays the client's policy rather than
being re-derived server-side where the two could disagree.

Also: **`downloaded` now counts as finished.** A package whose archive downloaded
but whose extraction never ran sits in that state permanently — it is exactly the
"100% complete but still listed" row, and the old filter matched only `extracted`
and `failed`.

## Tests — 13

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
