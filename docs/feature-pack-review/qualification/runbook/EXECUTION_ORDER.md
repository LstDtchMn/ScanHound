# Execution order

Follow this sequence exactly. Stop immediately on any mandatory stop condition.

## 0. Place bundle on real server

Copy this directory outside the ScanHound checkout and outside all production
data directories. Create a durable evidence directory.

## 1. Preflight

```bash
python scripts/00_preflight.py   --project "$SCANHOUND_PROJECT_DIR"   --db "$SCANHOUND_DB_PATH"   --config "$SCANHOUND_CONFIG_PATH"   --evidence-dir "$SCANHOUND_EVIDENCE_DIR"
```

Do not continue unless `ok` is true.

Record the running container and image:

```bash
docker ps --no-trunc
docker inspect <current-container> > "$SCANHOUND_EVIDENCE_DIR/current-container-inspect.json"
docker image inspect "$SCANHOUND_OLD_IMAGE" > "$SCANHOUND_EVIDENCE_DIR/old-image-inspect.json"
```

## 2. Snapshot production DB/config

```bash
python scripts/01_snapshot_db.py   --source "$SCANHOUND_DB_PATH"   --evidence-dir "$SCANHOUND_EVIDENCE_DIR"

cp --preserve=all "$SCANHOUND_CONFIG_PATH" "$SCANHOUND_EVIDENCE_DIR/config-before.json"
sha256sum "$SCANHOUND_EVIDENCE_DIR/config-before.json"   > "$SCANHOUND_EVIDENCE_DIR/config-before.sha256"
```

Production remains on the old image.

## 3. Build accepted image

```bash
cd "$SCANHOUND_PROJECT_DIR"
git rev-parse HEAD
git diff --name-only a6b4a7b14d6613c27f17de670677ed848fec458d..HEAD
docker build   --label org.opencontainers.image.revision=a6b4a7b14d6613c27f17de670677ed848fec458d   -t "$SCANHOUND_NEW_IMAGE" .
docker image inspect "$SCANHOUND_NEW_IMAGE"   > "$SCANHOUND_EVIDENCE_DIR/new-image-inspect.json"
```

Everything after the code-tested SHA must be evidence/documentation only.

## 4. Migration matrix

Use the snapshot path from `01_snapshot.json`.

```bash
python scripts/02_migration_matrix.py   --snapshot /path/from/01_snapshot.json   --evidence-dir "$SCANHOUND_EVIDENCE_DIR"   --new-image "$SCANHOUND_NEW_IMAGE"   --old-image "$SCANHOUND_OLD_IMAGE"
```

Do not merge or deploy unless `ok` is true.

## 5. Merge/deploy fail-closed

Use the normal non-force workflow. On first startup force:

- `auto_rename_enabled=false`
- `auto_grab_enabled=false`
- `hdencode_enabled=false`
- `hdencode_discovery_mode=listing`
- `hdencode_rss_auto_grab_enabled=false`
- `background_scan_enabled=false`

```bash
python scripts/04_settings_guard.py   --base-url "$SCANHOUND_BASE_URL"   --token "$SCANHOUND_AUTH_TOKEN"   --stage disabled   --evidence-dir "$SCANHOUND_EVIDENCE_DIR"   --execute
```

Verify health, image digest, DB integrity, and zero HDEncode traffic.

## 6. Sentinel

For every approved path, dry-run first:

```bash
python scripts/03_filesystem_sentinel.py   --parent /approved/scanhound-sentinel   --evidence-dir "$SCANHOUND_EVIDENCE_DIR"
```

Then execute only after verifying it is a dedicated empty directory:

```bash
python scripts/03_filesystem_sentinel.py   --parent /approved/scanhound-sentinel   --secondary-parent /approved-other/scanhound-sentinel   --evidence-dir "$SCANHOUND_EVIDENCE_DIR"   --execute
```

Never use an existing media, download, database, trash, config, or source path.

## 7. Enable RSS shadow only

```bash
python scripts/04_settings_guard.py   --base-url "$SCANHOUND_BASE_URL"   --token "$SCANHOUND_AUTH_TOKEN"   --stage shadow   --evidence-dir "$SCANHOUND_EVIDENCE_DIR"   --execute
```

Auto-rename, general auto-grab, RSS auto-grab, and RSS-primary must remain off.

## 8. Seven-day observation

Run at least daily and after every restart:

```bash
python scripts/05_shadow_evidence.py   --db "$SCANHOUND_DB_PATH"   --evidence-dir "$SCANHOUND_EVIDENCE_DIR"
```

Optionally add `--base-url "$SCANHOUND_BASE_URL" --token "$SCANHOUND_AUTH_TOKEN"`
to also capture the app's own `GET /rss/status` readiness and reconcile it
against this collector's independent, DB-derived computation.

Required:

- at least seven calendar days;
- at least 20 valid comparison cycles;
- zero relevant misses;
- positive request reduction;
- restart/catch-up recovery;
- healthy normal feeds;
- no uncontrolled fallback;
- no discovery-triggered link retrieval/download.

Any relevant miss is an immediate stop condition.

## 9. Finalize

```bash
python scripts/06_finalize_evidence.py   --evidence-dir "$SCANHOUND_EVIDENCE_DIR"
```

Return the full evidence directory to ChatGPT and Claude for final independent
reconciliation before RSS-primary, any auto-grab, or Auto-rename enablement.
