# Adding Dolby Vision FEL/MEL Detection & Tagging to ScanHound

## Goal

When 4K movies enter the library — both newly-added files flowing through the rename pipeline and, retroactively, the thousands of files already sitting in established Plex folders — ScanHound should detect the **Dolby Vision enhancement-layer type** of each file (FEL vs MEL vs single-layer/none), record it durably, and surface a **"FEL" badge on the Plex poster** via a Kometa overlay. Optionally, the detection should also stamp the file's own metadata as a portable, self-describing record. The marquee outcome: a user browsing Plex can see at a glance which of their Profile 7 titles are the rare, full-fat **FEL** rips versus the functionally-HDR10 **MEL** ones — without ScanHound ever re-running an expensive scan on a file it has already classified.

This brief seeds a brainstorming + design session. The sections below lock down what is **known-correct** and **known-failed** (do not relitigate those), cite the concrete integration points already mapped in the codebase, and leave the genuinely open design decisions for discussion.

### Locked decisions (owner has ruled — do not relitigate)

1. **ScanHound owns its own DV database.** A dedicated `dv_scan` table is the single source of truth (resolves the DB-shape question in favor of **Option B**, below). The Plex label and the optional file tag are *derived outputs* of this table, not the system of record.
2. **A one-time seed import bootstraps the database — from TWO lists.** The owner has already scanned thousands of movies and will supply **a FEL list and a non-FEL list**. Build a seed-import path that loads both into `dv_scan` as trusted records (`source='seed'`):
   - FEL list → `dv_layer='fel'` (confirmed positive).
   - non-FEL list → `dv_layer='not_fel'` (a known-negative sentinel: confirmed NOT FEL by a prior scan, but finer layer — MEL vs P5 vs P8 vs none — not differentiated). This is primarily a **skip-list**: a retroactive `dovi_tool` sweep skips `not_fel` rows entirely, so detection effort goes only to genuinely-unknown files. A later "classify MEL precisely" pass can still refine `not_fel` → `mel`/`profile5`/`none` if wanted.
   - Live detection can confirm/override either (it writes `source='ingest'`/`'scan'`, which supersede `'seed'`).
   - (Exact line format TBD with owner — design the importer to accept newline-delimited entries; see the seeding bullet for path-vs-title matching.)
3. **Retroactive whole-library scans are MANUAL-ONLY.** Inline auto-detection runs **only** on freshly downloaded/ingested items flowing through the rename pipeline. Any broader sweep of an existing library folder is an explicit, user-initiated action in the program (a button / endpoint) — never automatic, never on a schedule.
4. **The self-describing file tag is the video track Name via `mkvpropedit`** (verified MediaInfo-visible; see "Self-Describing File Tag" below).

---

## ⚠️ The Known Pitfall — Read This First

**Inferring FEL from the SIZE of the enhancement-layer sub-track (via ffprobe / MediaInfo) does NOT work. This was already tried and abandoned. Do not propose it again.**

Why it fails, concretely:
- In Profile 7 Blu-ray remuxes the BL and EL are usually **interleaved into a single HEVC elementary stream** (EL NALs carry a different `nuh_layer_id`), so ffprobe/MediaInfo frequently report **one video track** — there is no sub-track size to measure.
- When a second track *does* exist, its byte count is dominated by muxer/encoder choices, not EL content. A MEL EL is **not zero bytes** (it still carries NAL/slice headers and RPU NALs per frame). **No stable size threshold separates MEL from FEL** across sources.
- MediaInfo reports profile/codec/`BL+EL+RPU` presence but **does not parse the RPU**, so it cannot and does not report MEL vs FEL. The `dvhe.07` codec string confirms dual-layer P7 but says **nothing** about FEL vs MEL.

**Detection MUST use stream/RPU-level analysis** using the verified recipe below.

---

## The Verified Detection Method (locked down)

