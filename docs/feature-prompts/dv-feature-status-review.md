# REVIEW-KICKOFF: ScanHound Dolby Vision FEL/MEL Feature Cluster — Status & Gaps Review

## 1. Context

ScanHound is a standalone Dockerized media-management app (private repo `LstDtchMn/ScanHound`, deployed at `X:\Docker Apps\ScanHound`, served at `scanhound.turtleland.us`). It scrapes release sites for media, sends grabs to JDownloader, auto-renames the results into a Plex library, and surfaces a scan-results grid of what's missing / upgradable / already owned. **Deploy is ONLY via `docker compose up -d --build` from the project root — the frontend is baked into the image, so `docker restart` deploys nothing.** This review covers one feature cluster: (a) **Dolby Vision FEL/MEL detection + inventory** (classify each 4K title's DV layer via `dovi_tool`, store it, surface it), (b) the **auto-rename pipeline** that ingests JDownloader/manual files into the library, and (c) the **scan-results "already grabbed" UX** — the `Downloaded Similar` status, prior-grab sibling annotations, and the configurable grid view. The goal of this review is to confirm exactly what is live, what is built but inert, and what remains to be built, so the next build phase is correctly scoped.

> **Authority note:** Where the raw audits and the adversarial verification conflict, the **adversarial verification is authoritative.** All "wired vs stubbed" claims below follow it.

---

## 2. SHIPPED & DEPLOYED (live in the running image)

### DV detection core
- **`dovi_tool 2.3.2` (static-musl) + `mkvtoolnix` installed in the image** — `Dockerfile`; version pinned, binary verified at build (`dovi_tool --version`).
- **`detect_layer(path)`** — `backend/rename/dv_detect.py`. Two-stage recipe (extract-rpu → `info -s`), fail-safe, returns `{layer, tool, error}`. Layer constants (FEL/MEL/PROFILE5/PROFILE8/NONE/UNKNOWN); verified token parser `_parse_info()`; `dependency_status()`.

### DV database layer
- **`dv_scan` table** — `backend/database.py`. PRIMARY KEY `path`; columns `title, dv_layer, sig_mtime, sig_size, source, rating_key, imdb_id, scanned_at, last_seen_at`. **No `year` column.** Index `idx_dv_scan_layer`.
- **Full CRUD + cache helpers** — `upsert_dv_scan` (ON CONFLICT, preserves `scanned_at`), `get_dv_scan`, `get_dv_scans(layer, limit)`, `count_dv_scans_by_layer`, `dv_scan_is_current(path, mtime, size)` (1s mtime slack, exact size).

### DV manual scan + inventory (the ONLY live entry point)
- **`scan_folder_dv(folder, force, progress_cb)`** — `backend/rename/service.py`. Walks a folder, calls `detect_layer()` per file, upserts `dv_scan`, streams WebSocket progress, single-flighted under `_bulk_lock`, signature-skip via `dv_scan_is_current`, per-file fail-safe stores `layer="unknown"`. Returns `{folder, found, scanned, skipped, by_layer}`.
- **`POST /rename/dv-scan-folder`** + **`GET /rename/dv-scans`** — `backend/api/routes/rename.py`. Background run + WebSocket progress/result; inventory `{scans, counts}` with optional `layer` filter.
- **`/rename/health`** reports a `dv_detection` capability.

### DV frontend
- **Dolby Vision panel** — `frontend/src/routes/renames/+page.svelte`: folder input, `force` checkbox, Scan button, `dvScan()` (stays "running" until WebSocket `dv:scan_done`); FEL/MEL/etc. badge styling.
- **Store wiring** — `frontend/src/lib/stores/renames.ts`: `dvScanRunning/Progress/Result`, `dvScans`, `dvCounts`, `loadDvScans()`, WS listeners `dv:scan_progress` / `dv:scan_done`.

### DV seed import (script-only) — DONE, data loaded
- **`scripts/parse_dv_seed.py`** (host) — reads the UTF-16 PowerShell exports (`[MARKER] filename - Location: <NAS path>`), writes `data/dv_seed.json` as `[{path, title, dv_layer}]`. **Real run loaded 3729 rows: 862 FEL / 581 MEL / 2286 unknown.**
- **`scripts/import_dv_seed.py`** (container) — reads `/data/dv_seed.json`, upserts into `dv_scan` with `source='seed'`, **guarded write** (never overwrites `source='scan'`). Run via `docker compose exec -T scanhound python - < scripts/import_dv_seed.py`.

