# ScanHound renaming pipeline and 4K metadata audit

**Audit date:** 2026-07-22

**Repository:** `LstDtchMn/ScanHound`

**Audit branch:** `agent/rename-pipeline-audit-fixes`

**Reviewed parent:** `5012ea2b7d87b48da7d8148a4d1eb876133a7ff6`

**Code-tested correction SHA:** `8e1f9519a5dbf99be741c7aff202bf8c21cd5aaf`

**Production image at audit:** `sha256:c62a131fabc166ab92f37cfa071d1d17b078c86932b881b34ddd30de33acb8e6`

**Production posture:** RSS shadow; Auto-rename and all auto-grab disabled

## Executive verdict

**NO-GO for Auto-rename.** Keep it disabled.

The forward placement and trash primitives are substantially hardened: mutation
entry points require the runtime writer lock, final publication is no-replace,
cross-device copy is verified before source disposal, and trash metadata is
prepared durably before byte movement. The audit nevertheless found an unsafe
undo primitive in the deployed code, an unchecked restore-key database write,
an unjournaled undo crash window, and no application-level sentinel on the real
production filesystems. The first two defects are corrected on the audit branch;
the latter two remain rollout gates.

Manual preview, matching, and non-destructive review are safe. Manual Apply,
Undo, Keep Plex, Restore, trash deletion, and Auto-rename should remain held
until the corrections are independently reviewed, deployed, and an
application-level sentinel exercises the real paths on each production mount.

The 4K metadata verdict is also unambiguous: **the complete 4K library has not
been authoritatively scanned.** Only 6 of 4,270 Plex 4K movie records had a
current `media_probe` signature. Historical `dv_scan` seed rows are inventory
hints, not proof of a successful local-file scan.

## Scope and evidence classification

The audit combined:

- static call-path and schema review at the reviewed parent;
- executable unit/integration/fault-injection tests with generated temporary
  files only;
- read-only inspection of the live container, database, mount configuration,
  Plex labels, and Kometa configuration;
- aggregate queries that did not modify settings, files, Plex, or Kometa.

Evidence labels used below:

- **PROVEN:** executable code/test or read-only production artifact directly
  demonstrates the claim;
- **PARTIAL:** important behavior is covered, but a crash/platform/application
  boundary remains unproved;
- **ASSERTED:** code intends the behavior but lacks adequate executable proof;
- **VIOLATED:** a reachable path breaks the invariant;
- **UNVERIFIED:** production evidence was unavailable or stale.

## Environment and production context

| Item | Observed value |
|---|---|
| Local review host | Windows, Python 3.12.9, Node 24.14.0, npm 11.9.0 |
| Production container | `scanhound`, running as UID 0 |
| Production Python | 3.12.13 |
| Production media tools | ffprobe 5.1.9; dovi_tool 2.3.2 |
| Production database | SQLite `user_version=6`; `integrity_check=ok` |
| Production storage | Docker volume for DB; read-only Plex media mounts; separate writable download/library mounts |
| Production settings | Auto-rename off; general/RSS auto-grab off; background scan off; RSS shadow |

Sensitive settings and exact mount paths are intentionally omitted. A read-only
configuration inspection exposed credentials in the interactive tool output;
those credentials are not reproduced here and should be rotated.

## Architecture and execution map

```text
JDownloader/package or manual folder
        |
        v
RenameService.process_package/process_folder
        |
        +--> parser -> filename facts -> TMDB candidates -> confidence/reasons
        |                                  |
        |                                  +--> optional LLM/OCR/subtitle/vision
        |                                       (forces needs_review)
        v
rename_jobs row (matched / needs_review)
        |
        +--> API preview/rematch/conflict analysis -> Plex cache + media probes
        |
        v
queue_apply (background thread; status=applying)
        |
        v
RenameService.apply
        |
        +--> optional durable trash of existing library copy
        +--> fileops.place_file
        |      +--> hardlink/no-replace
        |      +--> renameat2/MoveFileEx no-replace
        |      +--> verified temp copy + atomic no-replace publication
        |      +--> recoverable source trash for cross-device move
        v
rename_jobs status=applied (checked; rollback on write failure)
        |
        +--> undo_place + optional restore overwritten copy
        +--> trash/restore/delete/sweep APIs and transaction journals
        +--> Plex scan/sort-title integration
```