FEL vs MEL is a property of the **RPU's enhancement-layer NLQ (non-linear quantizer) mapping data**, not the track size and not the container metadata. `dovi_tool` (by quietvoid) reads the RPU and resolves it to an authoritative `(FEL)` / `(MEL)` label. Mechanistically: `dovi_tool` computes `el_type` from `RpuDataNlq::is_mel()`, which is MEL iff the NLQ coefficients (`nlq_offset`, `vdr_in_max`, `linear_deadzone_*`, across all three components) are all zero/default; any nonzero coefficient ⇒ FEL. The NLQ block exists only for dual-layer profiles (7/4). **You do not parse these bits yourself — `dovi_tool` resolves them to the printed label.** (Note: `el_spatial_resampling_filter_flag` / `disable_residual_flag` are *profile*-detection flags, NOT the FEL/MEL discriminator — do not key off them.)

### The single most reliable recipe (use this)

Two stages, full pass, letting `dovi_tool` demux the container itself (this avoids ffmpeg silently dropping EL NALs and misreporting FEL as MEL):

```bash
# Stage 1: extract the RPU (full pass, no pixel decode, no re-encode)
dovi_tool extract-rpu "input.mkv" -o /tmp/rpu.bin

# Stage 2: read the FEL/MEL token from the summary
dovi_tool info -i /tmp/rpu.bin -s | grep -Eo 'Profile: [0-9.]+ \([^)]*\)|Profile: [0-9.]+'
```

The discriminator is the **parenthetical token on the `Profile:` line** of `dovi_tool info -s` output — e.g. `Profile: 7 (FEL)`, `Profile: 7 (MEL)`, or even `Profile: 7 (MEL, FEL)` for a mixed title. There is **no** `RPU EL type:` line and **no** `Enhancement layer type:` line — grep the parenthetical, exactly as the br3ndonland container does.

**Decision logic on the grep output:**
| Output | Classification |
|---|---|
| contains `(FEL)` (including `(MEL, FEL)`) | **P7 FEL** — the prize |
| contains `(MEL)` and not `FEL` | **P7 MEL** (≡ Profile 8.1; lossless to drop EL) |
| `Profile: 8` (no parenthetical) | single-layer DV (8.1/8.2/8.4; EL absent or stripped) |
| `Profile: 5` | single-layer DV (non-HDR10-compatible) |
| `extract-rpu` errors / "no RPU found" | **no Dolby Vision** (may still be HDR10/HDR10+ — out of scope) |

**Only Profile 7 (and the historically-rare Profile 4) can be FEL.** For Profile 5/8.x, the FEL/MEL question is moot — report "single-layer, no EL" and move on; treat the absence of a parenthetical as "single-layer," **not** as an error.

### Hard technical constraints (do not violate)

- **No single-call HEVC→FEL/MEL path exists.** `dovi_tool info` accepts only `-i <RPU.bin>` — not an HEVC stream, not stdin. Always do the two-stage `extract-rpu` → `info -i` dance. (Piping HEVC into `info` does not work — do not design around it.)
- **Performance: this is a NAL-walk + RPU parse, NOT a pixel decode and NOT a re-encode.** It is IO-bound, CPU-light, mostly single-threaded. A 4K P7 remux takes tens of seconds to a couple of minutes, dominated by disk read.
- **Do a FULL pass (or short-circuit on first FEL) — do NOT classify MEL from a truncated `-t 60` probe.** FEL is per-RPU; a title can be `(MEL, FEL)`, and a short window starting in flat/letterboxed content can show only MEL and miss FEL later. The safe speed optimization is *short-circuit the moment FEL appears*, not capping read time. Only conclude MEL after a complete pass with no FEL frames.
- **Prefer `dovi_tool extract-rpu input.mkv` (or `mkvextract tracks in.mkv N:raw.hevc | dovi_tool extract-rpu -`) over an ffmpeg pipe for any FEL candidate.** ffmpeg's HEVC handling can drop/reorder EL NALs on dual-layer muxes and flip a true FEL to a misdetected MEL/P8. Use the ffmpeg pipe only for clean single-track convenience cases; `hevc_mp4toannexb` is needed for MP4/MKV length-prefixed HEVC but NOT for `.m2ts`.
- **Trust the RPU, not the filename.** A scene release named "...FEL..." may have been converted to P8.1 (EL discarded) before muxing. The RPU is the source of truth; the filename lies.

### Container install (Dockerfile)

