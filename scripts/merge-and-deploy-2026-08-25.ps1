<#
.SYNOPSIS
    Merge PRs #96/#97/#98 in the required order, redeploy ScanHound, and verify.

.DESCRIPTION
    Written 2026-08-25. Everything here was verified by hand first; this script
    exists so the sequence runs the same way twice, not to decide anything.

    THE ORDER MATTERS. origin/main currently fails one of its own tests
    (test_config.py::test_default_config_has_no_unexpected_keys). #96 fixes it.
    #97 and #98 inherit that failure and show as UNSTABLE until #96 lands, so
    merging #96 first is what turns the other two green -- not a preference.

    WHAT IT REFUSES TO DO
      * merge a PR whose only failing check is NOT the known main failure;
      * deploy if the pinned recovery compose has drifted from the working tree;
      * report success it has not verified.

    THE DRIFT CHECK IS NOT CEREMONIAL. On 2026-08-11/12 the scheduled task
    ScanHound-MountNASShares (Boot + Logon + 288x/day) recreated the container
    from C:\ProgramData\ScanHound\deploy\docker-compose.yml, a pinned copy that
    had gone stale. It lost SCANHOUND_DV_INGEST_KEY_SHA256 and the
    127.0.0.1:9721 port publish, and the DV detector failed with WinError 10061
    until someone noticed. The two files agreed when this was written; they must
    agree at the moment you run it, because that task can fire in between.

.PARAMETER SkipMerge
    Deploy only. Use if the PRs are already merged.

.PARAMETER WhatIf
    Run every check and print the plan; change nothing.

.EXAMPLE
    .\scripts\merge-and-deploy-2026-08-25.ps1 -WhatIf
    .\scripts\merge-and-deploy-2026-08-25.ps1
#>

