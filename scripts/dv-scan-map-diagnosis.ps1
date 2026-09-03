# Classifies why run-dv-scan.ps1's mapped-drive step could not confirm
# $MapDrive -> $MapTarget, AFTER its establishment attempt has already run.
# Contains only the diagnosis -- it never touches the mapping itself.
#
# THE BUG THIS FIXES (DV-1, review of 2026-09-02, reproduced live). The
# wrapper's abort message read:
#   "ABORT: could not establish Y: -> '\\TURTLELANDSRV2\4K HDR Geronimo'
#    (got '\\TURTLELANDSRV2\4K HDR Geronimo')."
# with the reported and expected paths byte-identical, because the ONLY
# failing check was Test-Path -LiteralPath 'Y:\' -- the '-ine' name check was
# false. The message accuses a mapping mismatch that never happened; the real
# cause was the mapped share not answering (the NAS was off). This function
# separates the three outcomes that can reach that abort so the log names the
# one that actually occurred, instead of always printing the mismatch wording:
#
#   NotMapped            - no mapping exists at $MapDrive at all
#   MappedElsewhere       - mapped, but to a different share than requested
#   MappedButUnreachable  - mapped to the right share, but it isn't answering
#
# Exit code 16 is unchanged for all three; only the diagnosis text changes.

function Get-DvMapDriveAbortDiagnosis {
    param(
        [Parameter(Mandatory)] [bool]   $TestPathOk,
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $CurN,   # normalised current mapping, '' if none
        [Parameter(Mandatory)] [string] $WantN,                        # normalised expected target
        [AllowNull()] [string] $Cur,                                   # raw (un-normalised) current mapping, for the log
        [Parameter(Mandatory)] [string] $MapDrive,
        [Parameter(Mandatory)] [string] $MapTarget
    )

    if (-not $CurN) {
        return [pscustomobject]@{
            Outcome = 'NotMapped'
            Message = "ABORT: $MapDrive could not be mapped to '$MapTarget' -- no mapping was established (New-SmbMapping and net use both failed)."
        }
    }
    if ($CurN -ine $WantN) {
        return [pscustomobject]@{
            Outcome = 'MappedElsewhere'
            Message = "ABORT: $MapDrive ended up mapped to '$Cur', not '$MapTarget' -- refusing to scan under a mismatched identity."
        }
    }
    # $CurN -ieq $WantN here, so the only remaining failure is $TestPathOk
    # being false: the name matches but the share did not answer.
    return [pscustomobject]@{
        Outcome = 'MappedButUnreachable'
        Message = "ABORT: $MapDrive is mapped to '$Cur' as expected, but the share is not responding (Test-Path failed) -- the NAS or network path is likely down, this is NOT a mapping mismatch."
    }
}
