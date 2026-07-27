# Stage 0 installer for ScanHound-MountNASShares.
#
# Registers the scheduled task from a deterministic definition, then EXPORTS
# the installed task and asserts every security-critical field -- because a
# registration command returning success is not evidence of what got installed.
#
# Must run elevated: a task with RunLevel=Highest cannot be registered from a
# non-elevated shell (verified 2026-07-26: "Access is denied").
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-mount-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-mount-task.ps1 -WhatIf
#
# Rollback: scripts\rollback-mount-task.ps1

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$DeployDir = 'C:\ProgramData\ScanHound\deploy',
    [string]$BackupDir = 'C:\ProgramData\ScanHound\backup'
)

$ErrorActionPreference = 'Stop'

$TaskName = 'ScanHound-MountNASShares'
$Sid      = 'S-1-5-21-2209700402-3938720563-1532606968-1001'
$Deployed = Join-Path $DeployDir 'mount-nas-shares.ps1'
$Compose  = Join-Path $DeployDir 'docker-compose.yml'
$Manifest = Join-Path $DeployDir 'MANIFEST.json'

# --- preconditions ---------------------------------------------------------

# Every precondition below is read-only, so none of them may be suppressed by
# a dry run. $WhatIfPreference propagates into nested calls inside cmdlets like
# Get-FileHash (an advanced function in 5.1), which made the integrity check
# return an EMPTY hash under -WhatIf instead of verifying anything. Clear it
# for the precondition block and restore it before ShouldProcess is consulted.
$requestedWhatIf = $WhatIfPreference
$WhatIfPreference = $false

# -WhatIf must be runnable unelevated so preconditions can be checked without
# admin rights; only the actual registration requires elevation.
$principalCheck = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
$isElevated = $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isElevated) {
    if ($requestedWhatIf) {
        Write-Warning "Not elevated -- checking preconditions only. Registration would fail."
    } else {
        $WhatIfPreference = $requestedWhatIf
        throw "Not elevated. Registering a RunLevel=Highest task requires an elevated shell."
    }
}

foreach ($f in @($Deployed, $Compose, $Manifest)) {
    if (-not (Test-Path -LiteralPath $f)) { throw "Missing deployment input: $f" }
}

# Mechanical dependency gate: refuse to register against a script that is not
# the PR #35-era hardened version. Identity verification is its defining
# feature; without it, a wrong share can be mounted and reported healthy.
$deployedText = Get-Content -LiteralPath $Deployed -Raw
foreach ($marker in @('verify_identity', 'path=UNC', 'Stop-ScanhoundVerified')) {
    if ($deployedText -notmatch [regex]::Escape($marker)) {
        throw "Deployed script lacks '$marker' -- refusing to register a pre-PR#35 script."
    }
}
if ($deployedText -notmatch [regex]::Escape('--project-directory')) {
    throw "Deployed script still recreates from the working tree -- refusing to register."
}

# Hashes must match what was recorded at deploy time. An empty hash is treated
# as a hard failure rather than a mismatch, so a verification step that fails
# to run can never be mistaken for one that ran and disagreed.
$mf = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
foreach ($entry in $mf.files) {
    $path = Join-Path $DeployDir $entry.name
    $have = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if ([string]::IsNullOrWhiteSpace($have)) {
        throw "Could not compute a hash for $path -- refusing to proceed unverified."
    }
    if ($have -ne $entry.sha256) {
        throw "Hash mismatch for $($entry.name):`n  manifest: $($entry.sha256)`n  on disk : $have"
    }
    Write-Output "hash OK  $($entry.name)  $($entry.sha256.Substring(0,16))..."
}

# Preconditions are done; honour the caller's dry-run choice from here on.
$WhatIfPreference = $requestedWhatIf

# --- backup ----------------------------------------------------------------

New-Item -ItemType Directory -Force $BackupDir | Out-Null
$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $BackupDir "$TaskName.$stamp.xml"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Export-ScheduledTask -TaskName $TaskName | Out-File -FilePath $backup -Encoding utf8
    Write-Output "backed up existing task -> $backup"
} else {
    Write-Output "no existing task to back up"
}

# --- definition ------------------------------------------------------------

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Deployed`""

# Boot: delayed. On the 2026-07-26 boot Docker's backend did not accept IPC
# until T+119 s; the task previously fired at T+80 s and never retried.
$tBoot = New-ScheduledTaskTrigger -AtStartup
$tBoot.Delay = 'PT3M'

# Logon: with LogonType=Interactive this is the trigger that actually fires
# when the account logs in after startup, so it is a real recovery trigger.
$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $Sid
$tLogon.Delay = 'PT1M'

# EXACTLY ONE trigger owns repetition. A dedicated time trigger also starts the
# periodic schedule at registration rather than at the next reboot.
$tTime = New-ScheduledTaskTrigger -Once -At ([datetime]'2026-07-26T00:00:00') `
    -RepetitionInterval (New-TimeSpan -Minutes 15)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

