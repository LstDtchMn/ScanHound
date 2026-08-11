# Security/correctness audit request — the live DV detector enablement (ops, not code)

**Repository:** `LstDtchMn/ScanHound`  **Host:** TurtleLandSRVR (.170)  **Date:** 2026-08-11
**Scope:** the *deployment/ops* that turned the DV detector on. The ingest-key CODE is already
merged (`main` `9227578`) and was reviewed over three rounds. This audit is the host + compose
changes, which are NOT fully in git.

## What was changed (exact)

**1. Container port publish (docker-compose.yml, uncommitted local edit).**
```yaml
    ports:
      - "127.0.0.1:9721:9721"   # loopback ONLY; verified: `docker port scanhound` -> 127.0.0.1:9721
```
Rationale: the host-side detector posts to `http://127.0.0.1:9721`; the app was previously only on
the `proxy` docker network (NPM/Cloudflare), unreachable from a host process. Bound to `127.0.0.1`
so it is not on the LAN. The app still requires auth (a session token or the scoped ingest key) on
that port — publishing it does not remove auth.

**2. Ingest key config (docker-compose.yml, uncommitted local edit).**
```yaml
    environment:
      - SCANHOUND_DV_INGEST_KEY_SHA256=b898…c6c4   # SHA-256 of the secret; not reversible
```
Both compose edits are uncommitted; a working backup is at
`C:\DockerData\scanhound\docker-compose.deployed-20260811.yml`. (A separate task tracks committing
the port line generically + moving the hash to an untracked `.env`.)

**3. The raw secret on the host.** `C:\DockerData\scanhound\dv_ingest_key.secret`, 43 ASCII bytes.
ACL (inheritance removed): `NT AUTHORITY\SYSTEM:(F)`, `BUILTIN\Administrators:(F)`,
`TURTLELANDSRVR\NLSur:(F)`. Verified: `sha256(secret) == the compose hash`.

**4. Scheduled task `ScanHound DV Detector`.**
- Principal: `NLSur` / **Interactive** / RunLevel **Highest**.
- Settings: `MultipleInstances=IgnoreNew`, `ExecutionTimeLimit=PT6H`, `StartWhenAvailable=True`.
- Trigger: every 6h. Action: `powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden
  -File "C:\DockerData\scanhound\run-dv-scan.ps1"`.
- Interactive = runs only while NLSur is logged in. A future switch to run-logged-off (stored
  password) is planned; see the roots finding below, which that switch depends on.

**5. Wrapper `C:\DockerData\scanhound\run-dv-scan.ps1` (current, post-fix):**
```powershell
$ErrorActionPreference='Stop'
$env:Path='C:\Tools;'+$env:Path
$env:SCANHOUND_DV_INGEST_KEY=(Get-Content -LiteralPath 'C:\DockerData\scanhound\dv_ingest_key.secret' -Raw).Trim()
if (-not (Test-Path 'Y:\')) {
  try { New-SmbMapping -LocalPath 'Y:' -RemotePath '\\TURTLELANDSRV2\4K HDR Geronimo' -Persistent $false -ErrorAction Stop | Out-Null } catch {}
  if (-not (Test-Path 'Y:\')) { cmd /c 'net use Y: "\\TURTLELANDSRV2\4K HDR Geronimo"' 2>&1 | Out-Null }
}
if (-not (Test-Path 'Y:\')) { Write-Error 'DV scan aborted: could not establish Y: mapping to the media share.'; exit 3 }
Set-Location -LiteralPath 'X:\Docker Apps\ScanHound'
& '<python312>' 'scripts\host-detector\dv_host_scan.py' --config '…\data\dv_host.json' --db '…\data\dv_host.db' --api 'http://127.0.0.1:9721' --mode backfill --max-runtime-minutes 300
```

**6. DV roots.** The app OWNS `data/dv_host.json` (`app_service.export_dv_host_config`, called on
startup), and its `dv_library_roots` are `Y:/…` (a per-session SMB mapping to
`\\TURTLELANDSRV2\4K HDR Geronimo`). Kept as `Y:` deliberately — `backend/rename/dv_paths.py`
canonicalizes `Y:`↔UNC to match the detector's recorded paths against Plex (without the `Y:` mapping
entry, 371 of 463 DV files silently lose their labels).

## Verified good

- `docker port scanhound` → `127.0.0.1:9721` (host-loopback only; not LAN-exposed).
- Canary over loopback with the real `X-DV-Ingest-Key`: `ok=true, processed=1, failed=0`; the exact
  tuple `(path,'fel',1.0,42,'DV enable canary','scan')` landed in `crawler.db` and was deleted.
- The detector's real `_post_rows` authenticated with the file-loaded key (`dv-host-rows OK -> True`).
- App healthy HTTP 200; migration schema v9 present; queue intact 313; secret hash matches.
- All four resolved UNC roots readable (730 files); `dovi_tool 2.3.1`; `dv_detect.available()==True`.