Primary components:

- `backend/rename/service.py`: discovery, identity, job state, conflict
  resolution, Apply, Undo, Keep Plex, queues;
- `backend/rename/fileops.py`: publication, verified copy, durable trash,
  restore/delete/sweep journals and repair;
- `backend/database.py`: `rename_jobs`, `dv_scan`, `media_probe`, Plex cache;
- `backend/api/routes/rename.py`: review/apply/undo/trash/DV endpoints;
- `frontend/src/routes/renames/+page.svelte`: operator review and actions;
- `backend/plex_metadata_scan.py`: full-library media/DV probe orchestration;
- `backend/rename/dv_labeler.py`: Plex managed-label reconciliation;
- Kometa `dv-layer.yml`: poster overlays keyed by Plex `DV FEL`/`DV MEL` labels.

## Prioritized findings

### RP-01 — deployed Undo can overwrite a racing source

- **Severity:** High
- **Status:** Confirmed defect; fixed on audit branch
- **Code:** `backend/rename/fileops.py::undo_place`
- **Failure:** deployed code performs `exists(src)` followed by
  overwrite-capable `shutil.move(dst, src)`. Another writer can create `src`
  after the check and before publication.
- **Correction:** Undo now uses durable no-replace publication. EXDEV uses
  verified atomic copy followed by durable unlink. Link/copy undo also persists
  directory-entry removal.
- **Tests:** no-replace primitive contract and EXDEV verified-copy rollback.

### RP-02 — library replacement can consume a copy without persisting its undo key

- **Severity:** High
- **Status:** Confirmed defect; fixed on audit branch
- **Code:** `RenameService.apply(... replace_library_dup ...)`
- **Failure:** the library duplicate is durably trashed, then
  `conflict_replaced_path` was written without checking the database result.
  A false return permits placement while losing the key needed to restore the
  displaced library copy.
- **Correction:** fail closed, restore the displaced copy, leave the download
  untouched, and return failure when the restore key does not persist.

### RP-03 — Undo lacks a durable transaction journal

- **Severity:** High
- **Status:** Evidence gap / probable consistency defect
- **Failure:** a process crash after filesystem reversal but before
  `status=reverted` leaves files restored while the database still says
  `applied`. Retrying Undo may then behave inconsistently.
- **Required fix:** journal prepared/files-restored/database-committed phases;
  make startup reconciliation idempotent.

### RP-04 — queued Apply workers are not lifecycle-owned

- **Severity:** High
- **Status:** Probable defect
- **Code:** `RenameService.queue_apply`; API lifespan teardown
- **Failure:** the daemon worker has no registry generation check and teardown
  does not join it before clearing services/closing the database. A stale worker
  can continue into a later lifespan or lose its DB mid-operation.
- **Required fix:** lifespan generation ownership plus cancel-and-join before DB
  shutdown; prove in-flight work settles before teardown completes.

### RP-05 — Undo may return success while an overwritten original remains unrestored

- **Severity:** Medium/High
- **Status:** Confirmed contract risk
- **Code:** `RenameService.undo`
- **Failure:** restoration is best-effort; response remains `ok=true` with a
  warning. Files remain recoverable in trash, but filesystem and intended state
  do not agree.
- **Required fix:** explicit `partial`/`recovery_required` state and UI, rather
  than semantic success.

### RP-06 — destructive trash APIs rely only on ordinary authenticated POSTs

- **Severity:** Medium
- **Status:** Hardening gap
- **Code:** `/rename/trash/delete`, `/rename/trash/empty`
- **Risk:** frontend confirms, but the backend has no one-time confirmation
  token or typed destructive intent. A stale/automated authenticated request can
  permanently delete.

### RP-07 — identity remains heuristic at the automatic threshold

