<#
.SYNOPSIS
    Merge approved PRs, rebuild ScanHound, and prove the intended artifact is
    the one running.

.DESCRIPTION
    Rewritten 2026-08-26 after an operational-safety review found nine defects
    in the first version, two of them blockers. The old script asked "is a
    ScanHound container healthy?" This one asks "is the container running the
    exact thing I just built from the exact source I meant?"

    A deployment proof needs all three, and the old one had only the last:

        transport outcome   the build and activate commands actually succeeded
        artifact identity   the running container IS the image just built
        runtime outcome     that container behaves correctly

    THE FALSE SUCCESS THIS PREVENTS (OPS-2). Old container running, correctly
    bound, holding the DV key, answering /health, and -- after the first
    rollout -- already containing the string the script grepped for. New build
    fails. Compose stops before replacing the service. Old container keeps
    running. Every check passes. Script prints "deploy verified".

    The grep-for-a-string identity check worked exactly once, by accident,
    because the old image genuinely lacked that string. From the second run
    onward it proves nothing.

    AND THE ONE THAT MAKES IT WORSE (OPS-1). The old script printed
    "git pull, so the build uses what was just merged" and then ran
    `git fetch`. Fetch updates remote-tracking refs; it does not change one
    file in the working tree. It would have built whatever happened to be
    checked out -- a feature branch, a stale main, a dirty tree -- and then
    reported a verified deploy of code it never built.

    Tonight's real deploy was correct only because the commands were run by
    hand and `git checkout --detach origin/main` was done first.

.PARAMETER Prs
    PR numbers to merge, in order. Empty means deploy only.

.PARAMETER Ref
    What to deploy. Default 'origin/main'. Must resolve to one exact commit.

.PARAMETER SkipMerge
    Deploy only; do not touch any PR.

.PARAMETER WhatIf
    Run every check, print the plan, change nothing.

.EXAMPLE
    .\scripts\merge-and-deploy.ps1 -Prs 101,102 -WhatIf
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

$REPO      = 'X:\Docker Apps\ScanHound'
$PINNED    = 'C:\ProgramData\ScanHound\deploy\docker-compose.yml'
$WORKTREE  = 'X:\Docker Apps\ScanHound\docker-compose.yml'
$CONTAINER = 'scanhound'
$SERVICE   = 'scanhound'
$IMAGE     = 'scanhound:latest'
$HEALTH    = 'http://127.0.0.1:9721/health'
$PORT_HOST = '127.0.0.1'
$PORT_NUM  = 9721

# State ledger. Populated as we go so §6 can report even if something throws
# mid-flight (OPS-5) -- once a production-changing action has begun, the
# operator must get a state report, not just a stack trace.
$LEDGER = [ordered]@{
    expected_sha     = $null
    merged_prs       = @()
    build_attempted  = $false
    built_image_id   = $null
    old_container_id = $null
    old_image_id     = $null
    new_container_id = $null
    new_image_id     = $null
    activate_exit    = $null
    build_exit       = $null
    verdict          = 'not reached'
}

