# Stage 0 installer for ScanHound-MountNASShares.
#
# ONE elevated operation: deploy the reviewed bundle to a stable location,
# hash it, lock it down, register the task, then EXPORT the installed task and
# assert every security-critical field -- because a registration command
# returning success is not evidence of what got installed.
#
# Deployment and registration are deliberately not separable. Splitting them
# lets the deployed script drift from the task that points at it, which is the
# exact class of "undiscoverable configuration" this installer exists to end.
#
# Must run elevated: a RunLevel=Highest task cannot be registered from a
# non-elevated shell (verified 2026-07-26: "Access is denied"), and the deploy
# directory is intentionally not user-writable.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-mount-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-mount-task.ps1 -WhatIf
#
# Rollback: scripts\rollback-mount-task.ps1

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # MANDATORY, full 40-character SHA. The commit to deploy must be supplied
    # from OUTSIDE mutable repository state: the repo is writable by the
    # ordinary account, so a same-user process could move HEAD or its branch
    # and have the elevated installer faithfully deploy -- and certify -- an
    # attacker-selected commit. Content addressing only authenticates once the
    # expected id is fixed independently. HEAD is now informational only.
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ExpectedCommit,

    [string]$SourceRepo = 'X:\Docker Apps\ScanHound',
    [string]$DeployDir  = 'C:\ProgramData\ScanHound\deploy',
    [string]$BackupDir  = 'C:\ProgramData\ScanHound\backup',
    [string]$RunDir     = 'C:\ProgramData\ScanHound\run'
)

$ErrorActionPreference = 'Stop'

$TaskName = 'ScanHound-MountNASShares'
$Sid      = 'S-1-5-21-2209700402-3938720563-1532606968-1001'

# Elevated native dependencies are pinned for the same reason the runtime
# task's are: an elevated process must not run whichever git.exe or icacls.exe
# happens to be first on an ambient PATH.
$IcaclsExe = Join-Path $env:SystemRoot 'System32\icacls.exe'
$GitExe    = @(
    'C:\Program Files\Git\cmd\git.exe',
    'C:\Program Files\Git\bin\git.exe',
    'C:\Program Files (x86)\Git\cmd\git.exe'
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $GitExe) { throw "git.exe not found at any pinned location. Refusing to resolve it via PATH." }

$SrcScript  = Join-Path $SourceRepo 'scripts\mount-nas-shares.ps1'
$SrcCompose = Join-Path $SourceRepo 'docker-compose.yml'
$Deployed   = Join-Path $DeployDir  'mount-nas-shares.ps1'
$Compose    = Join-Path $DeployDir  'docker-compose.yml'
$Manifest   = Join-Path $DeployDir  'MANIFEST.json'

# Every precondition below is read-only, so none may be suppressed by a dry
# run. $WhatIfPreference propagates into nested calls inside cmdlets like
# Get-FileHash (an advanced function in 5.1), which made the integrity check
# return an EMPTY hash under -WhatIf instead of verifying anything. Clear it
# for validation and restore it before anything mutates.
$requestedWhatIf  = $WhatIfPreference
$WhatIfPreference = $false

# --- helpers ---------------------------------------------------------------

