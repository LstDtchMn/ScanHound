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
| `--db` | `scripts/host-detector/dv_host.db` | The detector's own SQLite store. **See the path-mismatch warning below.** |
| `--api` | `http://localhost:9721` | Base URL the script POSTs the import trigger to after a scan. |

Run it from the repo root (`X:\Docker Apps\ScanHound`) so the relative defaults resolve
correctly:

```
python scripts\host-detector\dv_host_scan.py --config data\dv_host.json --db data\dv_host.db --api http://localhost:9721
```

### `--db` path mismatch — pass it explicitly

The script's own `--db` default (`scripts/host-detector/dv_host.db`, relative to CWD)
does **not** match where the container looks for it. `POST /rename/dv-import` reads
`host_db_path` from the request body, defaulting to the `SCANHOUND_DV_HOST_DB`
environment variable, which itself defaults to `/data/dv_host.db` inside the container
— i.e. `X:\Docker Apps\ScanHound\data\dv_host.db` on the host, via the same
`./data:/data` bind mount as the config file. `docker-compose.yml` does not set
`SCANHOUND_DV_HOST_DB`, so that container-side default is what's actually in effect.

**Always pass `--db data\dv_host.db`** (or an equivalent absolute path under
`X:\Docker Apps\ScanHound\data\`) so the file the script writes is the same file the
import endpoint reads. If you use the script's bare default instead, `dv_host.db` lands
in `scripts\host-detector\` and `/rename/dv-import` will find nothing there, silently
returning `{"imported": 0, "updated": 0}`.

## Ordering (the walk -> import -> sync -> Kometa chain)

The nightly run must happen in this exact order:

1. **Walk + tag** — `python dv_host_scan.py` recurses each root, skips files whose
   signature is unchanged (mtime within `DV_MTIME_TOL` = 2.0s AND same size), runs
   `dovi_tool` on the rest, upserts `dv_host.db`, and (if `dv_file_tagging`) writes the
   MKV track name then re-stats + re-upserts the post-tag signature.
2. **Import** — the action then bridges the store into the container by POSTing the
   import trigger (the container is the sole `crawler.db` owner; this upserts `dv_scan`
   `source='scan'`). **Known bug:** the script's own `_post_import()` currently builds
   this request against `/api/rename/dv-import`, but the router only mounts at bare
   `/rename` (`APIRouter(prefix="/rename", ...)` in `backend/api/routes/rename.py`,
   included with no additional `/api` prefix in `backend/api/main.py`) — so the script's
   automatic POST 404s. Until the script is patched, trigger the import manually or via
   the scheduled task's own action step instead of relying on the script's internal call:
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

`backend/rename/dv_paths.py`'s `DEFAULT_DV_MAPPINGS` (the drive-letter <-> UNC-path
table `normalize_path()` uses to recognize that e.g. `Y:\Movies\A\f.mkv` and
`\\SRV\Share\Movies\A\f.mkv` are the same physical file) ships **empty by design**:

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
silently fail to match — `pick_layer` returns `None` for that title, it's treated as
"no detected layer," and any existing managed label on it gets removed rather than
confirmed.

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
   This performs the full reconciliation (including path normalization against your
   mapping table) but skips every `pm.add_label`/`pm.remove_label` write. Check the
   `dv:sync_done` WebSocket payload / the resulting notification's `matched` count
   against your actual library size, and spot-check a handful of titles that you know
   live behind a UNC share or a non-default drive letter to confirm they show up as
   matched rather than silently dropped.
3. Only after the dry-run sample looks correct, run the same call with
   `dry_run: false` (or omit `dry_run` — it defaults to `false`) to write labels for
   real.

Skipping this gate on a library with any drive/UNC path skew will desync labels
(remove-then-miss-re-add) rather than error loudly, so treat it as a hard precondition,
not an optional check.

## Task Scheduler setup

Create a nightly task (Task Scheduler > Create Task):

- **General:** Run whether user is logged on or not.
- **Triggers:** Daily, e.g. 03:00.
- **Actions:** Start a program — `powershell.exe` with arguments:
  ```
  -NoProfile -Command "$env:PATH = 'C:\path\to\host-detector;' + $env:PATH; python 'X:\Docker Apps\ScanHound\scripts\host-detector\dv_host_scan.py' --config 'X:\Docker Apps\ScanHound\data\dv_host.json' --db 'X:\Docker Apps\ScanHound\data\dv_host.db' --api http://localhost:9721; Invoke-WebRequest -Method POST -Uri http://localhost:9721/rename/dv-import -Body '{\"host_db_path\": \"X:\\Docker Apps\\ScanHound\\data\\dv_host.db\"}' -ContentType 'application/json'"
  ```
  The `$env:PATH` prefix is what makes `dovi_tool.exe` resolvable under the stripped
  scheduled environment. The explicit `--db`/`host_db_path` absolute paths route around
  both the `--db` default mismatch and the script's own `/api/rename/dv-import` bug
  (see "Ordering" above) by having the scheduled task itself make the correct POST,
  rather than depending on the script's internal `_post_import()` call.

## Never touches `crawler.db`

The script opens only `dv_host.db`. It must **not** import ScanHound's
`DatabaseManager` (its `__init__` runs DDL/`user_version` writes; a second
DDL-running process is what corrupted the DB previously). It reuses only
`dv_detect.detect_layer` for classification.
