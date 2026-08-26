<#
.SYNOPSIS
    Grant or revoke the Claude Code permission rules this script owns, with the
    candidate validated BEFORE it becomes the live settings file.

.DESCRIPTION
    Rewritten 2026-08-26 after an operational-safety review. The previous
    version wrote a UTF-8 BOM into the live settings.json, making it
    strictly-invalid JSON -- the rule it added landed correctly, so by its own
    reckoning it had succeeded, while the file it edited stopped parsing. That
    risks every setting in the file, not the one being added.

    The BOM was one instance of a wider rule the old design could not honour:

        candidate bytes must not become authoritative until validated

    So this is prepare -> validate -> commit -> verify, not
    commit -> discover whether it was bad. A candidate is written to a sibling
    temp file, fully validated there, and only then committed over the live
    file -- see COMMIT SEMANTICS below for exactly what that commit does and
    does not promise.

    COMMIT SEMANTICS, STATED HONESTLY (OPS-7). An earlier write-up of this
    script called the commit an "atomic move". It was not one, and nothing here
    claims to be one now.

    The candidate is written through a FileStream and Flush($true)'d before the
    handle closes. Flush($true) issues FlushFileBuffers, which asks Windows to
    push that file's own bytes past the OS cache to the device -- so the
    candidate's CONTENT is on stable storage before anything replaces the live
    file.

    The commit itself is [System.IO.File]::Replace -- the Win32 ReplaceFile
    primitive, whose documented purpose is replacing one existing file with
    another. A generic Move-Item -Force overwrite is documented only as "move,
    overwriting"; it carries no replacement or crash-consistency contract at
    all. ReplaceFile additionally preserves the REPLACED file's security
    descriptor, so the ACL on settings.json survives the commit instead of
    being silently swapped for whatever ACL the temp file inherited from its
    directory. Move-Item does not: it leaves the temp file's ACL in place.

    WHAT IS STILL NOT GUARANTEED. ReplaceFile is a replacement primitive, not a
    POSIX-style durability barrier:

      * the DIRECTORY entry is never fsync'd. Windows exposes no supported way
        to fsync a directory, so after a host or power loss the replacement
        itself may not have reached the disk even though the candidate's bytes
        did.
      * FlushFileBuffers can still be defeated by a drive whose volatile write
        cache lies about flushing.
      * ReplaceFile REQUIRES the destination to already exist, and requires
        both paths to be on the SAME volume. Both hold here: the script refuses
        at the top if the settings file is absent, and the candidate is written
        as a sibling of it, in the same directory.

    WHAT THE FAILURE HANDLER MAY AND MAY NOT CLAIM. Until 2026-08-26 the
    sentence above ended "-- the live file is left UNTOUCHED", and the catch
    block printed "settings.json is UNCHANGED and the candidate was discarded"
    with no Test-Path, no re-read and no comparison behind it. That is the
    exact class of claim OPS-7 was written to stop making: an assertion about
    on-disk state that the code never measured.

    It is also not a safe assumption. ReplaceFile has documented PARTIAL
    failure modes -- ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 is described as leaving
    the file-to-be-replaced under the BACKUP name, i.e. no longer present under
    its own. The old code passed [NullString]::Value for lpBackupFileName, so
    in that mode there WAS no backup name, and it then ran
    Remove-Item $candidate -- deleting the replacement as well. Destination
    gone, candidate gone, operator told nothing happened.

    Two changes:

      1. a REAL backup path is passed to ReplaceFile. lpBackupFileName is the
         parameter that makes the partial failure recoverable at all; a
         timestamped .bak- copy taken earlier does not help an operator who was
         told to look for nothing. On a verified-successful commit that copy is
         removed, so a clean run still leaves no litter.
      2. the catch MEASURES. It hashes the destination before the commit and
         after the failure, re-reads it, and reports which of these actually
         holds: unchanged (byte-identical -- the only case allowed to print
         UNCHANGED), present but altered, present but unparseable, or absent.
         It deletes the candidate ONLY in the verified-unchanged case;
         in every other case it deletes nothing and prints a recovery command.

    READ-ONLY DESTINATIONS -- A DELIBERATE BEHAVIOUR CHANGE (2026-08-26).
    Measured on plain NTFS: with the ReadOnly attribute set on the destination,
    [System.IO.File]::Replace throws "Access to the path is denied." while the
    pre-OPS-7 Move-Item -Force SUCCEEDED -- and silently cleared the ReadOnly
    attribute as a side effect. So a user who marked settings.json read-only on
    purpose could grant and revoke before OPS-7 and cannot now.

    That regression is now deliberate and named: the script detects the
    attribute up front and REFUSES before taking a backup or writing a
    candidate, with a message that gives the attrib command to clear it. It
    does not clear the attribute itself -- silently stripping a protection the
    user set is what the old Move-Item did, and it is not this script's call to
    make. -WhatIf still previews, and warns.

    So: this is safe against script or process failure, and safe against
    committing invalid bytes. It is NOT proven durable against host, VM or
    power loss. Do not describe it as atomic.

    AND THE ALLOW KEY IS NORMALISED. A settings.json whose permissions section
    has a deny list but NO allow key -- the fresh-user shape, and precisely the
    person who runs a script that adds a first allow rule -- used to report
    "current allow list: 1 rule(s)" for ZERO rules, because @($null) has
    Count 1, and then died with a raw SetValueInvocationException from
    $settings.permissions.allow = $wanted, which is outside any try/catch. A
    missing or null allow key is now normalised to an empty list at read time.

    AND THE UNDO IS VERIFIED (OPS-6). The old -Revoke wrote and immediately
    announced success without checking the rules were gone or the file still
    parsed. For a security undo, silent false success is worse than a failed
    grant: a standing authorization remains while the user believes it is gone.
    It also only removed the deploy rules if you remembered to repeat
    -IncludeDeploy, though the help said it removed "the rules this script
    adds". Revoke now always removes ALL script-owned rules.