# icacls reports failure through its EXIT CODE, not through PowerShell errors,
# and piping it to Out-Null discards that. A silently failed lockdown would
# leave the deployment world-writable while the installer printed success --
# the fail-open shape this whole effort exists to eliminate.
function Invoke-Icacls {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$IcaclsArgs)
    $out = & $script:IcaclsExe @IcaclsArgs /q 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed (exit $LASTEXITCODE): icacls $($IcaclsArgs -join ' ')`n$out"
    }
}

# Runs git with replacement objects DISABLED and returns raw bytes.
#
# Two separate problems this solves. First, git honours refs/replace/ by
# default in almost every command, so anyone who can write the repository can
# change what `rev-parse` resolves and what `cat-file` prints --
# --no-replace-objects is the documented way to see the original object.
# Second, capturing native output through the PowerShell pipeline decodes it as
# text, drops the native line framing and re-synthesises newlines; that is not
# a byte-exact blob, whatever the surrounding comments claim. Reading
# StandardOutput.BaseStream gives the actual bytes.
function Invoke-GitBytes {
    param([string[]]$GitArgs)

    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName               = $script:GitExe
    $psi.WorkingDirectory       = $script:SourceRepo
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true
    # PowerShell 5.1 has no ArgumentList on ProcessStartInfo; these arguments
    # are all literal SHAs, fixed flags and repo-relative paths with no spaces.
    $psi.Arguments = (@('--no-replace-objects') + $GitArgs) -join ' '

    $p = [Diagnostics.Process]::Start($psi)
    $ms = New-Object IO.MemoryStream
    $p.StandardOutput.BaseStream.CopyTo($ms)
    $err = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    if ($p.ExitCode -ne 0) {
        throw "git $($GitArgs -join ' ') failed (exit $($p.ExitCode)): $err"
    }
    return $ms.ToArray()
}

function Invoke-GitText {
    param([string[]]$GitArgs)
    return ([Text.Encoding]::UTF8.GetString((Invoke-GitBytes $GitArgs))).Trim()
}

# Proves a directory is genuinely locked, rather than assuming the grants took.
# Checks OWNERSHIP too: an owner can always rewrite the DACL, so a directory
# owned by an unprivileged account is not protected no matter what it grants.
# Works on FILES as well as directories, and recurses. A directory-only check
# is insufficient: a file created before the new DACL can retain an explicit
# write ACE of its own, and a reparse point anywhere in the chain defeats a
# lexical "inside the hardened root" argument. Every artifact an elevated task
# reads or executes gets proven individually.
# Write-shaped rights, tested with ATOMIC flags only. Composite rights (Modify,
# FullControl) CONTAIN the read bits, so masking against them made every
# read-only ACE look writable -- including the deliberate ReadAndExecute grant
# this installer itself applies, which would have made it fail on its own
# correctly-hardened directory during the elevated run.
function Test-WriteShapedRight([Security.AccessControl.FileSystemRights]$rights) {
    $R = [Security.AccessControl.FileSystemRights]
    $mask = [int]($R::WriteData -bor $R::AppendData -bor $R::WriteExtendedAttributes -bor
                  $R::WriteAttributes -bor $R::Delete -bor $R::DeleteSubdirectoriesAndFiles -bor
                  $R::ChangePermissions -bor $R::TakeOwnership)
    return (([int]$rights -band $mask) -ne 0)
}

function Assert-AdminOnlyPath {
    param([string]$Path, [switch]$Recurse, [switch]$Quiet)

    $trusted = @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM', 'NT SERVICE\TrustedInstaller')
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "$Path is a reparse point; a junction/symlink here redirects a trusted path."
    }

    $acl = Get-Acl -LiteralPath $Path
    if ($trusted -notcontains $acl.Owner) {
        throw ("$Path is owned by '$($acl.Owner)'. An owner can rewrite the DACL, so " +
               "every restriction on it is advisory. Ownership must be Administrators.")
    }
    foreach ($ace in $acl.Access) {
        if ($ace.AccessControlType -ne 'Allow') { continue }
        if ($trusted -contains $ace.IdentityReference.Value) { continue }
        if (Test-WriteShapedRight $ace.FileSystemRights) {
            throw ("$Path grants '$($ace.FileSystemRights)' to " +
                   "'$($ace.IdentityReference)'. An unprivileged principal must not be " +
                   "able to modify anything an elevated task reads or executes.")
        }
    }
    if (-not $Quiet) { Write-Output "locked  : $Path (owner $($acl.Owner), no non-admin write)" }

    if ($Recurse -and $item.PSIsContainer) {
        foreach ($child in (Get-ChildItem -LiteralPath $Path -Force -Recurse)) {
            Assert-AdminOnlyPath -Path $child.FullName -Quiet
        }
    }
}

# ---------------------------------------------------------------------------
# 1. Elevation
# ---------------------------------------------------------------------------

$principalCheck = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
$isElevated = $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isElevated) {
    if ($requestedWhatIf) {
        Write-Warning "Not elevated -- validating only. Deployment and registration would fail."
    } else {
        $WhatIfPreference = $requestedWhatIf
        throw "Not elevated. Deploying and registering this task requires an elevated shell."
    }
}

# ---------------------------------------------------------------------------
# 2. Validate the SOURCE before it is allowed to become the deployment
# ---------------------------------------------------------------------------

foreach ($f in @($SrcScript, $SrcCompose)) {
    if (-not (Test-Path -LiteralPath $f)) { throw "Missing source input: $f" }
}

# Pinned git.exe must itself be trustworthy before it is used to establish
# provenance for anything else.
Assert-AdminOnlyPath $GitExe    -Quiet
Assert-AdminOnlyPath $IcaclsExe -Quiet
Write-Output "tools   : $GitExe"

# $ExpectedCommit is the authority. HEAD is reported for context only, and a
# mismatch is surfaced rather than silently deploying whatever is checked out.
$srcCommit = $ExpectedCommit
$headNow   = Invoke-GitText @('rev-parse', 'HEAD')
$srcBranch = Invoke-GitText @('rev-parse', '--abbrev-ref', 'HEAD')

# The commit must exist and actually BE a commit in this repository.
$objType = Invoke-GitText @('cat-file', '-t', $srcCommit)
if ($objType -ne 'commit') {
    throw "$srcCommit is a '$objType', not a commit, in $SourceRepo."
}

Write-Output "source  : $SourceRepo"
Write-Output "deploy  : $srcCommit  (APPROVED, supplied out-of-band)"
if ($headNow -ne $srcCommit) {
    Write-Output "note    : working tree HEAD is $headNow ($srcBranch) -- NOT used; the approved commit governs."
} else {
    Write-Output "head    : matches ($srcBranch)"
}

# DEPLOY FROM GIT'S OBJECT STORE, NOT FROM THE WORKING TREE.
#
# The previous flow validated $SrcScript by one read and let Copy-Item REOPEN
# it later. The repository is writable by the ordinary account while this
# installer runs elevated, so an unelevated same-user process could swap the
# file between validation and copy -- and because the manifest hashed whatever
# was copied, the post-copy check would have certified the attacker's content
# as the deployed truth. Same trust-boundary shape as the staging exploit:
# elevated reader, user-writable source, validation separated from use.
#
# Rather than race that window, remove it: the content comes from the committed
# object at $srcCommit, which is immutable and content-addressed. The working
# tree is never read for deployment, so there is nothing to swap. The earlier
# attempt -- hashing the working-tree bytes and comparing to the blob id --
# could not work anyway: git applies the autocrlf filter, so working-tree bytes
# legitimately hash differently from the stored object (measured on this repo).
function Get-CommittedBlob {
    param([string]$RepoRelative)

    $blobSha = Invoke-GitText @('rev-parse', "${srcCommit}:$RepoRelative")
    if ($blobSha -notmatch '^[0-9a-f]{40}$') {
        throw "Unexpected blob id '$blobSha' for $RepoRelative at $srcCommit."
    }
    $type = Invoke-GitText @('cat-file', '-t', $blobSha)
    if ($type -ne 'blob') { throw "$RepoRelative at $srcCommit is a '$type', not a blob." }

    $declared = [int](Invoke-GitText @('cat-file', '-s', $blobSha))
    $bytes    = Invoke-GitBytes @('cat-file', 'blob', $blobSha)
    if ($bytes.Length -ne $declared) {
        throw ("Byte-count mismatch extracting $RepoRelative`: git reports $declared bytes, " +
               "the stream produced $($bytes.Length). Refusing to deploy a partial object.")
    }
    return @{ Bytes = $bytes; Blob = $blobSha; Size = $declared }
}

