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
        as a sibling of it, in the same directory. If Replace throws anyway --
        a locked file, an unusual filesystem -- the live file is left UNTOUCHED,
        the candidate is discarded, and the script fails closed.

    So: this is safe against script or process failure, and safe against
    committing invalid bytes. It is NOT proven durable against host, VM or
    power loss. Do not describe it as atomic.

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

$existing  = @($settings.permissions.allow)
$otherKeys = @($settings.PSObject.Properties.Name | Where-Object { $_ -ne 'permissions' })
Say ("current allow list: {0} rule(s)" -f $existing.Count)

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

if ($WhatIf) { Write-Host ""; Warn "-WhatIf: nothing written"; exit 0 }

# ------------------------------------------------- prepare/validate/commit --
$backup = "$SettingsPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item $SettingsPath $backup
Good "backup written to $backup"

$settings.permissions.allow = $wanted
try {
    $candidate = Write-CandidateAndValidate -FinalPath $SettingsPath -Object $settings `
                    -MustContain $adding -MustNotContain $removing -OriginalOtherKeys $otherKeys
} catch {
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
# The third argument is a real null, via [NullString]::Value: PowerShell
# converts a bare $null to "" when binding to [string], and ReplaceFile then
# rejects "" with "The path is not of a legal form." We already took the
# timestamped .bak- copy above, so a second backup would only be litter.
# If Replace throws, the live file is untouched -- discard the candidate, stop.
try {
    [System.IO.File]::Replace($candidate, $SettingsPath, [NullString]::Value)
} catch {
    Remove-Item $candidate -Force -ErrorAction SilentlyContinue
    Die "the commit FAILED; settings.json is UNCHANGED and the candidate was discarded. Backup remains at $backup. $_"
}

# ----------------------------------------------------------- verify live ---
if (-not (Test-NoBom $SettingsPath)) { Die "the live file has a BOM after commit. Restore from $backup" }
try { $final = [System.IO.File]::ReadAllText($SettingsPath) | ConvertFrom-Json }
catch { Die "the live file does not parse after commit. Restore from $backup" }

$live = @($final.permissions.allow)
foreach ($r in $adding)   { if ($live -notcontains $r) { Die "live file is missing $r. Restore from $backup" } }
foreach ($r in $removing) { if ($live -contains $r)    { Die "live file still contains $r. Restore from $backup" } }
foreach ($k in $otherKeys){ if (-not ($final.PSObject.Properties.Name -contains $k)) { Die "live file lost '$k'. Restore from $backup" } }
Good ("live file verified: {0} rule(s), all other settings intact" -f $live.Count)

Write-Host ""
Say "The FILE on disk has changed. A running Claude Code process has NOT"
Say "reloaded it -- restart Claude Code for this to take effect."

# Explicit, so "success means exit 0" is a contract the tests can assert rather
# than an accident of whatever the last statement happened to leave behind
# (SR2-3).
exit 0