- **Severity:** Medium
- **Status:** Product risk
- **Evidence:** deterministic matching preserves reasons and pushes auxiliary
  LLM/OCR/vision decisions to review. Season packs and ambiguous episodes are
  conservatively handled. A close title with a one-year discrepancy can still
  score highly with a warning, so external ID or explicit review remains safer
  for remakes and weak release names.

### META-01 — full-library scan results were written to the wrong inventory source

- **Severity:** High for evidence/Kometa correctness
- **Status:** Confirmed defect; fixed on audit branch
- **Failure:** `PlexMetadataScanJob` wrote DV results as `source=metadata-scan`,
  while `/rename/dv-scans`, scheduled label sync, and Kometa handoff read only
  `source=scan`.
- **Correction:** authoritative results now enter `source=scan`.

### META-02 — failures could look successfully processed and become permanently wedged

- **Severity:** High for completeness claims
- **Status:** Confirmed defect; partially fixed
- **Failure:** every attempted file incremented `processed`; failed ffprobe,
  failed cache writes, and `dovi_tool` unknown results were not separately
  counted. An unknown DV result could be cached as current and skipped forever.
- **Correction:** expose `succeeded`/`failed`; require a current persisted
  `media_probe`; treat unknown/error DV results as failed and retryable.
- **Remaining:** run history and per-file failures are in memory only. Persist a
  scan-run ledger before claiming durable completeness.

### META-03 — HDR10+ detection probes only the first decoded frame

- **Severity:** Medium
- **Status:** Confirmed limitation
- **Code:** `backend/rename/mediainfo.py::probe_specs`
- **Risk:** dynamic SMPTE 2094-40 metadata may not be present on the first frame.
  A file can be recorded as plain HDR10 even though later frames carry HDR10+.
- **Required fix:** authoritative stream metadata/tooling or bounded multi-point
  sampling with a documented confidence/provenance field.

### KOMETA-01 — scheduled label sync was not convergent

- **Severity:** Medium
- **Status:** Confirmed defect; fixed on audit branch
- **Production evidence:** 444 current `source=scan` Plex identities had a
  managed DV label, but two had stale `DV FEL` labels while current scan data
  expected P5/P8.
- **Causes:** additive-only mode never removed a wrong managed label; the change
  detector used `MAX(scanned_at)` even though upsert updates only
  `last_seen_at`.
- **Correction:** matched movies converge to the current managed label;
  unmatched movies remain untouched; scheduling observes `last_seen_at`.

### SEARCH-01 — the 4K metadata inventory is not comprehensively searchable

- **Severity:** Medium/product gap
- **Status:** Confirmed gap
- **Current:** `/rename/dv-scans` supports exact layer plus a maximum 2,000 rows;
  the Renames UI shows an inventory but has no general metadata/status search.
- **Missing:** HDR10+, DV profile, layer/provenance, current/stale/unscanned,
  failure reason, library, title/path search, and paging. There is no public
  `media_probe` inventory endpoint.

## Safety invariant matrix

| # | Invariant | Status | Evidence / gap |
|---:|---|---|---|
| 1 | Placement never overwrites a destination racer | PROVEN | no-replace race tests; atomic publication |
| 2 | Restore never overwrites | PROVEN | restore no-replace tests |
| 3 | Recovery metadata precedes source consumption | PROVEN for trash | durable-trash fault injection |
| 4 | Source bytes remain recoverable after modeled failure | PARTIAL | Apply/trash proven; unjournaled Undo crash gap |
| 5 | Success means filesystem and DB agree | PARTIAL | Apply final write checked; Undo partial restore remains success |
| 6 | Failure cannot report success | PARTIAL | corrected Apply/Undo writes; other unchecked route mutations remain |
| 7 | One writer mutates shared state | PROVEN at entry points | 11 guard-first tests; lifecycle worker gap remains |
| 8 | Unsupported filesystem guarantees fail visibly | PARTIAL | primitive sentinels; application-level mount sentinel missing |
| 9 | Cross-device copy is verified before source disposal | PROVEN | hash verification and EXDEV tests |
| 10 | Manifest/DB ordering supports recovery | PROVEN for trash; PARTIAL globally | Undo has no journal |
| 11 | Crash recovery is idempotent | PARTIAL | trash repair proven; Undo not covered |
| 12 | Recovery replay cannot overwrite/delete valid media | PARTIAL | no-replace restore; missing Undo journal |
| 13 | Permanent deletion is explicit | PARTIAL | UI confirm; no backend destructive-intent token |
| 14 | Auto-rename cannot act on ambiguous identity | PARTIAL | conflict/review gates strong; heuristic threshold remains |
| 15 | Unknown metadata stays unknown | PARTIAL | DV unknown fixed; HDR10+ probe failure degrades to HDR10 |
| 16 | Wrong remake/season cannot capture another | PARTIAL | year/episode scoring and review; no universal external-ID requirement |
| 17 | Errors are visible without public raw internals | PARTIAL | structured errors broadly used; `restore_warning` semantic issue |
| 18 | Stale workers cannot publish | VIOLATED/UNPROVEN | queue worker not lifespan-owned |

