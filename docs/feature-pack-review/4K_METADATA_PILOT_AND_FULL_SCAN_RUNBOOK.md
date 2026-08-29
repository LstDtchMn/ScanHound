# Controlled 4K metadata inventory rollout

> **Status (2026-08): HISTORICAL.** The pilot gate cleared long ago — the 4K metadata inventory and DV detection pipeline are live in production. Kept as reference for the original rollout procedure.

This runbook qualifies ScanHound's read-only local-file metadata inventory before
any Plex-label or Kometa change. It does not authorize media mutation.

## Frozen safety posture

- **Auto-rename remains disabled.**
- General auto-grab, RSS auto-grab, and RSS-primary remain disabled.
- Media mounts remain read-only to the metadata scanner.
- The scanner processes one file at a time.
- Unknown or failed DV/HDR10+ evidence remains unknown and retryable.
- Do not run Kometa, write Plex labels, or edit the production Kometa config during
  the pilot. Those are separate reviewed gates.

## What the scan records

For each cached Plex 4K movie path, the durable manifest records the library,
Plex rating key, file path, state, attempts, stage, and a stable public error
code. A successful local-file analysis records resolution, codec, color/HDR
facts, HDR10+ state (`present`, `absent`, or `unknown`), Dolby Vision profile,
FEL/MEL/P5/P8 layer evidence, file signature, tool evidence, and scan time.

Historic imported FEL/MEL rows are preserved in `dv_seed_baseline`. They are
comparison evidence, not proof that the current file was scanned. The inventory
shows `verified`, `seed_unverified`, `live_only`, and explicit differences such
as `seed_fel_live_mel`.

## Preflight and backup

1. Record the branch, commit SHA, image digest, container UID/GID, database
   location, Plex cache timestamp, and configured path mappings.
2. Confirm the deployed image contains working `ffprobe`, `dovi_tool`, and
   `hdr10plus_tool` versions recorded in the implementation report.
3. Confirm all selected media paths resolve inside the container and the media
   mounts are read-only.
4. Confirm no other metadata scan is `running` or `paused`.
5. Use SQLite's online backup API (or `.backup`) to create a consistent database
   copy outside the live database directory. Record source and backup SHA-256.
6. Export the pre-pilot `/plex/media-inventory/export.csv` and preserve it with
   the backup. Do not include credentials in the evidence package.

Stop if database integrity is not `ok`, path mappings are unresolved, tools are
missing/mismatched, a second writer is active, or media mounts are writable when
they were expected to be read-only.

## Pilot selection (25–50 movies)

Choose **25–50** cached Plex 4K movie rating keys. The pilot must include, where
available:

- historic seed FEL and seed MEL entries;
- known P5 and P8 titles;
- HDR10+ and plain HDR10 titles;
- titles with no Dolby Vision;
- large remuxes and smaller WEB encodes;
- at least two libraries/mounts;
- at least one previously failed or stale item;
- filenames containing punctuation and non-ASCII characters.

Select the titles in **4K Metadata → Start selected pilot**, or call:

```http
POST /plex/metadata-scans
{"scope":"pilot","ids":["<plex-rating-key>"]}
```

Record the returned `run_uuid`. Do not use `scope=full` yet.

## Observe the pilot

During the run, record storage throughput/latency, container CPU/memory, NAS
errors, and tool timeouts. The UI and these endpoints are authoritative:

```text
GET  /plex/metadata-scans/{run_uuid}
GET  /plex/metadata-scans/{run_uuid}/items
GET  /plex/metadata-scans/{run_uuid}/discrepancies
POST /plex/metadata-scans/{run_uuid}/pause
POST /plex/metadata-scans/{run_uuid}/resume
POST /plex/metadata-scans/{run_uuid}/cancel
POST /plex/metadata-scans/{run_uuid}/retry-failures
```

Pause after the current file if storage latency becomes material. A restart must
turn an in-flight item/run into `interrupted`; resuming must not reprocess
already-current items.

## Pilot acceptance

Accept the pilot only when all of these are true:

- every manifest row is `current`, or every failure has an understood public
  reason and a successful controlled retry;
- no source file signature changed during analysis;
- no file was created beside, moved, renamed, tagged, or deleted from media;
- HDR10+ failures remain `unknown`, never `absent`;
- FEL/MEL/P5/P8 spot checks agree with independent `dovi_tool` evidence;
- seed/live differences were reviewed instead of silently overwritten;
- pause, restart, resume, and retry evidence is durable;
- storage and container load remain within the operator's accepted limits;
- CSV counts reconcile with run/item/database counts.

Any source mutation, false negative, unexplained identity mismatch, database
divergence, or unbounded storage impact rejects the pilot.

## Full-library scan

Only after documented pilot acceptance, create a fresh database backup and call:

```http
POST /plex/metadata-scans
{"scope":"full"}
```

