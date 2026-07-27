# Roll ScanHound-MountNASShares back to a previously exported task definition.
#
# Restores the task only. The deployed bundle under C:\ProgramData\ScanHound\deploy
# is left in place; a restored pre-Stage-0 task simply stops pointing at it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File rollback-mount-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File rollback-mount-task.ps1 -BackupXml <path>
#
# Must run elevated (RunLevel=Highest task registration).

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$BackupXml,
    [string]$BackupDir = 'C:\ProgramData\ScanHound\backup'
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ScanHound-MountNASShares'

$principalCheck = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Not elevated. Restoring a RunLevel=Highest task requires an elevated shell."
}

if (-not $BackupXml) {
    # Default to the pre-Stage-0 capture, else the newest backup.
    $pre = Join-Path $BackupDir "$TaskName.pre-stage0.xml"
    if (Test-Path -LiteralPath $pre) {
        $BackupXml = $pre
    } else {
        $newest = Get-ChildItem $BackupDir -Filter "$TaskName.*.xml" -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $newest) { throw "No backup found in $BackupDir" }
        $BackupXml = $newest.FullName
    }
}
if (-not (Test-Path -LiteralPath $BackupXml)) { throw "Backup not found: $BackupXml" }

Write-Output "restoring from: $BackupXml"
$content = Get-Content -LiteralPath $BackupXml -Raw

# Capture the current definition first, so a rollback is itself reversible.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $pre   = Join-Path $BackupDir "$TaskName.pre-rollback.$stamp.xml"
    Export-ScheduledTask -TaskName $TaskName | Out-File -FilePath $pre -Encoding utf8
    Write-Output "current definition saved -> $pre"
}

if (-not $PSCmdlet.ShouldProcess($TaskName, "Restore from $BackupXml")) {
    Write-Output "-WhatIf: task NOT restored."
    return
}

Register-ScheduledTask -TaskName $TaskName -Xml $content -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
Write-Output "`n=== restored ==="
Write-Output "action     : $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
Write-Output "restart    : count=$($t.Settings.RestartCount) interval=$($t.Settings.RestartInterval)"
Write-Output "time limit : $($t.Settings.ExecutionTimeLimit)"
Write-Output "enabled    : $($t.Settings.Enabled)"
exit 0