### Auto-rename pipeline (mature, deployed, SOLID)
- **Three-tier identify ladder** (`service.py`): deterministic (IMDB-id exact → tiered title+year queries with year scoring), cross-type multi-search, Ollama (only on miss + configured; guarded against enabled-but-unconfigured).
- **Media file-reading fallbacks always force `needs_review`** (subtitle/OCR/vision; method shown in `warning_message`).
- **Episode intelligence**: combined-file detection, wrong-episode rescan (gated `delta < -10`, ±3 episodes), split-file sibling detection — all defensively wrapped.
- **Runtime/validity scoring** (`confidence.py`): percentage-based runtime delta, episode-length penalty, filesize fallback.
- **Duplicate detection**: case-insensitive, path-separator-normalized (`_dest_key`), spans all CLAIMING_STATUSES, flags only active jobs.
- **Keep-recommendation** (`_quality_score`): ranks resolution → DV → HDR → source → audio → edition (DV regex guards false positives like "DV.Cam"/"dv-rip"). **NOTE: reads filename tokens only.**
- **Library-not-configured guard**: forces `needs_review` + warning if the destination library isn't set (prevents orphaning files in CWD).
- **Concurrency**: `_claim_path()` atomic check-then-set under lock; `_bulk_lock` non-blocking serialization of process_folder / reidentify_all / scan_folder_dv; thread-local TMDB `search_memo`.
- **`process_folder`**: host→container path translation, per-file `_claim_path` dedup, dry-run `_preview_folder()` (deterministic-only, accurately labeled).

### Scan-results "already grabbed" UX
- **Prior-grab annotation** — `MediaItem.prior_grab` `{resolution, size, downloaded_at, hdr, dovi}` computed by `_download_status_for()` (`scanner_service.py`) against `_downloaded_titles_lookup` by normalized title+season. Optimistic sibling tagging via `markGrabbedSiblings()` (`results.ts`) fires on grab success before backend re-match.
- **`DOWNLOADED_SIMILAR` status** — enum + orange `#f97316` STATUS_COLORS + "Downloaded Similar" text; logic (sibling of same title at equal-or-worse resolution, no DV upgrade). Frontend `constants.ts` → `'orange'`; `Badge.svelte` orange variant.
- **Configurable grid** — `results.ts`: tileSize (sm/md/lg), posterAspect (2:3 / 16:9 / 1:1), tileShowMeta, gridGap, gridColumns (auto-fill or fixed 2–8), all localStorage-persisted; `gridStyle` in `+page.svelte`; `min-w-0` overflow fix.
- **Cache re-annotation on grab** — `_persist_grab_annotations()` → `scanner.rematch_cache()` after single (`downloads.py`) and batch grabs.
- **Background cache re-annotation** — `background_scanner.py`, 3-hourly.

---

## 3. BUILT BUT NOT WIRED INTO THE LIVE FLOW

- **`detect_layer()` has NO per-file ingest hook.** It is invoked at exactly one runtime call site — inside `scan_folder_dv()` (the manual button) — plus tests. **Confirmed:** zero calls in `_process_file_inner()` (the universal funnel for JDownloader packages and manual folder renames) and none in `process_folder()`. Consequence: a file grabbed via JDownloader and auto-renamed gets a rename job and lands in the library, but **no DV layer is detected and no `dv_scan` row is created on ingest.** DV detection is "scan a folder afterward," not "detect on ingest."
- **Seed import path translation — defined intent, not wired.** `parse_dv_seed.py` reads raw NAS paths (`\\TURTLELANDSRV2\...`) and `import_dv_seed.py` stores them verbatim into `dv_scan.path` (a container-view PRIMARY KEY). No translation exists. Seed rows land in the DB but their paths can never resolve to on-disk files in the container — so they are inert for re-scan or any path-based match.
- **LLM badge (narrow by design, looks like a gap).** The frontend lights "LLM" only for `match_source === 'llm'` (the identify-ladder Ollama rung). File-reading fallbacks set `source` to `llm_subtitle` / `ocr_credits` / `llm_vision`, which the badge doesn't catch. These still force `needs_review` and explain the method in `warning_message` — **correct per design, not a bug** — but confirm the reviewer agrees the badge scope is intended.