$scriptObj  = Get-CommittedBlob -RepoRelative 'scripts/mount-nas-shares.ps1'
$composeObj = Get-CommittedBlob -RepoRelative 'docker-compose.yml'
Write-Output ("content : blobs {0} ({1} B) / {2} ({3} B), byte-exact from the object store" -f
              $scriptObj.Blob.Substring(0,8), $scriptObj.Size,
              $composeObj.Blob.Substring(0,8), $composeObj.Size)

# Every gate below runs against the COMMITTED bytes, never a working-tree read.
$srcText = [Text.Encoding]::UTF8.GetString($scriptObj.Bytes)

# PR #35: mount identity verification. Without it a wrong share can be mounted
# and reported healthy -- the failure this whole effort exists to prevent.
foreach ($marker in @('verify_identity', 'path=UNC', 'Stop-ScanhoundVerified')) {
    if ($srcText -notmatch [regex]::Escape($marker)) {
        throw "Source script lacks '$marker' -- refusing to deploy a pre-PR#35 script."
    }
}

# Stage 0: the recreate must not read its recipe from the mutable working tree.
if ($srcText -notmatch [regex]::Escape('--project-directory')) {
    throw "Source script still recreates from the working tree -- refusing to deploy."
}

# --project-directory pins the recipe but still resolves relative paths against
# the working tree, so a MISSING local image would let Compose build recovery
# from checked-out source. These two flags are what actually close that tail.
foreach ($marker in @('--no-build', '--pull never')) {
    if ($srcText -notmatch [regex]::Escape($marker)) {
        throw ("Source script lacks '$marker' -- a recovery run could build from the " +
               "working tree or pull over the validated image. Refusing to deploy.")
    }
}

