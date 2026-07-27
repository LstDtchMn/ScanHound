# Disposable Task Scheduler semantics test.
#
# Stage 0's load-bearing claim is: "a failed mount run exits nonzero and Task
# Scheduler starts another attempt." Exported XML proves the SETTING exists.
# It does not prove that this host, this principal and this PowerShell action
# produce the expected runtime sequence. This test proves the mechanism.
#
# Touches NOTHING production: no Docker, no WSL, no NAS, no ScanHound mutex,
# no modification of the real task. It registers its own throwaway task under a
# distinct name, observes it, asserts, and unregisters.
#
# Proves in one run:
#   * the repeating trigger fires;
#   * IgnoreNew suppresses an overlapping repetition;
#   * a nonzero exit activates RestartOnFailure;
#   * a successful restart ends the restart chain;
#   * periodic firing continues afterwards.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File test-scheduler-semantics.ps1
#
# Elevated is preferred: only then can the disposable task match production's
# RunLevel=Highest. Unelevated it falls back to RunLevel=Limited and says so --
# the scheduler mechanisms under test are not RunLevel-dependent, but the match
# is imperfect and the report states which mode produced the evidence.

[CmdletBinding()]
param(
    # Internal: the scheduled action re-invokes this same file as the worker.
    [switch]$Worker,
    [string]$StateDir,

    [int]$RepetitionMinutes = 2,
    [int]$RestartMinutes    = 1,
    [int]$HoldSeconds       = 140,   # > RepetitionMinutes so run 1 spans a boundary
    [int]$FailExitCode      = 42
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ScanHound-SchedulerSemanticsTest'

# ---------------------------------------------------------------------------
# Worker: the body the disposable task actually executes
# ---------------------------------------------------------------------------

function Write-Event([string]$dir, [hashtable]$record) {
    # Append under an exclusive handle. Concurrent invocations are the very
    # thing under test, so the log must not be the thing that loses the race.
    $line = ($record | ConvertTo-Json -Compress)
    $path = Join-Path $dir 'events.jsonl'
    for ($i = 0; $i -lt 60; $i++) {
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
    # Atomic counter: CreateNew fails if another invocation already claimed
    # this number, so each invocation ends up with a distinct one.
    for ($n = 1; $n -le 100; $n++) {
        $claim = Join-Path $dir "claim.$n"
        try {
            $fs = [IO.File]::Open($claim, 'CreateNew', 'Write', 'None')
            $fs.Dispose()
            return $n
        } catch { continue }
    }
    throw "Could not claim an invocation number"
}

if ($Worker) {
    if (-not $StateDir -or -not (Test-Path -LiteralPath $StateDir)) {
        exit 90   # misconfigured worker; orchestrator will see no events
    }
    $n   = Get-InvocationNumber $StateDir
    # NOT $pid -- PowerShell is case-insensitive, so that assigns to the
    # automatic $PID variable rather than creating a local.
    $myPid = $PID
    Write-Event $StateDir @{ event = 'start'; n = $n; pid = $myPid
                             t = (Get-Date).ToString('o') }
    if ($n -eq 1) {
        # Stay alive across the next repetition boundary, then fail.
        Start-Sleep -Seconds $HoldSeconds
        Write-Event $StateDir @{ event = 'exit'; n = $n; pid = $myPid
                                 code = $FailExitCode; t = (Get-Date).ToString('o') }
        exit $FailExitCode
    }
    Start-Sleep -Seconds 2
    Write-Event $StateDir @{ event = 'exit'; n = $n; pid = $myPid
                             code = 0; t = (Get-Date).ToString('o') }
    exit 0
}

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

$principalCheck = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
$isElevated = $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$runLevel   = if ($isElevated) { 'Highest' } else { 'Limited' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dir   = "C:\ProgramData\ScanHound\task-test\$stamp"
New-Item -ItemType Directory -Force $dir | Out-Null

Write-Output "=== Task Scheduler semantics test ==="
Write-Output "state dir  : $dir"
Write-Output "run level  : $runLevel$(if (-not $isElevated) { '  (NOT elevated -- does not match production RunLevel=Highest)' })"
Write-Output "repetition : $RepetitionMinutes min   restart: $RestartMinutes min   hold: $HoldSeconds s"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$self   = $PSCommandPath
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    "-NoProfile -ExecutionPolicy Bypass -File `"$self`" -Worker -StateDir `"$dir`" " +
    "-HoldSeconds $HoldSeconds -FailExitCode $FailExitCode")

$start   = (Get-Date).AddSeconds(30)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes $RepetitionMinutes)

# Mirror production's policies -- these are what is under test.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes $RestartMinutes) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$settings.DisallowStartIfOnBatteries = $false
$settings.AllowHardTerminate         = $true

$principal = New-ScheduledTaskPrincipal -Id 'Author' `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value) `
    -LogonType Interactive -RunLevel $runLevel

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'DISPOSABLE Stage 0 scheduler-semantics probe. Safe to delete.' | Out-Null

$t0 = $start
Write-Output "first run  : $($t0.ToString('HH:mm:ss'))"

# Watch long enough for: run1 (fail) -> restart (succeed) -> next repetition.
$deadline = $t0.AddMinutes($RepetitionMinutes * 2).AddSeconds($HoldSeconds + 120)
Write-Output "watching until $($deadline.ToString('HH:mm:ss'))...`n"

$seen = 0
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 15
    $p = Join-Path $dir 'events.jsonl'
    if (Test-Path $p) {
        $c = (Get-Content $p | Measure-Object -Line).Lines
        if ($c -ne $seen) {
            $seen = $c
            Write-Output ("  [{0}] {1} events" -f (Get-Date).ToString('HH:mm:ss'), $c)
        }
    }
}

# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------

$events = @()
$p = Join-Path $dir 'events.jsonl'
if (Test-Path $p) {
    $events = Get-Content $p | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json } |
              Sort-Object { [datetime]$_.t }
}

Write-Output "`n=== observed timeline ==="
foreach ($e in $events) {
    $off = [int]((([datetime]$e.t) - $t0).TotalSeconds)
    Write-Output ("  T+{0,4}s  {1,-5} n={2} pid={3}{4}" -f $off, $e.event, $e.n, $e.pid,
        $(if ($e.event -eq 'exit') { "  exit=$($e.code)" } else { '' }))
}

# Replay for peak concurrency: the IgnoreNew proof.
$concurrent = 0; $peak = 0
foreach ($e in $events) {
    if ($e.event -eq 'start') { $concurrent++; if ($concurrent -gt $peak) { $peak = $concurrent } }
    else { $concurrent-- }
}

$starts = @($events | Where-Object { $_.event -eq 'start' })
$exits  = @($events | Where-Object { $_.event -eq 'exit' })
$failExit = $exits | Where-Object { $_.code -eq $FailExitCode } | Select-Object -First 1

# A restart is a start that follows the failing exit but is NOT on a repetition
# boundary -- boundaries are multiples of the interval from the start boundary.
$restartStart = $null
if ($failExit) {
    $tolerance = 25
    foreach ($s in $starts) {
        $ts = [datetime]$s.t
        if ($ts -le ([datetime]$failExit.t)) { continue }
        $off      = ($ts - $t0).TotalSeconds
        $fromRep  = [Math]::Abs($off - ([Math]::Round($off / ($RepetitionMinutes * 60)) * $RepetitionMinutes * 60))
        if ($fromRep -gt $tolerance) { $restartStart = $s; break }
    }
}

$laterStart = $null
if ($restartStart) {
    $laterStart = $starts | Where-Object { [datetime]$_.t -gt [datetime]$restartStart.t } |
                  Select-Object -First 1
}

$results = [ordered]@{
    'repeating trigger fired at all'            = ($starts.Count -ge 1)
    'peak concurrency is exactly 1 (IgnoreNew)' = ($peak -eq 1)
    'run 1 exited nonzero as designed'          = ($null -ne $failExit)
    'RestartOnFailure produced a new run'       = ($null -ne $restartStart)
    'the restart exited zero'                   = ($null -ne ($exits | Where-Object { $restartStart -and $_.n -eq $restartStart.n -and $_.code -eq 0 }))
    'periodic firing continued after success'   = ($null -ne $laterStart)
}

Write-Output "`n=== assertions ==="
$failed = 0
foreach ($k in $results.Keys) {
    $ok = [bool]$results[$k]
    Write-Output ("{0}  {1}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $k)
    if (-not $ok) { $failed++ }
}
Write-Output "`npeak concurrency observed: $peak   invocations: $($starts.Count)"

# Preserve evidence, remove the task.
$summary = [ordered]@{
    run_level = $runLevel; elevated = $isElevated; t0 = $t0.ToString('o')
    repetition_minutes = $RepetitionMinutes; restart_minutes = $RestartMinutes
    peak_concurrency = $peak; invocations = $starts.Count
    results = $results; failed = $failed
}
$summary | ConvertTo-Json -Depth 5 | Out-File (Join-Path $dir 'summary.json') -Encoding utf8

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "disposable task unregistered"
}

Write-Output "evidence: $dir"
if ($failed -gt 0) { Write-Output "`n=== $failed ASSERTION FAILURE(S) ==="; exit 1 }
Write-Output "`n=== all assertions passed ==="
exit 0
