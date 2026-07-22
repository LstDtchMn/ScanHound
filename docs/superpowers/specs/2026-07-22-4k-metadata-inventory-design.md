# Authoritative 4K Metadata Inventory and Full-Scan Design

**Date:** 2026-07-22
**Status:** Approved direction; written-spec review pending
**Depends on:**

- `2026-07-12-plex-library-metadata-scan-design.md`
- `2026-07-12-audio-hdr-metadata-design.md`
- `2026-06-30-dv-fel-mel-labeling-design.md`
- `docs/feature-pack-review/RENAMING_PIPELINE_AND_4K_METADATA_AUDIT_2026-07-22.md`

## 1. Goal

Build a durable, searchable, evidence-bearing inventory of every movie in the
configured Plex 4K libraries before running one controlled full-library scan.
The inventory must distinguish historical seed information from current local
file analysis; classify Dolby Vision FEL/MEL/Profile 5/Profile 8 and HDR10+
without turning missing evidence into a negative; expose every unscanned,
stale, failed, unsupported, missing, or cancelled file; and support a reviewed
Plex-label/Kometa reconciliation after the scan.

The intended result is one trustworthy scan of the current library rather than
an early scan followed by a second pass after bookkeeping and HDR10+ detection
are corrected.

## 2. Frozen safety boundaries

- Auto-rename, general auto-grab, and RSS auto-grab remain disabled throughout
  implementation, pilot, and full scan.
- The metadata scanner opens media read-only. It never moves, renames, tags,
  modifies, or deletes a media file.
- `dv_file_tagging` remains disabled. Enabling it is outside this project.
- Scan analysis writes only to ScanHound's SQLite database. Plex label writes
  are a separate, explicit reconciliation stage after result review.
- Kometa is not invoked until the Plex-label dry run and reconciliation report
  are accepted.
- Initial scan concurrency is one file. A later maximum of two may be enabled
  only after pilot evidence shows storage latency and error rates remain safe.
- A full scan cannot start until the pilot gate in section 12 passes.
- No seed classification is destroyed or silently replaced by a live result.
- A failed tool invocation remains retryable and cannot be stored as a proven
  negative.

## 3. System architecture

```text
Plex 4K inventory
        |
        v
durable scan run + expected-file manifest
        |
        v
read-only local analysis
        |-- ffprobe: container/video/audio/subtitle/mastering facts
        |-- hdr10plus_tool: full-stream HDR10+ evidence where required
        `-- dovi_tool: DV profile and FEL/MEL enhancement layer
        |
        v
current media inventory + immutable per-run attempt evidence
        |
        |-- seed-versus-live discrepancy report
        |-- search/filter/export API and UI
        `-- controlled Plex managed-label dry run
                                      |
                                      v
                              Plex reconciliation
                                      |
                                      v
                               Kometa overlays
```

The existing `media_probe` and `dv_scan` tables remain compatibility caches for
rename/conflict code. New tables own run history, seed provenance, and the
searchable current projection. The scanner updates the compatibility caches and
the new inventory in one application-level result transaction.

## 4. Persistent data model

### 4.1 `dv_seed_baseline`

Preserves the owner's original 3,729-row classification independently from
live analysis.

| Column | Purpose |
|---|---|
| `path` TEXT PRIMARY KEY | Original imported path |
| `normalized_path` TEXT NOT NULL | Search/match identity |
| `title` TEXT | Original parsed title |
| `seed_layer` TEXT NOT NULL | `fel`, `mel`, `profile5`, `profile8`, `none`, or `unknown` |
| `source_hash` TEXT | Hash of the source export/JSON used for the import |
| `imported_at` TEXT NOT NULL | Provenance timestamp |

Migration copies all current `dv_scan.source='seed'` rows; the production audit
proves that all 3,729 imported seed rows are still present, so this is the
required baseline source and does not depend on a file in the Git checkout. If
the original `/data/dv_seed.json` is still present on the production data volume,
a read-only reconciliation command hashes it and reports any row missing from
the database baseline. That optional cross-check never writes `source='scan'`
and never changes live classifications.

### 4.2 `metadata_scan_runs`

One row per pilot, selected, full, resume, or retry-failures operation.

Required fields:

