# ScanHound — Remaining Work

> Living checklist of what's left, kept in the repo (on the server) so it survives
> sessions. Last updated 2026-06-29. Deploy ONLY via `docker compose up -d --build`.

## Dolby Vision FEL/MEL feature

Full design + locked decisions: [docs/feature-prompts/dv-fel-mel-detection.md](feature-prompts/dv-fel-mel-detection.md).

### Done (deployed)
- [x] `dovi_tool` 2.3.2 + mkvtoolnix (`mkvpropedit`) in the image; `/rename/health` reports the `dovi_tool` binary + `dv_detection` capability.
- [x] `backend/rename/dv_detect.py` — `detect_layer(path)` (verified 2-stage recipe, fail-safe) → `fel|mel|profile5|profile8|none|unknown`.
- [x] `dv_scan` DB table + helpers (upsert/get/list/count, `dv_scan_is_current` change-signal skip).
- [x] Manual **Dolby Vision** scan: `POST /rename/dv-scan-folder` + `GET /rename/dv-scans`, `RenameService.scan_folder_dv` (detection-only, populates `dv_scan`, WS progress), Renames-page UI with FEL/MEL/P5 badges + inventory.

### Blocked on the owner's two lists (FEL + non-FEL), coming this evening
- [ ] **Seed importer** — load the FEL list → `dv_layer='fel'` and the non-FEL list → `dv_layer='not_fel'` (skip-list), `source='seed'`, into `dv_scan`. Confirm the line format first (full paths preferred vs titles → fuzzy match) and whether "non-FEL" is opaque or specifically MEL.
- [ ] **Real-file accuracy validation** — run `detect_layer` on a known-FEL file and a known-MEL file in the container; confirm `dovi_tool`'s `(FEL)`/`(MEL)` token classifies correctly. (The recipe is source-verified; this confirms it on the actual library.)

### Still to build (needs the above + design calls)
- [ ] **Per-file hook on ingest** — call `detect_layer` in `_process_file_inner` behind a default-off `dv_detection_enabled` config flag, writing through to `dv_scan` (decide: inline vs queued, since the RPU walk adds seconds per 4K add).
- [ ] **Plex label write-path** — `PlexManager` method to `addLabel('DV FEL')`/`removeLabel` (map file→item via path/`rating_key`/`imdb_id`); clear the opposite label; record `rating_key` back into `dv_scan`. (No `addLabel` usage exists yet.)
- [ ] **Kometa overlay** — `config/overlays/dv_fel.yml` gated on the `DV FEL` label (config lives in the Kometa volume, outside this repo). Decide the scheduling handoff so a new label lands before Kometa's next run.
- [ ] **Optional file tag** — `mkvpropedit --edit track:v1 --set name="Dolby Vision Profile 7 FEL"` behind a default-off `dv_file_tagging_enabled` flag; **re-record `(mtime,size)` into `dv_scan` after tagging** so the write doesn't self-invalidate the cache / trigger a Plex re-analyze. MKV-only.
- [ ] **Config (4-place pattern)** — `dv_detection_enabled`, `dv_file_tagging_enabled`, `dv_library_paths`, `dv_fel_label` in `config.py` (AppConfig + defaults), `settings.py` (SettingsUpdate, `extra="forbid"`), and the Settings → Renaming tab.

### Open design questions (settle during build)
- [ ] Badge MEL on the poster too, or only FEL?
- [ ] Final Plex label vocabulary (`DV FEL`/`DV MEL` vs `FEL`/`MEL`).
- [ ] Ship the file tag in v1, default on or off?
- [ ] Re-rip change detection beyond `(mtime,size)` — is a per-folder "force re-scan" enough, or add a partial content hash?
- [ ] `dovi_tool` version pinning + verifying the exact `(FEL)`/`(MEL)` summary string once in-container (wording has drifted across versions).
- [ ] Profile-8 "EL stripped" — surface distinctly from native P8, or one bucket?

## Other follow-ups (non-DV)
- [ ] **TV library is unset** (`auto_rename_tv_library = ""`). TV matches now hold for review with a clear message; set a path if/when TV auto-rename is wanted.
- [ ] **`G:\Downloads` not path-mapped** as a *source* (only `F:\Downloads → /library/movies`). Intentional today (JD saves to F:); add `G:\Downloads => /library/movies-4k` if 4K grabs ever land on G:.
- [ ] **`/rename/jobs` full-jobs fetch** for conflict/keep annotation is O(all jobs) per call — fine at current scale; revisit (cache / status-filtered query) only if the table grows to many thousands. (Reviewed: intentional, not a defect.)

## Scan pagination — deferred
- [ ] **`POST /results/select-all` with a body is dormant/unwired.** Implemented and tested (`backend/api/routes/results.py`, `SelectAllRequest` — filters by source/status/search/category/genre/language/quick, returns matched `group_keys`), but the UI's select-all only uses the URL-keyed "select all loaded" path (`selectAll(filteredKeys)` in `frontend/src/lib/stores/results.ts`, which POSTs the no-body `select-all` today). Kept in place for a future "select all N filtered" action (select beyond what's currently loaded/paged in).
- [ ] **Live mode has no pagination.** `pagedMode=false` (live scan results) holds its full result set in memory with client-side filtering — there's no infinite-scroll/page fetch for it like the paged/cache browse view has. A reloaded live set is capped at whatever `+page.svelte`'s onMount fetches in one shot (`per_page: '500'`); a prior live scan with more than 500 results will have items 501+ unreachable until a fresh scan re-streams them. Tracking note only — no fix planned unless a live scan regularly exceeds 500 results.

## Notes
- The duplicate **keep-recommendation** infers quality from the *filename* only (rename jobs don't store size/HDR/DV columns). Good enough for resolution/DV/HDR/source/audio; it can't see actual file size.
- Heuristic fallbacks (subtitle/OCR/vision/cast) always route to `needs_review`, never auto-apply — preserve this.
