# ScanHound — Dolby Vision FEL/MEL Host-Side Detection + Plex Labeling: Design Spec

> **Revision note (post adversarial bug review):** the cross-process DB design was changed from "host writes `crawler.db` directly" to **host writes its own store + container imports** (only the container ever opens `crawler.db`); the label reconciler was changed from a `"DV "` prefix wildcard to a **closed managed label set** (it was deleting user labels); path normalization no longer builds on the dead `PlexManager.translate_path` hook; inventory counts now exclude the seed; media/part resolution iterates all parts; the mtime tolerance and host↔container stat skew are addressed; and config delivery to the host is pinned to a bind-mounted export file. See §12 for the residual risks.

## 1. Overview & Goal

Host-side Dolby Vision layer detection feeding in-app Plex labeling, so **Kometa renders per-copy DV badges** (`DV FEL`, `DV MEL`, `DV P8`, `DV P5`) on movie posters.

ScanHound already has a DV detector (`backend/rename/dv_detect.py`) and a `dv_scan` table, but the existing in-app scanner reaches only container-mounted paths, and there is **no Plex write-path at all** (verified: zero `addLabel`/`removeLabel` in the codebase). This closes both gaps: (a) get authoritative FEL/MEL classification for files that live on a second machine reachable only over SMB, and (b) turn that classification into Plex labels **on the exact copy Plex serves**, then let Kometa badge it. Matching by exact served path means a title with multiple copies is labeled by the specific copy Plex chose. Non-DV titles get no label.

**Label text:** `DV FEL`, `DV MEL`, `DV P8`, `DV P5` — a **closed, reserved set** managed exclusively by ScanHound (see §7.5).

## 2. Topology & Host/Container Split

| Machine | Role | Media |
|---|---|---|
| **TurtleLandSRVR = 192.168.1.170** | Docker host; runs Plex (native Windows); runs the container; runs the **host detector** | Many **local** drives A–M, Q–Z |
| **TURTLELANDSRV2 = 192.168.1.180** | Second media machine | Media over **SMB**, mounted on .170 as `Y:`/`P:`/`4K Magellan`/… |

- **Detection is host-side** because FEL vs MEL requires `dovi_tool` to read the full RPU stream, and the container **cannot** reach the `.180` SMB media (Docker Desktop rejects mapped-drive/UNC bind-mounts — verified; a CIFS volume would bake `.180` credentials into an internet-exposed container). The `.170` host already reaches both its local drives and `.180` over SMB with the host's existing auth — no new credentials anywhere.
- **Labeling is in-container** because the app already reaches Plex via `host.docker.internal:32400` and has the `python-plexapi` client.
- **They do NOT share `crawler.db`.** (Corrected from the original design after the review found a live `crawler.db.corrupt.*` backup on disk and confirmed `DatabaseManager.__init__` runs DDL on every construction.) Instead:
  - The host detector writes to its **own store** on the host FS: `<repo>\host-detector\dv_host.db` (a standalone SQLite the host owns) — never `crawler.db`.
  - A container endpoint **`POST /rename/dv-import`** ingests the host store into `dv_scan` (`source='scan'`). Only the container ever opens `crawler.db` and is its **sole schema owner**.

```
.180 media (SMB) + .170 local drives
        │
        ▼
HOST DETECTOR (.170): dovi_tool.exe + walker + mkvpropedit
        │  writes rows to its OWN store  ── host-detector\dv_host.db
        │  (host-native paths; NEVER opens crawler.db)
        ▼
   (POST /rename/dv-import  — reads dv_host.db, upserts dv_scan source='scan')
        │
        ▼
crawler.db (container is SOLE owner; PK=path)
        │  read by
        ▼
SCANHOUND CONTAINER: labeler → Plex addLabel/removeLabel (managed set only)
        │
        ▼
Kometa (separate container): label-gated overlay → badge
```

## 3. Current State (Code-Grounded) & Gaps

