# ScanHound - the steps still outstanding after tonight's deploy.
#
# RUN IN AN ELEVATED POWERSHELL (Run as Administrator).
#
# WHAT ALREADY WORKED, so this script does not redo it:
#   merge + push + deploy   done 20:42-20:43; verified live in the container
#   mount-script logging    installed, and already recording the real reason
#
# WHAT DID NOT, and why:
#   the backup task         MY BUG. The previous script built the schtasks
#                           command as a string with nested escaped quotes:
#                             /TR "powershell.exe ... -File `"$path`""
#                           PowerShell mangles that on the way to schtasks.exe,
#                           so /TR arrived malformed. Worse, I piped the result
#                           to Out-Null, so the failure was invisible and the
#                           script reported on to the next step.
#                           This version uses Register-ScheduledTask with real
#                           objects -- no string parsing -- and prints the error
#                           if it still fails.
#
# Steps 3 and 4 are PROMPTED. They change what the system does, so they are
# yours to choose, not something a script should decide.
#
# Safe to re-run.

$ErrorActionPreference = 'Continue'
$Repo = 'X:\Docker Apps\ScanHound'
$results = [ordered]@{}

function Say([string]$m) { Write-Host $m -ForegroundColor Gray }
function Head([string]$m) { Write-Host ''; Write-Host "=== $m ===" -ForegroundColor Cyan }
function Good([string]$m) { Write-Host "  OK    $m" -ForegroundColor Green }
function Bad([string]$m)  { Write-Host "  FAIL  $m" -ForegroundColor Red }
function Warn([string]$m) { Write-Host "  NOTE  $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------- preflight --
Head 'Preflight'

$admin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Bad 'Not elevated. Restart PowerShell with "Run as Administrator".'; return }
Good 'Running elevated'

if (-not (Test-Path -LiteralPath $Repo)) { Bad "Repo not found at $Repo"; return }
Set-Location -LiteralPath $Repo
Good "Repo found"

# --------------------------------------------------- 1. the backup schedule --
Head 'Step 1 of 4  -  Schedule the nightly database backup'
Say '  The database has ONE backup, taken by hand at 18:50 today. Nothing repeats'
Say '  it. This is the most important thing left.'