# Executables must be pinned by absolute path, and the in-distro timeout must
# be present. Both matter more once the task fires every 5 minutes elevated:
# PATH resolution turns into a recurring elevated-execution hazard, and without
# the in-distro timeout a wedged mount can outlive its run and overlap the next.
foreach ($marker in @('$WslExe', '$DockerExe', 'Assert-PinnedExe', 'timeout -s TERM -k 5')) {
    if ($srcText -notmatch [regex]::Escape($marker)) {
        throw ("Source script lacks '$marker' -- executables must be pinned and the " +
               "in-distro timeout present before an elevated repeater is installed.")
    }
}
# And no bare invocations may survive alongside the pins.
foreach ($bad in @('wsl -d docker-desktop -- sh', 'docker compose -f $ComposeFile')) {
    if ($srcText -match [regex]::Escape($bad)) {
        throw "Source script still contains an unpinned invocation: '$bad'."
    }
}

# The deployed script must point at the deploy directory it is being installed
# into; a script hard-coded to a different path would be silently inert.
if ($srcText -notmatch [regex]::Escape($DeployDir)) {
    throw "Source script does not reference $DeployDir -- its Compose pin targets elsewhere."
}

Write-Output "gates   : PR#35 markers + Compose pinning + no-build/no-pull  OK"

if ($requestedWhatIf) {
    Write-Output "`n-WhatIf: source validated; nothing deployed, nothing registered."
    return
}

# ---------------------------------------------------------------------------
# 3. Deploy
# ---------------------------------------------------------------------------

$WhatIfPreference = $requestedWhatIf   # honour the caller from here on

# THE DESTINATION IS SECURED BEFORE THE FIRST BYTE IS WRITTEN.
#
# Writing first and hardening afterwards left a window in which an unelevated
# process could alter the deployed script, recipe or manifest and have the
# manifest certify the result. Worse, a pre-planted REPARSE POINT at
# deploy\mount-nas-shares.ps1 would have been followed by an elevated
# WriteAllText -- an arbitrary-file-overwrite primitive -- with the reparse
# assertion only noticing after the damage.
#
# Order is now: harden and assert the directory, destroy any pre-existing
# payload path WITHOUT following it, create each file with CreateNew, harden
# and assert the file, and only then write.
function Remove-PayloadPathUnsafeToKeep([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer) {
        # A directory where a file belongs, or a junction: remove the entry
        # itself. Directory.Delete does not traverse a reparse point.
        [IO.Directory]::Delete($item.FullName, $false)
    } else {
        # File.Delete removes the link, never the link's target.
        [IO.File]::Delete($item.FullName)
    }
}

function New-SecureDeployedFile([string]$path, [byte[]]$bytes) {
    Remove-PayloadPathUnsafeToKeep $path
    # CreateNew fails outright if anything reappeared in the meantime.
    $fs = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $fs.Dispose()
    Invoke-Icacls $path '/setowner' 'BUILTIN\Administrators'
    Invoke-Icacls $path '/inheritance:r'
    Invoke-Icacls $path '/grant'    'BUILTIN\Administrators:(F)'
    Invoke-Icacls $path '/grant'    '*S-1-5-18:(F)'
    Invoke-Icacls $path '/grant'    "*$($script:Sid):(RX)"
    Assert-AdminOnlyPath $path -Quiet
    [IO.File]::WriteAllBytes($path, $bytes)
}