- `id`, `run_uuid`, `scope`, optional `library_filter`;
- `status`: `queued`, `running`, `paused`, `cancelled`, `interrupted`,
  `completed`, or `completed_with_failures`;
- `expected_count`, `attempted_count`, `current_count`, `failed_count`,
  `unsupported_count`, `missing_count`, `cancelled_count`;
- `started_at`, `completed_at`, `heartbeat_at`;
- exact code SHA and image digest;
- ffprobe, dovi_tool, and hdr10plus_tool versions;
- a redacted configuration hash.

Startup changes any abandoned `running` row with an expired heartbeat to
`interrupted`. Resume continues the same manifest and never redoes an item whose
stored file signature and detector versions remain current.

### 4.3 `metadata_scan_items`

Immutable per-run expected-file manifest plus latest attempt state for that run.
The unique key is `(run_id, path)`.

Required fields:

- Plex identity: rating key, library, title, year, original path, normalized path;
- expected signature: mtime and size captured when the manifest is built;
- `status`: `pending`, `running`, `current`, `failed`, `unsupported`, `missing`,
  or `cancelled`;
- `stage`: `stat`, `ffprobe`, `hdr10plus`, `dovi`, `persist`, or `complete`;
- attempt count and first/last attempt timestamps;
- stable failure code and sanitized failure detail;
- resulting signature, metadata JSON, live DV layer, seed layer, and discrepancy;
- detector methods and versions.

Every manifest item must end in a terminal state. A run cannot be marked
completed while any item remains `pending` or `running`.

### 4.4 `media_inventory`

Materialized current projection optimized for user search. Primary key is path;
rating key is indexed but not assumed unique because Plex can expose multiple
parts/copies.

Searchable columns include:

- Plex/library identity: rating key, title, normalized title, year, library,
  path, normalized path;
- currentness: scan state, signature, last attempt, last success, last run UUID,
  failure code;
- file facts: size, container, duration, bitrate, resolution, width, height,
  video codec/profile/level, bit depth, chroma, frame rate, aspect ratio;
- color/HDR facts: color primaries, transfer, matrix, HDR format, HDR10+ state,
  HDR10+ method, mastering display, MaxCLL, MaxFALL;
- DV facts: profile, compatibility ID, enhancement layer, detector method;
- audio summary and structured all-track JSON;
- subtitle summary and structured all-track JSON;
- seed layer, live layer, and discrepancy state;
- tool versions and a complete evidence JSON for fields not promoted to columns.

Normal indexes cover library, scan state, resolution, HDR format, HDR10+ state,
DV layer, DV profile, discrepancy, and last success. SQLite FTS5 indexes title,
path, and library for text search.

## 5. Metadata evidence rules

### 5.1 General probe

One structured ffprobe call reads the container and all video, audio, and
subtitle streams. The implementation no longer assumes the first audio stream
is the complete audio inventory. It records:

- container, duration, size, and overall bitrate;
- primary video codec/profile/level, dimensions, frame rate, aspect ratio,
  bit depth, pixel/chroma format, color primaries/transfer/matrix;
- mastering-display metadata, MaxCLL, and MaxFALL where present;
- every audio track's codec/profile, channel layout, language, title, Atmos or
  DTS:X evidence;
- every subtitle track's codec, language, forced/default flags, and SDH/title
  evidence.

The existing compact `probe_specs()` result remains backward compatible for
rename comparison consumers. A new detailed probe object supplies the inventory.

### 5.2 HDR10+

HDR10+ is a three-state fact: `present`, `absent`, or `unknown`.

1. If ffprobe exposes SMPTE ST 2094-40 dynamic metadata, record `present` with
   method `ffprobe_frame`.
2. A first-frame miss is never a negative.
3. For a PQ/HEVC stream without a quick positive, run pinned
   `hdr10plus_tool` full-stream extraction. A successful extraction with dynamic
   metadata records `present`; a successful complete parse with none records
   `absent`; tool failure, timeout, unsupported container/codec, or interrupted
   parsing records `unknown` with a stable error code.
4. Plain HDR10 is recorded only after authoritative HDR10+ absence, otherwise
   the format remains `HDR10 (HDR10+ unknown)`.

The Docker image pins the tool version and verifies the release checksum during
the build. Tool availability/version is reported by the health endpoint and
stored with every result.

