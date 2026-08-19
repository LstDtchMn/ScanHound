# Registers ScanHound-DVScan: the daily Dolby Vision host detection pass.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-dv-scan-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-dv-scan-task.ps1 -WhatIf
#
# WHY THIS IS 200 LINES AND install-mount-task.ps1 IS 1000.
#
# That installer defends an ELEVATED task: RunLevel=Highest, firing hourly, so
# anything it executes is an elevated-execution input and a user-writable script
# path is a privilege-escalation route. Hence the ProgramData deployment, the
# git-object-store extraction and the ACL assertions.
#
# This task runs UNELEVATED as Jesse, at RunLevel=Limited, and must: it reads
# media files, runs dovi_tool.exe and POSTs to localhost:9721 -- none of which
# needs admin -- and it depends on a MAPPED DRIVE (Y:), which only exists inside
# his interactive logon session. Running it elevated would arguably see a
# DIFFERENT set of drive mappings and scan nothing. So there is no elevation
# boundary to defend here, and importing that apparatus would be cargo-culting a
# threat model that does not apply. What IS carried over is the part that earned
# its place: assert the INSTALLED task rather than trusting the registration
# call, and never let a failure register as a success.
#
# THE ONE HARD CONSTRAINT: Y: is a per-session network mapping
# (\\TURTLELANDSRV2\4K HDR Geronimo). LogonType=Interactive is therefore
# mandatory, and the honest limitation is that this task only runs while Jesse
# is logged on. run-dv-scan.ps1 turns an absent mapping into a loud exit 11
# rather than a silent zero-file "success".
#
# Uninstall:  Unregister-ScheduledTask -TaskName ScanHound-DVScan -Confirm:$false

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RepoRoot = 'X:\Docker Apps\ScanHound',
    [string]$TaskName = 'ScanHound-DVScan',
    # 03:00 local. Deliberately outside the download queue's busy evening window
    # and after the nightly NAS mount trigger has had time to settle.
    [string]$DailyAt  = '03:00',
    # RETRY COMES FROM REPETITION, NOT RestartOnFailure. Measured on this host
    # 2026-07-26: RestartOnFailure does NOT fire when the ACTION exits nonzero,
    # only when the task fails to LAUNCH. So if the 03:00 run aborts because the
    # NAS is briefly away (exit 11), the only thing that tries again is this
    # repetition. 4 hours x 6 gives a full day of chances without hammering.
    [int]$RetryHours  = 4
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$wrapper = Join-Path $RepoRoot 'scripts\run-dv-scan.ps1'

# --- preconditions (read-only; must not be skipped by -WhatIf) --------------

$requestedWhatIf  = $WhatIfPreference
$WhatIfPreference = $false

if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
    throw "Wrapper not found: $wrapper"
}
foreach ($required in @(
    (Join-Path $RepoRoot 'scripts\host-detector\dv_host_scan.py'),
    (Join-Path $RepoRoot 'scripts\host-detector\dovi_tool.exe'),
    (Join-Path $RepoRoot 'data\dv_host.json')
)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Detector dependency missing: $required"
    }
}

$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $PowerShellExe -PathType Leaf)) {
    throw "Windows PowerShell not found at '$PowerShellExe'."
}

# Register for the CURRENT user by SID. Resolved live rather than hard-coded so
# this cannot silently install a task for the wrong account.
$Sid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
Write-Output "user    : $env:USERNAME ($Sid)"
Write-Output "wrapper : $wrapper"

# Parse the time before anything mutates, so a typo fails here.
try   { $at = [datetime]::ParseExact($DailyAt, 'HH:mm', $null) }
catch { throw "-DailyAt must be HH:mm (24-hour); got '$DailyAt'." }

if ($RetryHours -lt 1 -or $RetryHours -gt 12) {
    throw "-RetryHours must be 1..12; got $RetryHours."
}

# Warn about the mapped-drive dependency at install time, using the config the
# detector will actually read.
$cfgPath = Join-Path $RepoRoot 'data\dv_host.json'
$cfg     = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
$rootsRaw = ''
if ($cfg.PSObject.Properties.Name -contains 'dv_library_roots') { $rootsRaw = $cfg.dv_library_roots }
$mapped = @($rootsRaw -split ';' | Where-Object { $_ -match '^[A-Za-z]:' })
if ($mapped.Count -gt 0) {
    Write-Output ""
    Write-Output "NOTE: $($mapped.Count) library root(s) use a drive letter:"
    $mapped | ForEach-Object { Write-Output "        $_" }
    Write-Output "      Drive mappings are per-session, so this task will only scan them"
    Write-Output "      while you are logged on. It exits 11 (not 0) if they are absent."
}

if ($requestedWhatIf) {
    Write-Output ""
    Write-Output "-WhatIf: preconditions pass; nothing registered."
    return
}
$WhatIfPreference = $requestedWhatIf

# --- definition ------------------------------------------------------------

$action = New-ScheduledTaskAction -Execute $PowerShellExe `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"{0}`"" -f $wrapper)

