# ScanHound Host Detector (Dolby Vision FEL/MEL)

Runs on the Docker **host** (TurtleLandSRVR, 192.168.1.170), NOT inside the container.
Detection is host-side because FEL vs MEL requires `dovi_tool` to read the full RPU
stream, and the container cannot reach the `.180` SMB media. This artifact is **not**
part of `docker build` — the container image never contains it.

## Contents

| File | Role |
|---|---|
| `dv_host_scan.py` | Walks `dv_library_roots`, classifies each file, writes `dv_host.db`, optionally tags MKVs. |
| `dovi_tool.exe` | quietvoid **v2.3.2** (pinned via `DOVI_TOOL_VERSION` in the repo `Dockerfile` — must match the image's Linux `dovi_tool` for identical classification). |
| `mkvpropedit.exe` | MKVToolNix; only needed when `dv_file_tagging` is enabled. |
| `dv_host.db` | The detector's OWN SQLite store. Created by the script. NEVER opens `crawler.db`. |

## Placement

1. Put `dovi_tool.exe` and `mkvpropedit.exe` in this folder (or anywhere), and ensure
   their directory is on `PATH`. `detect_layer` resolves the binary with
   `shutil.which("dovi_tool")`, which honors `PATHEXT` so `dovi_tool.exe` resolves.
2. Do **not** rely on your interactive user `PATH` for scheduled runs — a Windows
   Task Scheduler action runs with a stripped environment. Set the binary directory
   on `PATH` inside the scheduled action itself (see below).

## Config source

The container writes `X:\Docker Apps\ScanHound\data\dv_host.json` on every settings
save (bind-mounted as `./data:/data` in `docker-compose.yml`, so the container-side
`/data/dv_host.json` and the host-side `data\dv_host.json` are the same file). It
contains `{dv_library_roots, dv_detection, dv_file_tagging, dv_label_vocab}`. The host
script reads THAT file (`--config`, default `data/dv_host.json` relative to the current
working directory) — it does not import `config.py`. If `dv_detection` is false or the
roots are empty, the script logs and exits (exit code `0`).

## CLI arguments

`dv_host_scan.py` takes three flags, all optional (`python scripts/host-detector/dv_host_scan.py --help` shows the same):

| Flag | Default | Notes |
|---|---|---|
| `--config` | `data/dv_host.json` | Path to the container-exported config, relative to CWD. |
| `--db` | `<repo-root>/data/dv_host.db` (resolved from the script's own location, not CWD) | The detector's own SQLite store. Matches where the container looks for it — see below. |
| `--api` | `http://localhost:9721` | Base URL the script POSTs the import trigger to after a scan. |

Run it from the repo root (`X:\Docker Apps\ScanHound`) so the `--config` relative default
resolves correctly (`--db` and `--api` already default to the right place regardless of
CWD):

```
python scripts\host-detector\dv_host_scan.py
```

### `--db` default — matches the container's mount, override only if needed

`--db` defaults to `<repo-root>/data/dv_host.db`, computed from the script's own file
location (`scripts/host-detector/dv_host_scan.py` -> `parents[2]` == repo root), not
CWD. That's the same file the container reads: `POST /rename/dv-import` reads
`host_db_path` from the request body, defaulting to the `SCANHOUND_DV_HOST_DB`
environment variable, which itself defaults to `/data/dv_host.db` inside the container
— i.e. `X:\Docker Apps\ScanHound\data\dv_host.db` on the host, via the same
`./data:/data` bind mount as the config file. `docker-compose.yml` does not set
`SCANHOUND_DV_HOST_DB`, so that container-side default is what's actually in effect, and
the script's default now matches it out of the box.

**Only pass `--db` explicitly** if you need the store somewhere else (e.g. a one-off test
run) — in that case also pass the matching `host_db_path` to `/rename/dv-import` (or set
`SCANHOUND_DV_HOST_DB`) so the file the script writes is still the file the import
endpoint reads. Pointing `--db` at a path the container can't also resolve will make
`/rename/dv-import` find nothing there, silently returning `{"imported": 0, "updated": 0}`.

## Ordering (the walk -> import -> sync -> Kometa chain)

The nightly run must happen in this exact order:

1. **Walk + tag** — `python dv_host_scan.py` recurses each root, skips files whose
   signature is unchanged (mtime within `DV_MTIME_TOL` = 2.0s AND same size), runs
   `dovi_tool` on the rest, upserts `dv_host.db`, and (if `dv_file_tagging`) writes the
   MKV track name then re-stats + re-upserts the post-tag signature.
2. **Import** — the script's own `main()` does this automatically as its last step:
   after the walk it POSTs the import trigger to `{--api}/rename/dv-import` (bridging the
   store into the container; the container is the sole `crawler.db` owner, and this
   upserts `dv_scan` `source='scan'`). The request body is `{}`, so the endpoint falls
   back to its own default (`SCANHOUND_DV_HOST_DB`, effectively `/data/dv_host.db` in the
   container == `<repo-root>/data/dv_host.db` on the host) — which matches the script's
   `--db` default from the same repo root, so no manual trigger is needed in the common
   case. If you ran the scan with a non-default `--db`, trigger the import manually with
   a matching `host_db_path` instead of relying on the script's internal call:
   ```
   curl -X POST http://localhost:9721/rename/dv-import -H "Content-Type: application/json" -d "{\"host_db_path\": \"data/dv_host.db\"}"
   ```
   (the `host_db_path` in the body should point at wherever `--db` above actually wrote
   the file — pass an absolute path if the curl's CWD differs from the repo root).
3. **Sync labels** — trigger from the ScanHound UI ("Sync Plex labels") or
   `curl -X POST http://localhost:9721/rename/dv-sync-labels -H "Content-Type: application/json" -d "{}"`.
4. **Kometa** — runs on its own schedule; it badges the labels applied in step 3.
   A mis-ordered Kometa run overlays stale labels until the next pass.

## Rollout gate — clear this BEFORE the first real label sync

> **STATUS 2026-08-04 -- this gate is CLEARED.** The mapping table below was
> populated on 2026-07-11 (`Y:` -> the 4K HDR Geronimo share) after a dry run
> matched only 92 of 463 files without it, and the first real label sync ran on
> 2026-07-26. The section is kept because it explains WHY the table matters and
> what to do when a new root is added -- but the phrase "ships empty by design"
> below describes the ORIGINAL state, not today's.

`backend/rename/dv_paths.py`'s `DEFAULT_DV_MAPPINGS` (the drive-letter <-> UNC-path
table `normalize_path()` uses to recognize that e.g. `Y:\Movies\A\f.mkv` and
`\\SRV\Share\Movies\A\f.mkv` are the same physical file) originally shipped **empty by design**:

```python
# (drive_root, unc_root) pairs, e.g. ("Y:", r"\\SRV\Share"). Both roots must
# point at the SAME physical storage. Empty by default — populated from
# dv_label_vocab/config or the dry-run sampling gate (design §7.4).
DEFAULT_DV_MAPPINGS: List[Tuple[str, str]] = []
```

With an empty table, `/rename/dv-sync-labels` still runs, but path matching between the
host detector's paths (drive letters, since `dovi_tool.exe` runs against locally-mapped
drives) and Plex's served paths (which may be UNC, or a different drive letter than the
host detector used) only succeeds where the two happen to already be textually
identical. Anything reachable only via a different drive letter or a UNC share will
silently fail to match — `pick_layer` returns `None` for that title.

