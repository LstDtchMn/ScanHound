# SCHEDULER-DEFINITION rollback for ScanHound-MountNASShares.
#
# Restores the previous TRIGGERS, SETTINGS and PRINCIPAL. It deliberately does
# NOT restore the previous ACTION: the action is a safety invariant, not
# configuration that should be allowed to regress. Every restore points at the
# current reviewed deployed script, whatever the backup said.
#
# THIS IS NOT A CODE ROLLBACK. It cannot return you to an older version of
# mount-nas-shares.ps1 -- the installer overwrites the deployed bundle in place
# and no prior versioned bundle is kept. If code rollback is ever needed, the
# answer is versioned hashed bundles (e.g. ...\releases\<commit>\), not
# trusting a historical action out of a backup file.
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

# The ONLY action this task is ever allowed to carry. Absolute interpreter
# path, absolute script path under the hardened deploy directory, nothing else.
$DeployedScript     = 'C:\ProgramData\ScanHound\deploy\mount-nas-shares.ps1'
$CanonicalCommand   = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$CanonicalArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$DeployedScript`""

$principalCheck = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Not elevated. Restoring a RunLevel=Highest task requires an elevated shell."
}

if (-not (Test-Path -LiteralPath $BackupDir)) { throw "Backup directory not found: $BackupDir" }

# --- 1. the backup directory must not be attacker-writable -----------------

$trusted = @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM', 'NT SERVICE\TrustedInstaller')

# ATOMIC write flags only. Composite rights (Modify, FullControl) contain the
# read bits, so masking against them matches every read-only ACE too -- a check
# that says "writable" about everything protects nothing.
function Test-WriteShapedRight([Security.AccessControl.FileSystemRights]$rights) {
    $R = [Security.AccessControl.FileSystemRights]
    $mask = [int]($R::WriteData -bor $R::AppendData -bor $R::WriteExtendedAttributes -bor
                  $R::WriteAttributes -bor $R::Delete -bor $R::DeleteSubdirectoriesAndFiles -bor
                  $R::ChangePermissions -bor $R::TakeOwnership)
    return (([int]$rights -band $mask) -ne 0)
}

$acl = Get-Acl -LiteralPath $BackupDir
if ($trusted -notcontains $acl.Owner) {
    throw ("$BackupDir is owned by '$($acl.Owner)'. An owner can rewrite the DACL, so its " +
           "contents cannot be trusted as input to an elevated registration. Run " +
           "install-mount-task.ps1 (elevated) once to harden it, or fix ownership by hand.")
}
foreach ($ace in $acl.Access) {
    if ($ace.AccessControlType -ne 'Allow') { continue }
    if ($trusted -contains $ace.IdentityReference.Value) { continue }
    if ((Test-WriteShapedRight $ace.FileSystemRights)) {
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

# --- 3. the FILE itself must be trustworthy, not merely its parent ---------

# A file can keep an explicit write ACE after its parent is locked down, and a
# reparse point can defeat the lexical containment check above.
$fileItem = Get-Item -LiteralPath $resolvedXml -Force
if ($fileItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Refusing to read '$resolvedXml': it is a reparse point."
}
$facl = Get-Acl -LiteralPath $resolvedXml
if ($trusted -notcontains $facl.Owner) {
    throw "Refusing to read '$resolvedXml': owned by '$($facl.Owner)', not an admin principal."
}
foreach ($ace in $facl.Access) {
    if ($ace.AccessControlType -ne 'Allow') { continue }
    if ($trusted -contains $ace.IdentityReference.Value) { continue }
    if ((Test-WriteShapedRight $ace.FileSystemRights)) {
        throw ("Refusing to read '$resolvedXml': it grants '$($ace.FileSystemRights)' to " +
               "'$($ace.IdentityReference)', so its contents are attacker-controllable.")
    }
}

# --- 4. restore SETTINGS, never a stale ACTION -----------------------------
#
# The pre-Stage-0 backup's action points at the mutable working tree, and the
# critical dropped-TV-record bug is still on main. Restoring that action
# verbatim would re-arm a known-vulnerable code path and undo the whole stable
# deployment boundary. So rollback means: restore the previous triggers,
# settings and principal, but ALWAYS run the reviewed deployed script.
#
# The action is therefore REWRITTEN to the canonical form rather than
# validated-and-trusted -- a validator can be argued with; a rewrite cannot.

# Rewriting the action to a canonical path is only safe if that target actually
# exists and is itself trustworthy -- otherwise rollback cheerfully registers a
# task pointing at a missing or tampered script.
if (-not (Test-Path -LiteralPath $DeployedScript -PathType Leaf)) {
    throw ("Canonical deployed script not found: $DeployedScript. Run install-mount-task.ps1 " +
           "(elevated) first -- there is nothing safe to point the restored task at.")
}
$depItem = Get-Item -LiteralPath $DeployedScript -Force
if ($depItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Canonical deployed script '$DeployedScript' is a reparse point."
}
$dacl = Get-Acl -LiteralPath $DeployedScript
if ($trusted -notcontains $dacl.Owner) {
    throw "Canonical deployed script is owned by '$($dacl.Owner)', not an admin principal."
}
foreach ($ace in $dacl.Access) {
    if ($ace.AccessControlType -ne 'Allow') { continue }
    if ($trusted -contains $ace.IdentityReference.Value) { continue }
    if (Test-WriteShapedRight $ace.FileSystemRights) {
        throw ("Canonical deployed script grants '$($ace.FileSystemRights)' to " +
               "'$($ace.IdentityReference)' -- refusing to point a restored elevated task at it.")
    }
}
# And it must still be the bytes the installer recorded.
$manifestPath = Join-Path (Split-Path $DeployedScript -Parent) 'MANIFEST.json'
if (Test-Path -LiteralPath $manifestPath) {
    $mf  = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $ent = $mf.files | Where-Object { $_.name -eq 'mount-nas-shares.ps1' } | Select-Object -First 1
    if ($ent) {
        $have = (Get-FileHash -LiteralPath $DeployedScript -Algorithm SHA256).Hash
        if ($have -ne $ent.sha256) {
            throw ("Deployed script no longer matches its manifest hash " +
                   "(manifest $($ent.sha256), on disk $have). Refusing to restore against it.")
        }
        Write-Output "deployed target verified against manifest ($($ent.sha256.Substring(0,16))...)"
    }
}

$content = Get-Content -LiteralPath $resolvedXml -Raw
try { $xml = [xml]$content } catch { throw "Backup is not valid XML: $resolvedXml" }

$nsUri = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
$ns = New-Object Xml.XmlNamespaceManager($xml.NameTable)
$ns.AddNamespace('t', $nsUri)

$actionsNode = $xml.SelectSingleNode('//t:Task/t:Actions', $ns)
if (-not $actionsNode) { throw "Backup has no Actions element: $resolvedXml" }

# Exactly one action of any kind -- counting Exec/Command nodes would not
# notice a second action of a different type sitting alongside it.
$actionChildren = @($actionsNode.ChildNodes | Where-Object { $_.NodeType -eq 'Element' })
if ($actionChildren.Count -ne 1) {
    throw "Refusing to restore: expected exactly one action, found $($actionChildren.Count)."
}
if ($actionChildren[0].LocalName -ne 'Exec') {
    throw "Refusing to restore: the action is '$($actionChildren[0].LocalName)', not Exec."
}

$oldCmd = $xml.SelectSingleNode('//t:Actions/t:Exec/t:Command', $ns)
$oldArg = $xml.SelectSingleNode('//t:Actions/t:Exec/t:Arguments', $ns)
Write-Output "backup action (NOT restored): $(if ($oldCmd) { $oldCmd.InnerText }) $(if ($oldArg) { $oldArg.InnerText })"

# Rebuild the Exec node from scratch so no stray child element survives.
[void]$actionsNode.RemoveAll()
$exec = $xml.CreateElement('Exec', $nsUri)
$cmd  = $xml.CreateElement('Command', $nsUri)
$cmd.InnerText = $CanonicalCommand
$arg  = $xml.CreateElement('Arguments', $nsUri)
$arg.InnerText = $CanonicalArguments
[void]$exec.AppendChild($cmd)
[void]$exec.AppendChild($arg)
[void]$actionsNode.AppendChild($exec)

Write-Output "restoring from: $resolvedXml"
Write-Output "  action      : $CanonicalCommand $CanonicalArguments  (rewritten to the deployed script)"

$content = $xml.OuterXml

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

# Assert what was actually installed, not what was intended -- and if the
# restored task is not what we meant, disable it rather than leave an
# unverified elevated definition running on its own schedule.
$t = Get-ScheduledTask -TaskName $TaskName
$problems = @()
if ($t.Actions.Count -ne 1)                          { $problems += "action count = $($t.Actions.Count)" }
if ($t.Actions[0].Execute  -ne $CanonicalCommand)    { $problems += "Execute = '$($t.Actions[0].Execute)'" }
if ($t.Actions[0].Arguments -ne $CanonicalArguments) { $problems += "Arguments = '$($t.Actions[0].Arguments)'" }
if ($t.Principal.RunLevel  -ne 'Highest')            { $problems += "RunLevel = '$($t.Principal.RunLevel)'" }
if ($t.Principal.LogonType -ne 'Interactive')        { $problems += "LogonType = '$($t.Principal.LogonType)'" }

Write-Output "`n=== restored ==="
Write-Output "action     : $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
Write-Output "principal  : $($t.Principal.UserId) / $($t.Principal.LogonType) / $($t.Principal.RunLevel)"
Write-Output "restart    : count=$($t.Settings.RestartCount) interval=$($t.Settings.RestartInterval)"
Write-Output "time limit : $($t.Settings.ExecutionTimeLimit)"
Write-Output "enabled    : $($t.Settings.Enabled)"

if ($problems.Count -gt 0) {
    Write-Output "`n=== POST-RESTORE ASSERTIONS FAILED ==="
    $problems | ForEach-Object { Write-Output "  $_" }
    try {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        Write-Output "The task has been DISABLED so an unverified definition cannot fire."
    } catch {
        Write-Output "WARNING: could not disable the task: $($_.Exception.Message)"
    }
    exit 1
}
Write-Output "`nall post-restore assertions passed"
exit 0
