# Completes the 2026-08-16 ScanHound work that Claude cannot do itself.
#
# RUN IN AN ELEVATED POWERSHELL (Run as Administrator).
#
# WHAT NEEDS YOU, AND WHY:
#   merge / push / deploy   standing rule: those are Jesse's calls, not Claude's
#   schtasks /Create        Claude gets Access Denied registering scheduled tasks
#   the pinned mount script C:\ProgramData\ScanHound\deploy is SYSTEM+Admin only
#   the verification hold   releasing it sends 39 items back at HDEncode
#
# EVERY STEP VERIFIES ITS OUTCOME, not its exit code. `docker` writes progress to
# stderr, which makes PowerShell 5.1 report failure on a successful command -- so
# checking $? here would report the opposite of the truth. Each step therefore
# asks the system what actually happened.
#
# Safe to re-run. Nothing here is destructive; the merge is a normal merge commit
# and every other step is idempotent.

$ErrorActionPreference = 'Continue'
$Repo = 'X:\Docker Apps\ScanHound'
$results = [ordered]@{}

function Say([string]$m, [string]$colour = 'Gray') { Write-Host $m -ForegroundColor $colour }
function Head([string]$m) { Write-Host ''; Write-Host "=== $m ===" -ForegroundColor Cyan }
function Good([string]$m) { Write-Host "  OK    $m" -ForegroundColor Green }
function Bad([string]$m)  { Write-Host "  FAIL  $m" -ForegroundColor Red }
function Warn([string]$m) { Write-Host "  NOTE  $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------- preflight --
Head 'Preflight'

$admin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Bad 'Not elevated. Close this and start PowerShell with "Run as Administrator".'
    return
}
Good 'Running elevated'

if (-not (Test-Path -LiteralPath $Repo)) { Bad "Repo not found at $Repo"; return }
Set-Location -LiteralPath $Repo

# THE COMPOSE FILE IS THE ONE THING THAT CAN QUIETLY BREAK.
# docker-compose.yml carries the DV ingest key and the 127.0.0.1:9721 binding as
# UNCOMMITTED working-tree edits. A git operation that loses them silently breaks
# DV detection. It has already been swallowed once today by a `git stash`, so it
# is fingerprinted before and after and the script stops if it changes.
$composePath = Join-Path $Repo 'docker-compose.yml'
$composeBefore = (Get-FileHash -LiteralPath $composePath -Algorithm SHA256).Hash
$composeBackup = "C:\DockerData\scanhound\docker-compose.pre-merge-$(Get-Date -Format 'yyyyMMdd-HHmmss').yml"
New-Item -ItemType Directory -Force -Path (Split-Path $composeBackup) -ErrorAction SilentlyContinue | Out-Null
Copy-Item -LiteralPath $composePath -Destination $composeBackup -Force
Good "docker-compose.yml backed up to $composeBackup"

$port = (Select-String -LiteralPath $composePath -Pattern '9721:9721' -AllMatches).Count
if ($port -lt 1) { Bad 'compose is missing the 127.0.0.1:9721 binding BEFORE we start. Stopping.'; return }
Good 'compose has its required local modifications'

# ------------------------------------------------------------------- merge ---
Head 'Step 1 of 6  -  Merge the reviewed work into main'
Say '  feat/bulk-clear-download-results contains BOTH approved branches (14 commits).'
Say '  Verified clean in an isolated worktree before this script was written.'

git checkout main 2>&1 | Out-Null
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'main') { Bad "Could not switch to main (still on $branch). Stopping."; return }
git pull --ff-only 2>&1 | Out-Null
$before = (git rev-parse --short HEAD).Trim()

git merge --no-ff feat/bulk-clear-download-results -m "Merge the 2026-08-16 queue fixes and bulk clear (3 peer-review rounds, approved)" 2>&1 | Out-Null
$after = (git rev-parse --short HEAD).Trim()
$conflicts = git diff --name-only --diff-filter=U
if ($conflicts) {
    Bad "Merge conflicts in: $($conflicts -join ', ')"
    Warn 'Run `git merge --abort` and tell Claude. Nothing else in this script has run.'
    return
}
if ($after -eq $before) { Bad 'Merge produced no new commit. Stopping.'; return }
Good "main moved $before -> $after"
$results['merge'] = "$before -> $after"

git push 2>&1 | Out-Null
$remote = (git ls-remote origin main).Split()[0].Substring(0,7)
if ($remote -ne $after) {
    Bad "Push did not land. Remote is $remote, local is $after."
    Warn 'A push can report success and move nothing -- that happened today. Stopping before deploy.'
    return
}
Good "pushed; remote main = $remote"
$results['push'] = $remote

$composeAfterGit = (Get-FileHash -LiteralPath $composePath -Algorithm SHA256).Hash
if ($composeAfterGit -ne $composeBefore) {
    Bad 'docker-compose.yml CHANGED during the git operations.'
    Warn "Restore it with:  Copy-Item '$composeBackup' '$composePath' -Force"
    return
}
Good 'compose survived the git operations unchanged'

# ------------------------------------------------------------------ deploy ---
Head 'Step 2 of 6  -  Rebuild and deploy (this takes 10+ minutes)'
Say '  Expect a long silence. That is normal.'

