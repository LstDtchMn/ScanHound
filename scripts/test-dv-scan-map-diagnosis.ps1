# Unit tests for scripts\dv-scan-map-diagnosis.ps1 (DV-1 fix).
#
# WHY A SEPARATE UNIT TEST FILE. scripts\test-dv-scan-drive-mapping.ps1 drives
# the wrapper end-to-end and already covers "not mapped" (its case 2) and
# "mapped elsewhere" (its case 3, real-network dependent and SKIPped on CI).
# It cannot cover "mapped to the RIGHT share but not answering" -- that state
# can only be produced by a real share that stops responding after the
# mapping is already up (e.g. the NAS going offline mid-session), which no
# test fixture on this host can fake. So this file tests the diagnosis
# function directly with synthetic inputs, which is deterministic and covers
# all three outcomes, including the exact live bug report (DV-1): $curN and
# $wantN byte-identical, only Test-Path false.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-dv-scan-map-diagnosis.ps1

param([string]$HelperPath)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$helper = if ($HelperPath) { $HelperPath } else { Join-Path $PSScriptRoot 'dv-scan-map-diagnosis.ps1' }
. $helper

$script:Failures = 0
function Assert-That {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Output "    [PASS] $Name" }
    else { $script:Failures++; Write-Output "    [FAIL] $Name"; if ($Detail) { Write-Output "           $Detail" } }
}

# --- the exact live defect: byte-identical strings, only Test-Path failed ---
Write-Output 'case 1: mapped to the right share but not answering (DV-1 reproduction)'
$target = '\\TURTLELANDSRV2\4K HDR Geronimo'
$d = Get-DvMapDriveAbortDiagnosis -TestPathOk $false -CurN $target -WantN $target `
                                   -Cur $target -MapDrive 'Y:' -MapTarget $target
Assert-That 'outcome is MappedButUnreachable' ($d.Outcome -eq 'MappedButUnreachable') "got $($d.Outcome)"
Assert-That 'message does NOT claim a mismatch (the old, wrong wording)' `
    ($d.Message -notmatch "expected '.*', got '.*'" -and $d.Message -notmatch 'could not establish .* -> ') `
    $d.Message
Assert-That 'message says the share is not answering' ($d.Message -match 'not responding') $d.Message
Assert-That 'message explicitly denies a mismatch' ($d.Message -match 'NOT a mapping mismatch') $d.Message

# --- not mapped at all -------------------------------------------------------
Write-Output ''
Write-Output 'case 2: never got mapped'
$d = Get-DvMapDriveAbortDiagnosis -TestPathOk $false -CurN '' -WantN $target `
                                   -Cur $null -MapDrive 'Y:' -MapTarget $target
Assert-That 'outcome is NotMapped' ($d.Outcome -eq 'NotMapped') "got $($d.Outcome)"
Assert-That 'message says no mapping was established' ($d.Message -match 'no mapping was established') $d.Message

# --- mapped, but to a genuinely different share ------------------------------
Write-Output ''
Write-Output 'case 3: mapped to a different share than requested'
$other = '\\otherhost\othershare'
$d = Get-DvMapDriveAbortDiagnosis -TestPathOk $true -CurN $other -WantN $target `
                                   -Cur $other -MapDrive 'Y:' -MapTarget $target
Assert-That 'outcome is MappedElsewhere' ($d.Outcome -eq 'MappedElsewhere') "got $($d.Outcome)"
Assert-That 'message names the actual and expected targets' `
    ($d.Message -match [regex]::Escape($other) -and $d.Message -match [regex]::Escape($target)) $d.Message

# --- case-insensitive match still counts as MappedButUnreachable, not a mismatch
Write-Output ''
Write-Output 'case 4: differs only by case -- still the right share (-ine), so unreachable not mismatch'
$d = Get-DvMapDriveAbortDiagnosis -TestPathOk $false -CurN $target.ToUpper() -WantN $target `
                                   -Cur $target.ToUpper() -MapDrive 'Y:' -MapTarget $target
Assert-That 'outcome is MappedButUnreachable, not MappedElsewhere' ($d.Outcome -eq 'MappedButUnreachable') "got $($d.Outcome)"

Write-Output ''
if ($script:Failures -gt 0) { Write-Output "FAILURES: $script:Failures"; exit 1 }
Write-Output 'all cases passed'
exit 0
