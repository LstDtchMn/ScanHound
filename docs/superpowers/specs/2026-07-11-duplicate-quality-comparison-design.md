# ScanHound — Auto-Analyzed Duplicate Quality Comparison: Design Spec

## 1. Overview & Goal

Today, when an incoming file collides with an existing one, `RenameRow.svelte` shows an "⚠ Already in library" badge with the raw `warning_message` (a full sentence including exact byte counts) as a hover tooltip, and the rich Existing-vs-Incoming spec table (`RenameReviewCard.svelte` + `specRows()`) only renders once you open the card/modal and its `conflict_preview` fetch resolves. FEL/MEL Dolby Vision layer resolution is manual-only (a "Scan DV layers" button). And a duplicate that's already in the library **at a different path** than the incoming file's destination (e.g. a movie already present under `movies/Title (Year) [4K]/` while the incoming release would land at `movies-4k/Title (Year)/`) is invisible to all of this — `hasDestinationConflict()` only fires on an exact destination-path collision, so the row shows no warning and the card shows "Destination is free."

This closes three gaps:
1. **Every duplicate gets analyzed automatically** (not just exact-path collisions, not just on-demand), so the row can show a real quality diff the instant it renders.
2. **The row's tooltip becomes a concise one-line diff** instead of the raw byte-count sentence.
3. **FEL/MEL detection runs automatically when it's the deciding factor**, not only when the user clicks a button.

Recommendations remain **advice only** — every resolution action (Overwrite / Apply anyway / Keep both / Skip) stays a human click. Nothing here auto-moves or auto-deletes a file.

## 2. Current State (Code-Grounded) & Gaps

### 2.1 Probing — `backend/rename/mediainfo.py` (present, reusable)
`probe_specs(path, timeout=30, db=None) -> dict | None`. One `ffprobe` call → `{present, path, size_bytes, container, duration_min, bitrate, resolution, video_codec, hdr, dv_layer, audio}`. Fail-safe: `None` on ffprobe error/timeout/missing binary, `{"present": False, ...}` when the file doesn't exist. `dv_layer` is read **only** from the `dv_scan` cache (`_cached_dv_layer`, signature-validated by `(mtime, size)`) — never shells `dovi_tool` itself. **Not cached** — every call re-probes, even for a path just probed seconds ago.

### 2.2 Ranking — `backend/rename/conflicts.py` (present, reusable)
`rank_conflict(existing, incoming) -> {recommended, reason}` and `_quality_score()` already rank on probed specs (resolution tier, DV bit, DV layer rank, HDR, source, audio, edition) with graceful degradation to filename heuristics when a probe is `None`/absent. `conflict_annotations()` already does group-wide same-destination duplicate detection + keeper recommendation for the **exact-path** case.

### 2.3 Preview endpoint — `POST /rename/jobs/{id}/conflict-preview` (present, reusable)
`RenameService.conflict_preview(job_id)` builds `{existing, incoming, recommended, reason}` on demand, no persistence. Called once per card-open from `RenameReviewCard.svelte`.

### 2.4 On-demand DV scan — `POST /rename/jobs/{id}/scan-dv-conflict` (present, reusable)
Fires `dovi_tool` on both files in a background thread, broadcasts `dv:conflict_scan_done`. Triggered only by the manual "Scan DV layers" button (`needsDvScan()` gates its visibility).

