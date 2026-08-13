# Live source links: link-based package provenance (plan)

**Branch:** `feat/download-first-grabbed-and-link`
**Status:** BOTH PARTS BUILT (2026-08-13). Finding 1 is closed: live rows are
authorised by recorded provenance, and name matching is deleted from this path.

- **Part 1** — provenance RECORDED (`download_package_links`, on a successful
  send from BOTH callers) and RESOLVABLE (`resolve_release_by_links`, fails
  closed on unknown or ambiguous links). Ambiguity guard mutation-verified.
- **Part 2** — CONSUMED. `poll_results()` resolves from the child links it
  already holds (no extra JD call), persists to `download_results.provenance_url`
  (COALESCEd, so a later poll that cannot re-derive it does not erase it), and
  `annotate_source_links()` reads only that. `get_download_source_links()` is
  DELETED rather than left unused.
- **Known gap, accepted:** packages submitted BEFORE part 1 shipped have no
  recorded links, so they show no source link until re-sent. Self-healing as new
  grabs accumulate; failing closed is the correct direction.
- **Test hole found by mutation, then fixed:** a name-matching fallback
  reintroduced into `annotate_source_links` was NOT caught, because the function
  returns early when no row carries provenance — so a list of only-unproven rows
  never reaches the per-row guard. A MIXED list (proven beside unproven, which is
  what a live JD list looks like) now covers it, and the mutation fails exactly
  that test.

**Remaining work, exactly:** add `download_results.provenance_url` (guarded ALTER);
resolve in `poll_results()` from the `child_links` it already holds; persist via
`upsert_download_result()` with COALESCE so a proven association sticks; select it
in `get_download_results()`; rewrite `annotate_source_links()` to read that column
and take `first_grabbed_at` from the proven release, retiring name matching for
live rows entirely. Then the peer's tests 1–3, and a whole-tree run.

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
6. `hdencode_action_service.py` also calls `send_to_jdownloader()`. NOW WIRED
   (`d8ab9df`) — it records against the action's `canonical_url` on a successful
   submit. Both send paths and both callers therefore record; a test driving each
   caller is still owed.

## Already closed on this branch

- **Finding 2 (LOW)** — `ad61f2e`. The label is "first seen", not "first grabbed":
  `download_item()` writes history for failed attempts, so `date_added` can name a
  moment when nothing was grabbed. Relabelled rather than backfilled, because a
  true first-success time cannot be reconstructed for the existing 579 rows.
- Reviewer accepted as sound: render-time `safeHttpUrl()` as the load-bearing sink
  defense, the shared REST/WebSocket annotation, annotation-after-signature,
  degrade-to-None on lookup failure, fresh-database index ordering, and query cost.