$backupScript = 'C:\DockerData\infra-ops\scripts\scanhound-backup.ps1'
if (-not (Test-Path -LiteralPath $backupScript)) {
    Bad "Backup script missing at $backupScript"
    $results['backup_task'] = 'FAILED - script missing'
} else {
    # Register-ScheduledTask, NOT schtasks. The argument is passed as a real
    # string on a real object, so nothing has to survive two rounds of quoting.
    try {
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $backupScript)
        $trigger   = New-ScheduledTaskTrigger -Daily -At 2am
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' `
                        -LogonType ServiceAccount -RunLevel Highest
        $settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 1)

        Register-ScheduledTask -TaskName 'ScanHound DB Backup' -Action $action `
            -Trigger $trigger -Principal $principal -Settings $settings `
            -Description 'Nightly SQLite online-backup of ScanHound crawler.db, verified after write.' `
            -Force -ErrorAction Stop | Out-Null

        $task = Get-ScheduledTask -TaskName 'ScanHound DB Backup' -ErrorAction SilentlyContinue
        if ($task) {
            $info = Get-ScheduledTaskInfo $task
            Good "registered as SYSTEM; next run $($info.NextRunTime)"
            $results['backup_task'] = 'registered'
        } else {
            Bad 'Register-ScheduledTask reported success but the task is not there.'
            $results['backup_task'] = 'FAILED - not present after register'
        }
    } catch {
        # PRINTED, not swallowed. Hiding this is why the last attempt looked fine.
        Bad "Could not register: $($_.Exception.Message)"
        $results['backup_task'] = 'FAILED'
    }
}

# ------------------------------------------- 2. prove the SCHEDULE works -----
Head 'Step 2 of 4  -  Prove the scheduled task actually runs'
Say '  Registering a task and assuming it works is how the .ps1.NEW backup sat'
Say '  unrunnable for a day. This runs it THROUGH the scheduler and checks a new'
Say '  file appears -- proving the task definition works, not just the script.'

if ($results['backup_task'] -eq 'registered') {
    $before = @(Get-ChildItem 'C:\DockerData\scanhound-backup\db\*.db' -ErrorAction SilentlyContinue).Count
    Start-ScheduledTask -TaskName 'ScanHound DB Backup'
    Say '  running... (allow up to 2 minutes)'
    $deadline = (Get-Date).AddMinutes(2)
    do {
        Start-Sleep -Seconds 10
        $after = @(Get-ChildItem 'C:\DockerData\scanhound-backup\db\*.db' -ErrorAction SilentlyContinue).Count
        $state = (Get-ScheduledTask -TaskName 'ScanHound DB Backup').State
    } while ($state -eq 'Running' -and (Get-Date) -lt $deadline)

    $rc = (Get-ScheduledTaskInfo -TaskName 'ScanHound DB Backup').LastTaskResult
    if ($after -gt $before) {
        $newest = Get-ChildItem 'C:\DockerData\scanhound-backup\db\*.db' |
                  Sort-Object LastWriteTime -Descending | Select-Object -First 1
        Good ("scheduled run produced {0} ({1:N0} MB), exit {2}" -f `
              $newest.Name, ($newest.Length / 1MB), $rc)
        $results['backup_proven'] = 'yes'
    } else {
        Bad "No new backup file appeared (task exit code $rc)."
        Warn 'Check C:\DockerData\scanhound-backup\scanhound-backup.log'
        $results['backup_proven'] = 'NO'
    }
} else {
    Warn 'skipped - the task did not register'
    $results['backup_proven'] = 'skipped'
}

# ------------------------------------------------ 3. the 39 held downloads ---
Head 'Step 3 of 4  -  Release the verification hold  (YOUR CHOICE)'
Say '  39 downloads are held behind ONE interactive challenge on HDEncode.'
Say '  No timer clears it -- only a probe that genuinely succeeds.'
Say ''
Say '  The container was rebuilt at 20:43, so this is a fair moment to retry.'
Say '  If the challenge is still up they will simply re-arm the hold, which'
Say '  costs nothing and tells you the challenge is still there.'
Say '  NOTE: this needs a login token. If it comes back 401, the hold card in'
Say '  step 4 is what gives you a button for it.'
$ans = Read-Host '  Try to release it? (y/N)'
if ($ans -eq 'y') {
    # THE PATH HAS NO /api PREFIX. app.include_router() is called without one,
    # so the route is /download/... . The first attempt used /api/download/...,
    # which fell through to the SPA catch-all -- a GET-only route -- and returned
    # 405 Method Not Allowed. That read like a broken endpoint when it was a
    # wrong URL. The real path answers 401, i.e. it exists and wants a token.
    try {
        $r = Invoke-RestMethod -Method Post `
                -Uri 'http://localhost:9721/download/verification-hold/clear' `
                -ContentType 'application/json' -Body '{"source":"hdencode"}' -TimeoutSec 30
        Good "released - cleared $($r.cleared) marker(s). $($r.next_action)"
        $results['hold'] = "released ($($r.cleared))"
    } catch {
        $code = $null
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        if ($code -eq 401) {
            Warn 'The API needs a login token, which this script deliberately does not handle.'
            Warn 'Release it from the UI instead -- that is exactly the button the hold'
            Warn 'card adds, so merging it in step 4 gives you a way to do this.'
            $results['hold'] = 'needs the UI (401)'
        } else {
            Bad "Could not release: $($_.Exception.Message)"
            $results['hold'] = 'FAILED'
        }
    }
} else {
    Say '  Left armed.'
    $results['hold'] = 'left armed'
}

