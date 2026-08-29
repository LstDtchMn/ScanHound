# First supervised run of the deploy script

**Precondition:** the ops branch is merged to main. **You are present.** The
whole thing takes about 20 minutes, most of it the build and the three-minute
log watch.

## Why this run is safe

- It redeploys `origin/main`. The intent is **no intentional runtime-feature
  change** — the only difference is *which tool* did the deploy. It is **not**
  a claim that `origin/main` is the commit currently running: the live container
  was deployed by hand, the repo cannot prove which commit that was, and once
  the ops PR merges, `origin/main` is by definition no longer it. **Before you
  start:** read the SHA the rehearsal prints, confirm it is the commit you
  expect, and check whatever provenance you actually have for what is running
  (the container's image id and `StartedAt`) rather than assuming they match.
- The build happens under a candidate tag, so `scanhound:latest` is untouched
  through the whole build and through the candidate's qualification.
- Promotion is a **transaction**, not a final step. Once the candidate passes
  every check the tag moves *provisionally*, because the last activation has to
  run against the plain recipe that names it. If that activation — or the
  container it leaves running — fails, the script puts the **previous image
  back** before it releases the recovery lock, and the ledger says
  `promotion_state = promoted, then REVERTED to the prior image`. So any run
  that does not print VERIFIED leaves `scanhound:latest` on the last verified
  image, and a recovery recreate is a real rollback.
  - The one exception, stated because the script states it: on a **first-ever**
    deploy there is no previous image. If such a run fails after promotion the
    script says so plainly — there is nothing to roll back to, and the next
    step is to fix the failure and deploy again.
- Worst case is a few minutes of ScanHound downtime, ended by one command
  (printed by the script itself on any failure).

## Step 1 — the rehearsal (no production change of any kind)

```bash
powershell -ExecutionPolicy Bypass -File "X:\Docker Apps\ScanHound\scripts\merge-and-deploy.ps1" -WhatIf
```

What you should see: every pre-flight check pass, the plan naming the commit
it *would* deploy, and `PLAN ONLY - no production state changed.` It does fetch
and prune git refs and create/remove a temporary worktree — no container,
image, or PR is touched.

**Abort the whole exercise if** the rehearsal names a commit you don't
recognise, or reports compose drift.

## Step 2 — the real run

```bash
powershell -ExecutionPolicy Bypass -File "X:\Docker Apps\ScanHound\scripts\merge-and-deploy.ps1" -SkipMerge
```

What happens, in order: clean-source build (~10 min, `latest` untouched) →
storage-identity probes → candidate activation → health poll (up to 2 min;
~63s is normal) → 3-minute log watch → promote *provisionally* → final
activation → the same checks again → commit the promotion → `VERIFIED`.
Anything that goes wrong from the promote onwards puts the previous image back
before the script lets go of the recovery lock.

## Success = the script prints VERIFIED and exits 0

Spot-check it told the truth:

```bash
curl -s http://127.0.0.1:9721/health
```

`status":"ok` and Plex connected — done. The script is production-proven.

## If it does NOT say VERIFIED

Read the ledger it prints — it states what is actually running, and
`promotion_state` says which of four things happened to `scanhound:latest`:
`never promoted`, `promoted, then REVERTED to the prior image`,
`promoted; NO PRIOR IMAGE existed to restore`, or `promoted; the REVERT FAILED`.
In the first two the tag names the last verified image and the command below is
a real rollback. In the last two it does not, and the script prints what to do
instead — follow that, not this section.

**One thing to check before you paste it.** If the script reported a STORAGE
failure (any `nas_*` line that is not `probed` / `0`), it says so and tells you
to run `scripts\mount-nas-shares.ps1` FIRST. Do that. The command below creates
a NEW container, and Docker resolves bind sources at container-create time, so
recreating while the NAS mounts are wrong binds `/library/tv` — the TV rename
destination — to an ordinary folder inside the VM. That is the 2026-07-26
outage.

Otherwise, if production is not serving, the rollback is the one command the
script prints, which recreates from the pinned recipe and the last verified
image:

```bash
docker compose -f "C:\ProgramData\ScanHound\deploy\docker-compose.yml" --project-directory "X:\Docker Apps\ScanHound" up -d --force-recreate --no-build --pull never
```

Then tell Claude what the ledger said. A failed first run is a finding, not an
emergency — the old image is still on disk, and unless `promotion_state`
says otherwise the recovery task points at that image and nothing else.
