<#
.SYNOPSIS
    Merge approved PRs, rebuild ScanHound, and prove the intended artifact is
    the one running.

.DESCRIPTION
    This is the PRODUCTION WRAPPER. It holds the real identities and nothing
    else; the engine is scripts/deploy-core.ps1, which is parameterised so the
    same code can be executed against a disposable Docker fixture by
    tests/test_deploy_core_docker.ps1.

    That split exists because of what the second review found: every deploy
    invariant the rewrite added had been reasoned about and none had been
    executed. Hard-coding production identities in the engine meant the only
    way to exercise it was to point it at production. Now the fixture supplies
    its own project, container, image and port, and the destructive paths run
    for real against something disposable.

    WHAT THIS PROVES, and what it deliberately does not:

      proves    the tree built is exactly the target commit, with no untracked
                or git-ignored local content in the build context
      proves    the running container is the image this run just built
      proves    the pinned recovery recipe matches the recipe deployed
      proves    an unqualified image never enters the scanhound:latest
                namespace: the build runs under a candidate tag and the tag is
                only touched after the candidate container has passed every
                check
      proves    R4-101-1 -- that promotion is a TRANSACTION. The tag moves
                provisionally so the final plain-recipe activation can run
                against the real recipe, and if that activation or the
                container it leaves running fails, the PRIOR image is put back
                before the recovery mutex is released. A run that does not say
                VERIFIED leaves scanhound:latest on the last verified image, so
                a recovery recreate is a real rollback. What it does NOT prove:
                that the tag never momentarily named the candidate -- it does,
                for the length of the final activation, under the mutex the
                recovery task must hold to recreate anything
      proves    the NAS sources are the intended 9p shares BEFORE the container
                is recreated against them, and that /library/tv -- the TV
                rename destination -- is writable and deletable from INSIDE the
                container afterwards
      proves    the container that is finally left running, after the
                post-promotion reconcile, passes the same runtime checks the
                candidate did
      proves    only one deploy of this repository runs at a time (SR3-5)
      observes  three minutes of log volume -- a window, not a mechanism

.PARAMETER Prs
    PR numbers to merge, in order. Empty means deploy only.

.PARAMETER Ref
    What to deploy. Default 'origin/main'. Must resolve to one exact commit.

.PARAMETER SkipMerge
    Deploy only; do not touch any PR.

.PARAMETER WhatIf
    Plan only. The contract is PRODUCTION-SAFE, not "changes nothing", and
    SR3-7 is the finding that the two were being stated as if they were the
    same claim.

    What -WhatIf does NOT do: merge any PR, build any image, tag any image,
    recreate any container, or mutate production in any way.

    What it DOES do, on disk: `git fetch origin --prune`, which updates and
    prunes this repository's remote-tracking refs; and it creates a disposable
    `git worktree` for the target commit, then removes it and prunes the
    worktree metadata again. Those are real writes under .git.

    With -Prs it validates each PR's gates -- state, mergeability and every
    required check -- and stops. It CANNOT inspect the hypothetical post-merge
    tree, because it never merges: the plan is qualified against the CURRENT
    ref, not against the source a real run would build after merging. A clean
    -WhatIf with -Prs therefore says the gates pass, and says nothing at all
    about what the merged tree would deploy.

.EXAMPLE
    .\scripts\merge-and-deploy.ps1 -WhatIf
    .\scripts\merge-and-deploy.ps1 -Prs 101,102
    .\scripts\merge-and-deploy.ps1 -SkipMerge
#>