`dovi_tool` is a Rust binary **not in Debian apt**. Install the **prebuilt static musl** release binary (do NOT build from source — that drags in the entire Rust toolchain and bloats the image). Target file: `X:\Docker Apps\ScanHound\Dockerfile` (currently installs `ffmpeg`/`tesseract-ocr` via `apt-get` around lines 24–28).

- Use the `x86_64-unknown-linux-musl` asset (the host is Windows/x86_64 Docker), not `gnu` — the static binary has zero shared-lib surprises on `slim-bookworm`.
- **Pin a real release tag and verify the asset name** — the archive is frequently named version-*less* (`dovi_tool-x86_64-unknown-linux-musl.tar.gz`), so a hardcoded `dovi_tool-${VER}-...` URL will likely 404. Resolve via the release tag / `releases/latest/download/...` and confirm whether the tarball is flat (bare `dovi_tool` binary) before choosing the `tar -C /usr/local/bin dovi_tool` vs `tar … && mv` form.
- Sanity-check in the build: `dovi_tool --version`. `ffmpeg` is already present and already includes `hevc_mp4toannexb` + HEVC/TS demuxers — no special ffmpeg build needed. (`hdr10plus_tool` is NOT required for DV FEL/MEL.)

---

## ScanHound Integration Points (concrete file references)

### Detection core — new module
- **`X:\Docker Apps\ScanHound\backend\rename\dv_detect.py` (new).** Mirror the fail-safe `shutil.which` + `subprocess.run` style of `llm_identify.py`. Public surface: `detect_layer(path) -> {"dv_layer": "none|profile5|mel|fel", "dv_profile": int|None, "bl_present": bool, "el_present": bool, "rpu_present": bool}` plus a `dependency_status()` contribution for `dovi_tool`. A cheap ffprobe pre-filter (as already proven in `llm_identify._is_hdr`, `llm_identify.py:432`) can narrow candidates to HEVC/DV files; the FEL/MEL discrimination itself goes through `dovi_tool`.

### Per-file hook in the rename pipeline
- **`X:\Docker Apps\ScanHound\backend\rename\service.py`.** `RenameService._process_file_inner` (`service.py:771`) is the single funnel every file passes through (JD packages, manual folder runs, re-identify). It already shells to ffprobe for the runtime check around `service.py:846–915`. Add the `dv_detect.detect_layer(path)` call in that same region (DV layer is independent of TMDB match, so it can run unconditionally near `service.py:851`), and fold `dv_layer`/`dv_profile` into the job dict assembled at `service.py:971` and `job.update(...)` at `service.py:984–993`.
- **Keep DV scanning OUT of `_preview_folder` (`service.py:660`)** — preview is explicitly deterministic/fast-only (`service.py:699–700`). Adding a multi-second-per-file RPU walk there would make the dry-run path slow.

### Retroactive walker
- **`RenameService.process_folder` (`service.py:615`)**, exposed via `POST /rename/process-folder` (`rename.py:176`), already does the "point at an arbitrary folder, walk `_video_files` (`service.py:755`), translate host→container paths via `_translate_path` (`service.py:562`), skip already-tracked via `db.path_has_rename_job` (`service.py:643`), under `self._bulk_lock` (`service.py:636`)" loop. Two viable shapes (a design question, see below): **(a) piggyback** — a normal `process_folder` run records `dv_layer` into each row for free; or **(b) dedicated** `process_folder_dv(folder)` that walks files and calls *only* `detect_layer` (skipping TMDB/Ollama entirely for speed), writing to a dedicated table. Mirror the dry-run return-dict shape so the existing preview UI renders it.