---

## 4. NOT YET BUILT / TODO

**DV end-to-end output path (the whole back half is missing):**
- **Per-file detection hook** — call `detect_layer()` inside `_process_file_inner()` behind a **default-off `dv_detection_enabled`** flag; decide inline vs queued (adds seconds per 4K file).
- **Plex label write-path** — no `addLabel('DV FEL')` / `removeLabel()` exists anywhere in `plex_service.py` (only the read-only `_check_dovi()`). Needs: map `dv_scan` row → Plex item → set/clear label, write `rating_key` back to `dv_scan`.
- **Kometa overlay** — no `config/overlays/dv_fel.yml`; lives in the external Kometa volume, needs a schedule handoff so labels land before Kometa runs.
- **Optional file tag (`mkvpropedit`)** — MKV track-name write behind **default-off `dv_file_tagging_enabled`**, then re-record post-tag `(mtime, size)`.
- **Config (4-place pattern)** — add `dv_detection_enabled`, `dv_file_tagging_enabled`, `dv_library_paths`, `dv_fel_label` across `config.py` + `settings.py` + Settings UI (today only `/health` reports capability; nothing is toggleable).
- **`dv_scan.year` column** — add it to support title+year matching (needed because seed paths can't resolve, so labeling will match Plex items by title+year — see Risks).
- **Real-file `dovi_tool` accuracy validation** — `detect_layer()` has never run against the actual FEL/MEL library; confirm v2.3.2 emits the expected `Profile: N (FEL)`/`(MEL)` tokens and the timeouts (1800s RPU / 120s info) suffice before going live.
- **Seed-import endpoint** — replace the `compose exec` script with a real `/rename/dv-seed-import` so it's self-serve and can apply path translation.

**Scan-results UX:**
- **`downloaded_similar` filter tab** — status renders but has no filter (tabs are All / Missing / Upgrades / In Library); users can't isolate "grabs I didn't know I had."
- **On-demand cache re-annotation endpoint** — re-annotation fires only on grab or the 3-hourly background pass; no `/cache/rematch-all` endpoint or "force refresh" button, so pre-existing rows show stale labels until the background scan.
- **Keep-recommendation real-storage audit** — `recommend_keep()` reads the scraped release `size` token only, not actual JDownloader/Plex on-disk sizes.

**Open design questions (from `docs/TODO.md`):** badge MEL on the poster too, or FEL only? · final Plex label vocabulary (`DV FEL`/`DV MEL` vs `FEL`/`MEL`)? · ship the file tag in v1, default on/off? · re-rip detection beyond `(mtime,size)` — per-folder force-rescan enough, or add a content hash?

---

## 5. RISKS & OPEN QUESTIONS

1. **Seed paths are NAS Plex paths the container can't reach.** `\\TURTLELANDSRV2\4K HDR...\X.mkv` ≠ any container mount (`/library/...`). Because `dv_scan.path` is the container-view PRIMARY KEY, seed rows are write-once and **un-re-verifiable**: the container can never re-run `detect_layer()` on them. **Implication for labeling:** the Plex write-path cannot key off `dv_scan.path` for seed rows — it must match Plex items by **title (+ year)** via the Plex API. This is the core reason `dv_scan.year` is on the TODO and why path-only matching is insufficient.
2. **Keep-recommendation reads only filenames.** Both the rename keep-recommendation and the scan-results quality compare use parsed filename tokens / scraped release size, never the real file on disk. A corrupt/short grab still reads as "keep the 1080p I have," potentially suppressing a legitimately-wanted upgrade.
3. **Cache re-annotation timing.** Statuses (`DOWNLOADED_SIMILAR`, prior-grab) refresh only on a new grab or the 3-hourly background scan. Pre-existing rows and mid-session library changes show stale labels until then; there's no manual force-refresh. The one-time backfills so far have been `docker exec` invocations, not a product feature.
4. **Manual-backfill-vs-endpoint gap.** Both the DV seed import and the cache re-annotation backfill require terminal/`exec` access. Operators can't self-serve either from the UI — fine for the maintainer, blocks handoff/repeatability.
5. **`dovi_tool` accuracy unproven on this library.** The recipe is source-correct but never validated against the user's real FEL/MEL files; going live without a validation pass risks systematic misclassification.
6. **No resume for long DV scans.** `scan_folder_dv()` walks the whole tree; a crash mid-scan restarts (the `(mtime,size)` cache does skip already-done files on re-run, which softens this).
7. **Library guard not re-checked on `rematch()`/apply.** `rematch()` rebuilds the destination without re-verifying the target library is configured; `apply()` trusts the stored destination. Low risk (deliberate user action + on-apply warning), but a media-type flip into an unconfigured library can reach a `matched` state that fails on apply.

---

## 6. WHAT THE REVIEWER SHOULD CHECK / VERIFY

- **DV ingest hook:** Confirm `detect_layer()` truly has only the one runtime call site (inside `scan_folder_dv`). Grep `_process_file_inner` and `process_folder` to verify no DV call sneaks in.
- **Plex write-path absence:** Confirm there is genuinely no `addLabel`/`removeLabel`/`addLabels` anywhere in `plex_service.py` (only `_check_dovi()` read). If the label path is to be built, decide path-match vs title+year-match now.
- **Seed reality check:** Inspect a populated `dv_scan` and confirm whether any seed `path` resolves inside the container. Expectation: none do. Decide whether seed rows should be keyed by title+year instead of path, or get a translation map.
- **`dv_scan` schema:** Confirm no `year` column exists and that downstream matching will need one.
- **dovi_tool live accuracy:** Pick 2–3 known-FEL and 2–3 known-MEL files reachable by the container, run `detect_layer()`, and confirm the returned `layer` matches ground truth and the `Profile: N (FEL)/(MEL)` token shape v2.3.2 emits.
- **Config surface:** Confirm none of `dv_detection_enabled` / `dv_file_tagging_enabled` / `dv_library_paths` / `dv_fel_label` exist in `config.py`/`settings.py`/Settings UI — only `/health` capability reporting.
- **Kometa overlay:** Confirm no `dv_fel.yml` (or equivalent) overlay exists in the Kometa config volume.
- **`downloaded_similar` filter:** Confirm FilterBar has no tab for it even though the status renders.
- **Cache backfill:** Confirm there is no `/cache/rematch-all` endpoint or UI trigger — only grab-time and 3-hourly re-annotation.
- **Deploy discipline:** Confirm any change you test is delivered via `docker compose up -d --build` (not `docker restart`).
- **Concurrency sanity (if touching ingest):** `scan_folder_dv` already holds `_bulk_lock`; a new per-file DV hook in `_process_file_inner` must not deadlock against it or `_claim_path`.

---

## 7. RECOMMENDED NEXT STEPS (ordering)

1. **Validate `dovi_tool` accuracy** on real container-reachable FEL/MEL files. Nothing downstream is trustworthy until classification is proven. (Cheap, unblocks everything.)
2. **Add `dv_scan.year`** + decide the matching key (title+year for seed rows, path for live scans). This shapes both the seed fix and the Plex write-path.
3. **Fix the seed pipeline:** add path translation (or pivot seed rows to title+year keys) and wrap it in a real `/rename/dv-seed-import` endpoint so it's repeatable and self-serve.
4. **Build the Plex label write-path** in `plex_service.py` (`addLabel`/`removeLabel`, clear opposite label, write `rating_key` back to `dv_scan`), matching Plex items by title+year.
5. **Add the per-file DV detection hook** in `_process_file_inner()` behind **default-off `dv_detection_enabled`**, with the 4-place config plumbing. Mind the `_bulk_lock`/`_claim_path` interaction.
6. **Wire Kometa:** add `dv_fel.yml` overlay + schedule handoff so labels land before Kometa runs.
7. **Optional file tagging** (`mkvpropedit`) behind **default-off `dv_file_tagging_enabled`**, with post-tag `(mtime,size)` cache re-record.
8. **Scan-results polish:** add the `downloaded_similar` filter tab and an on-demand `/cache/rematch-all` endpoint + button.
9. **Later:** real-storage keep-recommendation audit; resume/checkpoint for long DV scans; re-check library guard on `rematch()`.