[CmdletBinding()]
param(
    [switch]$SkipMerge,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

$REPO        = 'X:\Docker Apps\ScanHound'
$PINNED      = 'C:\ProgramData\ScanHound\deploy\docker-compose.yml'
$WORKTREE    = 'X:\Docker Apps\ScanHound\docker-compose.yml'
$CONTAINER   = 'scanhound'
$PR_ORDER    = @(96, 97, 98)

# The one failure origin/main has today. A PR showing ONLY this is safe to
# merge; anything else must stop the run and be looked at by a human.
$KNOWN_MAIN_FAILURE = 'test_default_config_has_no_unexpected_keys'

function Say([string]$m)  { Write-Host "  $m" }
function Head([string]$m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Good([string]$m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  WARN $m" -ForegroundColor Yellow }
function Die([string]$m) {
    Write-Host ""
    Write-Host "  STOP $m" -ForegroundColor Red
    Write-Host "  Nothing further has been attempted." -ForegroundColor Red
    exit 1
}

Set-Location $REPO

# =====================================================================
Head "1. Pre-flight"
# =====================================================================

foreach ($tool in @('gh', 'docker', 'git')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Die "$tool is not on PATH."
    }
}
Good "gh, docker and git are available"

# Compose drift. `config` renders what would ACTUALLY deploy, so comments and
# formatting differences do not register -- only semantic ones.
$pinnedCfg   = docker compose -f $PINNED   --project-directory $REPO config
$worktreeCfg = docker compose -f $WORKTREE --project-directory $REPO config
if (-not $pinnedCfg)   { Die "could not render the pinned compose at $PINNED" }
if (-not $worktreeCfg) { Die "could not render the working-tree compose" }

$drift = Compare-Object -ReferenceObject $pinnedCfg -DifferenceObject $worktreeCfg
if ($drift) {
    Write-Host ""
    $drift | Select-Object -First 20 | Format-Table -AutoSize | Out-String | Write-Host
    Die ("the pinned recovery compose has drifted from the working tree. " +
         "Deploying now would be reverted the next time ScanHound-MountNASShares " +
         "recreates the container -- the 2026-08-11 outage. The pinned file is " +
         "ACL'd to SYSTEM+Administrators, so reconciling it needs an elevated " +
         "console.")
}
Good "pinned recovery compose matches the working tree (no drift)"

# Baseline, so the post-deploy comparison means something.
$logsBefore = docker logs $CONTAINER --since 1h
$totalBefore = ($logsBefore | Measure-Object).Count
$spamBefore  = ($logsBefore | Select-String -SimpleMatch 'did not auto-resume' |
                Measure-Object).Count
$pctBefore = 0
if ($totalBefore -gt 0) { $pctBefore = [math]::Round(100 * $spamBefore / $totalBefore, 1) }
Say ("baseline: {0} log lines in the last hour, {1} of them auto-resume spam ({2}%)" `
     -f $totalBefore, $spamBefore, $pctBefore)

$portBefore = docker port $CONTAINER
Say ("baseline: port bindings -> {0}" -f ($portBefore -join ', '))

# =====================================================================
Head "2. Pull requests"
# =====================================================================

if ($SkipMerge) {
    Warn "-SkipMerge given; not touching the PRs"
} else {
    foreach ($pr in $PR_ORDER) {
        Head ("2.{0}  PR #{0}" -f $pr)

        $state = gh pr view $pr --json state -q '.state'
        if ($state -eq 'MERGED') { Good "already merged"; continue }
        if ($state -ne 'OPEN')   { Die  "PR #$pr is $state, not OPEN." }

        $mergeable = gh pr view $pr --json mergeable -q '.mergeable'
        if ($mergeable -ne 'MERGEABLE') {
            Die "PR #$pr reports mergeable=$mergeable. Resolve that by hand."
        }

        # Which checks are failing, and are they only the known main failure?
        $failing = @(gh pr checks $pr --json name,state,link |
                     ConvertFrom-Json |
                     Where-Object { $_.state -eq 'FAILURE' })
        if ($failing.Count -gt 0) {
            Say ("{0} failing check(s); inspecting the reason" -f $failing.Count)
            $runId = $null
            if ($failing[0].link -match '/runs/(\d+)') { $runId = $Matches[1] }
            $unexpected = @()
            if ($runId) {
                $log = gh run view $runId --log-failed
                $names = @($log | Select-String -Pattern 'FAILED (tests/\S+)' -AllMatches |
                           ForEach-Object { $_.Matches } |
                           ForEach-Object { $_.Groups[1].Value } |
                           Sort-Object -Unique)
                foreach ($n in $names) {
                    if ($n -notmatch [regex]::Escape($KNOWN_MAIN_FAILURE)) {
                        $unexpected += $n
                    }
                }
                foreach ($n in $names) { Say ("  failing test: {0}" -f $n) }
            } else {
                Die "PR #$pr has failing checks and the run id could not be read."
            }

            if ($unexpected.Count -gt 0) {
                Die ("PR #$pr fails tests beyond the known main failure:`n      " +
                     ($unexpected -join "`n      ") +
                     "`n    Those are real. Do not merge this PR yet.")
            }
            Warn ("only the known origin/main failure ({0}); #96 fixes it" `
                  -f $KNOWN_MAIN_FAILURE)
        } else {
            Good "all checks passing"
        }

        if ($WhatIf) { Warn "-WhatIf: would merge PR #$pr here"; continue }

        Say "merging..."
        gh pr merge $pr --merge
        if (-not $?) { Die "gh pr merge failed for #$pr." }
        Good "merged"

        # After #96 the other two need main's fix before their checks can pass.
        if ($pr -eq 96) {
            Say "waiting 30s for GitHub to register the merge before the next PR"
            Start-Sleep -Seconds 30
        }
    }
}

# =====================================================================
Head "3. Deploy"
# =====================================================================

Say "re-checking compose drift immediately before the destructive step"
$pinnedCfg2   = docker compose -f $PINNED   --project-directory $REPO config
$worktreeCfg2 = docker compose -f $WORKTREE --project-directory $REPO config
if (Compare-Object -ReferenceObject $pinnedCfg2 -DifferenceObject $worktreeCfg2) {
    Die "compose drifted between pre-flight and deploy. Something changed it mid-run."
}
Good "still no drift"

if ($WhatIf) {
    Warn "-WhatIf: would run 'docker compose up -d --build' here and stop"
    Write-Host ""
    Say "plan complete; nothing was changed."
    exit 0
}

Say "git pull, so the build uses what was just merged"
git -C $REPO fetch origin
git -C $REPO status --porcelain | Out-String | Write-Host

Say "building and recreating -- this takes over 10 minutes, do not interrupt"
docker compose -f $WORKTREE --project-directory $REPO up -d --build
# Do NOT test $LASTEXITCODE here: docker writes progress to stderr and PowerShell
# 5.1 reports a nonzero exit for a successful build. The verification below is
# the real check -- it looks at the container, not at an exit code.

# =====================================================================
Head "4. Verify -- the container, not the exit code"
# =====================================================================

Start-Sleep -Seconds 20
$problems = @()

$running = docker inspect -f '{{.State.Running}}' $CONTAINER
if ($running -ne 'true') { $problems += "container is not running" }
else { Good "container is running" }

# The two settings the 2026-08-11 recreate silently dropped.
$portAfter = docker port $CONTAINER
if ($portAfter -match '127\.0\.0\.1:9721') {
    Good "DV port binding 127.0.0.1:9721 survived the recreate"
} else {
    $problems += "DV port binding 127.0.0.1:9721 is GONE -- the host DV detector cannot reach the app"
}

$keySet = docker exec $CONTAINER sh -c 'test -n "$SCANHOUND_DV_INGEST_KEY_SHA256" && echo SET || echo MISSING'
if ($keySet -match 'SET') {
    Good "SCANHOUND_DV_INGEST_KEY_SHA256 is set"
} else {
    $problems += "SCANHOUND_DV_INGEST_KEY_SHA256 is MISSING -- DV row posts will 401"
}

$fixPresent = docker exec $CONTAINER sh -c "grep -c '_last_no_resume_reason' /app/backend/download_queue.py"
if ([int]$fixPresent -gt 0) {
    Good "the auto-resume log-spam fix is present in the deployed image"
} else {
    $problems += "the log-spam fix is NOT in the deployed image -- did #97 merge?"
}

Say "waiting 3 minutes to measure the log rate on the new container"
Start-Sleep -Seconds 180
$logsAfter = docker logs $CONTAINER --since 5m
$totalAfter = ($logsAfter | Measure-Object).Count
$spamAfter  = ($logsAfter | Select-String -SimpleMatch 'did not auto-resume' |
               Measure-Object).Count
Say ("after: {0} lines in 5 minutes, {1} auto-resume (was {2}% of the hour before)" `
     -f $totalAfter, $spamAfter, $pctBefore)
if ($spamAfter -gt 20) {
    $problems += "still $spamAfter auto-resume lines in 5 minutes; the fix is not taking effect"
} else {
    Good "auto-resume spam is suppressed"
}

# Health, including the field round 28 added a consumer for.
try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:9721/health' -TimeoutSec 20
    Good ("/health answers: status={0}" -f $health.status)
    if ($null -ne $health.quarantine_audit) {
        Say ("  quarantine_audit: {0}" -f $health.quarantine_audit.status)
    }
} catch {
    $problems += ("/health did not answer: " + $_.Exception.Message)
}

# =====================================================================
Head "5. Result"
# =====================================================================

if ($problems.Count -eq 0) {
    Good "deploy verified: container up, DV settings intact, log spam suppressed"
    exit 0
}

Write-Host ""
foreach ($p in $problems) { Write-Host "  PROBLEM  $p" -ForegroundColor Red }
Write-Host ""
Write-Host "  The DV port binding and key env are the two that broke on" -ForegroundColor Yellow
Write-Host "  2026-08-11. If either is listed above, restore them before the" -ForegroundColor Yellow
Write-Host "  next scheduled DV scan:" -ForegroundColor Yellow
Write-Host "      docker compose -f `"$WORKTREE`" --project-directory `"$REPO`" up -d --force-recreate" -ForegroundColor Yellow
exit 1
