# First supervised run of the deploy script

**Precondition:** the ops branch is merged to main. **You are present.** The
whole thing takes about 20 minutes, most of it the build and the three-minute
log watch.

## Why this run is safe

- It redeploys `origin/main` — **the commit already running**, so the only
  change is *which tool* did the deploy.
- The build happens under a candidate tag; `scanhound:latest` is untouched
  until every check has passed, so the scheduled recovery task can only ever
  restore the known-good image.
- Worst case is a few minutes of ScanHound downtime, ended by one command
  (printed by the script itself on any failure).

## Step 1 — the rehearsal (no production change of any kind)

```bash
powershell -ExecutionPolicy Bypass -File "X:\Docker Apps\ScanHound\scripts\merge-and-deploy.ps1" -WhatIf
```

What you should see: every pre-flight check pass, the plan naming the commit
it *would* deploy, and `PLAN ONLY nothing was changed.` It does fetch git refs
and create/remove a temporary folder — no container, image, or PR is touched.

**Abort the whole exercise if** the rehearsal names a commit you don't
recognise, or reports compose drift.

## Step 2 — the real run

```bash
powershell -ExecutionPolicy Bypass -File "X:\Docker Apps\ScanHound\scripts\merge-and-deploy.ps1" -SkipMerge
```

What happens, in order: clean-source build (~10 min, `latest` untouched) →
storage-identity probes → candidate activation → health poll (up to 2 min;
~63s is normal) → 3-minute log watch → promote → final activation → the same
checks again → `VERIFIED`.

## Success = the script prints VERIFIED and exits 0

Spot-check it told the truth:

```bash
curl -s http://127.0.0.1:9721/health
```

`status":"ok` and Plex connected — done. The script is production-proven.

## If it does NOT say VERIFIED

Read the ledger it prints — it states what is actually running and whether
`scanhound:latest` was promoted. If production is not serving, the rollback is
the one command the script prints, which recreates from the pinned recipe and
the last verified image:

```bash
docker compose -f "C:\ProgramData\ScanHound\deploy\docker-compose.yml" --project-directory "X:\Docker Apps\ScanHound" up -d --force-recreate --no-build --pull never
```

Then tell Claude what the ledger said. A failed first run is a finding, not an
emergency — the old image is still on disk and the recovery task knows only
that image.