# Daily at $DailyAt, repeating every $RetryHours so a failed run gets further
# attempts the same day (see the RetryHours comment above for why repetition and
# not RestartOnFailure). MultipleInstances=IgnoreNew means a long scan is never
# overlapped by the next repetition.
$trigger = New-ScheduledTaskTrigger -Daily -At $at
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $at `
    -RepetitionInterval (New-TimeSpan -Hours $RetryHours) `
    -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

$settings.DisallowStartIfOnBatteries = $false
$settings.AllowHardTerminate         = $true
$settings.WakeToRun                  = $false

# Interactive + Limited, both load-bearing: Interactive because Y: is a
# per-session mapping, Limited because elevation would change which mappings the
# process sees and this task needs none of admin's powers.
$principal = New-ScheduledTaskPrincipal -Id 'Author' -UserId $Sid `
    -LogonType Interactive -RunLevel Limited

$desc = @"
ScanHound Dolby Vision host detection. Runs dovi_tool against the 4K DV
libraries, writes data/dv_host.db, then POSTs /rename/dv-import so the container
ingests the results. The container's label sync then updates the Plex DV
FEL/MEL/P8/P5 labels that the Kometa overlays key on.

Installed $(Get-Date -Format 'yyyy-MM-dd') because detection had NO scheduled
task at all: it was a manual script, last run 2026-07-25, leaving labels 14 days
stale.

Wrapper: $wrapper  (aborts with exit 11 if a library root is unreachable,
rather than scanning nothing and reporting success)
Logs   : $RepoRoot\data\dv-scan-logs

RETRY IS THE REPEATING TRIGGER, NOT RestartOnFailure -- the latter was measured
on this host not to fire on a nonzero action exit.
LIMITATION: LogonType=Interactive, required by the Y: mapping, means this only
runs while the account is logged on.
"@

if (-not $PSCmdlet.ShouldProcess($TaskName, 'Register scheduled task')) {
    Write-Output "-WhatIf: task NOT registered."
    return
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description $desc -Force | Out-Null

# --- assert what was INSTALLED, not what was intended ----------------------

$t = Get-ScheduledTask -TaskName $TaskName
$fail = @()

function Assert-Field($label, $actual, $expected) {
    $ok = ("$actual" -eq "$expected")
    Write-Output ("{0}  {1,-30} {2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $label, $actual)
    if (-not $ok) { $script:fail += "$label = '$actual' (expected '$expected')" }
}

# Windows resolves a SID-registered principal back to a display name, so compare
# identities rather than strings.
function Resolve-PrincipalSid([string]$value) {
    if (-not $value) { return '' }
    if ($value -match '^S-1-') { return $value }
    try {
        return (New-Object Security.Principal.NTAccount($value)
               ).Translate([Security.Principal.SecurityIdentifier]).Value
    } catch { return $value }
}

Write-Output ""
Write-Output "=== installed-task assertions ==="
Assert-Field 'action count'          $t.Actions.Count 1
Assert-Field 'action.Execute'        $t.Actions[0].Execute $PowerShellExe
Assert-Field 'action targets wrapper' ($t.Actions[0].Arguments -like "*$wrapper*") 'True'
Assert-Field 'principal.UserId'      (Resolve-PrincipalSid $t.Principal.UserId) $Sid
Assert-Field 'principal.LogonType'   $t.Principal.LogonType 'Interactive'
Assert-Field 'principal.RunLevel'    $t.Principal.RunLevel 'Limited'
Assert-Field 'MultipleInstances'     $t.Settings.MultipleInstances 'IgnoreNew'
Assert-Field 'StartWhenAvailable'    $t.Settings.StartWhenAvailable 'True'
Assert-Field 'ExecutionTimeLimit'    $t.Settings.ExecutionTimeLimit 'PT6H'
Assert-Field 'Enabled'               $t.Settings.Enabled 'True'

# A trigger in the XML does not prove the schedule is armed; only NextRunTime does.
$info = Get-ScheduledTaskInfo -TaskName $TaskName
if (-not $info.NextRunTime) {
    $fail += 'NextRunTime is null -- the schedule is not armed'
    Write-Output "FAIL  next run                       <null>"
} else {
    Write-Output ("PASS  {0,-30} {1:yyyy-MM-dd HH:mm:ss}" -f 'next run', $info.NextRunTime)
}

Write-Output ""
if ($fail.Count -gt 0) {
    Write-Output "=== $($fail.Count) ASSERTION FAILURE(S) ==="
    $fail | ForEach-Object { Write-Output "  $_" }
    try {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        Write-Output "`nTask DISABLED so an unverified definition cannot fire."
    } catch {
        Write-Output "`nWARNING: could not disable it: $($_.Exception.Message)"
    }
    exit 1
}

Write-Output "=== registered and verified ==="
Write-Output "Run it now without waiting for 03:00:"
Write-Output "  Start-ScheduledTask -TaskName $TaskName"
Write-Output "Then check the result:"
Write-Output "  (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult    # 0 = ok, 11 = a root was unreachable"
exit 0