.PARAMETER IncludeDeploy
    Grant the optional deploy rules too. Usually unnecessary: the allow list
    already contains Bash(docker compose:*), which covers the deploy path.
    Ignored by -Revoke, which always removes everything this script owns.

.PARAMETER Revoke
    Remove all script-owned rules.

.PARAMETER WhatIf
    Show the change; write nothing.

.EXAMPLE
    .\scripts\claude-permissions.ps1 -WhatIf
    .\scripts\claude-permissions.ps1
    .\scripts\claude-permissions.ps1 -Revoke
#>

[CmdletBinding()]
param(
    [switch]$IncludeDeploy,
    [switch]$Revoke,
    [switch]$WhatIf,
    # Testability. A security script whose undo path has never executed is not
    # a script anybody should trust, and the undo cannot be exercised safely
    # against the user's real settings file. Defaults to the real one.
    [string]$SettingsPath
)

$ErrorActionPreference = 'Stop'
if (-not $SettingsPath) {
    $SettingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
}

# Everything this script may add. Revoke removes ALL of them regardless of how
# the grant was invoked, so the user never has to remember which flags they used.
$MERGE_RULES  = @('Bash(gh pr merge:*)')
$DEPLOY_RULES = @('Bash(docker compose up:*)', 'Bash(docker compose build:*)', 'Bash(docker restart:*)')
$OWNED        = $MERGE_RULES + $DEPLOY_RULES

function Say([string]$m)  { Write-Host "  $m" }
function Good([string]$m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  WARN $m" -ForegroundColor Yellow }
function Die([string]$m)  { Write-Host "  STOP $m" -ForegroundColor Red; exit 1 }

function Test-NoBom {
    <# Guarded: indexing [0..2] on a file shorter than three bytes throws
       rather than reporting, and a truncated file must fail validation
       loudly. #>
    param([string]$Path)
    $fs = [System.IO.File]::OpenRead($Path)
    try {
        $buf = New-Object byte[] 3
        $n = $fs.Read($buf, 0, 3)
    } finally { $fs.Dispose() }
    if ($n -lt 3) { return $false }          # too short to be our settings file
    return -not ($buf[0] -eq 0xEF -and $buf[1] -eq 0xBB -and $buf[2] -eq 0xBF)
}

function Get-FileHashHex {
    <# SHA-256 of a file, or $null if it cannot be read at all. Used to decide
       -- by measurement, not assertion -- whether a failed commit left the
       destination byte-identical to what it was immediately before. OpenRead
       asks for FileShare.Read, so a destination another process holds with
       FileShare::Read can still be hashed; a destination held exclusively
       cannot, and $null then correctly means "cannot verify" rather than
       "unchanged". #>
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $fs = [System.IO.File]::OpenRead($Path)
            try { return ([BitConverter]::ToString($sha.ComputeHash($fs))).Replace('-', '') }
            finally { $fs.Dispose() }
        } finally { $sha.Dispose() }
    } catch { return $null }
}