The backend derives the manifest from cached 4K movies; it does not walk arbitrary
folders. Preserve the manifest count before the worker begins. Monitor the same
telemetry and stop conditions as the pilot. The run is complete only when the
expected Plex 4K set reconciles to current, failed, stale/source-changed, and
unmapped counts with no silent omissions.

## Plex label dry run

After the full scan completes, request a **Plex label dry run**:

```http
POST /rename/dv-sync-labels
{"dry_run":true}
```

Capture the `dv:sync_done` payload. It now reports each matched title's seed
layer, live layer, discrepancy, existing managed labels, desired label, additions,
and removals. Require `writes=0`. Review every removal and every seed/live
difference.

### The label vocabulary — three sets, not one

This step decides a **destructive** reconciliation, so the reviewer has to read
the removals against the right vocabulary. Three sets are involved and they are
routinely conflated. Ground truth is `backend/rename/dv_labeler.py`;
`tests/test_dv_label_vocabulary.py` fails if the lists below drift from it.

**1. `dv_labeler.MANAGED` — every label this sync may add or remove.**
Nine labels are managed, not four. Every removal the dry run reports comes
from this set, and nothing outside it is ever touched:

<!-- dv-vocab:managed -->
```text
DV · DV FEL · DV MEL · DV P5 · DV P8 · DV5 · DV7 · DV8 · HDR10
```
<!-- /dv-vocab:managed -->

`DV7` and `DV` are group tags derived from the same verdict — a FEL title
carries `DV FEL` **and** `DV7` **and** `DV`, three widths of one fact, not
alternatives. `HDR10` is the one tag not derived from a DV verdict alone: it
needs both an authoritative "the detector ran and found no Dolby Vision" and
Plex's own wide-gamut flag.

**2. The layer-badge subset — one label per detected layer.** This is what the
`dv_label_vocab` setting renames, and it is a **strict subset** of the managed
set, never the whole of it:

<!-- dv-vocab:layer-badges -->
```text
DV FEL · DV MEL · DV5 · DV8
```
<!-- /dv-vocab:layer-badges -->

**3. The retiring labels.** These are the pre-rename names for Profile 8 and
Profile 5. They stay in `MANAGED` deliberately, so that this sync **removes**
them from titles that still carry them — dropping them from `MANAGED` instead
would leave a stale label on every Profile 8 title forever, unmanaged and
indistinguishable from one the operator applied by hand. Expect them among the
dry run's removals; those removals are the migration working, not a fault:

<!-- dv-vocab:retiring -->
```text
DV P5 · DV P8
```
<!-- /dv-vocab:retiring -->

Non-managed labels, including Kometa's `Overlay` label, must never be changed.
Applying the label reconciliation requires a separate explicit operator decision.

## How Kometa displays the result

ScanHound does not call Kometa directly. After an explicitly approved Plex-label
apply and a Plex read-back proving desired labels equal actual labels, Kometa reads
those labels through `plex_search`.

**Deployed today** (owner-observed 2026-08-26 by reading the running `kometa`
container; the file lives on the host, outside this repository, so nothing here
can verify it). Kometa loads `/config/dv-layer.yml`, which draws pre-rendered
PNG pills and has only `dv-fel.png` and `dv-mel.png`:

- `DV FEL` → **DV FEL** poster badge;
- `DV MEL` → **DV MEL** poster badge;
- every other managed label → **no badge at all**.

So ScanHound applies `DV8` and `DV5` to Plex today, and `HDR10` only when both
halves hold — an authoritative "the detector ran and found no Dolby Vision" AND
Plex's own wide-gamut flag. When no HDR index is available at all (the log line
is `HDR index unavailable; HDR10 labels left untouched`) `HDR10` is neither
added nor removed, so an empty HDR10 column in a dry run is that condition, not
a labelling failure. Kometa renders nothing for any of them, which is a known
open gap, not a misconfiguration to fix from inside this runbook: closing it
needs three more 250×96 images for the deployed image-based design, or
adopting the text design wholesale. That decision is the repository owner's.

**Not deployed.** `docs/kometa/DV_BADGE_DESIGN.md` is an unadopted **proposal**
— a text-rendered, top-LEFT design covering more labels than the deployed file.
It is not the running config and must not be copied into Kometa.

HDR10+ remains searchable in ScanHound from authoritative local-file evidence.
The first release intentionally does not add a second ScanHound HDR10+ artwork
layer: Kometa's standard resolution/HDR overlay derives its generic HDR10+ badge
from Plex metadata. Any ScanHound-versus-Plex disagreement must be reviewed before
poster claims are treated as authoritative.

Validate a copied Kometa configuration and badge assets before production. Only
after the label read-back and config validation may the operator schedule Kometa.
Do not run Kometa as part of this runbook's implementation/test phase.

## Evidence package

Preserve exact SHAs/digests, tool versions, backup hashes, manifest and terminal
counts, item failures, discrepancy CSV, label dry-run JSON, resource telemetry,
Plex label read-back, Kometa config validation output, every stop/retry, and a
statement that Auto-rename and all auto-grab remained disabled.