### 5.3 Dolby Vision

The existing full-RPU `dovi_tool` detector remains authoritative for FEL/MEL.
The detailed result also stores DV profile and compatibility ID rather than
collapsing all evidence into `dv_layer`.

- `unknown` is a retryable failure and never cache-current.
- `none` is authoritative only after successful analysis.
- Profile 7 becomes FEL or MEL only from RPU analysis.
- Profiles 5 and 8 remain distinct from enhancement-layer terminology.

## 6. Scan service and state machine

The existing Plex metadata scanner becomes a durable service backed by the run
tables rather than only in-memory counters.

Supported scopes:

- `pilot`: exact Plex rating keys/paths selected from the inventory;
- `selected`: explicit set selected in the UI;
- `all_4k_movies`: every current movie part in configured 4K libraries;
- `retry_failures`: failed/unsupported items selected from a previous run;
- `resume`: continue a paused/interrupted run.

Behavior:

1. Snapshot the expected Plex file manifest before work begins.
2. Reject a second active metadata scan.
3. Process one file at a time by default.
4. Heartbeat and persist stage transitions before invoking each tool.
5. Compare signatures immediately before and after analysis; if the source
   changed, discard the result and record `source_changed`.
6. Persist the compatibility caches, current inventory, and run item result
   together; a failed persistence step records failure and never increments
   success.
7. Pause/cancel stops before the next file and leaves a resumable manifest.
8. Broadcast progress derived from durable counters, including honest elapsed
   time and ETA based on observed fast/DV/HDR10+ categories.

## 7. API design

Existing `/plex/scan-metadata` routes remain compatible and delegate to the new
service. New resource-oriented endpoints are:

```text
POST /plex/metadata-scans
GET  /plex/metadata-scans
GET  /plex/metadata-scans/{run_uuid}
POST /plex/metadata-scans/{run_uuid}/pause
POST /plex/metadata-scans/{run_uuid}/resume
POST /plex/metadata-scans/{run_uuid}/cancel
POST /plex/metadata-scans/{run_uuid}/retry-failures
GET  /plex/metadata-scans/{run_uuid}/items
GET  /plex/metadata-scans/{run_uuid}/discrepancies

GET  /media-inventory
GET  /media-inventory/{rating_key}
GET  /media-inventory/facets
GET  /media-inventory/export.csv
```

Inventory filters include text, library, resolution, HDR format, HDR10+ state,
DV layer, DV profile, scan state, discrepancy, failure code, currentness, sort,
page, and page size. Pagination is mandatory; no fixed 2,000-row ceiling.

## 8. UI design

Add a dedicated **Media inventory** route rather than expanding the already
dense Renames screen.

The page's single job is to answer: "What technical media do I have, and what
still needs attention?"

It contains:

- coverage header: expected/current/stale/unscanned/failed/unsupported;
- search box plus filter chips for 4K, DV, FEL, MEL, P5, P8, HDR10+, HDR10,
  HLG, current, stale, failed, and seed/live disagreement;
- paginated desktop table and compact mobile cards;
- per-item evidence drawer showing signatures, tool versions, all streams,
  seed/live comparison, Plex labels, and Kometa-relevant state;
- scan-run history with pause/resume/retry controls;
- CSV export and saved URL query state;
- actionable empty/error states and visible keyboard focus.

The visual vocabulary follows the existing application theme. The distinctive
element is an evidence rail that shows `Seed -> Live -> Plex -> Kometa` as four
compact states for each title, making provenance visible rather than decorative.

## 9. Plex and Kometa handoff

ScanHound does not call Kometa directly.

After a completed scan:

1. Generate a dry-run report for the closed managed Plex-label set:
   `DV FEL`, `DV MEL`, `DV P5`, `DV P8`.
2. Require zero unresolved path collisions and review all seed/live
   discrepancies affecting managed labels.
3. Apply the convergent Plex-label reconciliation explicitly.
4. Re-query Plex and prove expected labels equal actual labels.
5. Run Kometa.

Extend `/config/dv-layer.yml` with P5/P8 badge assets while retaining FEL/MEL at
the top-right. Kometa's standard resolution overlay remains responsible for the
generic bottom DV/HDR10+/HDR badge. The first release does not add duplicate
custom HDR10+ artwork. Instead, the inventory reports ScanHound-versus-Plex HDR
disagreements so they can be resolved without silently drawing two badges.