**What happens to an unmatched title depends on `additive_only`, and the default
changed.** `DvSyncRequest.additive_only` (`backend/api/routes/rename.py`) now defaults
to **`true`**, which inverts what this section used to describe:

- **Default (`additive_only` omitted, as in the `curl` examples below):** the title is
  left **strictly alone**. `is_authoritative(None)` is `False` and
  `may_remove = authoritative or not additive_only` is therefore `False`, so no label is
  added and **none is removed**. The failure is silent in the other direction: the title
  simply never gets its labels applied or refreshed, and it drops out of the `matched`
  count (`matched` is the *authoritative* flag, not "a row was found").
- **`{"additive_only": false}` — destructive reconciliation, which you have to ask
  for:** `may_remove` is `True` for an unmatched title, it is treated as "no detected
  layer," and **every existing managed label on it is removed** rather than confirmed —
  **with one exception, `HDR10`.**

  `HDR10` is the only managed label the `dv_scan` verdict does not settle on its own:
  it also needs Plex's own wide-gamut flag, read from the Plex HDR cache. **Whenever
  that flag is unknown for a title, `HDR10` is neither added nor removed** — the
  title keeps whatever `HDR10` state it already had, in destructive
  mode as much as in the default. Unknown is not "not HDR", and a cache gap must never
  be allowed to strip a correct `HDR10` label.

  Three things make it unknown, and **a first run is likely to be in one of them:**

  1. The title has no row in the Plex HDR cache yet.
  2. The cache read failed outright. The log line is `HDR index unavailable; HDR10
     labels left untouched`, and the **whole run** then proceeds with no HDR index at
     all, so every title is in this state.
  3. The database in use has no HDR cache to consult (older builds and test doubles),
     which disables `HDR10` handling rather than guessing.

  So: populate and verify the Plex HDR cache first if you want `HDR10` reconciled too.
  Otherwise expect every other managed label to be reconciled and `HDR10` to sit
  exactly where it already is.

