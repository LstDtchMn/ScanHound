# Disposable Task Scheduler semantics test.
#
# Stage 0 rests on how Task Scheduler actually behaves on THIS host. Exported
# XML proves a SETTING exists; it does not prove the runtime does what the
# setting's name suggests. This measures the behaviour.
#
# Touches NOTHING production: no Docker, no WSL, no NAS, no ScanHound mutex, no
# modification of the real task. It registers its own throwaway tasks under
# distinct names, observes them, and removes them in a finally block.
#
# TWO PHASES, deliberately separated:
#
#   A. Repetition + overlap. A task WITH a repeating trigger, whose first run
#      stays alive across the next boundary. Proves the trigger fires and that
#      IgnoreNew suppresses the overlapping occurrence.
#
#   B. Restart on nonzero exit. A task with NO repeating trigger at all, whose
#      action exits nonzero. Any second invocation can therefore ONLY be a
#      RestartOnFailure restart -- there is nothing else that could produce one.
#
# Phase B replaces an earlier design that ran one task with both a repeating
# trigger and a failing run, then tried to tell restarts from repetitions by
# timing. That heuristic could not distinguish them reliably, and worse, it
# could report the restart claim as satisfied by a run that was merely the next
# repetition. Removing the repeating trigger removes the ambiguity instead of
# reasoning about it.
#
# Phase B reports a MEASUREMENT, not a pass/fail: on this host (2026-07-26)
# RestartOnFailure did NOT fire on a nonzero action exit, which is a real and
# important property of the platform, not a defect in the test.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File test-scheduler-semantics.ps1
#
# Elevated is preferred: only then can the disposable tasks match production's
# RunLevel=Highest. Unelevated it falls back to RunLevel=Limited and says so.

[CmdletBinding()]
param(
    [switch]$Worker,
    [string]$StateDir,
    [int]$HoldSeconds  = 100,
    [int]$FailExitCode = 42,

    [int]$RepetitionMinutes = 1,
    [int]$RestartMinutes    = 1
)

$ErrorActionPreference = 'Stop'
$TaskA = 'ScanHound-SchedulerTest-Repetition'
$TaskB = 'ScanHound-SchedulerTest-Restart'

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

function Write-Event([string]$dir, [hashtable]$record) {
    # Exclusive append. Concurrency is the thing under test, so the
    # instrumentation must not be what loses the race.
    $line = ($record | ConvertTo-Json -Compress)
    $path = Join-Path $dir 'events.jsonl'
    for ($i = 0; $i -lt 80; $i++) {
        try {
            $fs = [IO.File]::Open($path, 'Append', 'Write', 'None')
            try {
                $sw = New-Object IO.StreamWriter($fs)
                $sw.WriteLine($line); $sw.Flush(); $sw.Dispose()
            } finally { $fs.Dispose() }
            return
        } catch { Start-Sleep -Milliseconds 100 }
    }
    throw "Could not append to $path"
}

function Get-InvocationNumber([string]$dir) {
    # CreateNew fails if the name exists, so each invocation claims a distinct
    # number without a read-modify-write race.
    for ($n = 1; $n -le 200; $n++) {
        try {
            $fs = [IO.File]::Open((Join-Path $dir "claim.$n"), 'CreateNew', 'Write', 'None')
            $fs.Dispose(); return $n
        } catch { continue }
    }
    throw "Could not claim an invocation number"
}

if ($Worker) {
    if (-not $StateDir -or -not (Test-Path -LiteralPath $StateDir)) { exit 90 }
    $n = Get-InvocationNumber $StateDir
    # NOT $pid -- PowerShell is case-insensitive, so that would assign to the
    # automatic $PID rather than create a local.
    $myPid = $PID
    Write-Event $StateDir @{ event = 'start'; n = $n; pid = $myPid; t = (Get-Date).ToString('o') }

    # Hold only in phase A's first run, to span a repetition boundary.
    if ($HoldSeconds -gt 0 -and $n -eq 1) { Start-Sleep -Seconds $HoldSeconds }
    else { Start-Sleep -Seconds 2 }

    Write-Event $StateDir @{ event = 'exit'; n = $n; pid = $myPid
                             code = $FailExitCode; t = (Get-Date).ToString('o') }
    exit $FailExitCode
}

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

$principalCheck = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
$isElevated = $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$runLevel   = if ($isElevated) { 'Highest' } else { 'Limited' }
$me         = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$self       = $PSCommandPath

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$root  = "C:\ProgramData\ScanHound\task-test\$stamp"
New-Item -ItemType Directory -Force $root | Out-Null

Write-Output "=== Task Scheduler semantics test ==="
Write-Output "evidence  : $root"
Write-Output "run level : $runLevel$(if (-not $isElevated) { '   (NOT elevated -- does not match production RunLevel=Highest)' })"