# Directories first, and PROVEN, before anything is created inside them.
# $RunDir holds shell payloads wsl.exe executes as root, so it is an
# elevated-execution input exactly like the deploy directory.
foreach ($dir in @($DeployDir, $BackupDir, $RunDir)) {
    New-Item -ItemType Directory -Force $dir | Out-Null
    Invoke-Icacls $dir '/setowner'     'BUILTIN\Administrators' '/T'
    Invoke-Icacls $dir '/inheritance:r'
    Invoke-Icacls $dir '/grant'        'BUILTIN\Administrators:(OI)(CI)(F)'
    Invoke-Icacls $dir '/grant'        '*S-1-5-18:(OI)(CI)(F)'
}
# The task account gets READ ONLY on deploy/backup, and NOTHING on staging: a
# SID ACE cannot distinguish the elevated token from the unelevated one, so the
# write right must simply not be granted.
Invoke-Icacls $DeployDir '/grant' "*$($Sid):(OI)(CI)(RX)" '/T'
Invoke-Icacls $BackupDir '/grant' "*$($Sid):(OI)(CI)(RX)" '/T'

Assert-AdminOnlyPath $DeployDir
Assert-AdminOnlyPath $BackupDir
Assert-AdminOnlyPath $RunDir

New-SecureDeployedFile $Deployed $scriptObj.Bytes
New-SecureDeployedFile $Compose  $composeObj.Bytes

$manifest = [ordered]@{
    deployed_at   = (Get-Date).ToString('o')
    stage         = 'Stage 0 - scheduler settings + stable deployment path'
    source_repo   = $SourceRepo
    source_commit = $srcCommit
    source_blobs  = @{ 'mount-nas-shares.ps1' = $scriptBlob; 'docker-compose.yml' = $composeBlob }
    source_branch = $srcBranch
    files         = @(
        [ordered]@{ name = 'mount-nas-shares.ps1'
                    sha256 = (Get-FileHash $Deployed -Algorithm SHA256).Hash
                    source_path = 'scripts/mount-nas-shares.ps1' },
        [ordered]@{ name = 'docker-compose.yml'
                    sha256 = (Get-FileHash $Compose -Algorithm SHA256).Hash
                    source_path = 'docker-compose.yml' }
    )
}
# The manifest is itself an elevated-trust input -- rollback verifies the
# deployed script against it -- so it gets the same create-harden-assert-write
# treatment as the payloads rather than a bare Out-File.
$utf8NoBom = New-Object Text.UTF8Encoding($false)
New-SecureDeployedFile $Manifest ($utf8NoBom.GetBytes(($manifest | ConvertTo-Json -Depth 5)))

# Lock the deployment: an unprivileged user must not be able to replace the
# script an elevated scheduled task executes. Read+execute for the task's
# account, write only for administrators. Mutable state deliberately lives in
# the PARENT directory, never here.
#
# Both directories are hardened, not just the deploy one: rollback registers a
# task definition read from the backup directory VERBATIM and elevated, so a
# writable backup directory is an privilege-escalation path -- plant an XML,
# wait for someone to roll back, get an arbitrary elevated task.
#
# OWNERSHIP IS PART OF THE LOCK. An object's owner can always rewrite its DACL
# (WRITE_DAC is implicit for owners), so leaving these directories owned by the
# unprivileged account makes every grant below advisory. Ownership moves to
# Administrators first, and is asserted afterwards.
# Re-assert AFTER writing, recursively: the pre-write pass proved the
# directories, this proves every artifact that now sits in them (including the
# manifest) still carries an admin-only descriptor and is not a reparse point.
Assert-AdminOnlyPath $DeployDir -Recurse
Assert-AdminOnlyPath $BackupDir -Recurse
Assert-AdminOnlyPath $RunDir

Write-Output "deployed: $DeployDir"
foreach ($e in $manifest.files) { Write-Output "          $($e.name)  $($e.sha256.Substring(0,16))..." }