[CmdletBinding()]
param(
    [int[]]$Prs = @(),
    [string]$Ref = 'origin/main',
    [switch]$SkipMerge,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy-core.ps1')

$cfg = New-DeployConfig @{
    Repo            = 'X:\Docker Apps\ScanHound'
    PinnedCompose   = 'C:\ProgramData\ScanHound\deploy\docker-compose.yml'
    Container       = 'scanhound'
    Service         = 'scanhound'
    ImageTag        = 'scanhound:latest'
    CandidatePrefix = 'scanhound:candidate-'
    # The mutex the scheduled recovery task already uses to serialise itself
    # (scripts/mount-nas-shares.ps1). Sharing it is what stops recovery from
    # recreating the container between activation and the promotion decision.
    MutexName       = 'Global\ScanHound-MountNASShares'
    # SR3-5. The deploy-instance lock, and deliberately NOT the same name.
    # MutexName above is shared with the recovery task and is held only around
    # the container transition. This one is held for the whole run and exists
    # to stop a second deploy of the same commit from deleting this run's
    # worktree, overwriting its candidate tag or rewriting its override file.
    DeployMutexName = 'Global\ScanHound-Deploy'

    Prs             = $Prs
    Ref             = $Ref
    SkipMerge       = [bool]$SkipMerge
    WhatIf          = [bool]$WhatIf

    HealthUrl       = 'http://127.0.0.1:9721/health'
    PortHost        = '127.0.0.1'
    PortNum         = 9721
    RequireEnvVar   = 'SCANHOUND_DV_INGEST_KEY_SHA256'

    # SR3-1. No share list, no paths, no filesystem type and no critical target
    # are named here ON PURPOSE. All of it is read out of
    # scripts/mount-nas-shares.ps1 at deploy time -- from the TARGET COMMIT's
    # copy -- so the deploy engine and the scheduled recovery task cannot
    # disagree about which shares exist or which one is the read-write TV
    # destination. A second list here would be the drift this review sequence
    # has already found twice.
    NasProbe        = $true
    SettleSeconds   = 15
    # Production startup is ~63s MEASURED (entrypoint lock-cleanup runs its
    # full 60s cap, then ~3s of app start; 2026-08-26 deploy: StartedAt
    # 21:59:55 -> first /health answer 22:00:58). 120 gives headroom without
    # masking a genuinely wedged start.
    HealthTimeoutSeconds = 120
    LogWindowSeconds = 180
    SpamPattern     = 'did not auto-resume'
    SpamThreshold   = 12
}

$result = Invoke-DeployCore -Config $cfg

Write-Host ""
switch ($result.Verdict) {
    'VERIFIED' {
        Write-Host "  VERIFIED  correct source, quarantined build, correct artifact, healthy runtime" -ForegroundColor Green
        exit 0
    }
    'plan only' {
        # SR3-7, reopened. "nothing was changed" is not true and the finding is
        # that the two claims were being printed as if they were one: -WhatIf
        # DOES fetch and prune this repository's remote-tracking refs and DOES
        # create and remove a git worktree. What it changes nothing of is
        # production.
        Write-Host "  PLAN ONLY - no production state changed." -ForegroundColor Yellow
        Write-Host "  (git refs were fetched and pruned and a temporary worktree was created and removed.)" -ForegroundColor Yellow
        exit 0
    }
    default {
        Write-Host "  NOT VERIFIED ($($result.Verdict))" -ForegroundColor Red
        Write-Host ""
        # R4-101-1. Three states, not two. `promoted` is the CURRENT state of
        # the tag; promotion_state is what actually happened to it, and a
        # promotion that was made and then REVERTED must not be reported as
        # one that was never made -- same tag value, different history, and the
        # operator needs to know a candidate image briefly occupied the
        # recovery namespace.
        #
        # if/elseif, not `switch -Wildcard`: that switch runs EVERY matching
        # clause unless each one breaks, and 'promoted, then REVERTED to the
        # prior image' matches both '*REVERTED*' and a bare '*promoted*'. Two
        # contradictory paragraphs about the same tag is worse than either one.
        $pstate = "$($result.Ledger.promotion_state)"
        if ($pstate -like '*REVERT FAILED*') {
            Write-Host "  $($cfg.ImageTag) was promoted and the REVERT FAILED." -ForegroundColor Red
            Write-Host "  The recovery namespace still names an image this run did not" -ForegroundColor Red
            Write-Host "  qualify, so a recovery recreate would activate THAT image." -ForegroundColor Red
            Write-Host "  Repoint it by hand before leaving this alone:" -ForegroundColor Red
            Write-Host ""
            Write-Host "    docker tag $($result.Ledger.recovery_tag_before) $($cfg.ImageTag)" -ForegroundColor Cyan
        }
        elseif ($pstate -like '*NO PRIOR IMAGE*') {
            Write-Host "  $($cfg.ImageTag) was promoted and could NOT be reverted: this run" -ForegroundColor Yellow
            Write-Host "  found no previous $($cfg.ImageTag) at all, so THERE IS NO PRIOR" -ForegroundColor Yellow
            Write-Host "  IMAGE TO ROLL BACK TO. A recovery recreate would recreate this" -ForegroundColor Yellow
            Write-Host "  same unqualified image. There is no image rollback here; the" -ForegroundColor Yellow
            Write-Host "  next step is to fix the failure and deploy again." -ForegroundColor Yellow
        }
        elseif ($pstate -like '*REVERTED*') {
            Write-Host "  $($cfg.ImageTag) was promoted provisionally and has been REVERTED" -ForegroundColor Yellow
            Write-Host "  to the image that was there before this run. It points at the" -ForegroundColor Yellow
            Write-Host "  last verified image again, so if ScanHound-MountNASShares" -ForegroundColor Yellow
            Write-Host "  recreates the container it will restore that image, not this" -ForegroundColor Yellow
            Write-Host "  candidate." -ForegroundColor Yellow
        }
        elseif ($result.Ledger.promoted) {
            # Not reachable by any path in the engine today -- every
            # non-VERIFIED exit runs the revert. Printed rather than assumed
            # away, because "unreachable" is a claim about code that changes.
            Write-Host "  $($cfg.ImageTag) IS promoted to this run's candidate and was not" -ForegroundColor Red
            Write-Host "  reverted, even though the run did not verify. A recovery recreate" -ForegroundColor Red
            Write-Host "  would activate the unqualified image. Report this: the promotion" -ForegroundColor Red
            Write-Host "  transaction has a path that does not close." -ForegroundColor Red
        }
        else {
            Write-Host "  $($cfg.ImageTag) was NOT promoted. It still points at the last" -ForegroundColor Yellow
            Write-Host "  verified image, so if ScanHound-MountNASShares recreates the" -ForegroundColor Yellow
            Write-Host "  container it will restore that image, not this candidate." -ForegroundColor Yellow
        }
        if (@($result.Ledger.merged_prs).Count -gt 0) {
            Write-Host ""
            Write-Host "  Merged PRs are NOT rolled back: $($result.Ledger.merged_prs -join ', ')" -ForegroundColor Yellow
        }
        Write-Host ""
        Write-Host "  UNKNOWN is not OK. A check that could not be measured has not passed." -ForegroundColor Yellow
        Write-Host "  Read the 'observed' block above for what is actually running now." -ForegroundColor Yellow

        # If the container was already replaced, production is serving the
        # candidate while scanhound:latest still names the last verified image.
        # That asymmetry is deliberate -- it is what makes rollback a single
        # command -- but it must not be left implicit, because the scheduled
        # recovery task would otherwise perform it at an unpredictable moment.
        #
        # SR3-6. The decision is Test-RollbackAdvisable, which reads the
        # OBSERVER. This used to ask whether the ledger's new_container_id had
        # been filled in, and that field is written in section 6 -- so a
        # Compose run that partially replaced the container and then returned
        # nonzero left it null while the observer plainly showed a new
        # container running, and the operator was denied the rollback command
        # in exactly the case that needed it.
        if (Test-RollbackAdvisable -Ledger $result.Ledger) {
            $obs = $result.Ledger.observed
            Write-Host ""
            Write-Host "  OBSERVED: container $($obs.container_id) is what is running now" -ForegroundColor Yellow
            Write-Host "  (running=$($obs.running), health=$($obs.health)); before this run it was" -ForegroundColor Yellow
            Write-Host "  $(if ($result.Ledger.old_container_id) { $result.Ledger.old_container_id } else { 'no container at all' })." -ForegroundColor Yellow
            Write-Host ""
            Write-Host "  The container WAS replaced and is serving the unverified candidate," -ForegroundColor Yellow
            Write-Host "  while $($cfg.ImageTag) names the last verified image, so recreating" -ForegroundColor Yellow
            Write-Host "  from the pinned recipe restores that image." -ForegroundColor Yellow

            $recreate = "docker compose -f `"$($cfg.PinnedCompose)`" --project-directory `"$($cfg.Repo)`" up -d --force-recreate --no-build --pull never"

            # R4-101-1. The recreate CREATES a container, and Docker resolves
            # bind SOURCES at container-create time. If what failed this run was
            # a storage proof, running it is not a recovery: it is a second
            # container created against sources whose identity is still
            # unproven, binding /library/tv -- the TV download, extract and
            # rename DESTINATION -- to whatever those paths currently are. That
            # is the 2026-07-26 outage, performed deliberately, on the
            # operator's own keystroke.
            #
            # So the order of the two commands is the guidance. The recreate is
            # NOT printed first and then walked back; when a storage proof did
            # not pass, the mount-recovery path is step one and the recreate is
            # step two.
            if (Test-StorageFailureObserved -Ledger $result.Ledger) {
                Write-Host ""
                Write-Host "  DO NOT RECREATE YET. A STORAGE proof failed or could not be" -ForegroundColor Red
                Write-Host "  measured this run:" -ForegroundColor Red
                Write-Host "    host      $($result.Ledger.nas_host_reason) / $($result.Ledger.nas_host_code)" -ForegroundColor Red
                Write-Host "    candidate $($result.Ledger.nas_candidate_reason) / $($result.Ledger.nas_candidate_code)" -ForegroundColor Red
                Write-Host "    final     $($result.Ledger.nas_final_reason) / $($result.Ledger.nas_final_code)" -ForegroundColor Red
                Write-Host "  (only 'probed / 0' is proven; anything else, including a probe" -ForegroundColor Red
                Write-Host "  that could not be run, is not.)" -ForegroundColor Red
                Write-Host ""
                Write-Host "  Bind sources are resolved when a container is CREATED, so a" -ForegroundColor Red
                Write-Host "  recreate now would bind the shares to whatever those paths are" -ForegroundColor Red
                Write-Host "  right now -- which is the failure being reported. Re-establish" -ForegroundColor Red
                Write-Host "  and re-prove the mounts FIRST:" -ForegroundColor Red
                Write-Host ""
                Write-Host "    powershell -ExecutionPolicy Bypass -File `"$($cfg.Repo)\scripts\mount-nas-shares.ps1`"" -ForegroundColor Cyan
                Write-Host ""
                Write-Host "  THEN, once that reports every share mounted and identity-verified:" -ForegroundColor Red
                Write-Host ""
                Write-Host "    $recreate" -ForegroundColor Cyan
            } else {
                Write-Host "  To roll back now rather than waiting for ScanHound-MountNASShares" -ForegroundColor Yellow
                Write-Host "  to do it:" -ForegroundColor Yellow
                Write-Host ""
                Write-Host "    $recreate" -ForegroundColor Cyan
            }
        }
        exit 1
    }
}
