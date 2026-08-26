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
         UNCHANGED), absent, present-but-unreadable, present-but-unparseable,
         present-and-valid-but-not-comparable, or present but altered. It
         deletes the candidate ONLY in the verified-unchanged case.

    AND IT MAY NOT INVENT A VERDICT EITHER (F1, later the same day). The first
    version of that catch traded one unmeasured claim for its opposite: any
    failure to READ the destination -- a sharing violation included -- was
    recorded as "does not parse", and the handler then told the operator their
    settings file was corrupt and offered a Copy-Item over it, while the file
    was byte-identical and valid. Readability, parseability and identity are
    three separate three-state questions now, and "not measured" is a verdict
    the handler is allowed to reach and required to print. Nothing that could
    overwrite the destination is ever recommended on the strength of an access
    error. See the notes on Write-CommitFailureReport.

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

    WHAT -REVOKE ACTUALLY MATCHES (F6). Broadening revoke to the whole owned
    set is what made this bite, so it is stated plainly here and in the
    on-screen message: -Revoke removes these rule STRINGS whether or not this
    script is what added them.

    There is no provenance anywhere. Revoke compares each entry in the allow
    list, by exact text, against a fixed vocabulary of four rules. Nothing
    records who wrote a rule, so an entry a human typed by hand is
    indistinguishable from one this script granted, and -Revoke removes it
    either way. Reproducible here rather than asserted: New-Fixture -Shape
    PreOwned starts with owned rules that no grant in the test put there, and
    the revoke case asserts they are removed anyway. Defensible as vocabulary;
    false as English, to an operator running a security undo.

    A provenance sidecar was considered and rejected. It would make a revoke's
    completeness depend on a second file that can be lost, copied to another
    machine, or left stale by a hand edit -- and the failure mode when it goes
    missing is a revoke that leaves a standing authorization in place while
    reporting success. That is precisely the outcome OPS-6 above judged worse
    than a failed grant, so buying a truer sentence with a less complete undo
    is the wrong trade. The undo stays maximal; the sentence gets fixed, and
    the rules about to go are printed before anything is written so the
    operator can see a hand-added one in the list.

.PARAMETER IncludeDeploy
    Grant the optional deploy rules too. Usually unnecessary: the allow list
    already contains Bash(docker compose:*), which covers the deploy path.
    WITHOUT this switch the deploy rules are not granted at all -- a plain run
    adds Bash(gh pr merge:*) and nothing else. Ignored by -Revoke, which always
    removes every rule string in the owned set.