## Failure-injection summary

| Phase | Existing proof | Important missing proof |
|---|---|---|
| destination reservation | thread/process no-replace races | real application path on every production mount |
| same-volume publication | renameat2/MoveFileEx/hardlink tests | power-loss durability on CIFS where dir fsync is unsupported |
| cross-volume copy | corruption/hash/EXDEV/temp cleanup tests | disconnect, quota, source mutation on production SMB |
| source disposal | durable trash transaction tests | ACL/share violation on actual mounts |
| final DB commit | Apply rollback tests | kill process at each byte/DB boundary |
| Undo | new no-replace/EXDEV tests | durable phase journal and restart replay |
| Restore/delete/sweep | intent journals and repair tests | concurrent restore-vs-sweep/delete on production FS |
| process ownership | runtime writer lock tests | in-flight queue during lifespan teardown |

## 4K metadata completeness

Read-only reconciliation joined Plex movie paths, live file signatures,
`media_probe`, and `dv_scan`:

| Metric | Count |
|---|---:|
| Plex movie records | 16,111 |
| Plex 4K movie records | 4,270 |
| current authoritative `media_probe` | 6 |
| stale `media_probe` | 1 |
| no current probe | 4,263 |
| 4K records with no DV inventory row | 841 |
| matched 4K DV rows from authoritative `source=scan` | 463 |
| matched 4K historical `source=seed` rows | 2,966 |
| inventory signatures matching current files | 5 |
| stale/missing inventory signatures | 3,424 |

The all-library `dv_scan` table contains 4,192 rows: 463 `scan` and 3,729
`seed`. Its layer values include FEL, MEL, profiles 5/8, none, and many unknowns,
but seed rows and stale signatures cannot prove current local-file analysis.

### Persistence and identifying gaps

- Successful probes persist JSON plus `(mtime,size)` in `media_probe`.
- Successful DV analysis persists layer plus `(mtime,size)` in `dv_scan`.
- Currentness is exact size plus a one-second mtime tolerance.
- Before this branch, failed files were neither persisted nor separately
  counted. The branch provides truthful in-memory success/failure counters and
  refuses success without a durable probe row.
- There is still no durable scan-run table containing expected paths, success,
  failure code, attempt time, tool version, and completion status. After restart,
  operators cannot ask which files failed in the previous run.

## Kometa handoff

ScanHound does not call Kometa directly. It writes managed Plex labels:

- `DV FEL`
- `DV MEL`
- `DV P5`
- `DV P8`

The 4K Kometa library loads the default resolution overlay and a local
`dv-layer.yml`. The local file uses only `DV FEL` and `DV MEL` to add custom
poster tags. Profile 5/8 labels are not represented by that file. HDR10+ is not
sent by ScanHound; Kometa's default resolution overlay independently derives
DV/HDR10+/HDR from Plex metadata.

All 444 current authoritative Plex identities had at least one managed DV label,
but two labels were stale/wrong. Therefore the claim “all relevant movies reached
Kometa correctly” is **not proven**. The branch makes the labeler convergent, but
the library scan itself remains overwhelmingly incomplete.

## Searchability

The answer is **no** for the requested inventory.

Currently searchable/filterable:

- exact DV layer through `/rename/dv-scans?layer=...`;
- limited client display of scan inventory and layer counts.

Not searchable/filterable:

- authoritative versus seed provenance;
- current, stale, unscanned, or failed status;
- HDR10+ and other HDR results;
- DV profile and layer together;
- filename/title/library/path across the complete inventory;
- tool/error status and last attempt;
- more than 2,000 rows with proper pagination.

## Tests and validation

Added regressions cover:

- Undo no-replace publication and EXDEV verified-copy rollback;
- false database success during Undo;
- restore-key write failure during library replacement;
- truthful metadata scan success/failure counts;
- non-persistence and unknown DV failures remaining retryable;
- full-library results using the live `source=scan` inventory;
- label convergence for a positive path match;
- label-sync wakeup when an existing row changes.

Validation performed against the code-tested SHA's exact tree:

| Check | Result |
|---|---|
| Linux production-image backend, Python 3.12.13 / pytest 9.1.1 | **3,992 passed, 4 skipped, 0 failed** |
| Focused rename/trash/DV/metadata/API matrix on Windows | **232 passed, 0 failed** |
| Final new-regression consolidation | **47 passed, 0 failed** |
| Frontend typecheck | **0 errors**; 3 pre-existing accessibility warnings |
| Frontend unit tests | **373 passed, 0 failed** |
| Frontend production build | **passed** |
| Python compilation | **passed** |
| `git diff --check` | **passed** |

The unrestricted backend command ran from a disposable container using the
production image, with the repository mounted read-only:

```text
pytest -q -o cache_dir=/tmp/pytest-cache tests
```

The four skips are pre-existing environment/platform conditions. An initial
container attempt launched from `/tmp` and produced two repository-relative-path
test failures; rerunning from `/work` resolved both without a code change. A
Windows-only trash-root assertion also fails under its `PosixPath` monkeypatch;
the same test passes in the final Linux matrix.

## Required plan before Auto-rename

### P0 — destructive-operation closure

1. Deploy and independently review RP-01/RP-02 corrections.
2. Add an Undo transaction journal and startup repair.
3. Make queued Apply workers lifecycle-owned; cancel/join before DB close.
4. Introduce an explicit partial/recovery-required status for failed overwrite
   restoration.
5. Run the real application entry points against generated sentinel files on
   every production mount: same-volume, EXDEV, collision race, rollback, Undo,
   trash, restore, repair, and simulated kill boundaries.

### P1 — metadata completeness and Kometa correctness

1. Add persistent `metadata_scan_runs` and per-file attempt rows with tool
   version, signature, result, failure code, and retry state.
2. Replace first-frame HDR10+ detection with authoritative or documented
   multi-point analysis.
3. Run a complete 4K scan and require:
   `current_success + current_failure = expected_current_files`, with zero
   unexplained omissions.
4. Reconcile Plex labels after the scan and prove no conflicting managed labels.
5. Run Kometa and verify overlays against a stratified FEL/MEL/HDR10+/P5/P8
   sample plus aggregate counts.

### P2 — inventory/API/UI

1. Add a paginated metadata inventory endpoint with title/path/library search.
2. Add filters for FEL, MEL, P5, P8, HDR10+, HDR10, HLG, current, stale,
   unscanned, and failed.
3. Show scan-run completeness, failed-file reason codes, and retry controls.
4. Export a secret-safe CSV/JSON completeness report for Kometa reconciliation.

## Final go/no-go checklist

Auto-rename remains **NO-GO** until all are true:

- correction branch is reviewed, green on Python 3.11/3.12 and root/UID 1000;
- Undo journal and lifecycle-owned queue are proven;
- app-level filesystem sentinel passes on each real mount;
- no destination overwrite or unrecoverable source is observed;
- source/destination/DB/trash state agrees after every injected failure;
- production settings remain fail-closed during rollout;
- one manual canary batch succeeds with deterministic recovery evidence;
- only then is a narrowly limited Auto-rename canary considered.

The metadata/Kometa work is not itself a prerequisite for safe file publication,
but it is a prerequisite for claiming that the 4K inventory and poster tags are
complete and trustworthy.