function Write-CommitFailureReport {
    <#
      MEASURE, DO NOT ASSERT. Never returns -- every path ends in Die.

      The previous handler was three lines: delete the candidate, then print
      "settings.json is UNCHANGED and the candidate was discarded". There was
      no Test-Path, no re-read and no comparison behind that sentence. It is
      the same class of claim OPS-7 was written to stop making, and it is not
      a safe one: ReplaceFile's documented partial failures -- notably
      ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 -- can leave the destination moved
      aside, at which point the message was false AND the deletion destroyed
      the only other copy of the intended content.

      So: observe the destination, report exactly what was observed, and delete
      the candidate ONLY where the observation says the commit did nothing.
      $PreCommitHash is deliberately untyped -- typing it [string] would bind a
      $null to "" and turn "could not hash" into a hash that matches nothing.
    #>
    param([string]$Err, [string]$Destination, [string]$Candidate,
          [string]$ReplacedCopy, [string]$Backup, $PreCommitHash)

    $destExists = Test-Path -LiteralPath $Destination
    $postHash   = $null
    $destParses = $false
    if ($destExists) {
        $postHash = Get-FileHashHex $Destination
        try {
            $null = ([System.IO.File]::ReadAllText($Destination) -replace "^\xEF\xBB\xBF", '' | ConvertFrom-Json)
            $destParses = $true
        } catch { $destParses = $false }
    }
    $keptCopy = Test-Path -LiteralPath $ReplacedCopy
    $candLeft = Test-Path -LiteralPath $Candidate
    $verifiedIdentical = ($destExists -and $null -ne $PreCommitHash -and
                          $null -ne $postHash -and $postHash -eq $PreCommitHash)

    Write-Host ""
    Warn "the commit FAILED: $Err"
    Say  "MEASURED state of the destination after that failure:"
    Say  ("    exists ..................... {0}" -f $destExists)
    if ($destExists) {
        Say ("    parses as JSON ............. {0}" -f $destParses)
        if ($null -eq $PreCommitHash -or $null -eq $postHash) {
            Say  "    byte-identical to pre-commit UNKNOWN (the file could not be hashed)"
        } else {
            Say ("    byte-identical to pre-commit {0}" -f $verifiedIdentical)
        }
    }
    Say ("    ReplaceFile backup copy .... {0}" -f $(if ($keptCopy) { $ReplacedCopy } else { 'not created' }))
    Say ("    candidate still present .... {0}" -f $(if ($candLeft) { $Candidate } else { 'no' }))
    Say ("    timestamped backup ......... {0}" -f $Backup)
    Write-Host ""

    if ($verifiedIdentical) {
        # The ONLY branch permitted to say UNCHANGED, and it now says it
        # because a hash comparison showed it, not because the code assumed it.
        if ($candLeft) { Remove-Item $Candidate    -Force -ErrorAction SilentlyContinue }
        if ($keptCopy) { Remove-Item $ReplacedCopy -Force -ErrorAction SilentlyContinue }
        Die ("settings.json is UNCHANGED -- verified byte-identical to the copy hashed " +
             "immediately before the commit. The candidate was discarded. Backup remains at $Backup.")
    }

    # Every other outcome is a partial failure. Delete NOTHING: the candidate
    # and the ReplaceFile backup copy may be the only remaining copies.
    if (-not $destExists) {
        Warn "settings.json NO LONGER EXISTS under its own name."
        if ($keptCopy) {
            Say ("recover with:  Move-Item -LiteralPath '" + $ReplacedCopy + "' -Destination '" + $Destination + "'")
        } else {
            Say ("recover with:  Copy-Item -LiteralPath '" + $Backup + "' -Destination '" + $Destination + "'")
        }
    } elseif (-not $destParses) {
        Warn "settings.json EXISTS but does not parse."
        Say ("recover with:  Copy-Item -LiteralPath '" + $Backup + "' -Destination '" + $Destination + "'")
    } else {
        Warn "settings.json EXISTS and parses, but is NOT byte-identical to its pre-commit state."
        Say ("compare against:  " + $Backup)
    }
    Write-Host ""
    Die ("the commit FAILED PARTWAY: settings.json is NOT in its pre-commit state. " +
         "Nothing was deleted -- recover using the paths listed above.")
}