### 2.5 Row-level conflict detection — `frontend/src/lib/renames/review.ts` `hasDestinationConflict()` + `job.warning_message`
The row's "Already in library" state comes from the backend's collision branch in `RenameService.apply()`/the pre-apply guard, which only fires on an **exact destination-path** collision (`conflicts.py`'s `_dest_key()` — normalized `destination_path + new_filename`). `job.warning_message` is a full sentence (`"A file already exists at the destination: <path> (existing N bytes vs candidate M bytes) — review to replace or keep the existing file."`) rendered verbatim as a `title=` tooltip on `RenameRow.svelte`'s badge. This is gap #2.

### 2.6 Plex cache — `backend/plex_service.py` + `database.py: plex_cache` table
`plex_service.py`'s scan already computes `part.file` (the served path), `videoResolution`, `dovi`, `hdr`, and `size` per movie while walking `lib.all()` (lines ~448-563) — **but `save_plex_cache()` does not persist the file path column**; `plex_cache`'s schema is `key, title, original_title, year, res, size, imdb_id, rating_key, media_id, is_tv, season, episode_count, content_type, dovi, hdr, last_updated, library_name`. This is gap #1's blocker for library-wide duplicates: the served path needed to `ffprobe` the existing copy isn't stored anywhere today.

## 3. New Schema

### 3.1 `media_probe` table (new) — ffprobe result cache
```sql
CREATE TABLE IF NOT EXISTS media_probe (
    path TEXT PRIMARY KEY,
    sig_mtime REAL,
    sig_size INTEGER,
    probe_json TEXT,        -- the full probe_specs() dict, JSON-encoded
    probed_at TEXT
)
```
Mirrors `dv_scan`'s `(mtime, size)` signature-invalidation pattern exactly (`dv_scan_is_current` already does this — reuse the same helper shape, `media_probe_is_current(path, mtime, size)`). `probe_specs()` gains a cache-check at its top: if the on-disk `(mtime, size)` matches the cached signature, decode and return `probe_json` instead of shelling `ffprobe`. A probe failure (`None`) is **not** cached — always retried next time, since a transient ffprobe hiccup shouldn't wedge a file into permanent "unknown" state.

### 3.2 `rename_jobs.conflict_analysis` (new column, TEXT/JSON, nullable)
```sql
ALTER TABLE rename_jobs ADD COLUMN conflict_analysis TEXT
```
Written by the background analyzer (§4). Shape:
```json
{
  "kind": "same_path" | "library_duplicate",
  "existing": { "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "mel", "audio": "TrueHD 7.1", "size_bytes": 26881474560, "path": "..." },
  "incoming": { "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "fel", "audio": "TrueHD 7.1", "size_bytes": 31138512896, "path": "..." },
  "recommended": "incoming" | "existing" | "tie" | null,
  "reason": "2160p · Dolby Vision (FEL) · TrueHD",
  "degraded": false,
  "analyzed_at": "2026-07-11T12:00:00+00:00"
}
```
`degraded: true` when either side's probe genuinely failed (mirrors `conflict_preview`'s existing `existing_probe_failed` handling) — the row still shows what it has, but suppresses a confident recommendation. This column is the row's **sole read path** — `RenameRow.svelte` never live-probes; it renders straight from the last analysis, same as every other job field.

### 3.3 `plex_cache.file_path` (new column, nullable)
```sql
ALTER TABLE plex_cache ADD COLUMN file_path TEXT
```
Populated for free during the existing `save_plex_cache()` write — `plex_service.py` already computes `part.file` in memory (§2.6) for every movie during the same `lib.all()` sweep that fills every other cached field; this just stops discarding it. **No new Plex API calls.**

## 4. Background Analyzer

A new `analyze_conflicts()` pass in `RenameService`, structurally mirroring the existing DV-scan background thread (fire-and-forget, per-item try/except, WS broadcast per completed job so the row updates live without a page reload):

