# Comprehensive review request — everything ScanHound shipped 2026-07-13 → 2026-08-10

**Repository:** `LstDtchMn/ScanHound` (private)
**Review range:** `a4090c3` (2026-07-13) → `9227578` (`main`, 2026-08-10)
**Size:** 426 commits, 30 merged PRs (#22–#60), 388 files, ~132k insertions
**Status of this code:** **LIVE IN PRODUCTION** (deployed 2026-08-10/11 on TurtleLandSRVR)
**Date:** 2026-08-12

This is an **independent** comprehensive review. It is deliberately unanchored: the other peer
(Claude) is reviewing the same range in parallel and its findings are **not** included here, so
that agreement between the two reviews is real corroboration rather than confirmation bias.

## What the app does

ScanHound is a homelab media-automation service (FastAPI + Svelte + SQLite). It scrapes release
listing sites (primarily HDEncode), queues and performs downloads through JDownloader, renames and
moves media files into a Plex library across SMB shares, and detects Dolby Vision variants
(FEL/MEL/Profile 8/Profile 5) so Plex overlays can badge them. It runs in Docker on Windows and
touches irreplaceable user media on disk.

## What shipped in this window (by theme)

1. **Download queue durability + recovery policy** (#22–#31, #44–#46, #51–#55) — a durable staggered
   download queue, a pure `queue_recovery_policy.decide()` shared by the app and operator tools,
   item-level cooldown authority, unknown-outcome safety, duplicate-resolution, and diagnostics.
2. **Cloudflare Turnstile verification hold** (#57) — a download hold that a timer cannot release.
   Source-scoped (`download_queue_batches.verification_hold_source`, schema **v9**), released only by
   an affirmative source reveal or an explicit operator clear. Includes challenge/interstitial
   classification and a standalone migration script.
3. **Dolby Vision post-rows redesign** (#58) — the container no longer reads the host detector's
   SQLite file over the Windows bind mount (a read that silently returned 0 rows behind HTTP 200).
   The host detector now POSTs its rows to `POST /rename/dv-host-rows` as a cumulative idempotent
   snapshot, with a mechanically checkable response contract.
4. **Scoped DV ingest key** (#60) — a least-privilege machine credential that authorizes **only**
   `POST /rename/dv-host-rows`. Server configures `SCANHOUND_DV_INGEST_KEY_SHA256` (hash only);
   detector sends `X-DV-Ingest-Key` via an unredirected header through an opener that refuses
   redirects and ambient proxies.
5. **RSS shadow discovery** (#43, and the hdencode_* modules) — RSS feed polling running in
   *shadow* mode alongside listing scraping, with miss accounting, a readiness gate, and a
   request-pacing coordinator. Not promoted to primary.
6. **Rename / fileops** (#36, #38, #40–#42, and ongoing) — path canonicalization, duplicate
   resolution, trash/undo, process control, cache-wipe and category-switch hotfixes.
7. **Security + infrastructure** (#37, #33–#35, #39) — an external security report review, NAS mount
   hardening, and path-confinement work.

## Highest-churn files (where to concentrate)

```
backend/database.py                     ~3236 lines changed
backend/download_queue.py               ~2342
backend/download_service.py             ~1619
backend/rename/fileops.py               ~1505
backend/hdencode_shadow.py               ~667
backend/hdencode_coordinator.py          ~605
backend/download_outcome.py              ~584
backend/api/routes/rss.py                ~524
backend/scanner_service.py               ~496
backend/hdencode_action_service.py       ~454
backend/rename/service.py                ~417
backend/hdencode_candidate_service.py    ~394
backend/background_scanner.py            ~385
backend/hdencode_rss_service.py          ~380
scripts/host-detector/dv_host_scan.py    ~357
tests/  (103 files)                    ~22982
```

## What we want from this review

Find **correctness, security, and data-loss defects in code that is live in production.** Ground
every finding in `file:line` and a concrete failure scenario (inputs/state → wrong outcome).

Priority order:

1. **Correctness** — wrong results, silent failures, a failed operation reported as success, state
   machines that can wedge, lose, or duplicate work.
2. **Security** — auth bypass or scope escape, credential exposure, path traversal, injection.
3. **Data loss** — anything that can destroy or misplace user media (`rename/fileops.py` especially).
4. **Interactions between the recently-changed subsystems** — the queue, the hold, DV, RSS, and
   fileops all changed at once and share `database.py`.
5. **Tests that do not discriminate** — assertions that would still pass if the logic were inverted
   or deleted, mocks that stub out the very thing under test, negative tests with no positive
   control. These matter because they manufacture false confidence.

### Method notes (this project's recurring defect classes)

These are the failure shapes that have actually bitten this codebase; they are the highest-yield
things to hunt for:

- **A rule enforced in one layer and bypassed by an adjacent one.** Walk backward from every
  *effect* to the authority that should gate it, and grep every consumer — a bypass may never call
  the authority at all.
- **A failure reported as success.** Three upsert adapters recently converted a failed write into
  `True`; a bind-mount read returned "0 rows, OK" when it had actually failed. Assume this class
  recurs and look for survivors.
- **"Process failed" conflated with "found nothing."** These want opposite handling.
- **Fail-open under uncertainty.** Several gates were fixed to fail closed; check the new ones.
- **Tests asserting rows, while bugs live in columns** — a test that checks one case passing does
  not establish the invariant holds for every consumer.

### Out of scope / do not report

Style, naming, formatting, type hints, speculative refactors, "consider adding a comment", anything
not groundable in a specific `file:line`, and anything outside the diff range unless the range made
it newly reachable or newly dangerous.

## Deliverable

A prioritized list of findings (severity, `file:line`, the concrete failure scenario, and a
suggested fix), plus an explicit **completeness statement**: what you did *not* review, and which of
your conclusions rest on reading code rather than executing it. If a subsystem looks sound, say so
plainly — a clean result is a useful result, and we would rather have five well-grounded findings
than thirty speculative ones.

Merge, deploy, and production settings are Jesse's decisions only.