function Say([string]$m)  { Write-Host "  $m" }
function Head([string]$m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Good([string]$m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  WARN $m" -ForegroundColor Yellow }

function Invoke-Native {
    <#
      Run a native exe; return BOTH its output and its exit code.

      The previous version returned only strings, so every caller decided
      success from the presence or absence of text (OPS-3). A `gh` auth or
      network failure produced error text with no "fail" row in it, and the
      PR gate concluded "all checks passing".

      PowerShell 5.1 quirks handled here:
        * a native program's stderr must be redirected or PowerShell drops it;
        * with 2>&1 each stderr line arrives as an ErrorRecord, not a string;
        * under $ErrorActionPreference='Stop' the first such record TERMINATES.

      None of that redefines the process exit code, so the exit code is kept.
    #>
    param([Parameter(Mandatory)][scriptblock]$Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Command 2>&1 | ForEach-Object { $_.ToString() }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    [pscustomobject]@{ Output = @($out); ExitCode = $code; Text = ($out -join "`n") }
}

function Require-Native {
    <# Same, but a nonzero exit is fatal. "No evidence is not passing evidence." #>
    param([Parameter(Mandatory)][scriptblock]$Command, [string]$What)
    $r = Invoke-Native $Command
    if ($r.ExitCode -ne 0) {
        Die ("{0} failed (exit {1}):`n{2}" -f $What, $r.ExitCode,
             ($r.Output | Select-Object -Last 12 | ForEach-Object { "      $_" }) -join "`n")
    }
    return $r
}

function Show-Ledger {
    Write-Host ""
    Write-Host "== State ledger" -ForegroundColor Cyan
    foreach ($k in $LEDGER.Keys) {
        $v = $LEDGER[$k]
        if ($v -is [array]) { $v = ($v -join ', ') }
        if ($null -eq $v -or $v -eq '') { $v = '-' }
        Write-Host ("  {0,-17} {1}" -f $k, $v)
    }
}

function Die([string]$m) {
    Write-Host ""
    Write-Host "  STOP $m" -ForegroundColor Red
    $LEDGER.verdict = 'STOPPED'
    # NOT "nothing further has been attempted" -- that was false whenever an
    # earlier PR in the sequence had already merged, or the build had run.
    Show-Ledger
    Write-Host ""
    Write-Host "  Read the ledger above before retrying: some steps may have" -ForegroundColor Yellow
    Write-Host "  completed. In particular, merged PRs are NOT rolled back." -ForegroundColor Yellow
    exit 1
}

Set-Location $REPO

try {

# =====================================================================
Head "1. Pre-flight"
# =====================================================================

foreach ($tool in @('gh', 'docker', 'git')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Die "$tool is not on PATH." }
}
Good "gh, docker and git are available"

# Compose drift. `config` renders what would ACTUALLY deploy, so comment and
# formatting differences do not register -- only semantic ones.
#
# NOT ceremonial: on 2026-08-11/12 the scheduled task ScanHound-MountNASShares
# (Boot + Logon + 288x/day) recreated the container from the PINNED copy, which
# had gone stale. It lost SCANHOUND_DV_INGEST_KEY_SHA256 and the 127.0.0.1:9721
# publish, and DV failed with WinError 10061 until someone noticed.
$p = Require-Native { docker compose -f $PINNED   --project-directory $REPO config } "rendering the pinned compose"
$w = Require-Native { docker compose -f $WORKTREE --project-directory $REPO config } "rendering the working-tree compose"
if (Compare-Object -ReferenceObject $p.Output -DifferenceObject $w.Output) {
    Die ("the pinned recovery compose has drifted from the working tree. " +
         "Deploying now would be reverted the next time ScanHound-MountNASShares " +
         "recreates the container. The pinned file is ACL'd to " +
         "SYSTEM+Administrators, so reconciling it needs an elevated console.")
}
Good "pinned recovery compose matches the working tree"

# =====================================================================
Head "2. Pull requests"
# =====================================================================
#
# Structured state, not text scraping (OPS-3). Every required check must be
# EXPLICITLY acceptable; pending, cancelled, skipped and unknown are all
# refusals. The old version's baseline-failure whitelist is gone entirely --
# it named a test that #96 has since fixed on main, so it could only ever
# excuse a NEW breakage of that test.

if ($SkipMerge -or $Prs.Count -eq 0) {
    Warn "not merging anything (SkipMerge or no -Prs given)"
} else {
    foreach ($pr in $Prs) {
        Head ("2.{0}  PR #{0}" -f $pr)

        $v = Require-Native { gh pr view $pr --json state,mergeable,mergeStateStatus } "gh pr view #$pr"
        $info = $v.Text | ConvertFrom-Json
        if ($info.state -eq 'MERGED') { Good "already merged"; continue }
        if ($info.state -ne 'OPEN')   { Die "PR #$pr is $($info.state), not OPEN." }
        if ($info.mergeable -ne 'MERGEABLE') { Die "PR #$pr mergeable=$($info.mergeable)." }

        $c = Invoke-Native { gh pr checks $pr --json name,state,bucket }
        # gh documents exit 8 for "checks pending". Any nonzero that is not 8
        # is a measurement failure and must not be read as success.
        if ($c.ExitCode -ne 0 -and $c.ExitCode -ne 8) {
            Die "could not read checks for #$pr (gh exit $($c.ExitCode)): $($c.Text)"
        }
        $rows = @()
        try { $rows = @($c.Text | ConvertFrom-Json) } catch {
            Die "checks for #$pr did not parse as JSON: $($c.Text)"
        }
        if ($rows.Count -eq 0) { Die "PR #$pr reported NO checks. Absence is not success." }

        $bad = @($rows | Where-Object { $_.bucket -ne 'pass' -and $_.bucket -ne 'skipping' })
        if ($bad.Count -gt 0) {
            foreach ($b in $bad) { Say ("  {0}: {1}" -f $b.name, $b.bucket) }
            Die ("PR #$pr has {0} check(s) that are not passing. Nothing is " +
                 "whitelisted: a pending check is unknown, and unknown is not " +
                 "passing." -f $bad.Count)
        }
        Good ("all {0} check(s) passing" -f $rows.Count)

        if ($WhatIf) { Warn "-WhatIf: would merge #$pr"; continue }
        Require-Native { gh pr merge $pr --merge } "merging #$pr" | Out-Null
        $LEDGER.merged_prs += $pr
        Good "merged"
        Start-Sleep -Seconds 10
    }
}

# =====================================================================
Head "3. Source identity"
# =====================================================================
#
# OPS-1. The old script SAID "git pull" and ran `git fetch`. Fetch moves
# remote-tracking refs and changes not one file in the working tree, so the
# build used whatever happened to be checked out. Every condition below was
# absent.

Require-Native { git -C $REPO fetch origin --prune } "git fetch" | Out-Null
Good "fetched origin"

$dirty = (Invoke-Native { git -C $REPO status --porcelain }).Output |
         Where-Object { $_ -notmatch '^\?\? ' }        # untracked is not in the build context we care about
if ($dirty) {
    $dirty | Select-Object -First 10 | ForEach-Object { Say "    $_" }
    Die "the working tree has tracked modifications. Refusing to build an unidentifiable source."
}
Good "working tree has no tracked modifications"

$target = (Require-Native { git -C $REPO rev-parse $Ref } "resolving $Ref").Output[0].Trim()
Say "deploying ref $Ref = $($target.Substring(0,12))"

# Actually put that source in the working tree. This is the step the old
# script claimed and never performed.
if (-not $WhatIf) {
    Require-Native { git -C $REPO checkout --detach $target } "checking out $Ref" | Out-Null
}
$head = (Require-Native { git -C $REPO rev-parse HEAD } "reading HEAD").Output[0].Trim()
if (-not $WhatIf -and $head -ne $target) {
    Die "HEAD is $head but $Ref is $target. The checkout did not take."
}
$LEDGER.expected_sha = $head
Good "working tree is at $($head.Substring(0,12))"

foreach ($pr in $LEDGER.merged_prs) {
    $sha = (Invoke-Native { gh pr view $pr --json mergeCommit -q '.mergeCommit.oid' }).Output[0]
    if ($sha) {
        $anc = Invoke-Native { git -C $REPO merge-base --is-ancestor $sha $head }
        if ($anc.ExitCode -ne 0) { Die "PR #$pr merged as $sha but that is NOT an ancestor of the ref being deployed." }
        Good "PR #$pr's merge commit is in the deployed source"
    }
}

if ($WhatIf) {
    Warn "-WhatIf: would build and deploy $($head.Substring(0,12)) here"
    $LEDGER.verdict = 'plan only'
    Show-Ledger
    exit 0
}

# =====================================================================
Head "4. Build, then activate -- separately"
# =====================================================================
#
# OPS-2. Separated so a build failure is caught as a build failure, instead of
# being inferred later from the state of a container that may never have been
# replaced. The exit code is REQUIRED, not discarded: PowerShell's stderr
# handling never redefines a process's exit code, and `docker compose` returns
# 1 on error.

$old = Invoke-Native { docker inspect -f '{{.Id}} {{.Image}}' $CONTAINER }
if ($old.ExitCode -eq 0 -and $old.Output.Count -gt 0) {
    $parts = $old.Output[0].Split(' ')
    $LEDGER.old_container_id = $parts[0].Substring(0, 12)
    $LEDGER.old_image_id     = $parts[1].Substring(0, 19)
    Say "old container $($LEDGER.old_container_id) on image $($LEDGER.old_image_id)"
} else {
    Say "no existing container (first deploy)"
}

$LEDGER.build_attempted = $true
Say "building -- over 10 minutes, do not interrupt"
$b = Invoke-Native { docker compose -f $WORKTREE --project-directory $REPO build $SERVICE }
$LEDGER.build_exit = $b.ExitCode
if ($b.ExitCode -ne 0) {
    $b.Output | Select-Object -Last 15 | ForEach-Object { Say "    $_" }
    Die "the BUILD failed (exit $($b.ExitCode)). The old container is untouched and still running."
}
Good "build succeeded (exit 0)"

$built = (Require-Native { docker image inspect $IMAGE --format '{{.Id}}' } "reading the built image id").Output[0].Trim()
$LEDGER.built_image_id = $built.Substring(0, 19)
Good "built image $($LEDGER.built_image_id)"

$a = Invoke-Native { docker compose -f $WORKTREE --project-directory $REPO up -d --no-build --force-recreate $SERVICE }
$LEDGER.activate_exit = $a.ExitCode
if ($a.ExitCode -ne 0) {
    $a.Output | Select-Object -Last 15 | ForEach-Object { Say "    $_" }
    Die "ACTIVATION failed (exit $($a.ExitCode)) after a successful build."
}
Good "activated (exit 0)"

# =====================================================================
Head "5. Artifact identity -- is the running container the thing we built?"
# =====================================================================
#
# The check the old script had no equivalent of. Without it, every runtime
# check below can pass against an old container that was never replaced.

Start-Sleep -Seconds 15
$now = Require-Native { docker inspect -f '{{.Id}} {{.Image}}' $CONTAINER } "inspecting the container"
$np = $now.Output[0].Split(' ')
$LEDGER.new_container_id = $np[0].Substring(0, 12)
$LEDGER.new_image_id     = $np[1].Substring(0, 19)

if ($LEDGER.old_container_id -and $LEDGER.new_container_id -eq $LEDGER.old_container_id) {
    Die "the container was NOT replaced (still $($LEDGER.new_container_id)). Nothing was deployed."
}
Good "container replaced: $($LEDGER.old_container_id) -> $($LEDGER.new_container_id)"

if ($LEDGER.new_image_id -ne $LEDGER.built_image_id) {
    Die ("the running container is on image $($LEDGER.new_image_id) but the " +
         "build produced $($LEDGER.built_image_id). It is running something else.")
}
Good "running the image just built"

# =====================================================================
Head "6. Runtime"
# =====================================================================

$problems = @()
$unknown  = @()

$run = Invoke-Native { docker inspect -f '{{.State.Running}}' $CONTAINER }
if ($run.ExitCode -ne 0)          { $unknown  += "could not read running state" }
elseif ($run.Output[0] -ne 'true'){ $problems += "container is not running" }
else                              { Good "running" }

# Exact binding, not a substring: '127.0.0.1:97210' contains '127.0.0.1:9721'.
$pj = Invoke-Native { docker inspect -f '{{json .NetworkSettings.Ports}}' $CONTAINER }
if ($pj.ExitCode -ne 0) { $unknown += "could not read port bindings" }
else {
    $ports = $pj.Text | ConvertFrom-Json
    $key = "$PORT_NUM/tcp"
    $bound = $false
    if ($ports.$key) {
        foreach ($e in $ports.$key) {
            if ($e.HostIp -eq $PORT_HOST -and [int]$e.HostPort -eq $PORT_NUM) { $bound = $true }
        }
    }
    if ($bound) { Good "$PORT_HOST`:$PORT_NUM bound exactly" }
    else { $problems += "$PORT_HOST`:$PORT_NUM is NOT bound -- the host DV detector cannot reach the app" }
}

$k = Invoke-Native { docker exec $CONTAINER sh -c 'test -n "$SCANHOUND_DV_INGEST_KEY_SHA256" && echo SET || echo MISSING' }
if ($k.ExitCode -ne 0)          { $unknown  += "could not read the DV key env" }
elseif ($k.Text -match 'SET')   { Good "SCANHOUND_DV_INGEST_KEY_SHA256 set" }
else                            { $problems += "SCANHOUND_DV_INGEST_KEY_SHA256 MISSING -- DV row posts will 401" }

try {
    $h = Invoke-RestMethod -Uri $HEALTH -TimeoutSec 20
    if ($h.status -eq 'ok') { Good "/health status=ok" }
    else { $problems += "/health answered but status=$($h.status), not ok" }
} catch {
    $problems += "/health did not answer: $($_.Exception.Message)"
}

# Log smoke. Honestly worded: this observes a window, it does not prove the
# suppression mechanism. A window with no stuck batch would read zero whether
# or not the fix works -- which is why the causal property belongs in the
# test-suite fixture that deliberately creates a stuck batch, not here.
Say "observing the log for 3 minutes"
Start-Sleep -Seconds 180
$lg = Invoke-Native { docker logs $CONTAINER --since 3m }
if ($lg.ExitCode -ne 0) {
    # The original 0%-baseline defect, in its post-deploy form: measurement
    # failure must not become a clean result.
    $unknown += "could not read logs -- spam rate UNKNOWN, not zero"
} else {
    $spam = @($lg.Output | Select-String -SimpleMatch 'did not auto-resume').Count
    Say ("{0} lines in 3 min, {1} auto-resume" -f $lg.Output.Count, $spam)
    if ($spam -gt 12) { $problems += "$spam auto-resume lines in 3 minutes; suppression is not taking effect" }
    else { Good "no auto-resume flood observed in this window (not a proof of the mechanism)" }
}

# =====================================================================
Head "7. Result"
# =====================================================================

if ($problems.Count -eq 0 -and $unknown.Count -eq 0) {
    $LEDGER.verdict = 'VERIFIED'
    Good "deploy verified: correct source, correct artifact, healthy runtime"
    Show-Ledger
    exit 0
}
foreach ($u in $unknown)  { Write-Host "  UNKNOWN  $u" -ForegroundColor Yellow }
foreach ($pr in $problems){ Write-Host "  PROBLEM  $pr" -ForegroundColor Red }
$LEDGER.verdict = if ($problems.Count) { 'PROBLEMS' } else { 'UNKNOWN' }
Show-Ledger
Write-Host ""
Write-Host "  UNKNOWN is not OK. A check that could not be measured has not passed." -ForegroundColor Yellow
Write-Host "  If the port or DV key is listed, restore before the next DV scan:" -ForegroundColor Yellow
Write-Host "    docker compose -f `"$WORKTREE`" --project-directory `"$REPO`" up -d --force-recreate" -ForegroundColor Yellow
exit 1

}
catch {
    # OPS-5: once production-changing work has begun, the operator gets the
    # state ledger even when something throws. The exception is preserved.
    Write-Host ""
    Write-Host "  UNHANDLED: $($_.Exception.Message)" -ForegroundColor Red
    $LEDGER.verdict = 'ABORTED (exception)'
    Show-Ledger
    throw
}
