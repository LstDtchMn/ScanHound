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

## Blocked — requires Jesse

### Objective 7 — production DB snapshot (runbook step 2)

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