1. **Trigger points:**
   - Duplicate *detection* (setting `destination_conflict` or `library_duplicate` — both cheap/synchronous, no probing) happens inline at job creation/update, same as today's exact-path check. The moment either flag is set, this analyzer is enqueued fire-and-forget — mirrors how `scan_conflict_dv` already fires from the collision path today, just automatic instead of button-gated. Detection and analysis are deliberately two speeds: the flag (and thus the row's badge) appears instantly; the probe-backed diff text (`conflict_analysis`) fills in shortly after.
   - Maintenance-loop backfill pass (`app_service.py`'s existing `_run_maintenance_pass`, §2 — add one more sweep): find every job with an active duplicate flag and a stale/missing `conflict_analysis` (`analyzed_at` older than the job's `detected_at`, or null), re-analyze up to **50 per pass** (conservative vs. `backfill_posters`' 200 — ffprobe/`dovi_tool` are far heavier per-item than a TMDB lookup; the maintenance loop already runs hourly, so a large backlog drains within a few passes without saturating I/O in any one pass). Catches jobs whose duplicate only became detectable later (e.g. a library scan added the matching Plex title after the job was created).
2. **Per job:**
   a. Resolve the comparison target — same-path existing file (§2.5's existing logic) **or** a library-duplicate match (§5).
   b. `probe_specs()` both sides (cache-backed per §3.1 — near-instant on a re-analysis).
   c. `rank_conflict()` → recommendation.
   d. **Smart FEL/MEL gate, precisely defined:** `_quality_score()`'s comparison tuple is `(res_rank, dv, dv_layer_rank, hdr, source, audio, edition)` (`conflicts.py`) — `dv_layer_rank` (index 2) is exactly the field a `dovi_tool` scan would resolve. Compute both sides' tuples with index 2 forced to `0` (i.e. as if unscanned); if those two modified tuples are **equal** and both sides have `dv == 1`, then the real (unforced) tuples can only differ, if at all, on `dv_layer_rank` — DV layer is the sole possible tiebreaker, so it's worth resolving. In that case only, fire `dv_detect.detect_layer()` on both (reusing the exact code path `scan_conflict_dv` already calls), wait for it synchronously within this background job (already off the request thread), and re-rank with the resolved layers. Any other outcome (tuples already differ elsewhere, or either side isn't DV) skips the scan — most duplicates resolve on resolution/source/audio alone and never pay the multi-minute `dovi_tool` cost.
   e. Write `conflict_analysis`, broadcast `rename:job` (existing event, already causes the row to re-render — no new WS event type needed).
3. **Throttle:** a small inter-item delay (mirrors `dv-sync-labels`' "inter-write throttle" note) so a big batch of freshly-detected duplicates doesn't saturate ffprobe/SMB I/O.

## 5. Library-Wide Duplicate Detection (the "already in library, different path" case)

New pure function `find_library_duplicate(job, plex_cache_rows) -> dict | None` in `conflicts.py` (DB-free, unit-testable like its siblings):
- Match by `imdb_id` first (exact), falling back to normalized `title + year` using the existing `normalize_title()` (`backend/app_service.py:345`, an alias for `clean_string`) — do not reinvent a second normalizer.
- Only considers **movie** jobs for the first cut (TV's season/episode granularity makes "duplicate" ambiguous — explicitly out of scope, noted in §7).
- Returns the matched `plex_cache` row (with its new `file_path`) or `None`.
- `hasDestinationConflict()`'s frontend logic gains a second source: the job carries a `library_duplicate: bool` flag (mirrors the existing `destination_conflict` flag) set whenever `find_library_duplicate` matches and the job's own destination path is *not* the same as the match (i.e., genuinely a different-path case — same-path is already covered by §2.5 and must not double-fire).
- The analyzer (§4) probes the matched `file_path` directly via `ffprobe` for full spec parity (audio/bitrate/duration that Plex's cached `res`/`dovi`/`hdr` fields don't carry) and folds in `dv_scan`'s cached layer by `rating_key` if present. If `file_path` is null (pre-existing cache row from before this feature) or unreachable (e.g. the SMB share is briefly down), degrade to Plex-cache-only fields (resolution/HDR/DV/size — audio/bitrate/duration show as unknown) rather than blocking.

### Adaptive resolution actions
When `conflict_analysis.kind === "library_duplicate"`, "Overwrite" (which targets a specific destination path) doesn't apply — there's no file *at* the incoming destination to overwrite. `RenameReviewCard.svelte`'s action row swaps to:
- **Apply anyway** — proceed to the normal destination, accepting you'll have two copies in the library.
- **Skip** — leave the incoming file alone; the library already has this title.
"Keep both" is dropped for this kind (there's no dedupe-naming decision to make — the incoming file was never going to collide with the *existing* file's actual name). The recommendation (`recommended: existing/incoming`) visually steers which button is emphasized, exactly like today's same-path case.

## 6. Frontend: Row & Modal

### 6.1 Row (`RenameRow.svelte`) — replaces the raw tooltip
New helper `conflictSummary(analysis: ConflictAnalysis): string` in `conflictView.ts` (pure, unit-tested): builds a compact one-liner from only the axes that **differ** between existing and incoming, e.g.:
> `Existing 4K·DV MEL·25 GB → Incoming 4K·DV FEL·29 GB · keep Incoming ★`

A same-resolution/same-HDR duplicate that differs only in size renders `Existing 22 GB → Incoming 26 GB · keep Incoming ★` (no redundant "4K·4K"). The badge itself is gated on `destination_conflict OR library_duplicate` (both cheap, synchronous flags set the moment a job is created/updated — §5's `find_library_duplicate` is a pure DB join, not a probe) so it appears immediately, including for a freshly-detected library-duplicate that never had a badge before today. `conflict_analysis` (the slower, probe-backed diff text) fills in whenever the background analyzer finishes; until then the badge shows with no diff text, never blocking the row on a live probe. The full-sentence `warning_message` is dropped as the tooltip; the badge's `title=` attribute (if any) becomes a short static string ("Click Compare for full details"), not the byte-count sentence. **Compare** button unchanged, still opens the modal.

### 6.2 Modal (`RenameReviewCard.svelte` / `ConflictModal.svelte`) — unchanged table, new data source
`loadPreview()` still calls `conflict_preview` for the **live**, freshest comparison when the card opens (specs can drift between background-analysis time and the moment you actually look — e.g. the file was re-encoded) — but now `conflict_preview` itself is cache-backed via `media_probe` (§3.1), so this call is fast even though it's still "live" in the sense of always re-checking `(mtime,size)`. `specRows()` (existing) and the ★ recommendation column (existing) are unchanged. `needsDvScan()`/"Scan DV layers" button stays as a manual override for cases the smart gate skipped (e.g. a large duplicate batch where the gate intentionally deferred the slow scan, or the two sides are close enough that the user wants the tie-break Anyway).

### 6.3 Relationship to the Existing `conflict_same_size`/`conflict_existing_size`/`conflict_incoming_size` Columns
`rename_jobs` already has these three columns (from the earlier Desktop Conflict Resolution feature), driving `RenameRow.svelte`'s current GB-chip badge (`"same size · 13.4 GB"` / `"22.1 GB → 28.7 GB"`, lines ~111-115) for the same-path case only. `conflict_analysis` (§3.2) is a strict superset of what those three columns convey (size, plus resolution/HDR/DV/audio) and covers both same-path and library-duplicate kinds. This feature **retires the GB-chip badge in favor of `conflictSummary()`** — the three old columns keep being set (unrelated code in `service.py`'s collision branch still needs `conflict_kind` for its overwrite/keep_both execution logic, out of scope here) but the row stops reading them for display. No column removal — just a display-ownership handoff, to avoid two competing size indicators on the same row.

## 7. Explicitly Out of Scope
- TV episode duplicate detection (season/episode granularity — a genuinely different comparison shape).
- Auto-resolving any conflict without a human click (confirmed in the clarifying round — recommendations are advice only).
- Cross-referencing duplicates against sources *other than* Plex (e.g. two incoming downloads of the same title that haven't reached the library yet) — this spec only covers "incoming vs. what's already in Plex," matching the request's "what is in the library and what has been downloaded" as: library-side always Plex; downloaded-side is whichever incoming job triggered the analysis.
- Changing `rank_conflict`'s scoring weights — this spec reuses the existing ranking function verbatim; if its heuristics need tuning, that's a separate change.

## 8. Testing Strategy
- **`media_probe` cache:** signature match → cached decode, no ffprobe call (mock/spy); signature mismatch (file changed) → re-probes; a failed probe is never cached (retried next call).
- **`find_library_duplicate`:** imdb_id match; title+year fallback; no match; same-path job is excluded (must not double-flag alongside `destination_conflict`); TV job always returns `None`.
- **Smart FEL/MEL gate:** both-DV-and-tied → `detect_layer` called on both paths (spy); any tier already decisive → `detect_layer` never called (spy asserts zero calls) — this is the cost-control property, must be pinned.
- **`conflictSummary()`:** only-differing-axes rendering; identical-except-size case; missing/degraded analysis → em-dash-safe fallback string, never throws.
- **Adaptive actions:** `library_duplicate` kind renders Apply-anyway/Skip, not Overwrite/Keep-both; `same_path` kind renders today's three buttons unchanged.
- **Backend regression:** existing `conflicts.py`/`mediainfo.py`/`conflictView.ts` test suites must stay green — this spec extends, not replaces, their logic.

## 9. Adversarial Review Gate
Per session precedent (a prior ranking-code change once passed a diff-level review while recommending overwriting a 4K Dolby Vision file with a 1080p one, caught only by an adversarial review that executed edge cases): before this ships, the ranking/summary code (`rank_conflict` usage in the new analyzer, `conflictSummary()`'s differing-axes logic, and the adaptive-action gating) gets an adversarial "try to break this" pass — concrete inputs traced through the real code, not a read-through — as part of the implementation plan's review step, not skipped as "already reviewed once before."
