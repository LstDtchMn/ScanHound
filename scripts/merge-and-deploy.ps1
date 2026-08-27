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
      proves    an unverified image never enters the scanhound:latest namespace
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
        Write-Host "  PLAN ONLY  nothing was changed." -ForegroundColor Yellow
        exit 0
    }
    default {
        Write-Host "  NOT VERIFIED ($($result.Verdict))" -ForegroundColor Red
        Write-Host ""
        if ($result.Ledger.promoted) {
            Write-Host "  scanhound:latest WAS promoted before this failure." -ForegroundColor Yellow
        } else {
            Write-Host "  scanhound:latest was NOT promoted. It still points at the last" -ForegroundColor Yellow
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
            Write-Host "  while $($cfg.ImageTag) still names the last verified image. To roll" -ForegroundColor Yellow
            Write-Host "  back now rather than waiting for ScanHound-MountNASShares to do it:" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "    docker compose -f `"$($cfg.PinnedCompose)`" --project-directory `"$($cfg.Repo)`" up -d --force-recreate --no-build --pull never" -ForegroundColor Cyan
        }
        exit 1
    }
}