function New-ProbeTask {
    param($Name, $Dir, $Hold, $Trigger)
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
        "-NoProfile -ExecutionPolicy Bypass -File `"$self`" -Worker -StateDir `"$Dir`" " +
        "-HoldSeconds $Hold -FailExitCode $FailExitCode")
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes $RestartMinutes) `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    $settings.DisallowStartIfOnBatteries = $false
    $settings.AllowHardTerminate         = $true
    $principal = New-ScheduledTaskPrincipal -Id 'Author' -UserId $me `
        -LogonType Interactive -RunLevel $runLevel
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Settings $settings -Principal $principal -Force `
        -Description 'DISPOSABLE ScanHound scheduler probe. Safe to delete.' | Out-Null
}

function Read-Events([string]$dir) {
    $p = Join-Path $dir 'events.jsonl'
    if (-not (Test-Path $p)) { return @() }
    @(Get-Content $p | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json }) |
        Sort-Object { [datetime]$_.t }
}

function Wait-Until([datetime]$deadline, [string]$dir) {
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 15
        $n = @(Read-Events $dir | Where-Object { $_.event -eq 'start' }).Count
        Write-Output ("  {0}  invocations={1}" -f (Get-Date).ToString('HH:mm:ss'), $n)
    }
}

$results = [ordered]@{}
$failed  = 0

# try/finally around EVERYTHING after the first registration: an abort between
# registering and unregistering would otherwise leave a task on the machine
# firing every minute, forever, long after this script is gone.
try {
    # --- Phase A: repetition fires, IgnoreNew suppresses overlap ------------
    Write-Output "`n--- Phase A: repetition + overlap (repeating trigger) ---"
    $dirA = Join-Path $root 'A'; New-Item -ItemType Directory -Force $dirA | Out-Null
    $startA = (Get-Date).AddSeconds(30)
    $trigA  = New-ScheduledTaskTrigger -Once -At $startA `
        -RepetitionInterval (New-TimeSpan -Minutes $RepetitionMinutes)
    New-ProbeTask -Name $TaskA -Dir $dirA -Hold $HoldSeconds -Trigger $trigA
    Write-Output "first run at $($startA.ToString('HH:mm:ss')); run 1 holds ${HoldSeconds}s across the $($RepetitionMinutes)-min boundary"
    Wait-Until $startA.AddSeconds($HoldSeconds + 150) $dirA

    $evA = Read-Events $dirA
    $concurrent = 0; $peak = 0
    foreach ($e in $evA) {
        if ($e.event -eq 'start') { $concurrent++; if ($concurrent -gt $peak) { $peak = $concurrent } }
        else { $concurrent-- }
    }
    $startsA = @($evA | Where-Object { $_.event -eq 'start' })
    Write-Output "`nPhase A timeline:"
    foreach ($e in $evA) {
        Write-Output ("  T+{0,4}s  {1,-5} n={2}" -f [int](([datetime]$e.t) - $startA).TotalSeconds, $e.event, $e.n)
    }
    $results['repeating trigger fires']              = ($startsA.Count -ge 2)
    $results['IgnoreNew: peak concurrency is 1']     = ($peak -eq 1)

    # --- Phase B: does a nonzero exit trigger a restart? --------------------
    # NO repeating trigger, so a second invocation can only be a restart.
    Write-Output "`n--- Phase B: RestartOnFailure on a nonzero action exit (NO repeating trigger) ---"
    $dirB = Join-Path $root 'B'; New-Item -ItemType Directory -Force $dirB | Out-Null
    $startB = (Get-Date).AddSeconds(30)
    $trigB  = New-ScheduledTaskTrigger -Once -At $startB      # no repetition
    New-ProbeTask -Name $TaskB -Dir $dirB -Hold 0 -Trigger $trigB

    $xmlB = [xml](Export-ScheduledTask -TaskName $TaskB)
    $rof  = $xmlB.Task.Settings.RestartOnFailure
    $reps = @($xmlB.SelectNodes('//*[local-name()="Repetition"]')).Count
    Write-Output "registered RestartOnFailure: Count=$($rof.Count) Interval=$($rof.Interval); repeating triggers=$reps"
    if ($reps -ne 0) { throw "Phase B has a repeating trigger; the measurement would be ambiguous." }

    Wait-Until $startB.AddSeconds(($RestartMinutes * 60 * 3) + 60) $dirB

    $evB     = Read-Events $dirB
    $startsB = @($evB | Where-Object { $_.event -eq 'start' })
    $infoB   = Get-ScheduledTaskInfo -TaskName $TaskB
    Write-Output "`nPhase B: invocations=$($startsB.Count)  LastTaskResult=$($infoB.LastTaskResult)"

    $results['Windows observed the nonzero exit'] = ($infoB.LastTaskResult -eq $FailExitCode)
    $restartFires = ($startsB.Count -ge 2)

    Write-Output ""
    Write-Output "MEASURED: RestartOnFailure $(if ($restartFires) { 'DOES' } else { 'does NOT' }) fire on a nonzero action exit."
    if (-not $restartFires) {
        Write-Output "  => Retry CANNOT come from RestartOnFailure on this host. The repeating"
        Write-Output "     trigger's interval is the real retry interval; size it accordingly."
    }
    $summaryExtra = @{ restart_on_failure_fires = $restartFires
                       phase_b_invocations      = $startsB.Count
                       last_task_result         = $infoB.LastTaskResult }

    Write-Output "`n=== assertions ==="
    foreach ($k in $results.Keys) {
        $ok = [bool]$results[$k]
        Write-Output ("{0}  {1}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $k)
        if (-not $ok) { $failed++ }
    }

    ([ordered]@{ run_level = $runLevel; elevated = $isElevated
                 results = $results; failed = $failed } + $summaryExtra) |
        ConvertTo-Json -Depth 5 | Out-File (Join-Path $root 'summary.json') -Encoding utf8
}
finally {
    foreach ($n in @($TaskA, $TaskB)) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
            Write-Output "unregistered disposable task: $n"
        }
    }
}

Write-Output "evidence: $root"
if ($failed -gt 0) { Write-Output "`n=== $failed ASSERTION FAILURE(S) ==="; exit 1 }
Write-Output "`n=== all assertions passed ==="
exit 0