## FOUND AND FIXED during this audit prep — the roots/session bug

**Symptom:** I originally rewrote `dv_host.json` roots `Y:`→UNC (so a logged-off task could see them).
That was WRONG twice over: (a) the app re-exports its `Y:` config over my edit (doesn't persist);
(b) had it persisted, the detector would record UNC-form paths and **duplicate** all ~649 files
against the existing `Y:` rows in `dv_host.db`/`dv_scan`, and could confuse Plex label matching.

**Fix applied:** keep `Y:` roots (matching the app + existing data); the **wrapper now
re-establishes the `Y:` mapping in its own session** (New-SmbMapping, `net use` fallback) and
**aborts loudly (exit 3)** if it can't — so the *future logged-off* task will still see the media,
and a broken mapping fails visibly instead of silently scanning zero files.

**Not yet possible to test:** the logged-off path itself (needs the pending stored-password switch).
The mapping mechanism + NLSur share access are verified; the in-task-session mapping should be
confirmed on the first run after the password switch.

## Questions for the auditor

1. **Port:** is `127.0.0.1:9721:9721` genuinely host-only on Windows/Docker Desktop (WSL2 backend)?
   Any path by which the LAN, another container, or WSL could reach it? Auth still required either way.
2. **Secret at rest:** ACL to SYSTEM+Administrators+NLSur with inheritance removed — sufficient? The
   detector reads it into an env var in the task; the app configures only the SHA-256. Any leak
   surface (task command line is clean; wrapper reads the file, doesn't echo)?
3. **Task:** Interactive/Highest now; the planned stored-password switch stores NLSur's password in
   the task vault. Is that the right trade vs a least-privileged dedicated account (which would need
   its own SMB rights to `\\TURTLELANDSRV2`)? Any issue with `-WindowStyle Hidden` + `-ExecutionPolicy
   Bypass` running a wrapper off `C:\DockerData` (a non-Program-Files, user-writable path)?
4. **Roots fix:** is "wrapper maps `Y:` + abort-on-failure" the right call, or should the deployment
   instead migrate the existing `Y:` paths in `dv_host.db`/`dv_scan` to UNC and drop the mapped-drive
   dependency entirely (bigger, but no per-session drive at all)? Any correctness risk in keeping `Y:`?
5. **Anything else** in this deployment that's a security or correctness gap.

Merge/deploy/enable/prod-settings are Jesse's.

---

## Remediation (2026-08-11, Claude)

- **F1 (blocking) — CLOSED.** Wrapper now captures the detector output to
  `C:\DockerData\scanhound\dv-scan.log` and ends with `exit $LASTEXITCODE`.
  Proven: `python -c "sys.exit(7)"` through `powershell -File` yields outer exit 7,
  so a failed final POST now propagates to Task Scheduler `LastTaskResult`. The
  mapping-failure exits (3/4) are preserved. Full task-level forced-failure proof
  is deferred to the first logged-off verification run.
- **F4 — CLOSED.** Wrapper verifies `Y:` maps EXACTLY to
  `\TURTLELANDSRV2\4K HDR Geronimo` (via `Get-SmbMapping`, confirmed target) and
  aborts (exit 4) on a wrong target; establishes + re-verifies on a missing one.
- **F8 — MITIGATED.** The raw key is now set only after the net-use fallback, so no
  mapping subprocess inherits it. Detector children (dovi_tool) still inherit it;
  fully closing that is tied to F2 (trusted tool chain).
- **F5 — NON-ISSUE.** `X:` is a Fixed local volume (Storage Spaces), not a session
  mapping; no logged-off dependency.
- **F3 — RESOLVED.** Docker Engine server = 29.6.2 (>= 28.0.0), so the pre-28
  localhost-publish defect does not apply. App auth remains mandatory. Residual: a
  LAN negative-connection test (needs a second host) + the pre-existing proxy-net
  east-west path (any proxy container can reach scanhound:9721, auth still required).
- **F6 — TRACKED.** A spawned task covers committing the port line + moving the hash
  to an untracked `.env`; the hash is integrity-sensitive (protect it).
- **F7 — VERIFIED.** `dv_file_tagging=false` in the live config; the detector does not
  mutate media. A read-only media identity aligns with this (see F2).
- **F2 — OPEN, Jesse's decision.** Highest + a user-writable wrapper/tool chain is the
  remaining P0 before the stored-password/logged-off switch. Options: (a) dedicated
  non-admin batch account (RunLevel=Limited) + directory-ACL protection of the
  wrapper/C:\Tools/Python/compose; (b) interim: lock those directories to admins-only
  write so a medium-integrity process can't plant code, keeping the NLSur/Highest task;
  (c) accept the risk on a single-user box. Recommend (a) as the durable fix, done as
  part of the password-switch work.