$settings.DisallowStartIfOnBatteries = $false
$settings.AllowHardTerminate         = $true   # explicit; do not rely on the default
$settings.WakeToRun                  = $false
$settings.Enabled                    = $true

$principal = New-ScheduledTaskPrincipal -Id 'Author' -UserId $Sid `
    -LogonType Interactive -RunLevel Highest

$desc = @"
Re-mounts the 9 TURTLELANDSRV2 NAS shares inside Docker Desktop's WSL2 distro,
verifies each mount's identity, and recovers the scanhound container.

Stage 0 (2026-07-26): runs from the stable deployed bundle under
$DeployDir -- NOT the git working tree -- with scheduler-owned retry
(3 x 5 min), a 3-minute boot delay, and one 15-minute recovery trigger.
Requires the PR #35-era hardened script (gated by this installer).
"@

if (-not $PSCmdlet.ShouldProcess($TaskName, 'Register scheduled task')) {
    Write-Output "`n-WhatIf: preconditions passed; task NOT registered."
    return
}

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger @($tBoot, $tLogon, $tTime) -Settings $settings `
    -Principal $principal -Description $desc -Force | Out-Null

Export-ScheduledTask -TaskName $TaskName |
    Out-File -FilePath (Join-Path $DeployDir "$TaskName.installed.xml") -Encoding utf8

# --- assert the INSTALLED task, not the intent -----------------------------

$t   = Get-ScheduledTask -TaskName $TaskName
$xml = [xml](Export-ScheduledTask -TaskName $TaskName)
$ns  = New-Object Xml.XmlNamespaceManager($xml.NameTable)
$ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')

$fail = @()
function Assert-Field($label, $actual, $expected) {
    $ok = ("$actual" -eq "$expected")
    Write-Output ("{0}  {1,-32} {2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $label, $actual)
    if (-not $ok) { $script:fail += "$label = '$actual' (expected '$expected')" }
}

Write-Output "`n=== installed-task assertions ==="
Assert-Field 'action.Execute'              $t.Actions[0].Execute 'powershell.exe'
Assert-Field 'action targets deployed'     ($t.Actions[0].Arguments -like "*$Deployed*") 'True'
Assert-Field 'action avoids working tree'  ($t.Actions[0].Arguments -notlike '*X:\Docker Apps*') 'True'
Assert-Field 'principal.UserId'            $t.Principal.UserId $Sid
Assert-Field 'principal.LogonType'         $t.Principal.LogonType 'Interactive'
Assert-Field 'principal.RunLevel'          $t.Principal.RunLevel 'Highest'
Assert-Field 'RestartCount'                $t.Settings.RestartCount 3
Assert-Field 'RestartInterval'             $t.Settings.RestartInterval 'PT5M'
Assert-Field 'StartWhenAvailable'          $t.Settings.StartWhenAvailable 'True'
Assert-Field 'DisallowStartIfOnBatteries'  $t.Settings.DisallowStartIfOnBatteries 'False'
Assert-Field 'StopIfGoingOnBatteries'      $t.Settings.StopIfGoingOnBatteries 'False'
Assert-Field 'AllowHardTerminate'          $t.Settings.AllowHardTerminate 'True'
Assert-Field 'ExecutionTimeLimit'          $t.Settings.ExecutionTimeLimit 'PT15M'
Assert-Field 'MultipleInstances'           $t.Settings.MultipleInstances 'IgnoreNew'
Assert-Field 'WakeToRun'                   $t.Settings.WakeToRun 'False'
Assert-Field 'Enabled'                     $t.Settings.Enabled 'True'

$bootDelay = $xml.SelectSingleNode('//t:BootTrigger/t:Delay', $ns)
Assert-Field 'BootTrigger.Delay'           $(if ($bootDelay) { $bootDelay.InnerText } else { '<none>' }) 'PT3M'

# The spec's key structural requirement: exactly one trigger repeats.
$repeaters = $xml.SelectNodes('//t:Triggers/*/t:Repetition/t:Interval', $ns)
Assert-Field 'triggers owning repetition'  $repeaters.Count 1
if ($repeaters.Count -eq 1) {
    Assert-Field 'repetition interval'     $repeaters[0].InnerText 'PT15M'
    $dur = $xml.SelectSingleNode('//t:Triggers/*/t:Repetition/t:Duration', $ns)
    Assert-Field 'repetition is indefinite' $(if ($dur) { $dur.InnerText } else { '<none>' }) '<none>'
}

Write-Output ""
if ($fail.Count -gt 0) {
    Write-Output "=== $($fail.Count) ASSERTION FAILURE(S) ==="
    $fail | ForEach-Object { Write-Output "  $_" }
    Write-Output "`nRoll back with: scripts\rollback-mount-task.ps1 -BackupXml `"$backup`""
    exit 1
}

Write-Output "=== all assertions passed ==="
Write-Output "installed XML: $(Join-Path $DeployDir "$TaskName.installed.xml")"
Write-Output "backup       : $backup"
Write-Output "next run     : $((Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime)"
exit 0
