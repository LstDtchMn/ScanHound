# Roll ScanHound-MountNASShares back to a previously exported task definition.
#
# Restores the task only. The deployed bundle under C:\ProgramData\ScanHound\deploy
# is left in place; a restored pre-Stage-0 task simply stops pointing at it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File rollback-mount-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File rollback-mount-task.ps1 -BackupXml <path>
#
# Must run elevated (RunLevel=Highest task registration).
#
# SECURITY: this script registers a task definition read from disk, VERBATIM,
# with administrator rights. That makes the backup file a privilege-escalation
# target -- plant an XML, wait for a rollback, get an arbitrary elevated task.
# So the source is constrained three ways before it is trusted: it must live
# inside the backup directory, that directory must not be writable by any
# unprivileged principal, and the definition itself must look like the task
# this script is allowed to restore.

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

if (-not (Test-Path -LiteralPath $BackupDir)) { throw "Backup directory not found: $BackupDir" }

# --- 1. the backup directory must not be attacker-writable -----------------

$trusted = @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM', 'NT SERVICE\TrustedInstaller')
$writeRights = [Security.AccessControl.FileSystemRights]::Write             -bor
               [Security.AccessControl.FileSystemRights]::Modify            -bor
               [Security.AccessControl.FileSystemRights]::FullControl       -bor
               [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
               [Security.AccessControl.FileSystemRights]::TakeOwnership     -bor
               [Security.AccessControl.FileSystemRights]::Delete

$acl = Get-Acl -LiteralPath $BackupDir
if ($trusted -notcontains $acl.Owner) {
    throw ("$BackupDir is owned by '$($acl.Owner)'. An owner can rewrite the DACL, so its " +
           "contents cannot be trusted as input to an elevated registration. Run " +
           "install-mount-task.ps1 (elevated) once to harden it, or fix ownership by hand.")
}
foreach ($ace in $acl.Access) {
    if ($ace.AccessControlType -ne 'Allow') { continue }
    if ($trusted -contains $ace.IdentityReference.Value) { continue }
    if (([int]$ace.FileSystemRights -band [int]$writeRights) -ne 0) {
        throw ("$BackupDir grants '$($ace.FileSystemRights)' to '$($ace.IdentityReference)'. " +
               "An unprivileged principal could plant a task definition that this script " +
               "would then register with administrator rights. Refusing to continue.")
    }
}

# --- 2. resolve the file, and require it to be inside that directory --------

if (-not $BackupXml) {
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

# Canonicalise both sides before comparing, so `..\` cannot escape the directory.
$resolvedXml = (Resolve-Path -LiteralPath $BackupXml).ProviderPath
$resolvedDir = (Resolve-Path -LiteralPath $BackupDir).ProviderPath
if (-not $resolvedXml.StartsWith($resolvedDir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw ("Refusing to register '$resolvedXml': it is outside the hardened backup " +
           "directory '$resolvedDir'. Only definitions this installer wrote may be restored.")
}

# --- 3. the definition itself must be the task we are allowed to restore ----

$content = Get-Content -LiteralPath $resolvedXml -Raw
try { $xml = [xml]$content } catch { throw "Backup is not valid XML: $resolvedXml" }

$ns = New-Object Xml.XmlNamespaceManager($xml.NameTable)
$ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')

$execNodes = @($xml.SelectNodes('//t:Actions/t:Exec/t:Command', $ns))
if ($execNodes.Count -ne 1) {
    throw "Refusing to register: expected exactly one Exec action, found $($execNodes.Count)."
}
$command = $execNodes[0].InnerText
if ($command -notmatch '(?i)powershell(\.exe)?$') {
    throw "Refusing to register: the action runs '$command', not powershell.exe."
}
$argNode   = $xml.SelectSingleNode('//t:Actions/t:Exec/t:Arguments', $ns)
$arguments = if ($argNode) { $argNode.InnerText } else { '' }
if ($arguments -notmatch [regex]::Escape('mount-nas-shares.ps1')) {
    throw ("Refusing to register: the action does not invoke mount-nas-shares.ps1. " +
           "Arguments were: $arguments")
}

Write-Output "restoring from: $resolvedXml"
Write-Output "  action      : $command $arguments"

# Capture the current definition first, so a rollback is itself reversible.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $prev  = Join-Path $BackupDir "$TaskName.pre-rollback.$stamp.xml"
    Export-ScheduledTask -TaskName $TaskName | Out-File -FilePath $prev -Encoding utf8
    Write-Output "current definition saved -> $prev"
}

if (-not $PSCmdlet.ShouldProcess($TaskName, "Restore from $resolvedXml")) {
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