So a bad mapping table under the default under-applies labels; under
`additive_only: false` it strips them — all of them except `HDR10`, which is left alone
wherever Plex's HDR flag is unknown.

**Before running `/rename/dv-sync-labels` against your real library for the first
time:**

1. **Populate the mapping table.** For every drive letter `dv_host_scan.py` walks that
   Plex might reference by a different path (UNC share, different letter, etc.), add a
   `(drive_root, unc_root)` pair. This currently means editing
   `DEFAULT_DV_MAPPINGS` in `backend/rename/dv_paths.py` directly (there is no settings
   UI for it yet — `normalize_path()`/`sync_labels()` both accept a `mappings=` override
   parameter for future config-driven wiring, but nothing currently populates one from
   config at runtime).
2. **Run a dry-run sample verification.** Call the sync endpoint with `dry_run: true`
   first:
   ```
   curl -X POST http://localhost:9721/rename/dv-sync-labels -H "Content-Type: application/json" -d "{\"dry_run\": true}"
   ```
   This runs the reconciliation (including path normalization against your mapping
   table) but skips every `pm.add_label`/`pm.remove_label` write. Note that both `curl`
   examples here omit `additive_only`, so both take the **safe** default above: an
   unmatched title is skipped, not stripped. Check the
   `dv:sync_done` WebSocket payload / the resulting notification's `matched` count
   against your actual library size, and spot-check a handful of titles that you know
   live behind a UNC share or a non-default drive letter to confirm they show up as
   matched rather than silently dropped.
3. Only after the dry-run sample looks correct, run the same call with
   `dry_run: false` (or omit `dry_run` — it defaults to `false`) to write labels for
   real.

Skipping this gate on a library with any drive/UNC path skew fails **silently** rather
than loudly, so treat it as a hard precondition, not an optional check. Under the
default it under-applies: the skewed titles are quietly skipped and keep whatever labels
they already carry, correct or not, while the `matched` count looks plausible because
nothing errored. Send `additive_only: false` on a library in that state and it desyncs
instead (remove-then-miss-re-add) — the destructive outcome is reachable, it just is not
the default any more.

## Task Scheduler setup

Create a nightly task (Task Scheduler > Create Task):

- **General:** Run whether user is logged on or not.
- **Triggers:** Daily, e.g. 03:00.
- **Actions:** Start a program — `powershell.exe` with arguments:
  ```
  -NoProfile -Command "$env:PATH = 'C:\path\to\host-detector;' + $env:PATH; python 'X:\Docker Apps\ScanHound\scripts\host-detector\dv_host_scan.py' --config 'X:\Docker Apps\ScanHound\data\dv_host.json' --api http://localhost:9721"
  ```
  The `$env:PATH` prefix is what makes `dovi_tool.exe` resolvable under the stripped
  scheduled environment. `--db` is omitted here because its default already resolves to
  `X:\Docker Apps\ScanHound\data\dv_host.db` (relative to the script's own location, not
  CWD), and the script's internal `_post_import()` call already POSTs to
  `/rename/dv-import` with no `/api` prefix — so the scan-then-import chain runs entirely
  inside the script with no separate `Invoke-WebRequest` step required. Pass `--db`
  explicitly only if you want the store somewhere other than the shared `data\` folder
  (see the "`--db` default" note above for the caveat that entails).

## Never touches `crawler.db`

The script opens only `dv_host.db`. It must **not** import ScanHound's
`DatabaseManager` (its `__init__` runs DDL/`user_version` writes; a second
DDL-running process is what corrupted the DB previously). It reuses only
`dv_detect.detect_layer` for classification.