### Database — DECIDED: dedicated `dv_scan` table (Option B is the source of truth)
- **`X:\Docker Apps\ScanHound\backend\database.py`.** Use the **additive column-migration** pattern (`_column_migrations` list at `database.py:399–411`, each `ALTER TABLE … ADD COLUMN` guarded by a duplicate-column try/except). Do **not** bump `SCHEMA_VERSION` (that gate, `database.py:421–431`, is for destructive/structural changes).
  - **Create the `dv_scan` table** (model on `background_scan_cache`, `database.py:333–344`):
    ```sql
    CREATE TABLE IF NOT EXISTS dv_scan (
        path        TEXT PRIMARY KEY,   -- container-view path (post _translate_path)
        title       TEXT,
        dv_layer    TEXT,               -- 'fel' | 'mel' | 'profile5' | 'none' | 'not_fel'(seed known-negative) | 'unknown'
        dv_profile  INTEGER,            -- 5 | 7 | 8 | NULL
        sig_mtime   REAL,               -- change-signal: file mtime at scan time
        sig_size    INTEGER,            -- change-signal: file size at scan time
        source      TEXT,               -- 'scan' | 'seed' | 'ingest'
        file_tagged INTEGER DEFAULT 0,  -- whether the mkvpropedit track-name marker was written
        rating_key  TEXT,               -- Plex item, captured once to skip future O(n) lookups
        imdb_id     TEXT,
        plex_labeled INTEGER DEFAULT 0, -- whether the Plex 'DV FEL'/'DV MEL' label has been applied
        scanned_at  TIMESTAMP,
        last_seen_at TIMESTAMP
    );
    ```
    Plus `upsert_dv_scan` / `get_dv_scan(path)` / `get_dv_scans(layer=...)` / cache-signature helpers. This table is the **system of record**; the Plex label and the file tag are derived from it. It holds files that have no rename job (the retroactive/seed case), which is exactly why a dedicated table beats columns on `rename_jobs`.
  - **Do NOT** also carry DV state on `rename_jobs` as a parallel source of truth. The live-ingest path may *write through* to `dv_scan` keyed by path, but `dv_scan` is canonical. (A read-only `dv_layer` shown on a rename job's row is a UI join, not a second store.)
  - Note `plex_cache` already carries a `dovi BOOLEAN` column (`database.py:230`, populated `database.py:516`); leave it as-is — `dv_scan` is the FEL/MEL authority.

### Plex tagging (write path)
- **`X:\Docker Apps\ScanHound\backend\plex_service.py` / `plex_manager.py`** already use python-plexapi (`plex_service.py:21`, `plex_manager.py:200,233`). Existing DV detection `PlexService._check_dovi` (`plex_service.py:539`) reads `DOVIPresent`/`doviProfile`/`doviBLPresent`/`doviELPresent` but returns only a bool — **extend it to report the layer** (profile 5 ⇒ `profile5`; profile 7 with EL ⇒ defer FEL/MEL to a file scan). **Plex's PMS metadata cannot reliably distinguish FEL from MEL** (it exposes profile + BL/EL-present but not RPU residual fullness), so Plex is good only for "profile 7 has an EL" — true FEL/MEL **requires the `dovi_tool` file scan**.
- File→item mapping is solid: `_extract_movie_data` (`plex_service.py:413`) captures `rating_key` (`:472`), `imdb_id` from `movie.guids` (`:457–461`), and `media.parts[0].file` (`:432`) is the on-disk path bridging a rename job's path to a Plex item.
- **Label-setting is NOT implemented yet** — there is no `addLabel`/`editTags` usage anywhere. Add a method to `PlexManager` (alongside `scan_library`, `plex_manager.py:512`): `fetchItem(rating_key)` → `item.addLabel("DV FEL")` / `removeLabel(...)`. Connection is already managed (`is_connected` `plex_manager.py:157`, `connect()` `:184`); reverse path translation exists via `PlexManager.translate_path` (`:437`).

### Health / dependency status
- **`llm_identify.dependency_status()` (`llm_identify.py:641`, currently `{ffmpeg, ffprobe, tesseract}` via `shutil.which`)** — add `"dovi_tool": bool(shutil.which("dovi_tool"))`.
- **`GET /rename/health` (`rename.py:146`)** builds `bins = llm_identify.dependency_status()` (`:153`) and a `capabilities` map (`:159–165`) — add `"dv_detection": bins.get("dovi_tool", False)` so the UI can show whether FEL/MEL discrimination is actually available. Frontend already consumes this via `api.renameHealth()` (`client.ts:340`).

### Config (four-place pattern)
Adding a setting touches four files, following the existing `auto_rename_*` threading:
1. **`config.py:18`** — `AppConfig` TypedDict (near the auto-rename block, `:116–129`): e.g. `dv_detection_enabled: bool`, `dv_library_paths: List[str]`, `dv_plex_tagging_enabled: bool`, `dv_fel_label: str` (default `"DV FEL"`).
2. **`config.py:289`** — `_DEFAULT_CONFIG` defaults (near `:355–367`).
3. **`settings.py:53`** — Pydantic `SettingsUpdate` (`extra="forbid"`, `:60` — **mandatory**, or undeclared keys 422). Add near the `auto_rename_*` fields (`:111–129`).
4. **`frontend\src\routes\settings\+page.svelte`** — the `rename` tab block (`:1381`); copy the toggle pattern at `:1394–1397` and the multiline-textarea pattern (`auto_rename_path_mappings`, `:1466`) for `dv_library_paths`. Either extend the `rename` tab or add a new `'dv'` tab.

### Frontend list / badge / progress
- **`frontend\src\routes\renames\+page.svelte`** job rows (`:290–358`) already render status/source badges (`:295–305`) — add a **DV-layer badge** keyed on `job.dv_layer` mirroring the `LLM` badge (`:296–298`): amber `FEL`, blue `MEL`, gray `P5`. A "FEL inventory" view can reuse the folder-preview list component (`:227–259`).
- **Types:** add `dv_layer?: string` / `dv_profile?: number` to `RenameJob` in `frontend\src\lib\api\types.ts`.
- **API client:** add `dvScanFolder` / `dvScanHealth` next to `renameProcessFolder` (`client.ts:335`) / `renameHealth` (`:340`).
- **WebSocket progress:** reuse the thread-safe `ws_manager.broadcast_sync` hub (`ws.py:68`) exactly as the rename pipeline does (`service.py:1221–1230` emits `rename:job`; `rename.py:192–212` emits `rename:folder_preview`/`folder_done`/`notification`). Emit `dv:job` per file and `dv:scan_done` + a `notification` at end — new event types are just new `type` strings, no protocol change.

---

## Tagging / Output Contract

The **entire handoff to Kometa is a Plex Label.** This is non-negotiable and dictates the whole design:

> **Plex does NOT import arbitrary MKV/container tags as anything Kometa can read.** Kometa targets Plex's own metadata model — Labels, collections. A custom `DV_EL_TYPE=FEL` MKV tag is invisible to Plex and therefore invisible to Kometa. The data contract MUST terminate in a Plex Label.

**The pipeline:**
```
ScanHound detects FEL  →  movie.addLabel("DV FEL")  via python-plexapi
                                      ↓
                         Kometa run reads labels live
                                      ↓
       overlay gated on  plex_search: { all: { label: DV FEL } }  stamps the poster
```

- **Label vocabulary (frozen):** `DV FEL`, `DV MEL`. **Mutually exclusive** — a file is FEL or MEL, never both. ScanHound owns writes; Kometa owns reads; nothing else writes these two. The write helper must clear the opposite label before adding (idempotent convergence: re-label only on change).
- **Kometa overlay** lives in the Kometa config volume (`config/overlays/dv_fel.yml`, referenced from the library's `overlay_files:` in `config.yml`). Minimal text overlay: `overlay: { name: text(FEL), … }` gated by `plex_search: { all: { label: DV FEL } }`; an independent `DV MEL` block in a different corner/grey plate. Kometa reads labels **fresh each run** (no Kometa-side label cache) and stamps its own reserved `Overlay` bookkeeping label — **ScanHound must never touch `Overlay`**, and once Kometa manages overlays, posters must not be hand-edited (double-stamping).
- **Optional self-describing file tag (portability/MediaInfo only, NOT a Kometa driver):** see the dedicated section below. It is purely so a human (or any tool) opening the file in MediaInfo sees "FEL"/"MEL". **Plex will not read it as a label** — never expect it to bridge to Kometa.
- **Seeding from the owner's two pre-existing lists (FEL + non-FEL):** see Locked Decision #2 for the layer mapping. Bootstrap converges three ways: (1) load both lists into `dv_scan` (`source='seed'`; FEL→`fel`, non-FEL→`not_fel`) so the retroactive sweep skips everything already classified and only scans true unknowns; (2) run a one-time bulk labeler that builds a `{normalized_path → Plex item}` index over `section.all()` once (O(n) acceptable for a one-shot), applies the `DV FEL` label to the FEL set, and captures each match's `rating_key`/`guid` into `dv_scan` so future updates skip the O(n) scan; (3) optionally write the MKV file tag to the FEL set. **Matching note:** if the lists are full file paths, key `dv_scan.path` directly (translate the host prefix to the container view first). If they're titles/folder names, fuzzy-match to Plex items / library files — less reliable, so full paths are strongly preferred. Plex stores paths as *Plex* sees them — translate the ScanHound prefix before comparing.

---

## Self-Describing File Tag (MediaInfo-visible) — VERIFIED METHOD

Optionally write a human-readable FEL/MEL marker **into the file** so anyone opening it in MediaInfo (or any container-metadata tool) sees the layer type — independent of Plex and of ScanHound's DB. This is the verified, adversarially-checked approach (do not substitute another):

- **Method: set the VIDEO TRACK NAME via `mkvpropedit` (MKV only).**
  ```bash
  mkvpropedit "input.mkv" --edit track:v1 --set name="Dolby Vision Profile 7 FEL"
  # MEL variant:
  mkvpropedit "input.mkv" --edit track:v1 --set name="Dolby Vision Profile 7 MEL"
  ```
  MediaInfo renders this in the **Video** section as the **`Title`** field, in the **default view**, directly adjacent to the `HDR format : Dolby Vision … BL+EL+RPU` line — which is exactly where MediaInfo itself shows only `BL+EL+RPU` and never the word FEL/MEL. That adjacency is the point.
- **Why NOT custom Matroska `\Tags`:** MediaInfo only surfaces a fixed allow-list of known tag names; an arbitrary `DV_EL_TYPE=FEL` SimpleTag typically shows **nothing** in MediaInfo's normal output. Do not use it as the primary marker. (If a General-section copy is wanted, *append* a bracket to the segment title — `--edit info --set "title=Dune (2021) [DV P7 FEL]"` — rather than use a `TITLE` tag, which collides version-dependently with the displayed Movie name.)
- **Non-destructive & fast:** `mkvpropedit` is a header-only edit (EBML Void-padded), sub-second regardless of file size — no remux, no playback/DV-render risk.
- **MKV-only is acceptable:** real Profile-7 FEL is almost always a MakeMKV `.mkv` remux. MP4 would require an ffmpeg stream-copy remux (not in-place) and `.m2ts` has no clean title slot — treat both as out-of-scope edge cases, classify-but-don't-tag.
- **⚠️ Cache/Plex interaction (must handle):** the write bumps **mtime** and *can* nudge **size** by a few bytes (so it would falsely invalidate a `(mtime, size)` cache and may trigger a Plex re-analyze). **Rule: tag the file FIRST, then record the post-tag `(mtime, size)` into `dv_scan.sig_mtime`/`sig_size` and set `file_tagged=1`.** That makes the marker part of the baseline so ScanHound never re-scans its own write. (mtime-restore via `os.utime` is a convenience but is not sufficient alone, since size can change.)
- **Tagging is opt-in** behind a config toggle (`dv_file_tagging_enabled`, default off) since it writes to library files in place — some users won't want their files touched.

---

## Retroactive Mode — MANUAL-ONLY (Locked Decision #3)

Point the utility at an established library folder and scan (and optionally tag) **in place**. These files already live in Plex and have no rename job — the `dv_scan` table is their home. **This whole mode is user-initiated only; it never runs automatically or on a schedule.** Auto-detection happens *only* inline on freshly downloaded/ingested items.

- **Trigger:** a dedicated endpoint + button (e.g. `POST /rename/dv-scan-folder` and a "Scan folder for Dolby Vision" action in the UI), distinct from the rename folder-processing button. Single-flighted under `_bulk_lock`.
- **Must be idempotent and cached.** Before scanning a file, check `dv_scan` for a matching `(path, sig_mtime, sig_size)`; skip unchanged files. RPU walks are expensive enough that re-scanning a settled library is unacceptable. (Honor `file_tagged=1` rows so a self-tagged file isn't re-scanned because the tag write changed its signature — see the file-tag section.)
- Reuse `process_folder`'s walk + host→container path translation, but route through `detect_layer` **only** (skip TMDB/Ollama) for speed.
- Emit WebSocket progress (`dv:job` / `dv:scan_done`) so a multi-thousand-file sweep shows live progress.
- On (re)detection, drive the **same `dv_scan` write + label/tag path** the live pipeline uses, so seed + retroactive + live ingest all converge on the one source of truth. A "force re-scan" option overrides the cache for a chosen folder.

---

## MUST NOT BREAK

- **Deploy ONLY via `docker compose up -d --build`.** The frontend is baked into the image and `docker restart` deploys nothing — the new `dovi_tool` binary and any frontend change require a rebuild.
- **The shared Ollama instance is untouched.** This feature adds no LLM load and must not alter Ollama config or usage. (DV detection is independent of TMDB/Ollama identification.)
- **Detection failures must be FAIL-SAFE.** A missing/erroring `dovi_tool`, an unreadable file, a malformed RPU, or any subprocess exception must **never crash the rename pipeline** — classify as `unknown`/`none`, log, and continue. The rename of a file must succeed even if DV detection throws. Mirror `llm_identify.py`'s defensive `shutil.which` + try/except style.
- **Do NOT reintroduce the size-heuristic approach.** Container/track-size inference is abandoned and wrong (see The Known Pitfall). Detection is RPU-level via `dovi_tool`, full stop.
- **Do not let DV scanning slow the deterministic preview path** (`_preview_folder`) or block JD-driven ingest. Keep the RPU walk off the fast/preview lanes.

---

## Open Design Questions (for brainstorming to resolve)

*(Four prior questions are now settled by the Locked Decisions: file-tagging method = MKV track name [#4]; DB shape = dedicated `dv_scan` table [#1]; retroactive = manual-only [#3]; seed import from the owner's list [#2]. The remainder stay open.)*

1. **MEL: tag it too, or only FEL?** Do we write a `DV MEL` label + overlay at all, or treat MEL as uninteresting (≡ P8.1) and only badge the prize FEL files? Affects label vocabulary and overlay count. (Note: the *file tag* and `dv_scan` row should record MEL regardless — this question is specifically about the Plex label/overlay.)
2. **Label naming.** `DV FEL` / `DV MEL` as proposed — or `FEL`/`MEL`, or richer (`DV P7 FEL`)? Frozen vocabulary, so settle it before seeding. (Independent of the file-tag track-name string, which is already fixed to `Dolby Vision Profile 7 FEL`/`MEL`.)
3. **Whether to ALSO embed the file tag, and default-on vs default-off.** The method is decided (MKV track name); open is whether to ship it at all in v1 and whether `dv_file_tagging_enabled` defaults off (it touches library files in place). The DB is the source of record regardless.
4. **Ingest aggressiveness on the live path.** Inline detection runs only on freshly ingested items (manual is the only library-wide path), but should even that inline RPU walk run synchronously during rename (adding tens of seconds to a 4K add) or be queued to a small worker so ingest stays snappy? (Retroactive is already off the hot path by Decision #3.)
5. **Re-rip / change-detection edge.** `(mtime, size)` is the change-signal; how aggressively do we trust it for a file re-ripped in place (same path, new content, plausibly similar size)? Offer a per-folder "force re-scan" (already planned) — is that sufficient, or do we want an optional partial content hash for the paranoid case?
6. **Reconciling with Kometa's schedule.** Detection must finish **before** Kometa's run or a new FEL badge slips to the *next* cycle (Kometa snapshots labels at run start). Chain them (`scanhound --label-new && kometa --run`), stagger schedules, or have ScanHound trigger a targeted `kometa --run` on any new label? Chaining is the deterministic, no-race option.
7. **`dovi_tool` version pinning & label-string drift.** Pin which release, and where do we verify the exact `(FEL)`/`(MEL)` summary string once in-container (since wording has drifted across versions) so the grep stays correct?
8. **Profile 8 "EL was stripped" nuance.** A disc that was P7 FEL but ripped to P8.1 reports `Profile: 8` — do we surface "single-layer (EL stripped)" distinctly from native P8, or collapse all P8 into one bucket?
