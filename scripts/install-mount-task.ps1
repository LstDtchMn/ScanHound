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
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-mount-task.ps1 -ExpectedCommit <40-hex-sha>
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-mount-task.ps1 -ExpectedCommit <40-hex-sha> -WhatIf
#
# -ExpectedCommit is MANDATORY and must be supplied out-of-band (not read from
# HEAD). Get it from the reviewed branch head after review, never from the
# working tree at run time.
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
    [string]$RootDir    = 'C:\ProgramData\ScanHound',
    [string]$DeployDir  = 'C:\ProgramData\ScanHound\deploy',
    [string]$BackupDir  = 'C:\ProgramData\ScanHound\backup',
    [string]$RunDir     = 'C:\ProgramData\ScanHound\run'
)

$ErrorActionPreference = 'Stop'

# Uninitialised variables become $null silently in PowerShell, which is exactly
# how the manifest came to record null blob ids after a rename: the assignment
# moved to $scriptObj/$composeObj while the manifest still read $scriptBlob.
# Strict mode turns that class of typo into an error instead of a quiet wrong
# value in a one-time provenance record.
Set-StrictMode -Version 2.0

$utf8NoBom = New-Object Text.UTF8Encoding($false)

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

# PARAMETER CONTAINMENT. The whole parent-boundary argument is that the three
# working directories sit beneath a root this installer protects. Overrides
# could put a child somewhere else entirely -- hardening RootDir while the
# deployed script lives under a user-writable parent, still replaceable through
# that parent's delete-child right. Defaults are coherent; this makes the
# guarantee true under overrides too, rather than only for one invocation.
$canonRoot = [IO.Path]::GetFullPath($RootDir).TrimEnd('\')
$seenChild = @{}
foreach ($childPath in @($DeployDir, $BackupDir, $RunDir)) {
    $full = [IO.Path]::GetFullPath($childPath).TrimEnd('\')
    if ($full -eq $canonRoot) {
        throw "'$childPath' resolves to the root itself; it must be a distinct child of $canonRoot."
    }
    if (-not $full.StartsWith($canonRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "'$childPath' resolves to '$full', which is not beneath '$canonRoot'."
    }
    if ($seenChild.ContainsKey($full)) {
        throw "'$childPath' duplicates another directory parameter ('$full')."
    }
    $seenChild[$full] = $true
}

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

# Parses a raw git tree object into its entries.
#
# Binary format, one entry after another with no separators:
#   <octal mode ASCII><space><name bytes><NUL><20 raw bytes of SHA-1>
function Get-GitTreeEntries {
    param([byte[]]$Bytes)

    $entries = @()
    $i = 0
    while ($i -lt $Bytes.Length) {
        $sp = $i
        while ($sp -lt $Bytes.Length -and $Bytes[$sp] -ne 0x20) { $sp++ }
        if ($sp -ge $Bytes.Length) { throw "Malformed tree object: no space after mode." }
        $mode = [Text.Encoding]::ASCII.GetString($Bytes, $i, $sp - $i)

        $nul = $sp + 1
        while ($nul -lt $Bytes.Length -and $Bytes[$nul] -ne 0x00) { $nul++ }
        if ($nul -ge $Bytes.Length) { throw "Malformed tree object: no NUL after name." }
        # Names are stored as raw bytes; UTF8 is correct for this repository.
        $name = [Text.Encoding]::UTF8.GetString($Bytes, $sp + 1, $nul - $sp - 1)

        if ($nul + 20 -ge $Bytes.Length + 0 -and $nul + 21 -gt $Bytes.Length) {
            throw "Malformed tree object: truncated object id for '$name'."
        }
        $oid = ''
        for ($k = 0; $k -lt 20; $k++) { $oid += $Bytes[$nul + 1 + $k].ToString('x2') }

        $entries += [pscustomobject]@{ Mode = $mode; Name = $name; Id = $oid }
        $i = $nul + 21
    }
    return $entries
}

# Resolves a repo-relative path to a blob id by WALKING AND VERIFYING the tree
# chain from the already-captured commit bytes.
#
# This replaces `git rev-parse <commit>:<path>`, which cannot be trusted here:
# fsck is a point-in-time consistency check, not a lock, so a same-user process
# can present an altered tree during rev-parse and restore the authentic one
# afterwards -- leaving the installer holding a substituted but independently
# valid blob that passes every other check. Walking the chain ourselves means
# each object is authenticated from its own captured bytes, so a mutation after
# capture is irrelevant and malformed bytes at an expected object name fail the
# recomputed-id test.
function Resolve-VerifiedBlobId {
    param([byte[]]$CommitBytes, [string]$RepoRelative)

    # The commit object begins with "tree <40-hex>\n".
    $head = [Text.Encoding]::ASCII.GetString($CommitBytes, 0, [Math]::Min(64, $CommitBytes.Length))
    if ($head -notmatch '^tree ([0-9a-f]{40})') {
        throw "Commit object does not begin with a tree header; refusing to resolve $RepoRelative."
    }
    $treeId = $Matches[1]

    $parts = $RepoRelative -split '/'
    for ($d = 0; $d -lt $parts.Count; $d++) {
        $treeBytes = Invoke-GitBytes @('cat-file', 'tree', $treeId)
        $computed  = Get-GitObjectId -Type 'tree' -Bytes $treeBytes
        if ($computed -ne $treeId) {
            throw ("Tree-identity check FAILED resolving ${RepoRelative}: object $treeId " +
                   "hashes to $computed. The object database is corrupt or was mutated.")
        }
        $entry = Get-GitTreeEntries -Bytes $treeBytes |
                 Where-Object { $_.Name -eq $parts[$d] } | Select-Object -First 1
        if (-not $entry) {
            throw "Path '$RepoRelative' not found: '$($parts[$d])' is absent from tree $treeId."
        }
        if ($d -eq $parts.Count - 1) {
            if ($entry.Mode -notmatch '^100') {
                throw "'$RepoRelative' is mode $($entry.Mode), not a regular file blob."
            }
            return $entry.Id
        }
        if ($entry.Mode -ne '40000') {
            throw "'$($parts[$d])' in '$RepoRelative' is mode $($entry.Mode), not a tree."
        }
        $treeId = $entry.Id
    }
    throw "Unreachable: failed to resolve $RepoRelative."
}

# git's object id is SHA1("<type> <length>\0" + content). Recomputing it lets
# the installer verify that the bytes it received really are the object it
# asked for, rather than trusting a writable database's own bookkeeping.
function Get-GitObjectId {
    param([string]$Type, [byte[]]$Bytes)
    $header = [Text.Encoding]::ASCII.GetBytes("$Type $($Bytes.Length)`0")
    $buf    = New-Object byte[] ($header.Length + $Bytes.Length)
    [Array]::Copy($header, 0, $buf, 0, $header.Length)
    [Array]::Copy($Bytes,  0, $buf, $header.Length, $Bytes.Length)
    $sha1 = [Security.Cryptography.SHA1]::Create()
    try { return (($sha1.ComputeHash($buf) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha1.Dispose() }
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

# Applies an EXACT protected descriptor, replacing whatever was there.
#
# `/inheritance:r` removes only INHERITED aces and `/grant` ADDS to the
# existing dacl, so the previous sequence could not produce the descriptor it
# claimed. Measured: an explicit user Modify ace planted beforehand SURVIVED
# /inheritance:r + /grant intact. Assert-AdminOnlyPath would then throw --
# fail-safe, but the installer would have been asserting a state it never
# actually applied.
#
# Building a fresh DirectorySecurity (rather than editing Get-Acl's result)
# guarantees no pre-existing rule is carried forward, including ones from
# principals this code has never heard of -- which is precisely the class an
# allowlist of /remove targets cannot cover.
function Set-ExactAdminDacl {
    param([string]$Path, [string]$ReadOnlySid)

    $sec = New-Object Security.AccessControl.DirectorySecurity
    $sec.SetAccessRuleProtection($true, $false)   # protect; discard inherited
    $sec.SetOwner((New-Object Security.Principal.NTAccount('BUILTIN\Administrators')))
    foreach ($id in @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM')) {
        $sec.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $id, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    if ($ReadOnlySid) {
        $sec.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            (New-Object Security.Principal.SecurityIdentifier($ReadOnlySid)),
            'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    Set-Acl -Path $Path -AclObject $sec
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

# NOTE: no working-tree existence check. Deployment content comes entirely from
# the approved commit's object tree, so requiring the files to exist in the
# CURRENT checkout would reject a perfectly valid approved commit whenever the
# tree happens to be on a different branch -- an operability trap with no
# security value.

# Pinned tools must themselves be trustworthy before they are used to establish
# provenance for anything else -- and the check has to cover the CHAIN, not just
# the file. Assert-AdminOnlyPath alone examines the named object; a protected
# executable inside a directory an ordinary user can rewrite is not protected.
function Assert-PinnedTool([string]$path, [string]$label) {
    $item = Get-Item -LiteralPath $path -Force
    if ($item.Extension -ne '.exe') { throw "$label at '$path' is not an .exe." }
    $root = [IO.Path]::GetPathRoot($item.FullName)
    $cur  = $item.FullName
    while ($cur -and $cur.TrimEnd('\') -ne $root.TrimEnd('\')) {
        Assert-AdminOnlyPath $cur -Quiet
        $parent = Split-Path $cur -Parent
        if (-not $parent -or $parent -eq $cur) { break }
        $cur = $parent
    }
    Write-Output "tool    : $path (chain verified)"
}
Assert-PinnedTool $GitExe    'git.exe'
Assert-PinnedTool $IcaclsExe 'icacls.exe'

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
# The commit object's own identity, for the same reason as the blobs: the
# approved SHA is only meaningful if the object it names really hashes to it.
$commitBytes = Invoke-GitBytes @('cat-file', 'commit', $srcCommit)
$commitId    = Get-GitObjectId -Type 'commit' -Bytes $commitBytes
if ($commitId -ne $srcCommit) {
    throw ("Object-identity check FAILED for the commit: the object stored as $srcCommit " +
           "hashes to $commitId. The object database is corrupt or tampered with.")
}

# THE TREES THAT BIND THE COMMIT TO THE PATHS.
#
# Verifying the commit and the two blobs individually is NOT sufficient. The
# commit does not reference blobs directly -- it names a root tree, which names
# subtrees, which map path entries to blob ids. A tampered tree can therefore
# point `scripts/mount-nas-shares.ps1` at some OTHER perfectly valid blob while
# every identity check above still passes: the commit bytes are untouched, the
# substituted blob is a real object, and its bytes hash to its own id.
#
# fsck reports "hash mismatch" for any stored object whose content does not
# hash to its database id, so running it rooted at the approved commit covers
# the intervening trees. The direct commit/blob calculations are kept as
# defence in depth rather than replaced.
# Verified on this host: git 2.52.0.windows.1 accepts this exact form, exit 0,
# ~0.9 s.
# Run through the SAME helper as every other git call, so it inherits the
# pinned executable, --no-replace-objects, a checked exit code and -- the point
# here -- WorkingDirectory = $SourceRepo. Invoking `& $GitExe` directly made
# fsck run in the CALLER's current directory, so the approved command would
# have failed outright when launched from, say, C:\Windows\System32. Fail-safe,
# but it made "the graph is always verified" depend on where Jesse happened to
# be standing.
#
# NOTE fsck is now DIAGNOSTIC ONLY, not load-bearing. It is a point-in-time
# consistency test, not a lock, so it cannot bind a path resolution that
# happens afterwards. Resolve-VerifiedBlobId carries that proof instead.
[void](Invoke-GitBytes @('fsck', '--full', '--no-dangling', '--no-reflogs', $srcCommit))
Write-Output "fsck    : object graph consistent from $($srcCommit.Substring(0,8)) (diagnostic)"

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

    # Resolved by walking the VERIFIED tree chain from the captured commit
    # bytes, not by `git rev-parse <commit>:<path>` -- see Resolve-VerifiedBlobId
    # for why that resolution cannot be trusted here.
    $blobSha = Resolve-VerifiedBlobId -CommitBytes $script:commitBytes -RepoRelative $RepoRelative
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

    # OBJECT IDENTITY, not merely type and size. The ordinary account can write
    # this repository's object database, and "git says it is a blob of N bytes"
    # is a weaker claim than "these bytes hash to the object id we asked for".
    # Recomputing the id closes that gap without trusting git to police itself.
    #
    # NOTE this is a different computation from the earlier abandoned attempt,
    # which hashed WORKING-TREE bytes and could never match because git applies
    # the autocrlf filter on the way in. These are the stored object bytes, so
    # the hash is the object id by definition.
    $computed = Get-GitObjectId -Type 'blob' -Bytes $bytes
    if ($computed -ne $blobSha) {
        throw ("Object-identity check FAILED for ${RepoRelative}: the extracted bytes hash to " +
               "$computed but were requested as $blobSha. The object database is corrupt or " +
               "has been tampered with. Refusing to deploy.")
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

# THE COMMON PARENT IS PART OF THE BOUNDARY.
#
# Hardening only the three children is insufficient: Windows permits deleting
# or renaming an object when the caller holds DELETE on it OR delete-child on
# its PARENT. An ordinary account with DeleteSubdirectoriesAndFiles on
# C:\ProgramData\ScanHound could therefore remove and re-create `deploy`,
# `backup` or `run` wholesale, no matter how perfectly the children themselves
# deny writes -- taking the elevated task script, the pinned recipe, the
# rollback inputs and the root-executed staging payloads with them.
#
# Also: the ancestor chain is checked for reparse points BEFORE any icacls
# runs. icacls without /L follows a junction, so hardening a redirected path
# would have applied an elevated ACL change to somebody else's directory and
# only been noticed by the assertion afterwards.
function Assert-SafeAncestry([string]$path) {
    $root = [IO.Path]::GetPathRoot($path)
    $cur  = $path
    while ($cur -and $cur.TrimEnd('\') -ne $root.TrimEnd('\')) {
        if (Test-Path -LiteralPath $cur) {
            $node = Get-Item -LiteralPath $cur -Force
            if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw ("'$cur' in the path of '$path' is a reparse point. Refusing to apply " +
                       "elevated ACL changes through a redirection.")
            }
        }
        $parent = Split-Path $cur -Parent
        if (-not $parent -or $parent -eq $cur) { break }
        $cur = $parent
    }
}

# Prove the chain before touching anything.
foreach ($d in @($RootDir, $DeployDir, $BackupDir, $RunDir)) { Assert-SafeAncestry $d }

# The protected ROOT first. Everything below inherits its protection against
# delete-child, which is the right the children cannot defend against
# themselves.
New-Item -ItemType Directory -Force $RootDir | Out-Null
Set-ExactAdminDacl -Path $RootDir -ReadOnlySid $Sid
Assert-AdminOnlyPath $RootDir

# $RunDir holds shell payloads wsl.exe executes as root, so it is an
# elevated-execution input exactly like the deploy directory.
#
# NOTE the absence of /T. Recursing an elevated ACL change over pre-existing,
# not-yet-validated content is the same mistake as writing before hardening:
# a planted reparse-point descendant would be followed. Payload files get
# their descriptor individually, at creation, from New-SecureDeployedFile.
# The task account gets READ ONLY on deploy/backup, and NOTHING on staging: a
# SID ACE cannot distinguish the elevated token from the unelevated one, so the
# write right must simply not be granted.
New-Item -ItemType Directory -Force $DeployDir | Out-Null
New-Item -ItemType Directory -Force $BackupDir | Out-Null
New-Item -ItemType Directory -Force $RunDir    | Out-Null
Set-ExactAdminDacl -Path $DeployDir -ReadOnlySid $Sid
Set-ExactAdminDacl -Path $BackupDir -ReadOnlySid $Sid
Set-ExactAdminDacl -Path $RunDir
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
    source_blobs  = @{ 'mount-nas-shares.ps1' = $scriptObj.Blob; 'docker-compose.yml' = $composeObj.Blob }
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
    # Through the secure helper, not Out-File: this XML is later consumed by an
    # ELEVATED rollback, so it is a trust input with the same requirements as
    # the deployed script. A new file's owner comes from the creating token,
    # not from the parent -- the reasoning already applied to the payloads
    # applies here too.
    New-SecureDeployedFile $backup ($utf8NoBom.GetBytes((Export-ScheduledTask -TaskName $TaskName)))
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
# Same chain-aware assertion as git/icacls: this is the interpreter an elevated
# task will run 288 times a day, so "it exists" is a weaker claim than the one
# made for the installer's own tools.
Assert-PinnedTool $PowerShellExe 'powershell.exe (task action)'
# -WindowStyle Hidden: the task repeats every 5 minutes under an INTERACTIVE
# logon type, so without this it paints a console window on the desktop every
# 5 minutes, forever - reported from the desktop on 2026-07-30. Interactive is
# not negotiable (the script mounts into the docker-desktop WSL2 distro and
# needs the user's session), so the window is suppressed instead of the logon
# type being changed.
#
# HONEST LIMITATION: this does not make the launch perfectly invisible.
# powershell.exe still allocates a console before it parses -WindowStyle, so a
# brief flash can remain. It removes the seconds-long visible window, not
# necessarily every frame of it. Fully flash-free needs a wrapper process,
# which is more moving parts than this is worth.
$action = New-ScheduledTaskAction -Execute $PowerShellExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Deployed`""

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
# So the interval here IS the retry mechanism, by the only mechanism that
# actually works. RestartCount is kept because it still covers genuine launch
# failures, but nothing depends on it.
#
# 60 minutes, chosen deliberately on 2026-07-30 and not to be lowered casually
# back to a "safer" small number without re-reading this: the boot and logon
# triggers each fire exactly ONCE, so this interval is the entire recovery
# budget after both have failed. At 60 the 2026-07-26 failure mode -- Docker's
# backend not accepting IPC until T+119 s -- self-repairs within an hour rather
# than within five minutes. That hour is a deliberate trade for not painting
# the desktop 288 times a day; it is not an accident, and it is the reason the
# window-hiding fix alone was not considered sufficient.
$RepetitionMinutes = 60
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
# REGISTERED DISABLED ON PURPOSE.
#
# Everything between registration and the final verdict can throw -- exporting
# the task, securely creating the evidence XML, four recursive filesystem
# assertions, re-reading the task, parsing its XML. A terminating error in any
# of them skips the disable-on-failure branch further down, and the previous
# version would have left an UNVERIFIED elevated task enabled with its first
# trigger already ~90 seconds out. Catching every exception narrows that
# window; registering disabled removes it. The task is enabled at the very end,
# only after every check has passed, and the enabled/armed state is then
# re-read and asserted.
$settings.Enabled                    = $false

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

# Evidence rather than an elevated input, but it lives in the deployment
# directory, so it gets the same descriptor -- otherwise the claim that every
# artifact there was proven would be false.
$installedXmlPath = Join-Path $DeployDir "$TaskName.installed.xml"
New-SecureDeployedFile $installedXmlPath ($utf8NoBom.GetBytes((Export-ScheduledTask -TaskName $TaskName)))

# NOW every artifact exists -- payloads, manifest, backup XML and installed XML
# -- so this is the first point at which a recursive assertion can honestly
# claim to cover the whole deployment. Running it earlier (as the previous
# version did) proved only what happened to exist at that moment.
Assert-AdminOnlyPath $RootDir
Assert-AdminOnlyPath $DeployDir -Recurse
Assert-AdminOnlyPath $BackupDir -Recurse
Assert-AdminOnlyPath $RunDir    -Recurse

# ---------------------------------------------------------------------------
# 6. Assert the INSTALLED task, not the intent
# ---------------------------------------------------------------------------

$t   = Get-ScheduledTask -TaskName $TaskName
$xml = [xml](Export-ScheduledTask -TaskName $TaskName)
$ns  = New-Object Xml.XmlNamespaceManager($xml.NameTable)
$ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')

$fail = @()

# Windows resolves a SID-registered principal back to its display name, so
# comparing the returned UserId literally against the SID can NEVER pass -- the
# task was correct and the assertion was wrong. Compare IDENTITIES, not strings.
function Resolve-PrincipalSid([string]$value) {
    if (-not $value) { return '' }
    if ($value -match '^S-1-') { return $value }
    try {
        return (New-Object Security.Principal.NTAccount($value)
               ).Translate([Security.Principal.SecurityIdentifier]).Value
    } catch {
        # Unresolvable: return it unchanged so the assertion fails loudly rather
        # than silently passing something we could not identify.
        return $value
    }
}

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
Assert-Field 'principal.UserId (SID)'      (Resolve-PrincipalSid $t.Principal.UserId) $Sid
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
# Deliberately still disabled at this point -- see the registration comment.
Assert-Field 'Enabled (pre-verdict)'       $t.Settings.Enabled 'False'

$bootDelay = $xml.SelectSingleNode('//t:BootTrigger/t:Delay', $ns)
Assert-Field 'BootTrigger.Delay' $(if ($bootDelay) { $bootDelay.InnerText } else { '<none>' }) 'PT3M'

# The key structural requirement: exactly one trigger repeats.
$repeaters = $xml.SelectNodes('//t:Triggers/*/t:Repetition/t:Interval', $ns)
Assert-Field 'triggers owning repetition' $repeaters.Count 1
if ($repeaters.Count -eq 1) {
    # Compare DURATION, not spelling. Task Scheduler normalises the ISO 8601
    # interval it stores: 5 minutes round-trips as PT5M, but 60 minutes comes
    # back as PT1H, so the old string equality would have failed the install
    # for a correctly-registered task the moment the interval stopped being a
    # sub-hour value. Parse both sides and compare elapsed minutes.
    $isoActual = $repeaters[0].InnerText
    $minActual = -1
    try   { $minActual = [int][System.Xml.XmlConvert]::ToTimeSpan($isoActual).TotalMinutes }
    catch { $minActual = -1 }
    Assert-Field "repetition interval (minutes, raw '$isoActual')" `
                 $minActual $RepetitionMinutes
    $dur = $xml.SelectSingleNode('//t:Triggers/*/t:Repetition/t:Duration', $ns)
    Assert-Field 'repetition is indefinite' $(if ($dur) { $dur.InnerText } else { '<none>' }) '<none>'
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

Write-Output "=== all assertions passed -- enabling ==="

# ONLY NOW, and the whole phase is exception-guarded.
#
# Handling only the $post assertion list was not enough: Enable-ScheduledTask
# can change state and then fail, and the reads and property accesses that
# follow can throw on their own (strict mode makes a missing property
# terminating). Any of those would have exited the installer with the task
# ENABLED and its verification incomplete -- precisely the state this design
# forbids. The catch attempts the re-disable unconditionally: harmless if the
# enable never took effect, essential if it did.
try {
    Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null

    $final = Get-ScheduledTask     -TaskName $TaskName -ErrorAction Stop
    $info  = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop

    $post = @()
    if ("$($final.Settings.Enabled)" -ne 'True') { $post += "Enabled = '$($final.Settings.Enabled)'" }

    # A PT5M interval in the XML does not prove the schedule is armed; only a
    # real NextRunTime does. Checked after enabling, because a disabled task
    # has none. StartWhenAvailable is why a boundary that elapsed during
    # validation is acceptable -- but a NextRunTime far in the PAST would mean
    # the schedule is not really live, so it is bounded on both sides.
    $next = $info.NextRunTime
    if (-not $next) {
        $post += 'NextRunTime is null -- the periodic schedule is not armed'
    } else {
        Write-Output ("PASS  {0,-32} {1:yyyy-MM-dd HH:mm:ss}" -f 'next run at', $next)
        if ($next -gt (Get-Date).AddMinutes($RepetitionMinutes + 2)) {
            $post += "NextRunTime $next is more than one interval away"
        }
        if ($next -lt (Get-Date).AddMinutes(-2)) {
            $post += "NextRunTime $next is in the past -- the schedule is stale, not armed"
        }
    }

    if ($post.Count -gt 0) { throw ($post -join '; ') }
}
catch {
    Write-Output "`n=== POST-ENABLE FAILURE ==="
    Write-Output "  $($_.Exception.Message)"
    try {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        Write-Output "The task has been DISABLED again; it cannot fire unverified."
    } catch {
        Write-Output "CRITICAL: could not re-disable the task: $($_.Exception.Message)"
        Write-Output "Disable it BY HAND immediately: Disable-ScheduledTask -TaskName $TaskName"
    }
    exit 1
}

Write-Output "PASS  Enabled                          True"
Write-Output ""
Write-Output "installed XML: $(Join-Path $DeployDir "$TaskName.installed.xml")"
Write-Output "backup       : $backup"
Write-Output "deployed from: $srcCommit"
exit 0