.PARAMETER Revoke
    Remove every rule string in this script's owned set from the allow list,
    whether or not this script added it. See WHAT -REVOKE ACTUALLY MATCHES.

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

      The 2026-08-26-morning handler was three lines: delete the candidate,
      then print "settings.json is UNCHANGED and the candidate was discarded".
      There was no Test-Path, no re-read and no comparison behind that
      sentence. It is the same class of claim OPS-7 was written to stop making,
      and it is not a safe one: ReplaceFile's documented partial failures --
      notably ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 -- can leave the destination
      moved aside, at which point the message was false AND the deletion
      destroyed the only other copy of the intended content.

      F1, 2026-08-26 evening: THE FIRST REWRITE OVERSHOT INTO THE SAME CLASS OF
      CLAIM, POINTING THE OTHER WAY. It set $destParses = $false on ANY
      exception out of ReadAllText -- a sharing violation included -- and then
      printed "settings.json EXISTS but does not parse" and died with
      "settings.json is NOT in its pre-commit state". Both are statements about
      CONTENT derived from an ACCESS error.

      That was guaranteed rather than incidental, because Get-FileHashHex
      (OpenRead, FileShare.Read) and ReadAllText (also FileShare.Read) fail
      TOGETHER: an unhashable destination was ALWAYS also an unparseable one,
      so every run that honestly printed "byte-identical ... UNKNOWN" then
      contradicted itself two lines later. Measured against a destination held
      with FileShare::None: the handler said "does not parse" and "NOT in its
      pre-commit state" while the file was, at that same instant,
      byte-identical to its pre-run state and valid JSON -- and it handed the
      operator a Copy-Item that would have clobbered it.

      So readability and identity are BOTH three-state here:

        readable   yes / no                 -- an ACCESS fact
        parses     yes / no / NOT MEASURED  -- a CONTENT fact, and only
                                               meaningful when readable is yes
        identity   same / differs / NOT DETERMINED
                                            -- NOT DETERMINED whenever either
                                               hash is $null; never "differs"

      An EXISTS-but-unreadable outcome gets its own branch, ahead of both
      content verdicts, and that branch recommends NOTHING that would overwrite
      the destination.

      $PreCommitHash is deliberately untyped -- typing it [string] would bind a
      $null to "" and turn "could not hash" into a hash that matches nothing.

      THE BRANCH MARKERS ARE LOAD-BEARING (F2). Every verdict branch below
      carries a marker comment on a line of its own, and
      tests/test_claude_permissions_script.ps1 extracts them and FAILS if any
      marker has no test exercising it, or if the test file's failure-mode axis
      names a mode with no matching marker. Half this function's decision
      surface previously had no assertion and no mutant. The marker set is what
      makes the NEXT uncovered branch visible instead of invisible: add a
      branch with its marker and the suite tells you it is uncovered.
    #>
    param([string]$Err, [string]$Destination, [string]$Candidate,
          [string]$ReplacedCopy, [string]$Backup, $PreCommitHash)

    $destExists   = Test-Path -LiteralPath $Destination
    $postHash     = $null
    $destReadable = $false
    $destParses   = $false
    if ($destExists) {
        $postHash = Get-FileHashHex $Destination
        $text = $null
        # Two separate try blocks ON PURPOSE. Merging them is exactly the F1
        # defect: it makes "the OS would not let me open this" indistinguishable
        # from "I read it and it is not JSON".
        try   { $text = [System.IO.File]::ReadAllText($Destination); $destReadable = $true }
        catch { $destReadable = $false }
        if ($destReadable) {
            try   { $null = ($text -replace "^\xEF\xBB\xBF", '' | ConvertFrom-Json); $destParses = $true }
            catch { $destParses = $false }
        }
    }
    $keptCopy  = Test-Path -LiteralPath $ReplacedCopy
    $candLeft  = Test-Path -LiteralPath $Candidate
    # F3: this row sits under a heading that says MEASURED, so measure it. It
    # used to print $Backup unconditionally while the two rows above it were
    # Test-Path-guarded -- and both recovery commands below are built from this
    # path, so an unverified row meant handing the operator a Copy-Item from a
    # file that may not be there.
    $bakExists = Test-Path -LiteralPath $Backup

    $identityKnown     = ($destExists -and $null -ne $PreCommitHash -and $null -ne $postHash)
    $verifiedIdentical = ($identityKnown -and $postHash -eq $PreCommitHash)

    Write-Host ""
    Warn "the commit FAILED: $Err"
    Say  "MEASURED state of the destination after that failure:"
    Say  ("    exists ..................... {0}" -f $destExists)
    if ($destExists) {
        if ($destReadable) {
            Say  "    readable ................... yes"
            Say ("    parses as JSON ............. {0}" -f $destParses)
        } else {
            Say  "    readable ................... NO (access denied, or another process holds it)"
            Say  "    parses as JSON ............. NOT MEASURED (the file could not be read)"
        }
        if ($identityKnown) {
            Say ("    byte-identical to pre-commit {0}" -f $verifiedIdentical)
        } else {
            Say  "    byte-identical to pre-commit NOT DETERMINED (the file could not be hashed)"
        }
    }
    Say ("    ReplaceFile backup copy .... {0}" -f $(if ($keptCopy) { $ReplacedCopy } else { 'not created' }))
    Say ("    candidate still present .... {0}" -f $(if ($candLeft) { $Candidate } else { 'no' }))
    Say ("    timestamped backup ......... {0}" -f $(if ($bakExists) { $Backup } else { 'NOT PRESENT' }))
    Write-Host ""

    $restoreCmd = "Copy-Item -LiteralPath '" + $Backup + "' -Destination '" + $Destination + "'"

    if ($verifiedIdentical) {
        # BRANCH:unchanged
        # The ONLY branch permitted to say UNCHANGED, and it says it because a
        # hash comparison showed it, not because the code assumed it.
        if ($candLeft) { Remove-Item $Candidate    -Force -ErrorAction SilentlyContinue }
        if ($keptCopy) { Remove-Item $ReplacedCopy -Force -ErrorAction SilentlyContinue }
        Die ("settings.json is UNCHANGED -- verified byte-identical to the copy hashed " +
             "immediately before the commit. The candidate was discarded. " +
             $(if ($bakExists) { "Backup remains at $Backup." }
               else { "NOTE: the timestamped backup is NOT present at $Backup." }))
    }

    # Every other outcome is a partial or undetermined failure. Delete NOTHING:
    # the candidate and the ReplaceFile backup copy may be the only remaining
    # copies of anything.
    if (-not $destExists) {
        # BRANCH:absent
        Warn "settings.json NO LONGER EXISTS under its own name."
        if ($keptCopy) {
            Say ("recover with:  Move-Item -LiteralPath '" + $ReplacedCopy + "' -Destination '" + $Destination + "'")
        } elseif ($bakExists) {
            Say ("recover with:  " + $restoreCmd)
        } else {
            Warn "and NO recoverable copy of the pre-commit file is present."
            if ($candLeft) {
                Say ("the only file left holding the INTENDED new content is:  " + $Candidate)
            }
        }
        $verdict = ("the commit FAILED PARTWAY: settings.json is NOT in its pre-commit state -- " +
                    "it is not there at all. Nothing was deleted; recover using the paths listed above.")

    } elseif (-not $destReadable) {
        # BRANCH:unreadable
        # F1. This branch exists so that an ACCESS failure can never be
        # reported as a CONTENT failure. It deliberately recommends nothing
        # that writes to the destination: the file may be perfectly intact, and
        # a Copy-Item issued on the strength of an access error would destroy
        # it.
        Warn "settings.json EXISTS but could NOT BE READ, so its state is UNKNOWN."
        Say  "This is an ACCESS result, not a finding about the file's contents."
        Say  "Something holds it open exclusively, or denied access. It may be"
        Say  "completely intact; this run cannot tell you either way."
        Say  "DO NOT overwrite it on the strength of this message. Find out what"
        Say  "is holding it, re-check, and only then decide."
        if ($bakExists) {
            Say ("a pre-commit copy is kept for comparison at:  " + $Backup)
        } else {
            Warn "no timestamped backup is present either."
        }
        $verdict = ("the commit FAILED and the destination could NOT BE READ: whether settings.json " +
                    "is in its pre-commit state was NOT determined. Nothing was deleted, and nothing " +
                    "here should be overwritten until it can be read.")

    } elseif (-not $destParses) {
        # BRANCH:unparseable
        Warn "settings.json EXISTS, was READ, and does NOT parse as JSON."
        if ($bakExists) {
            Say ("recover with:  " + $restoreCmd)
        } else {
            Warn "and the timestamped backup is NOT present, so there is no copy to restore from."
            if ($candLeft) { Say ("the candidate still holds the INTENDED new content:  " + $Candidate) }
        }
        $verdict = ("the commit FAILED PARTWAY: settings.json is NOT in its pre-commit state. " +
                    "Nothing was deleted; recover using the paths listed above.")

    } elseif (-not $identityKnown) {
        # BRANCH:identity-unknown
        # Readable and valid, but a hash was unavailable on one side, so
        # "differs" is not a thing this run may say. Same rule as the
        # unreadable branch: report the gap, recommend no overwrite.
        Warn "settings.json EXISTS and parses, but whether it still matches its"
        Warn "pre-commit bytes was NOT DETERMINED -- one of the two hashes could not be taken."
        if ($bakExists) { Say ("compare against:  " + $Backup) }
        $verdict = ("the commit FAILED and the destination's identity was NOT determined: " +
                    "settings.json parses, but this run cannot say whether it changed. " +
                    "Nothing was deleted; compare against the paths listed above before overwriting anything.")

    } else {
        # BRANCH:altered
        Warn "settings.json EXISTS and parses, but is NOT byte-identical to its pre-commit state."
        if ($bakExists) {
            Say ("compare against:  " + $Backup)
            Say ("recover with:  " + $restoreCmd)
        } else {
            Warn "and the timestamped backup is NOT present, so there is nothing to compare it against."
        }
        $verdict = ("the commit FAILED PARTWAY: settings.json is NOT in its pre-commit state. " +
                    "Nothing was deleted; recover using the paths listed above.")
    }

    Write-Host ""
    Die $verdict
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