function Write-CandidateAndValidate {
    <#
      prepare -> validate -> commit. Returns the temp path only after every
      check passes on the CANDIDATE, so the live file is never the thing being
      discovered to be bad.
    #>
    param([string]$FinalPath, $Object, [string[]]$MustContain, [string[]]$MustNotContain,
          [string[]]$OriginalOtherKeys)

    $tmp = "$FinalPath.candidate-$([guid]::NewGuid().ToString('N').Substring(0,8))"
    $json = $Object | ConvertTo-Json -Depth 20
    # UTF8Encoding($false) is the only reliable no-BOM write on PS 5.1;
    # Set-Content -Encoding UTF8 emits a BOM, which is the original defect.
    #
    # Written through a FileStream rather than WriteAllText so Flush($true) can
    # run before the handle closes: that is FlushFileBuffers, which pushes
    # these bytes past the OS cache to the device. WriteAllText offers no way
    # to ask for that, so the candidate could still have been sitting in cache
    # at the moment the live file was replaced. CreateNew, not Create: a
    # candidate name collision must throw, never silently clobber.
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($json)
    $fs = New-Object System.IO.FileStream($tmp,
            [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None)
    try {
        $fs.Write($bytes, 0, $bytes.Length)
        $fs.Flush($true)
    } finally { $fs.Dispose() }

    try {
        if (-not (Test-NoBom $tmp)) { throw "candidate has a BOM or is too short" }

        $reparsed = [System.IO.File]::ReadAllText($tmp, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        if (-not $reparsed.permissions) { throw "candidate lost its permissions section" }

        $allow = @($reparsed.permissions.allow)
        foreach ($r in $MustContain)    { if ($allow -notcontains $r) { throw "candidate is missing $r" } }
        foreach ($r in $MustNotContain) { if ($allow -contains $r)    { throw "candidate still contains $r" } }

        # Unrelated top-level settings must survive. The BOM incident risked
        # the whole file; a serialization slip could silently drop a key.
        foreach ($k in $OriginalOtherKeys) {
            if (-not ($reparsed.PSObject.Properties.Name -contains $k)) {
                throw "candidate lost the unrelated setting '$k'"
            }
        }
        return $tmp
    } catch {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        throw
    }
}

# --------------------------------------------------------------- read ------
if (-not (Test-Path $SettingsPath)) { Die "no settings file at $SettingsPath" }
if (-not (Test-NoBom $SettingsPath)) {
    Warn "the CURRENT settings.json already has a BOM; it is not strict JSON."
    Warn "This script will write a clean file, but check what produced it."
}
try   { $settings = [System.IO.File]::ReadAllText($SettingsPath) -replace "^\xEF\xBB\xBF", '' | ConvertFrom-Json }
catch { Die "settings.json does not parse: $_" }
if (-not $settings.permissions) { Die "settings.json has no 'permissions' section" }
if ($settings.permissions -isnot [PSCustomObject]) {
    Die "settings.json has a 'permissions' key that is not a JSON object"
}

# NORMALISE THE ALLOW KEY BEFORE COUNTING OR ASSIGNING.
#
# A permissions section with a deny list and no allow key at all is the
# fresh-user shape -- somebody who has denied things and never allowed
# anything, which is exactly who runs a script that adds a first allow rule.
# Un-normalised it broke twice over:
#
#   * @($settings.permissions.allow) is @($null), whose Count is 1, so the
#     script printed "current allow list: 1 rule(s)" for ZERO rules. A
#     confident wrong number, not a crash.
#   * $settings.permissions.allow = $wanted then threw
#     SetValueInvocationException ("The property 'allow' cannot be found on
#     this object") from OUTSIDE any try/catch, so the script died with a raw
#     .NET error instead of its own STOP message, after the .bak- copy had
#     already been taken and with nothing to collect it.
#
# An explicit "allow": null has the same Count-1 problem, so handle it too.
# A file with "allow": [] was always fine; the gap was precisely the absent key.
$allowProp = $settings.permissions.PSObject.Properties['allow']
if ($null -eq $allowProp) {
    $settings.permissions | Add-Member -MemberType NoteProperty -Name 'allow' -Value @()
} elseif ($null -eq $allowProp.Value) {
    $settings.permissions.allow = @()
}

$existing  = @($settings.permissions.allow)
$otherKeys = @($settings.PSObject.Properties.Name | Where-Object { $_ -ne 'permissions' })
Say ("current allow list: {0} rule(s)" -f $existing.Count)

# The ReadOnly attribute, read once here and acted on below. See READ-ONLY
# DESTINATIONS in .DESCRIPTION: File.Replace refuses a read-only destination,
# where the pre-OPS-7 Move-Item -Force silently succeeded and cleared the
# attribute. -Force so a Hidden settings.json is still readable by Get-Item.
$destReadOnly = ((Get-Item -LiteralPath $SettingsPath -Force).Attributes -band
                 [System.IO.FileAttributes]::ReadOnly) -ne 0

# ------------------------------------------------------------- compute -----
if ($Revoke) {
    $wanted   = @($existing | Where-Object { $OWNED -notcontains $_ })
    $removing = @($existing | Where-Object { $OWNED -contains $_ })
    $adding   = @()
} else {
    $grant    = if ($IncludeDeploy) { $MERGE_RULES + $DEPLOY_RULES } else { $MERGE_RULES }
    $adding   = @($grant | Where-Object { $existing -notcontains $_ })
    $wanted   = @($existing + $adding)
    $removing = @()
}

Write-Host ""
if ($Revoke) {
    if ($removing.Count -eq 0) { Good "no script-owned rules present; nothing to do"; exit 0 }
    Say "would REMOVE:"; foreach ($r in $removing) { Write-Host "      - $r" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "  This removes only the entries THIS SCRIPT owns. It does not" -ForegroundColor Yellow
    Write-Host "  revoke capability granted by other allow rules -- notably" -ForegroundColor Yellow
    Write-Host "  Bash(docker compose:*), which already covers the deploy path." -ForegroundColor Yellow
} else {
    Write-Host "  This grants Claude the following, WITHOUT prompting:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    gh pr merge   land ANY pull request, in ANY repo it can reach," -ForegroundColor Yellow
    Write-Host "                  into ANY branch. The rule syntax cannot express" -ForegroundColor Yellow
    Write-Host "                  'only ScanHound' or 'only PRs Claude opened'." -ForegroundColor Yellow
    if ($IncludeDeploy) {
        Write-Host "    docker compose up/build, docker restart -- recreate containers." -ForegroundColor Yellow
    }
    Write-Host ""
    if ($adding.Count -eq 0) {
        Good "every rule already present; no change needed"
        if (-not $IncludeDeploy) { Say "note: Bash(docker compose:*) already covers the deploy path." }
        exit 0
    }
    Say "would ADD:"; foreach ($r in $adding) { Write-Host "      + $r" -ForegroundColor Green }
}
Write-Host ""
Say "Reverse at any time:  .\scripts\claude-permissions.ps1 -Revoke"

if ($WhatIf) {
    Write-Host ""
    if ($destReadOnly) {
        Warn "NOTE: this file carries the READ-ONLY attribute. A real run would REFUSE"
        Warn ("to commit -- clear it first with:  attrib -R " + [char]34 + $SettingsPath + [char]34)
    }
    Warn "-WhatIf: nothing written"
    exit 0
}

# Refuse a read-only destination BEFORE taking a backup or writing a candidate,
# so a refusal leaves no litter at all. This is a deliberate, documented
# behaviour change from the pre-OPS-7 script -- see READ-ONLY DESTINATIONS in
# .DESCRIPTION. The direction is fail-closed, and the message has to be
# actionable, because "Access to the path is denied" from deep inside
# ReplaceFile names neither the cause nor the fix.
if ($destReadOnly) {
    Write-Host ""
    Say "This is a CHANGE from the pre-2026-08-26 script, which committed with"
    Say "Move-Item -Force: that succeeded here and silently cleared your"
    Say "READ-ONLY attribute as a side effect. This script will not strip a"
    Say "protection you set; clear it yourself if you meant to."
    Write-Host ""
    Die ("$SettingsPath carries the READ-ONLY attribute. The commit primitive " +
         "(ReplaceFile) refuses a read-only destination with 'Access to the path " +
         "is denied.' Nothing was written and no backup was taken. Clear the " +
         "attribute and re-run:  attrib -R " + [char]34 + $SettingsPath + [char]34)
}

# ------------------------------------------------- prepare/validate/commit --
$backup = "$SettingsPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item $SettingsPath $backup
Good "backup written to $backup"

$settings.permissions.allow = $wanted
try {
    $candidate = Write-CandidateAndValidate -FinalPath $SettingsPath -Object $settings `
                    -MustContain $adding -MustNotContain $removing -OriginalOtherKeys $otherKeys
} catch {
    # "untouched" is safe to assert HERE, unlike in the commit handler below,
    # and for a structural reason rather than an assumption: nothing in
    # Write-CandidateAndValidate ever opens $SettingsPath for writing. It
    # creates a separate sibling with FileMode::CreateNew and validates that.
    # The commit handler cannot say the same, because the primitive it calls
    # is the one thing in this script that does mutate the destination.
    Die "the candidate FAILED validation and was discarded; settings.json is untouched. $_"
}
Good "candidate validated (no BOM, parses, delta correct, unrelated settings intact)"

# COMMIT. [System.IO.File]::Replace is the Win32 ReplaceFile primitive, and it
# has two hard preconditions:
#   1. the DESTINATION must already exist -- Replace will not create it, it
#      throws FileNotFoundException. The read section above refuses at "no
#      settings file at ..." long before we reach here, so this is a documented
#      DEPENDENCY on that guard, not an assumption. Delete that guard and this
#      line breaks.
#   2. both paths must be on the SAME volume. $candidate is built as
#      "$FinalPath.candidate-<guid>" -- a sibling in the same directory.
#   3. the third argument is lpBackupFileName. It used to be [NullString]::Value
#      -- a real null, because PowerShell converts a bare $null to "" when
#      binding to [string] and ReplaceFile rejects "" with "The path is not of a
#      legal form." -- on the reasoning that the .bak- copy above made a second
#      backup litter. That reasoning was wrong. lpBackupFileName is the
#      parameter that makes ReplaceFile's PARTIAL failures recoverable:
#      ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 is documented as leaving the
#      file-to-be-replaced under the BACKUP name, which does not exist when NULL
#      is passed. So we pass a real sibling path, and remove it only once the
#      commit has been verified below.
$replacedCopy  = "$SettingsPath.replaced-$([guid]::NewGuid().ToString('N').Substring(0,8))"

# Measured BEFORE the commit, so the handler below can compare rather than
# assert. $null here means "could not hash", which is reported as UNKNOWN --
# never silently treated as unchanged.
$preCommitHash = Get-FileHashHex $SettingsPath

try {
    [System.IO.File]::Replace($candidate, $SettingsPath, $replacedCopy)
} catch {
    Write-CommitFailureReport -Err "$_" -Destination $SettingsPath -Candidate $candidate -ReplacedCopy $replacedCopy -Backup $backup -PreCommitHash $preCommitHash
}

# ----------------------------------------------------------- verify live ---
if (-not (Test-NoBom $SettingsPath)) { Die "the live file has a BOM after commit. Restore from $backup" }
try { $final = [System.IO.File]::ReadAllText($SettingsPath) | ConvertFrom-Json }
catch { Die "the live file does not parse after commit. Restore from $backup" }

$live = @($final.permissions.allow)
foreach ($r in $adding)   { if ($live -notcontains $r) { Die "live file is missing $r. Restore from $backup" } }
foreach ($r in $removing) { if ($live -contains $r)    { Die "live file still contains $r. Restore from $backup" } }
foreach ($k in $otherKeys){ if (-not ($final.PSObject.Properties.Name -contains $k)) { Die "live file lost '$k'. Restore from $backup" } }

# Only now: the commit is verified, so the copy ReplaceFile made of the OLD
# file is redundant with the timestamped .bak- and can go. Note the ordering --
# every Die above leaves it in place on purpose, because a commit that
# completed but verified wrong is exactly when a second copy is worth having.
if (Test-Path -LiteralPath $replacedCopy) { Remove-Item $replacedCopy -Force -ErrorAction SilentlyContinue }

Good ("live file verified: {0} rule(s), all other settings intact" -f $live.Count)

Write-Host ""
Say "The FILE on disk has changed. A running Claude Code process has NOT"
Say "reloaded it -- restart Claude Code for this to take effect."

# Explicit, so "success means exit 0" is a contract the tests can assert rather
# than an accident of whatever the last statement happened to leave behind
# (SR2-3).
exit 0