### 3.1 `dv_detect.py` (present, reusable, correct on Windows)
`detect_layer(path) -> {"layer", "tool", "error"}`; `layer` ∈ `fel/mel/profile5/profile8/none/unknown`; fully fail-safe (never raises). Recipe: `dovi_tool extract-rpu "<path>" -o <tmp.rpu.bin>` (`timeout=1800`, temp deleted in `finally`) → `dovi_tool info -i <rpu.bin> -s` (`timeout=120`). `_classify`: FEL parenthetical→`fel`; MEL→`mel`; profile 5→`profile5`; profile 7 no-token→`mel`; profile 8→`profile8`; `(MEL, FEL)`→`fel`; else `none`; stage-2 parse error→`unknown`.
**Windows-safe:** `detect_layer` resolves the binary with `shutil.which("dovi_tool")` and calls `subprocess.run([resolved_path, ...])` — `shutil.which` honors `PATHEXT`, so `dovi_tool.exe` resolves when on `PATH`. The host detector reuses this module verbatim. *(Caveat: a Windows Task Scheduler action runs with a stripped environment — the scheduled task must set `dovi_tool.exe`'s directory on `PATH` in the action itself, not rely on the interactive user `PATH`.)*
**Binaries are split:** the image's `dovi_tool`/`mkvpropedit` are Linux; the host needs its own `dovi_tool.exe` + `mkvpropedit.exe` (quietvoid v2.3.2 to match).

### 3.2 `dv_scan` table (present, correct shape) — `database.py:391-403`
PK `path`; cols `title, dv_layer, sig_mtime, sig_size, source, rating_key, imdb_id, scanned_at, last_seen_at`. **No `year`.** Helpers: `upsert_dv_scan`, `get_dv_scan`, `get_dv_scans`, `count_dv_scans_by_layer`, `dv_scan_is_current`, `clear_dv_scans`. **Note (bug found):** `count_dv_scans_by_layer` (`database.py:1322-1326`) has **no `source` filter** — see §6/§7.6 for the fix so the panel doesn't count seed rows.

### 3.3 `scan_folder_dv` — `service.py:906-977`
Container-only reach (`_translate_path`). Retained for container-mounted paths but **not** the authoritative source; the host detector is.

### 3.4 `plex_service` / `PlexManager` (read-only today)
- python-plexapi ≥4.13; `PlexServer(url, token)` via `host.docker.internal:32400`.
- `load_libraries` bulk-fetches `lib.all()` per movie section and iterates in-process (`plex_service.py:236-266`) — **full movie objects already in memory** (relevant to §7/§9 perf).
- `_extract_movie_data` (`plex_service.py:431-475`) already loops `for media in movie.media` and guards `part = media.parts[0] if media.parts else None`; it just doesn't store `part.file`.
- **No label read/write exists** (verified). `_check_dovi` is read-only.
- **`PlexManager.translate_path()` is effectively dead** — `add_path_mapping` is never called at runtime, so `_path_mappings` is empty, and `PathMapping.translate` is a bare `str.replace` (no case/separator handling). **Do NOT build normalization on it** (corrected from the original spec). The rename pipeline's own `service._translate_path` (config `auto_rename_path_mappings`) is a separate mechanism.

**Gaps this feature fills:** (1) host detector reaching `.180`; (2) capture `part.file`; (3) `add_label`/`remove_label`; (4) a dedicated `normalize_path`; (5) `POST /rename/dv-import` + `POST /rename/dv-sync-labels` + DV-panel UI; (6) DV settings + host-config export; (7) a Kometa overlay asset (external).

## 4. Architecture & Data Flow

`host detector → dv_host.db → POST /rename/dv-import → dv_scan(source='scan') → labeler → Plex → Kometa`.

**Component boundaries:** the host detector knows only files + its own `dv_host.db`; the import endpoint is the only writer of scan rows into `crawler.db`; the labeler knows only `dv_scan` + Plex; Kometa knows only Plex labels.

**Import run** (`POST /rename/dv-import`): reads `dv_host.db` (path→layer+signature rows), and for each row `upsert_dv_scan(path, layer, sig_mtime, sig_size, source='scan')` inside the container (the sole `crawler.db` owner). Idempotent; safe to call after every host run. Returns `{imported, updated}`.

**Sync run** (`POST /rename/dv-sync-labels {dry_run?}`): daemon thread (mirrors `dv-scan-folder`); build an in-memory `{normalized_path → dv_layer}` index from `dv_scan` where `source='scan'`; enumerate movies via the existing bulk `lib.all()` (objects already carry `media/parts`); for each movie resolve its served file path(s) **from the already-fetched object** (no per-movie `fetchItem`), normalize, look up, reconcile the managed label. Wrap each title in try/except; throttle between Plex writes; **always** broadcast `dv:sync_done` in a `finally` (so the UI never sticks). Back-write `rating_key` into `dv_scan` for matched rows.

## 5. Host Detector Component

**Artifacts (host, `.170`, `<repo>\host-detector\`):** `dovi_tool.exe`, `mkvpropedit.exe`, `dv_host_scan.py`, `dv_host.db` (created by the script), + a Task Scheduler task.

**Own store, never `crawler.db`.** The script opens **its own** SQLite `dv_host.db` (schema: `path TEXT PK, dv_layer TEXT, sig_mtime REAL, sig_size INTEGER, title TEXT, scanned_at`). It does **not** import ScanHound's `DatabaseManager` (whose `__init__`→`init_db` runs DDL/`DROP TABLE`/`user_version` writes — a second DDL-running process is what corrupted the DB before). It reuses only `dv_detect.detect_layer` for classification.

**Config delivery (pinned).** The container writes a small **`<repo>\data\dv_host.json`** on every settings save containing `{dv_library_roots, dv_detection, dv_file_tagging, dv_label_vocab}`. The host script reads **that** file (plain JSON at a fixed bind-mounted path) — it does **not** import `config.py` (whose Windows `%APPDATA%` resolution points at a different file than the container's `/data/.config`). If `dv_detection` is false or the file/roots are empty, the script logs and exits (no silent scan-of-nothing).

**The walk.** For each root in `dv_library_roots` (host-native, e.g. `Y:\Movies`, `E:\4K`, `\\TURTLELANDSRV2\Share\...`): recurse; consider `dv_detect._SUPPORTED_EXTS`; `st = os.stat(path)`.

**Signature-skip.** Skip when `dv_host.db` has a current signature: `abs(stored_mtime - st.st_mtime) <= DV_MTIME_TOL AND stored_size == st.st_size`, where **`DV_MTIME_TOL` defaults to 2.0s** (raised from 1.0s — the original was *below* FAT/exFAT's 2s granularity, causing endless re-scans) and is configurable. RPU extraction is 1–7 min/file over SMB, so skipping unchanged files is the dominant cost saver.

**Classify + write:** `layer = detect_layer(path)["layer"]`; upsert into `dv_host.db` with the signature. On `unknown`, write `sig_mtime=NULL` so the next run retries.

**File self-tag (MKV, gated on `dv_file_tagging`).** `mkvpropedit.exe "<file>" --edit track:v1 --set name="Dolby Vision Profile 7 FEL"` (map: `fel/mel/profile8/profile5` → the corresponding string; `none/unknown` → no tag). **After a successful tag write**, re-`os.stat()` and re-upsert the same layer with the *post-tag* signature (the header rewrite bumps mtime/size — without this the next run needlessly re-scans). Tagging happens **before** the container sync (§7).

**Scheduling.** Task Scheduler nightly + on demand; the action ends by calling `POST /rename/dv-import` (via `curl`/`Invoke-WebRequest` to the app) so scan results reach `dv_scan` right after the walk. It is a host artifact, **not** in `docker build`.

## 6. `dv_scan` Schema & Counts

- PK `path`; `dv_layer` ∈ fel/mel/profile5/profile8/none(+unknown); no `year`; path-only matching; `source='scan'` supersedes `source='seed'` on write.
- **Inventory counts exclude the seed (bug fix).** `count_dv_scans_by_layer` gains a `source` filter (default: `source='scan'` only), and `GET /rename/dv-scans` likewise, so the DV panel shows real detected counts — not the ~3729 dead seed rows (862 fel / 581 mel / 2286 unknown) that would otherwise dominate the numbers forever. The seed is genuinely ignored for **both** labeling (its dead paths never match) **and** counts.

## 7. In-App Plex Labeler

### 7.1 `plex_service` label methods (new)
```python
def add_label(self, rating_key, label): self._server.fetchItem(int(rating_key)).addLabel(label)
def remove_label(self, rating_key, label): self._server.fetchItem(int(rating_key)).removeLabel(label)
```
Verified available (python-plexapi `TagMixin`). `rating_key` is stored TEXT; `int()` round-trips cleanly.

### 7.2 Served file path — capture + resolve ALL parts
Add `'file': part.file if part else None` in `_extract_movie_data`, but **iterate every `(media, part)`** (a title can have multiple `Media`/`Part`s — editions, optimized versions, split files — and `media[0]` is not necessarily the served copy). The sync builds a per-movie list of candidate served paths from the already-fetched object, guarding empty `media`/`parts` and `None` `file` (the existing extractor already guards these; do **not** hard-index `media[0].parts[0]`).

### 7.3 Enumerate movies
Reuse the bulk `lib.all()` per `config["movie_libs"]` section; dedupe on `ratingKey`. Objects already carry `media/parts` — **no per-movie `fetchItem` for path resolution** (that would be N extra HTTP round-trips). Reserve `fetchItem` for the O(1) `rating_key` back-write only.

### 7.4 Path normalization (the headline risk — dedicated helper)
Write a standalone, unit-tested `normalize_path(p) -> str` (NOT built on the dead `translate_path`): (1) `\`→`/`; (2) casefold; (3) apply a **validated drive↔UNC mapping table** (longest-prefix-first) equating each mapped-drive root with its UNC share root; (4) trim trailing slashes/dots/spaces, collapse dup separators. Normalize `dv_scan.path` into an in-memory index and normalize each Plex `part.file` before lookup.
**Mandatory de-risk gate:** during implementation, run a **dry-run that logs real Plex `part.file` values** for `.170`-local and `.180`-SMB sample titles, diff against `dv_scan.path`, and codify the exact drive↔UNC/case pairs from observed reality. The mapping table must make **all** sample titles resolve before the feature is allowed to write. Guard against two different physical files normalizing to the same string (would mislabel) — validate the drive→UNC roots point at the same storage.

### 7.5 Match & reconcile — **closed managed set only**
`MANAGED = {"DV FEL","DV MEL","DV P8","DV P5"}` (from `dv_label_vocab`). Per movie:
1. For each candidate served path: `normalize` → look up the index.
2. Desired label from the matched `dv_layer` via the vocab; if no match or layer ∈ {`none`,`unknown`,`NULL`} → desired = **none**.
3. **Multi-copy tie-break:** if a movie's parts resolve to different layers, prefer the layer of the copy Plex is most likely to serve (highest-resolution / first enabled Media); if still ambiguous, prefer the "best" layer by rank `fel > mel > profile8 > profile5` and log the ambiguity.
4. Reconcile **only within `MANAGED`**: `existing_managed = set(movie.labels) ∩ MANAGED`. If `desired` and `desired ∉ existing_managed` → `add_label(desired)`. For each `stale ∈ existing_managed \ {desired}` → `remove_label(stale)`. **Never** touch a label outside `MANAGED` (this is the fix for the data-loss bug — a `"DV "`-prefix wildcard would delete user labels like `DV Cut`). Idempotent.
5. On match, back-write `rating_key` if empty.

### 7.6 Endpoints + DV panel
- `POST /rename/dv-import` (§4) — ingest `dv_host.db` → `dv_scan`.
- `POST /rename/dv-sync-labels {dry_run?}` — daemon thread; per-title try/except; throttle; broadcast `dv:sync_progress` / `dv:sync_done`; **`dv:sync_done` emitted in `finally`** so the UI unlocks even on mid-sync Plex failure.
- `GET /rename/dv-scans` — inventory (scan-source only).
- **DV panel:** inventory counts (scan-source), last-scan status, a **`Sync Plex labels`** button (disabled while `$dvSyncRunning`, cleared on `dv:sync_done`/error), and added/removed/unmatched result summary. Stores `dvSyncRunning`/`dvSyncProgress`/`dvSyncResult` mirror the existing scan stores.

## 8. Kometa Overlay
Kometa runs elsewhere (not in this repo). Deliverable = a label-gated `dv_badges.yml` (one block per `MANAGED` label) the user drops into their Kometa config. Ordering is user-owned: **host detector (walk + tag) → `dv-import` → `dv-sync-labels` → Kometa**. The managed label strings must equal the overlay gate labels exactly.

## 9. Config / Settings
Verified **4-place add-a-setting pattern**: (1) `config.py` `AppConfig` + `_DEFAULT_CONFIG`; (2) `api/routes/settings.py` `SettingsUpdate` (has `extra="forbid"` → mandatory, or the save 422s — **also add the already-missing `auto_rename_movie_library_4k` while here**); (3) `types.ts` `Settings`; (4) Settings UI.

New keys: `dv_library_roots` (host-native; newline/`;`-separated; **not** `_translate_path`'d), `dv_detection` (bool, default false), `dv_file_tagging` (bool, default false), `dv_label_vocab` (JSON layer→label; must match the Kometa gate labels). **On settings save, the container writes `<repo>\data\dv_host.json`** with these keys, which the host detector reads (§5).

## 10. Edge Cases & Safety
- **Single `crawler.db` owner.** Only the container opens `crawler.db`; the host owns `dv_host.db`; the import endpoint bridges them. Eliminates the DDL-on-shared-DB + cross-boundary-WAL corruption risk.
- **Managed-label reconciliation** never touches labels outside the closed set → no user-label destruction.
- **Multiple copies / multi-part** — iterate all parts, tie-break defined, guard None/empty (§7.2/7.5).
- **Unmatched path → no label (fail-safe) + log** for normalization tuning; a stale *managed* label on an unmatched movie is still removed.
- **mtime:** `DV_MTIME_TOL≥2.0s`. Host↔container `stat` skew is avoided because scan signatures now live in `dv_host.db` (host `stat`) and are imported as-is; the labeler never re-`stat`s. The container's own `scan_folder_dv` (container `stat`) targets disjoint container-mounted paths, so no same-file host/container signature clash.
- **File-tag before sync:** the host completes tagging (and its post-tag re-upsert) before `dv-import`/sync; the sync skips titles whose `last_seen_at` changed within the last N seconds to avoid reading a title mid-Plex-refresh.
- **`dovi_tool.exe` absent / Task-Scheduler PATH** → `detect_layer` `tool=False`; the host script logs and writes nothing.
- **Perf/robustness:** bulk `lib.all()` (not per-movie `fetchItem`), inter-write throttle, per-title try/except, `finally`-guaranteed `dv:sync_done`.
- **Seed** ignored for labeling (dead paths) **and** counts (source filter).

## 11. Testing Strategy
- **Detector/parse + skip:** `_classify` over captured `info -s` summaries; `dv_host.db` signature-skip incl. the 2.0s tolerance boundary and `NULL`→re-scan.
- **Import:** `dv-import` upserts `dv_host.db` rows into `dv_scan` with `source='scan'`; idempotent re-import is a no-op; a `source='seed'` row for the same path is overwritten.
- **Normalization + match:** table-driven drive↔UNC/case/separator variants resolve to one row; unmatched → None → no label; two-different-files-same-normalized guard.
- **Labeler reconciliation (mock `Movie`):** no-label+fel→one add; `DV MEL`+fel→add `DV FEL`+remove `DV MEL`; correct→zero writes (idempotent); unmatched/none+managed→remove only; **a non-managed `"DV Cut"` label is NEVER removed**; multi-part different-layer → tie-break; `dry_run`→zero writes; `rating_key` back-write.
- **Counts:** `count_dv_scans_by_layer(source='scan')` excludes seed rows.
- **File-tag + re-upsert:** post-`mkvpropedit` signature stored so next run skips.
- **Robustness:** Plex drops mid-sync → daemon still emits `dv:sync_done` (UI unlocks); one bad title doesn't abort the batch.
- **End-to-end acceptance (gate):** set roots + enable → host writes `dv_host.db` row → `dv-import` → `dv-sync-labels` → assert exactly one `DV FEL` label on the target movie in Plex. Without this the feature can pass units and still label nothing.

## 12. Open Questions / Risks
1. **PATH NORMALIZATION (headline).** Detector paths vs Plex `part.file` (drive↔UNC, case, separators, and the *actual* letter Plex stored). Mitigation is a hard gate: sample real Plex paths, codify the map, require all sample titles to resolve before writing. A miss → silent label drop (fail-safe); a bad drive→UNC equivalence → mislabel — validate roots point at the same storage.
2. **Plex `part.file` obtainability — RESOLVED** (verified available; iterate all parts, don't index `[0][0]`).
3. **Cross-process DB corruption — RESOLVED** by the single-owner design (host `dv_host.db` + `dv-import`); the container is the only `crawler.db` writer/schema-owner.
4. **Multi-copy tie-break heuristic** (§7.5 step 3) — the "which copy does Plex serve" ordering is heuristic; confirm against real multi-edition titles during the dry-run sampling.
5. **Kometa scheduling handoff** — ordering is user-owned; a mis-ordered Kometa run overlays stale labels until the next pass. Documented, not enforced.
6. **dovi_tool version parity** — host `.exe` = image v2.3.2 so classification matches.