## 10. Seed reconciliation

Seed data is evidence, not current proof.

The discrepancy vocabulary is:

- `match`;
- `seed_unknown_resolved`;
- `seed_fel_live_mel`;
- `seed_mel_live_fel`;
- `seed_dv_live_none`;
- `seed_missing_live_present`;
- `seed_present_live_missing`;
- `unmatched_seed_path`;
- `no_seed`;
- `live_unknown`.

No discrepancy automatically changes a Plex label until the full reconciliation
stage. The baseline remains queryable after live results exist.

## 11. Testing and verification

Development follows test-first red/green cycles.

Required automated coverage:

- additive schema migration, migration re-entry, real seed backfill, and rollback
  using disposable databases;
- run state transitions, abandoned-run interruption, resume, cancel, duplicate
  start rejection, terminal-count consistency, and persistence failure;
- source mutation during analysis;
- full-stream HDR10+ present/absent/unknown behavior, timeout, unsupported codec,
  and missing tool;
- all-stream audio/subtitle extraction and backward-compatible compact specs;
- DV unknown retry behavior and seed/live discrepancies;
- indexed inventory filters, FTS search, facets, pagination, stable sorting, and
  CSV injection-safe export;
- API authorization, input bounds, duplicate requests, and public error
  sanitization;
- UI filter state, desktop/mobile rendering, keyboard navigation, progress,
  resume, retry, and evidence display;
- Plex label dry-run/apply convergence and non-managed-label preservation;
- Python 3.11/3.12, root/UID 1000, frontend checks/unit/build, Playwright, and
  production-image tests.

## 12. Pilot and full-scan gates

### Pilot

Select 25-50 generated or explicitly approved real read-only library files
covering known FEL, MEL, P5, P8, HDR10+, HDR10, HLG, SDR, MKV, MP4, M2TS,
multiple audio/subtitle tracks, missing path, permission failure, and one file
changed during analysis.

Pilot acceptance requires:

- every expected item reaches an honest terminal state;
- pause/resume survives an application restart;
- known classifications match expected results;
- seed/live discrepancies are preserved;
- no media file signature changes because of scanning;
- database restart preserves run and inventory search results;
- Plex-label dry run is accurate and makes zero writes;
- measured storage load and duration support concurrency one;
- failed items can be retried without rescanning current successes.

### Full scan

The full scan runs only after explicit production authorization following pilot
review. It runs sequentially, preferably off-hours, with Auto-rename and all
automatic actions disabled. Plex label sync is deferred until scan review.

Full-scan acceptance requires:

```text
expected = current + failed + unsupported + missing + cancelled
pending = 0
running = 0
unexplained omissions = 0
```

Then retry failures, reconcile discrepancies, perform the Plex-label dry run,
apply reviewed labels, verify Plex, and run Kometa. Completion evidence records
the exact code SHA, image digest, tools, database snapshot/hash, run UUID,
counts, failure ledger, discrepancy report, Plex-label diff, and Kometa result.

## 13. Rollback

- Schema changes are additive; existing `media_probe` and `dv_scan` consumers
  remain compatible.
- Back up the production database before migration and again before the full
  scan.
- A code rollback can ignore the new tables without losing old behavior.
- Plex label reconciliation produces a before/after ledger and inverse plan.
- Kometa runs only after Plex labels are verified, so a pre-Kometa rollback is
  simply the inverse label plan.
- No rollback ever modifies media files because the scanner never writes them.

## 14. Definition of done

- The 3,729-row seed baseline is permanently preserved and searchable.
- Every current 4K movie part appears in the inventory with an explicit state.
- HDR10+ negatives come from completed full-stream evidence, not a first-frame
  assumption.
- FEL/MEL/P5/P8 results include current signatures and tool provenance.
- Search works across title, library, path, HDR, DV, state, failures, and
  discrepancies with pagination and export.
- Scan runs survive pause, cancellation, process restart, and retry.
- The pilot passes without modifying media.
- The full run has no unexplained omissions.
- Plex managed labels converge exactly and Kometa displays reviewed overlays.
- Auto-rename remains disabled until its independent safety gate is approved.