# ...AND ANY OTHER SHAPE IS REFUSED, NOT COERCED (F4).
#
# D3 above handles the two shapes that unambiguously mean "no rules": the key
# is absent, or it is null. It handled ONLY those, so three more inputs walked
# straight through the same door D3 was built to close -- measured against this
# script before this guard existed:
#
#   {"permissions":{"allow":{"a":1}}}            "1 rule(s)", exit 0, and it
#                                                COMMITTED an allow list whose
#                                                first element is a JSON object
#   {"permissions":{"allow":"Bash(x)"}}          silently rewrote a scalar into
#                                                a one-element list
#   {"permissions":{"allow":[null,"Bash(dir:*)"]}}
#                                                "2 rule(s)" for one rule, and
#                                                preserved the null
#
# REFUSE rather than normalise, for three reasons:
#
#   1. this file is a security file, and a coercion is a silent rewrite of
#      rules the operator did not ask this script to touch. That is the same
#      move as the pre-OPS-7 Move-Item quietly clearing a READ-ONLY attribute,
#      which this script already decided not to make on the user's behalf.
#   2. the meaning is genuinely unknown. "allow": {"a":1} is not a rule list
#      with a typo in it; nothing here can say what the operator meant, and
#      guessing produces a confident wrong answer -- the exact failure D3 named.
#   3. refusing is fail-closed and costs one hand edit, and the message can
#      name the offending element by index. Coercing costs a malformed
#      permissions file that Claude Code itself must then interpret.
#
# The consequence that matters: this script can no longer write a malformed
# allow list, because it stops before taking a backup or writing a candidate.
$allowValue = $settings.permissions.allow
if ($allowValue -isnot [System.Array]) {
    Die ("settings.json has a 'permissions.allow' that is not a JSON array (it is a " +
         $allowValue.GetType().Name + "). This script will not guess what you meant or " +
         "rewrite it for you. Make it an array of rule strings -- e.g. " +
         [char]34 + 'allow' + [char]34 + ': [] -- and re-run. Nothing was written.')
}
for ($i = 0; $i -lt $allowValue.Count; $i++) {
    if ($allowValue[$i] -isnot [string]) {
        $what = $(if ($null -eq $allowValue[$i]) { 'null' } else { $allowValue[$i].GetType().Name })
        Die ("settings.json has a 'permissions.allow' entry at index $i that is not a string " +
             "(it is $what). A permission rule is a string; this script will not count, " +
             "reorder or rewrite a non-string entry. Fix or remove that entry and re-run. " +
             "Nothing was written.")
    }
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
    # F6. This used to read "This removes only the entries THIS SCRIPT owns."
    # There is no provenance record anywhere -- revoke matches by STRING VALUE
    # against a fixed vocabulary -- so a rule a human typed by hand months ago
    # is removed exactly like one this script wrote, and the operator running a
    # security undo was reading that sentence as "only what I added". See
    # WHAT -REVOKE ACTUALLY MATCHES in .DESCRIPTION for why the fix is the
    # sentence and not a provenance file.
    Write-Host "  It removes these rule STRINGS from the allow list whether or not" -ForegroundColor Yellow
    Write-Host "  this script is what added them: matching is by exact rule text" -ForegroundColor Yellow
    Write-Host "  against the fixed set of rules this script can grant, and nothing" -ForegroundColor Yellow
    Write-Host "  anywhere records who added a rule. If you typed one of the lines" -ForegroundColor Yellow
    Write-Host "  listed above yourself, it goes too -- check the list before" -ForegroundColor Yellow
    Write-Host "  continuing." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  It does NOT revoke capability granted by other allow rules --" -ForegroundColor Yellow
    Write-Host "  notably Bash(docker compose:*), which already covers the deploy path." -ForegroundColor Yellow
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