# Verify what actually landed, rather than trusting the copy.
foreach ($e in $manifest.files) {
    $have = (Get-FileHash (Join-Path $DeployDir $e.name) -Algorithm SHA256).Hash
    if ([string]::IsNullOrWhiteSpace($have)) {
        throw "Could not hash $($e.name) after deployment -- refusing to proceed unverified."
    }
    if ($have -ne $e.sha256) { throw "Post-deploy hash mismatch for $($e.name)." }
}

# ---------------------------------------------------------------------------
# 4. Back up the task being replaced
# ---------------------------------------------------------------------------

New-Item -ItemType Directory -Force $BackupDir | Out-Null
$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $BackupDir "$TaskName.$stamp.xml"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Export-ScheduledTask -TaskName $TaskName | Out-File -FilePath $backup -Encoding utf8
    Write-Output "backup  : $backup"
} else {
    Write-Output "backup  : none (no existing task)"
}

# ---------------------------------------------------------------------------
# 5. Definition
# ---------------------------------------------------------------------------

# Absolute interpreter path, not 'powershell.exe' by name: the action runs
# elevated 288 times a day, and a name is resolved through a PATH this script
# does not control.
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $PowerShellExe -PathType Leaf)) {
    throw "Windows PowerShell not found at the pinned path '$PowerShellExe'."
}
$action = New-ScheduledTaskAction -Execute $PowerShellExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Deployed`""

# Boot: delayed. On the 2026-07-26 boot Docker's backend did not accept IPC
# until T+119 s; the task previously fired at T+80 s and never retried.
#
# NOTE the InteractiveToken limitation: this trigger only fires when the
# account already holds an interactive session, so it is opportunistic, not
# unattended pre-logon recovery. The logon and time triggers carry the load.
$tBoot = New-ScheduledTaskTrigger -AtStartup
$tBoot.Delay = 'PT3M'

$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $Sid
$tLogon.Delay = 'PT1M'

# EXACTLY ONE trigger owns repetition, and its start boundary is computed at
# install time, never hard-coded. A boundary already in the past is not a clean
# "starts now": it is a MISSED occurrence, so the first firing falls to
# StartWhenAvailable's queueing (documented to add up to ~10 minutes) rather
# than to the schedule. A boundary just ahead gives a real, assertable first
# run -- and a date literal would have rotted the moment this was re-run on a
# later day.
#
# THE REPETITION INTERVAL *IS* THE RETRY INTERVAL. This was 15 minutes while we
# believed RestartOnFailure would retry a failed run 3 times at 5-minute spacing.
# It does not: proved 2026-07-26 with a disposable task carrying no repeating
# trigger, so a second run could only have been a restart. RestartOnFailure was
# present on the registered task (Count=3 Interval=PT1M), the action exited 42,
# LastTaskResult recorded 42 so Windows SAW the failure -- and over four minutes
# there was exactly ONE run and zero restarts. RestartOnFailure covers a task
# failing to LAUNCH, not an action returning nonzero.
#
# So 5 minutes here reproduces the recovery cadence the design intended, by the
# only mechanism that actually works. RestartCount is kept because it still
# covers genuine launch failures, but nothing depends on it.
$RepetitionMinutes = 5
$triggerStart = (Get-Date).AddSeconds(90)
$tTime = New-ScheduledTaskTrigger -Once -At $triggerStart `
    -RepetitionInterval (New-TimeSpan -Minutes $RepetitionMinutes)

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

Stage 0 (2026-07-26): runs from the stable deployed bundle under $DeployDir --
NOT the git working tree -- with a 3-minute boot delay and one $RepetitionMinutes-minute
recovery trigger. Deployed from commit $srcCommit.

RETRY COMES FROM THE REPEATING TRIGGER, NOT FROM RestartOnFailure: the latter
was measured on this host NOT to fire on a nonzero action exit (only on a
launch failure), so the repetition interval is the real retry interval.

LIMITATION: LogonType=Interactive means every trigger requires an existing
interactive session for this account. There is no unattended pre-logon recovery.
"@