docker compose up -d --build 2>&1 | Select-Object -Last 6 | ForEach-Object { Say "    $_" }

Start-Sleep -Seconds 10
$running = (docker ps --filter 'name=scanhound' --format '{{.Names}}') -join ''
if ($running -notmatch 'scanhound') {
    Bad 'The scanhound container is not running after the deploy.'
    Warn 'Check: docker compose logs --tail 50 scanhound'
    return
}
Good 'scanhound container is running'
$results['deploy'] = 'container up'

# --------------------------------------------------- verify the code is live -
Head 'Step 3 of 6  -  Prove the container is really running the new code'
Say '  Deploying and assuming is how a fix stayed invisible for two weeks.'

$marker = docker exec scanhound grep -c '_results_state' /app/backend/download_service.py 2>$null
if ("$marker".Trim() -match '^[1-9]') {
    Good "new code IS live (found $($marker.Trim()) references to the new locking)"
    $results['verified_live'] = "yes ($($marker.Trim()))"
} else {
    Bad 'The container is NOT running the new code.'
    Warn 'The image may not have rebuilt. Try: docker compose up -d --build --force-recreate'
    $results['verified_live'] = 'NO'
}

$defer = docker exec scanhound grep -c "queue_reason = 'item_retry'" /app/backend/download_queue.py 2>$null
if ("$defer".Trim() -eq '0') {
    Good 'the crash-on-every-reveal-stall bug is gone from the running container'
} else {
    Warn 'the old item_retry bug is still present in the container'
}

# ------------------------------------------------------------ backup task ----
Head 'Step 4 of 6  -  Schedule the database backup'
Say '  A verified 50 MB backup was taken by hand today. Nothing repeats it.'

$backupScript = 'C:\DockerData\infra-ops\scripts\scanhound-backup.ps1'
if (-not (Test-Path -LiteralPath $backupScript)) {
    Bad "Backup script missing at $backupScript"
} else {
    schtasks /Create /TN "ScanHound DB Backup" `
        /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$backupScript`"" `
        /SC DAILY /ST 02:00 /RU SYSTEM /RL HIGHEST /F 2>&1 | Out-Null

    $task = Get-ScheduledTask -TaskName 'ScanHound DB Backup' -ErrorAction SilentlyContinue
    if ($task) {
        Good "registered; next run $((Get-ScheduledTaskInfo $task).NextRunTime)"
        $results['backup_task'] = 'registered'
    } else {
        Bad 'Task did not register.'
        $results['backup_task'] = 'FAILED'
    }
}

# ------------------------------------------------------- pinned mount script -
Head 'Step 5 of 6  -  Install the mount-script logging'
Say '  The task has failed ~288x/day and recorded the reason zero times,'
Say '  because it runs with no console. This makes it write a log.'

$src = Join-Path $Repo 'scripts\mount-nas-shares.ps1'
$dst = 'C:\ProgramData\ScanHound\deploy\mount-nas-shares.ps1'
if (-not (Test-Path -LiteralPath $src)) {
    Bad "Source not found at $src (did the merge run?)"
} else {
    Copy-Item -LiteralPath $dst -Destination "$dst.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')" -Force -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $has = (Select-String -LiteralPath $dst -Pattern 'Write-RunLog' -AllMatches).Count
    if ($has -ge 3) {
        Good "installed (log calls present: $has). Next failure will explain itself."
        Say  '        log: C:\ProgramData\ScanHound\logs\mount-nas-shares.log'
        $results['mount_logging'] = 'installed'
    } else {
        Bad 'Copy did not take -- the pinned file has no logging calls.'
        $results['mount_logging'] = 'FAILED'
    }
}

# ------------------------------------------------- the verification hold -----
Head 'Step 6 of 6  -  The 39 held downloads  (YOUR DECISION)'
Say '  One interactive challenge armed a SOURCE-WIDE hold on HDEncode.'
Say '  39 items are held behind it. No timer clears it -- by design, only a'
Say '  successful probe does, so a timer cannot feed items back into a challenge.'
Say ''
Say '  Releasing it lets those 39 start contacting HDEncode again.'
Say '  If the challenge is still up, they will simply re-arm the hold.'
$answer = Read-Host '  Release the hold now? (y/N)'
if ($answer -eq 'y') {
    $body = '{"source":"hdencode"}'
    try {
        $r = Invoke-RestMethod -Method Post -Uri 'http://localhost:9721/api/download/verification-hold/clear' `
                               -ContentType 'application/json' -Body $body -TimeoutSec 30
        Good "hold released: $($r | ConvertTo-Json -Compress)"
        $results['hold'] = 'released'
    } catch {
        Bad "Could not reach the API: $($_.Exception.Message)"
        Warn 'If the app requires a login, release it from the UI instead once PR #84 is merged.'
        $results['hold'] = 'FAILED'
    }
} else {
    Say '  Left armed. Nothing sent to HDEncode.'
    $results['hold'] = 'left armed'
}

# ----------------------------------------------------------------- summary ---
Head 'Summary'
foreach ($k in $results.Keys) { Say ("  {0,-16} {1}" -f $k, $results[$k]) }
Say ''
Say 'Compose backup kept at:' ; Say "  $composeBackup"
Say ''
Say 'Send Claude the lines above, plus anything marked FAIL.'
