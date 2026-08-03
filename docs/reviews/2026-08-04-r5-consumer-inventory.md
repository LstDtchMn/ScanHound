# R-5 — Consumer inventory of derived release facts (round-10 required item 6)

**Date:** 2026-08-04 · **Method:** three parallel enumerations (RSS vertical,
listing vertical, cross-cutting/UI/rename), every claim cited file:line; full
structured record at `evidence/2026-08-04-r5-consumer-inventory-full.json`.
This inventory is the prerequisite round 10 set for finalising R-4 —
"otherwise the invalidation design does not know all of its consumers."

## The rankings that matter (worst consequence first)

1. **`_validate_auto_action`** (`hdencode_action_service.py:381-463`) — the
   ONLY autonomous authorizer; reads seven derived fields with no notion of
   row age, and freezes its queue-time evidence into
   `authorized_evidence_json` as if current. **The prime `stale_derived`
   EXCLUDE consumer.** Note: explicit/manual actions bypass it entirely —
   the RSS UI row is the whole evidence gate for a manual grab.
2. **Grab-time write-through** (`save_to_history` both paths) — stale facts
   frozen into `downloads` rows that outlive every cache and mis-rank every
   future sibling (a stale 2160p hides genuine 4K upgrades forever).
3. **Package naming + JD folder routing** — four derived facts baked into
   the pipeline JOIN KEY at queue time; **invalidation must version facts,
   NEVER recompute package names** (confirmed: rename joins by
   package_name, not URL — `pipeline_service.py:273`).
4. **`rematch_cache` — "the launderer"** (`scanner_service.py:1331`) —
   re-scores stale blob facts every cycle and re-persists fresh-looking
   statuses; identity/quality fields age indefinitely under
   just-recomputed timestamps. The single loop R-4 must break.
5. **`matching.py` library comparison** — stale media_type/season/year →
   wrong-library queries, phantom upgrades, owned titles reading MISSING.
6-13. Auto-grab (both paths), pipeline regrab (**force=True bypasses both
   dedup gates**), candidate context/classification, results
   facets/bookmarks/dismiss-backfill, RSS serializers (one action reads
   facts at THREE times — queue/claim/submit — so mid-action staleness
   yields internally inconsistent records), dashboards, exports.

## Design constraints R-4 inherits (each measured)

* `hdencode_candidate_details.payload` is **WRITE-ONLY** (one INSERT, zero
  readers repo-wide) → version `hdencode_candidates` + `background_scan_cache`
  only; the payload is archive.
* The parse-cache gate `_bg_cache_rev` is **in-process** and bumped by
  exactly three writers → any R-4 migrator that touches blobs without
  bumping it makes `/results/cached` serve stale parses silently.
* `year_conflict`/`evidence_incomplete` are derived independently in SQL
  (`database.py:2242`) and Python (`rss.py:151-159`) → a staleness field
  must be honoured in BOTH or counts diverge.
* The feed-upsert hydration guard (`database.py:1483-1502`) is where
  staleness becomes PERMANENT → R-4's explicit completed→refetch transition
  must interact with exactly that CASE.
* **Verified negatives** (no stale signal needed): Kometa/DV labels read
  file scans only; rename identification re-derives from the file itself;
  `hdencode_shadow.py` + `backend/sweep/*` are identity-only (grep: zero
  derived-field reads) — Phase A instruments need nothing; **Phase B's
  bridge MUST exclude stale rows or bind a derivation-version digest**.
* Watchlist matching is **dormant** (zero callers of
  `check_against_scan_results`).

## Defect candidates surfaced (round-11 scope, not fixed here)

Two sites still reconstruct TV-ness from `season is not None` instead of the
carried verdict — `results.py:35` (category fallback) and `results.py:578`
(bookmark identity) — the exact pattern `web_item_facts` documents as the
bug that sent tokenless TV to the movie library.