if (-not $PSCmdlet.ShouldProcess($TaskName, 'Register scheduled task')) {
    Write-Output "`n-WhatIf: bundle deployed; task NOT registered."
    return
}

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger @($tBoot, $tLogon, $tTime) -Settings $settings `
    -Principal $principal -Description $desc -Force | Out-Null

Export-ScheduledTask -TaskName $TaskName |
    Out-File -FilePath (Join-Path $DeployDir "$TaskName.installed.xml") -Encoding utf8

# ---------------------------------------------------------------------------
# 6. Assert the INSTALLED task, not the intent
# ---------------------------------------------------------------------------

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
Assert-Field 'action count'                $t.Actions.Count 1
Assert-Field 'action.Execute (pinned)'     $t.Actions[0].Execute $PowerShellExe
Assert-Field 'action targets deployed'     ($t.Actions[0].Arguments -like "*$Deployed*") 'True'
Assert-Field 'action avoids working tree'  ($t.Actions[0].Arguments -notlike "*$SourceRepo*") 'True'
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
Assert-Field 'BootTrigger.Delay' $(if ($bootDelay) { $bootDelay.InnerText } else { '<none>' }) 'PT3M'

# The key structural requirement: exactly one trigger repeats.
$repeaters = $xml.SelectNodes('//t:Triggers/*/t:Repetition/t:Interval', $ns)
Assert-Field 'triggers owning repetition' $repeaters.Count 1
if ($repeaters.Count -eq 1) {
    Assert-Field 'repetition interval'      $repeaters[0].InnerText "PT$($RepetitionMinutes)M"
    $dur = $xml.SelectSingleNode('//t:Triggers/*/t:Repetition/t:Duration', $ns)
    Assert-Field 'repetition is indefinite' $(if ($dur) { $dur.InnerText } else { '<none>' }) '<none>'
}

# A PT15M interval in the XML does not prove the trigger has a useful next
# firing time. Assert the schedule is actually armed.
$next = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
if (-not $next) {
    Write-Output ("FAIL  {0,-32} {1}" -f 'NextRunTime present', '<null>')
    $fail += 'NextRunTime is null -- the periodic schedule is not armed'
} else {
    Assert-Field 'NextRunTime in the future' ($next -gt (Get-Date)) 'True'
    Assert-Field "NextRunTime within $($RepetitionMinutes + 1) min" `
        ($next -le (Get-Date).AddMinutes($RepetitionMinutes + 1)) 'True'
    Write-Output ("      {0,-32} {1:yyyy-MM-dd HH:mm:ss}" -f 'next run at', $next)
}

# The deployment must still match what was just recorded.
foreach ($e in $manifest.files) {
    $have = (Get-FileHash (Join-Path $DeployDir $e.name) -Algorithm SHA256).Hash
    Assert-Field "deployed hash $($e.name)" $have $e.sha256
}

Write-Output ""
if ($fail.Count -gt 0) {
    # An assertion failure means the task IS registered but is not the task that
    # was reviewed. Leaving it enabled while a human reads the error would let a
    # wrong definition fire on its own schedule, so disable it immediately and
    # let rollback restore the previous one deliberately.
    Write-Output "=== $($fail.Count) ASSERTION FAILURE(S) -- REGISTRATION FAILED OPERATIONALLY ==="
    $fail | ForEach-Object { Write-Output "  $_" }

    try {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        Write-Output "`nThe task has been DISABLED so the unverified definition cannot fire."
    } catch {
        Write-Output "`nWARNING: could not disable the task: $($_.Exception.Message)"
        Write-Output "Disable it by hand before anything else."
    }

    # $PSScriptRoot, not "$PSCommandPath\..", which is not a resolvable path.
    $rollbackScript = Join-Path $PSScriptRoot 'rollback-mount-task.ps1'
    Write-Output "`nRoll back FIRST, then troubleshoot:"
    if ($backup -and (Test-Path -LiteralPath $backup)) {
        Write-Output "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$rollbackScript`" -BackupXml `"$backup`""
    } else {
        Write-Output "  (no prior task existed, so there is nothing to restore --"
        Write-Output "   unregister it instead: Unregister-ScheduledTask -TaskName $TaskName)"
    }
    exit 1
}

Write-Output "=== all assertions passed ==="
Write-Output "installed XML: $(Join-Path $DeployDir "$TaskName.installed.xml")"
Write-Output "backup       : $backup"
exit 0
