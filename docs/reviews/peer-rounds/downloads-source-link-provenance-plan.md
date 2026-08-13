# Live source links: link-based package provenance (plan)

**Branch:** `feat/download-first-grabbed-and-link`
**Status:** PART 1 OF 2 BUILT. The branch must not merge until part 2 lands —
Jesse's decision, 2026-08-12.

- **Done:** provenance is RECORDED (`download_package_links`, written on a
  successful send) and RESOLVABLE (`resolve_release_by_links`, fails closed on
  unknown or ambiguous links). 10 tests, ambiguity guard mutation-verified.
- **NOT done, so Finding 1 is NOT yet fixed:** nothing consumes it. Live rows are
  still annotated by `get_download_source_links()` — the name-based resolver the
  reviewer rejected. Recording a fact does not change a decision; the wrong-link
  path is open until the consumer switches over.

## Why

Peer review Finding 1 (MEDIUM, confirmed against production code). The resolver's
safety property — "a name that maps to more than one release resolves to nothing" —
is a **closed-world** guarantee. It holds only if every candidate package came from
ScanHound.

`poll_results()` calls `device.downloads.query_packages([...])`
(`backend/download_service.py:1022`), an unfiltered query of JDownloader's entire
Downloads list. A package added to JD by hand is in scope. If its name happens to
equal one ScanHound history row's `package_name`, `COUNT(DISTINCT url) = 1` is
satisfied and the UI renders a confident link to an unrelated release — plus that
release's date. The guard cannot see the collision, because the foreign package
contributes no `downloads` row to collide with.

`jd_confirmed_name` does not close this: `capture_jd_confirmed_names()` is fed the
same global JD name set, so an external package can satisfy its unique folded match
too. It is empirical JD spelling, not provenance.

## Decision

**Establish provenance from the file-host links ScanHound actually submitted.**

Rejected alternatives, with reasons:

- **Tag the package name.** Cheapest, but `rename_jobs`, `_match_download_results`,
  `capture_jd_confirmed_names` and the auto-rename hand-off all key off that name.
  Changing its format to fix a cosmetic link risks the code that has already
  produced real production bugs. Bad trade.
- **Capture the package uuid just after `add_links`.** JD creates packages
  asynchronously via the linkgrabber, so the read-back is a race — and the
  `.crawljob` folder path has no API to ask at all, leaving that path unprotected.

Links are true by construction: the links ARE the release, so no naming
coincidence can produce a false positive, and both send paths know them.

## Shape

1. **Persist what was sent.** A table keyed by release url holding each submitted
   file-host link (normalised). Written in `send_to_jdownloader()` for BOTH the
   `api` and folder/`.crawljob` branches — the folder path is exactly where a
   uuid-based scheme would have failed, so it must not be skipped here.
2. **Resolve a live package by its links.** NO EXTRA JD CALL IS NEEDED:
   `poll_results()` already issues `device.downloads.query_links([...])` with
   `"url": True` and builds `by_pkg` (packageUUID -> child links) at
   `backend/download_service.py:1033`. Feed those urls to
   `resolve_release_by_links()` and persist the answer on the result row, so the
   REST path (which reads the DB, not the live poll) sees the same proven
   association as the WebSocket push.
3. **Fail closed.** No proven association -> `source_url` and `first_grabbed_at`
   stay `None`. Unproven is the default, not the exception.
4. **Evidence order** once provenance exists (peer's Finding 3): proven package
   identity > exact `jd_confirmed_name` > exact computed `package_name`. Empirical
   confirmed names should outrank computed ones rather than collide with them and
   suppress both. Do NOT add folded matching — it widens the collision surface,
   which is the opposite of what this fixes.

## Required tests (peer-specified)

1. A live `download_result` with **no** ScanHound provenance whose name exactly
   matches one historical `package_name` -> `source_url is None`,
   `first_grabbed_at is None`.
2. Positive control: the same name **with** affirmative provenance -> resolves.
   Without this the first test passes trivially if resolution breaks entirely.
3. Precedence: exact `jd_confirmed_name` on A colliding with computed
   `package_name` on B -> pinned, not mutually suppressed.
4. Folder/`.crawljob` send path records links too — the branch a uuid scheme
   would have missed. Recording sits at the `download_item()` call site, which is
   ABOVE the api/folder split inside `send_to_jdownloader()`, so both are covered.
   A test driving `download_item()` must pin that, rather than trusting a read of
   the call site.
5. A FAILED send records nothing — recording is inside the success branch.
6. **`hdencode_action_service.py:278` also calls `send_to_jdownloader()` and is NOT
   wired.** Its packages resolve to nothing: safe (no wrong link) but no link at
   all. Wire it in part 2, with a test, or state the gap explicitly at merge.

## Already closed on this branch

- **Finding 2 (LOW)** — `ad61f2e`. The label is "first seen", not "first grabbed":
  `download_item()` writes history for failed attempts, so `date_added` can name a
  moment when nothing was grabbed. Relabelled rather than backfilled, because a
  true first-success time cannot be reconstructed for the existing 579 rows.
- Reviewer accepted as sound: render-time `safeHttpUrl()` as the load-bearing sink
  defense, the shared REST/WebSocket annotation, annotation-after-signature,
  degrade-to-None on lookup failure, fresh-database index ordering, and query cost.