# -------------------------------------------------- 4. merge the hold card ---
Head 'Step 4 of 4  -  Merge the verification-hold card  (YOUR CHOICE)'
Say '  Peer-reviewed twice; both MEDIUMs and all three code follow-ups closed.'
Say '  Verified to merge cleanly into main.'
Say ''
Say '  It replaces 39 identical "Retry after 8:57 PM" cards with ONE card saying'
Say '  what is actually wrong, and gives you a Release button on your phone.'
Say ''
Warn 'NOT yet checked in a browser by anyone. That is the remaining gap, and it'
Warn 'is the same layer where the worst bug of the evening hid.'
$ans2 = Read-Host '  Merge and deploy it? (y/N)'
if ($ans2 -eq 'y') {
    $dirty = (git status --porcelain --untracked-files=no) |
             Where-Object { $_ -and ($_ -notmatch 'docker-compose\.yml') }
    if ($dirty) {
        Bad 'Uncommitted changes would block the merge:'
        $dirty | ForEach-Object { Say "        $_" }
        $results['hold_card'] = 'blocked - dirty tree'
    } else {
        $composePath = Join-Path $Repo 'docker-compose.yml'
        $composeBefore = (Get-FileHash -LiteralPath $composePath).Hash

        git checkout main 2>&1 | Out-Null
        if ((git rev-parse --abbrev-ref HEAD).Trim() -ne 'main') {
            Bad 'Could not switch to main.'
            $results['hold_card'] = 'FAILED'
        } else {
            $before = (git rev-parse --short HEAD).Trim()
            git merge --no-ff feat/source-hold-surface -m "Merge the source-level verification-hold surface (2 review rounds)" 2>&1 | Out-Null
            $conf = git diff --name-only --diff-filter=U
            if ($conf) {
                Bad "Conflicts: $($conf -join ', ')  -- run: git merge --abort"
                $results['hold_card'] = 'CONFLICTS'
            } else {
                $after = (git rev-parse --short HEAD).Trim()
                git push 2>&1 | Out-Null
                $remote = (git ls-remote origin main).Split()[0].Substring(0,7)
                if ($remote -ne $after) {
                    Bad "Push did not land (remote $remote, local $after)."
                    $results['hold_card'] = 'push FAILED'
                } elseif ((Get-FileHash -LiteralPath $composePath).Hash -ne $composeBefore) {
                    Bad 'docker-compose.yml changed during the merge. Stopping before deploy.'
                    $results['hold_card'] = 'compose changed'
                } else {
                    Good "merged $before -> $after and pushed"
                    Say '  rebuilding (10+ minutes, expect silence)...'
                    docker compose up -d --build 2>&1 | Select-Object -Last 4 | ForEach-Object { Say "    $_" }
                    Start-Sleep -Seconds 10
                    $live = docker exec scanhound grep -c 'active_verification_holds' /app/backend/download_queue.py 2>$null
                    if ("$live".Trim() -match '^[1-9]') {
                        Good 'hold card is LIVE in the container'
                        $results['hold_card'] = 'deployed'
                    } else {
                        Bad 'Deployed, but the new code is not in the container.'
                        $results['hold_card'] = 'deploy unverified'
                    }
                }
            }
        }
    }
} else {
    Say '  Skipped.'
    $results['hold_card'] = 'not merged'
}

# ----------------------------------------------------------------- summary ---
Head 'Summary'
foreach ($k in $results.Keys) { Say ("  {0,-16} {1}" -f $k, $results[$k]) }

if ($results['hold_card'] -eq 'deployed') {
    Write-Host ''
    Write-Host '  CHECK ON YOUR PHONE (the untested layer):' -ForegroundColor Yellow
    Say '    1. one held source shows ONE card, not 39 rows'
    Say '    2. unrelated failed/ready retries are still visible'
    Say '    3. "Show the N paused" reveals only that source rows'
    Say '    4. the card says it will NOT clear on a timer'
}
Write-Host ''
Say 'Send Claude everything above, including anything marked FAIL.'
