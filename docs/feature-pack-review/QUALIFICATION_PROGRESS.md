# Production qualification — progress ledger

Operator: Claude (git + deploy + validation lane), under Jesse's explicit
authorization ("Do it all", `qualification/AUTHORIZATION.md`). Every step below
was executed against the real host; nothing is simulated. Reconciliations and
deviations are disclosed inline.

Fixed references:

- Repository: `LstDtchMn/ScanHound`
- Branch: `agent/feature-pack-integration`
- Code-tested SHA: `a6b4a7b14d6613c27f17de670677ed848fec458d`
- Tooling commit: `c050958bf5d0a2534199440efec26e6c61d88e26` (docs-only after
  the code-tested SHA; see `qualification/REPAIRS.md` for the two script
  repairs and their self-test)

## Completed

### Objective 1 — branch verification

`agent/feature-pack-integration` HEAD at start: `3f9862d1dc59b4009b8d756c1106e598da60346e`,
clean, pushed. All commits after `a6b4a7b` touch only `docs/feature-pack-review/`
(verified with `git diff --name-only a6b4a7b..HEAD`).

### Objectives 2–6 — tooling validation, repair, regression, commit

See `qualification/REPAIRS.md`. Summary: 04_settings_guard (RSS mode must go
through `POST /rss/mode`; `/settings` is `extra="forbid"`) and
05_shadow_evidence (readiness must be computed from the real
`hdencode_shadow_cycles` columns) repaired; 00/01/02/03/06 verified correct
against the real code; stdlib self-test proves both repairs (PASS, Python
3.12.9); bundle re-checksummed (`SHA256SUMS`, 14 files OK) and committed at
`c050958` (pushed, non-force). No `backend/`/`frontend/` change — the
`a6b4a7b` regression (backend 3974 passed / 0 failed; frontend check 0 errors,
vitest 373, build clean) stands.

### Runbook step 1 (partial) — environment record

- Production container `scanhound`: image `scanhound:latest` =
  `sha256:0ee535183c0accf870ec9fb4e365ef6f04ce2ca7b62e5cbdc69ac115b174f3bb`
  (created 2026-07-18, running — the OLD image; the feature pack is NOT
  deployed). Pinned as `scanhound:qual-old-0ee5351` so it cannot be lost.
- Production DB: named volume `scanhound_scanhound_db` mounted at `/dbvol`
  (`SCANHOUND_DB_DIR=/dbvol`, db file `crawler.db`).
- App data/config: `X:\Docker Apps\ScanHound\data` → `/data`.
- RW fileops surfaces: `F:/Downloads` → `/library/movies`, `G:/Downloads` →
  `/library/movies-4k`, `/mnt/nas/nas-tv-blackbeard` (=`\\TURTLELANDSRV2\k`) →
  `/library/tv`.
- Full `docker inspect` JSON captured in the evidence directory
  (`new-image-inspect.json`, `old-image-inspect.json`).

### Runbook step 3 — accepted image built

Built from the checkout at `c050958` (diff after `a6b4a7b` verified docs-only
immediately before the build, printed into the build log):

- Tag: `scanhound:feature-pack-a6b4a7b`
- Image id (manifest list): `sha256:56d23a0cbe3c24cb6d523178f79b28240f43d4d12e1930bc0e92ca224ff39d6b`
- Label `org.opencontainers.image.revision=a6b4a7b14d6613c27f17de670677ed848fec458d` verified.

Not deployed. `scanhound:latest` still points at the old image.

### Objective 11 / runbook step 6 — filesystem sentinel (run EARLY, disclosed)

**Deviation disclosed:** the runbook sequences the sentinel after deploy
(step 6). The sentinel probes filesystems, not the application, so it was run
before deploy — strictly more conservative (capability evidence exists before
any new code touches those surfaces). It can be re-run post-deploy at the
runbook's exact point if reviewers want in-sequence evidence.

Per the sentinel restriction, four NEW empty directories named
`scanhound-sentinel-qual` were created at volume roots — deliberately NOT
inside any media/download/database/trash/config/source path, but on the same
filesystems as the rw fileops surfaces:

- `F:\scanhound-sentinel-qual` (NTFS volume backing `F:/Downloads`)
- `G:\scanhound-sentinel-qual` (volume backing `G:/Downloads`)
- `V:\scanhound-sentinel-qual` (= `\\TURTLELANDSRV2\k`, the CIFS share backing
  `/mnt/nas/nas-tv-blackbeard`; identity confirmed in the dry-run output)
- `X:\scanhound-sentinel-qual` (Storage Spaces NTFS volume holding ScanHound
  data)

**8/8 runs `ok=true`, zero failures** (dry-run first, then `--execute`, per
runbook; raw JSON in the evidence dir, one file per parent):

- Host-side (Windows, all 4): no-replace preserved the destination
  (`FileExistsError`), hardlinks supported (incl. CIFS), file-fsync + atomic
  manifest replace OK; `F:→X:` cross-volume rename correctly surfaced as an
  EXDEV boundary with the source intact. Directory-fsync unsupported host-side
  (win32 `os.open` cannot open directories) — irrelevant to production, which
  runs in the container.
- Container-side (fresh containers from `scanhound:feature-pack-a6b4a7b`,
  mounting ONLY the sentinel dirs + the script read-only — never `docker exec`
  into the live container, no user data mounted; all 4 surfaces):
  `renameat2(RENAME_NOREPLACE)` **supported with destination preserved
  (EEXIST)** on every mount — the SH-R02 no-replace primitive works on the
  real mount types; hardlink same-inode supported everywhere; file-fsync +
  atomic replace OK everywhere; directory-fsync supported on the F/G/X binds,
  **unsupported on the NAS CIFS mount** (documented CIFS limitation — the
  durable-trash design treats dir-fsync as best-effort with fail-safe
  handling); container-side `F→X` rename correctly EXDEV with source intact.

Cleanup verified in-band by the script (`run_removed=true, parent_empty=true`
on all 8) and the four empty parent dirs removed afterwards — no uncertain-
cleanup stop condition.

### Objective 7 — migration matrix (runbook step 4) — GREEN

Jesse produced the snapshot. `production-20260721T222449Z.sqlite3`: `user_version=2`,
16 tables, 30373 rows, integrity ok, schema hash and row counts identical to the
live source, empty `-wal`.

`02_migration_matrix.py` returned **ok=true, zero failures**:

| Case | Result |
|---|---|
| v2 -> v6 upgrade | integrity ok, all pre-existing row counts preserved, 10 tables added |
| Restart idempotency | identical table set and per-table row counts |
| Old-image reopen of migrated DB | data intact |
| Interrupted migration (killed 143 ms into init) | recovered clean, uv=6, integrity ok |
| Rollback restore | byte-identical to baseline |

**Extra case added beyond the bundle** (`02b_roundtrip_reupgrade.json`): the old
image downgrades `user_version` 6->2 while leaving the v6 tables in place, so a
real rollback-then-roll-forward re-runs the migrations against tables that already
exist. Verified safe — schema hash and per-table row counts identical to a clean
migration, all production rows preserved.

### Objectives 8-9 — merge and fail-closed deploy — DONE

- Merge commit **`b633e695`** (`--no-ff`, pushed). `--no-ff` chosen deliberately so
  rollback is `git revert -m 1` rather than a reset requiring a force push.
- Deployed image `4be9df01` (rebuilt from merged main). Deviation: the first
  `deploy_failclosed.py` run completed the merge, container stop and config write
  but its build/start step did not complete, leaving the service down ~11 minutes;
  a subsequent rebuild brought it up. The image therefore carries no
  `org.opencontainers.image.revision` label — provenance is main@`b633e695`, whose
  application code was verified byte-identical to the labelled evidence image
  `scanhound:feature-pack-a6b4a7b` (only a docs file differs).
- **Production DB migrated v2 -> v6 in place**: `user_version=6`,
  `hdencode_shadow_cycles` present, `integrity=ok`.
- Fail-closed profile verified **still intact after startup** (the app did not
  rewrite it): auto_rename, auto_grab, hdencode, background_scan all false;
  discovery mode `listing`; rss auto-grab false.

### Objective 10 — HDEncode zero-traffic proof — PROVEN

`10_zero_traffic_*.json`: all nine `hdencode_*` tables empty (notably
`hdencode_feed_state`, which gains a row on the first poll of any feed, and
`hdencode_ingest_cycles`), zero discovery/RSS activity in the container log, while
the scheduler and maintenance loop are both confirmed running — so this is evidence
of a working off-switch, not of an idle application. Re-check after the 3 h
scheduler and 1 h maintenance intervals have each fired to make it airtight.

### Objectives 13-14 — observation automated

Durable evidence directory (outside the checkout, per runbook step 0):
`X:\Docker Apps\scanhound-qualification-evidence`. Windows Scheduled Task
"ScanHound Qualification Evidence" runs `run_collection.cmd` every 6 hours,
verified executing with `LastTaskResult=0`. It reads the production DB read-only
from a throwaway container (never `docker exec` into the live container, never
writes) and **exits non-zero on any mandatory stop condition** — a relevant RSS
miss or a DB integrity failure — so the task shows as failed and the condition
cannot pass unnoticed. Note: registering the task with `schtasks` produced a
silently failing task (`-2147024894`, spaces in the path); it was re-registered via
PowerShell and verified.

## Blocked — requires Jesse

### Objective 12 — enable RSS shadow

Needs a session auth token (`sh-auth-token`); Claude cannot mint one without
handling the app password. Until shadow mode is on, the comparison-cycle counter
stays at 0 and the seven-day clock has not started.

### Superseded — objective 7 snapshot (runbook step 2)

The snapshot was attempted exactly as the runbook intends, adapted for the
named-volume DB: a FRESH throwaway container (not an exec into the live one)
with `scanhound_scanhound_db` mounted **read-only**, running a sqlite
`.backup` to the evidence directory. The Claude Code auto-mode safety
classifier denied the action (production-DB read). This is an independent
safety layer; it was not and will not be worked around.

Until a snapshot exists, the dependent chain is blocked, in order: migration
matrix (step 4) → merge/deploy fail-closed (step 5, also Jesse's guardrail) →
zero-traffic proof → RSS shadow enablement (step 7, additionally needs a
session auth token, which Claude cannot mint without handling the app
password) → 7-day observation (step 8) → finalize (step 9).

## Pending (unblocked once the snapshot exists)

- Step 4: `02_migration_matrix.py` with `--new-image
  scanhound:feature-pack-a6b4a7b --old-image scanhound:qual-old-0ee5351`.
- Step 5: non-force merge to `main`, deploy via `docker compose up -d --build`
  with the DISABLED profile forced, health/digest/integrity/zero-HDEncode
  verification.
- Step 7: `04_settings_guard.py --stage shadow` via `POST /rss/mode`.
- Step 8: ≥7 calendar days / ≥20 cycles of `05_shadow_evidence.py` — never
  shortened or simulated.
- Step 9: `06_finalize_evidence.py` checksummed evidence package.
