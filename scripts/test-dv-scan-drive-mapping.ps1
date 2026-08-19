# Prove the wrapper's network-drive step fails CLOSED.
#
# WHY THIS EXISTS. The wrapper now establishes Y: itself, ported from the minimal
# script that existed only because it could not. The dangerous case is not "the
# drive is missing" -- it is "the drive is present and points somewhere ELSE".
# dv_host.db keys rows on the raw path string, so scanning a different share
# under that letter records the wrong DV layer for every file walked, under an
# identity that already means something. So a wrong mapping must ABORT and must
# never be silently repaired by remapping.
#
# The other two suites pass -MapDrive "" and therefore skip this code entirely.
# Without this file the mapping step would ship with no coverage at all.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-dv-scan-drive-mapping.ps1

param([string]$Wrapper)

$ErrorActionPreference = 'Continue'
Set-StrictMode -Version 2.0

$wrapper = if ($Wrapper) { $Wrapper } else { Join-Path $PSScriptRoot 'run-dv-scan.ps1' }
$root    = Join-Path $env:TEMP 'dv-scan-map-test'
$script:Failures = 0

function Assert-That {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Output "    [PASS] $Name" }
    else { $script:Failures++; Write-Output "    [FAIL] $Name"; if ($Detail) { Write-Output "           $Detail" } }
}

function New-Fixture {
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
    $det = Join-Path $root 'scripts\host-detector'
    $dat = Join-Path $root 'data'
    New-Item -ItemType Directory -Force -Path $det, $dat, (Join-Path $root 'logs') | Out-Null
    Set-Content -LiteralPath (Join-Path $det 'dovi_tool.exe') -Value 'stub' -Encoding ascii
    $lib = Join-Path $root 'lib0'
    New-Item -ItemType Directory -Force -Path $lib | Out-Null
    Set-Content -LiteralPath (Join-Path $lib 'movie0.mkv') -Value 'x' -Encoding ascii
    @{ dv_library_roots = ($lib -replace '\\', '/'); dv_detection = $true } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dat 'dv_host.json') -Encoding ascii
    # A stub detector, so a case that DOES reach the scan exits promptly.
    Set-Content -LiteralPath (Join-Path $det 'dv_host_scan.py') -Encoding utf8 -Value @'
import logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("stub").info("stub detector ran")
'@
    $key = Join-Path $root 'key.secret'
    Set-Content -LiteralPath $key -Value 'dummy-key' -Encoding ascii
    return $key
}

function Invoke-Wrapper {
    param([string]$KeyFile, [string]$MapDrive, [string]$MapTarget)
    & $wrapper -RepoRoot $root -LogDir (Join-Path $root 'logs') -HeartbeatMinutes 0.05 `
               -IngestKeyFile $KeyFile -MapDrive $MapDrive -MapTarget $MapTarget *> $null
    return $LASTEXITCODE
}

function Get-FreeDriveLetter {
    # Every letter, not a hand-picked shortlist. The first version tried six and
    # skipped this case entirely on a machine that happened to use all six --
    # a test that silently opts out because of local drive assignments is not
    # coverage, it is a coin flip on whose machine it runs.
    # MATERIALISE the mappings before projecting a property off them. Under
    # StrictMode, `(Get-SmbMapping ...).LocalPath` on an EMPTY result is a
    # PropertyNotFoundStrict error -- which is exactly the state of a CI runner
    # with no mapped drives. The suite runs with EAP='Continue', so it printed
    # that error, carried on, and still reported success: a green run containing
    # an uncategorised PowerShell error, which is worse than a red one because
    # nobody reads it. Caught in the exact-head CI log by peer review.
    $maps = @(Get-SmbMapping -ErrorAction SilentlyContinue)
    $used = @(Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Name) +
            @($maps | ForEach-Object { $_.LocalPath -replace ':', '' })
    foreach ($c in [char[]]'NOSTUVWXYZQRIJKLM') {
        if ($used -notcontains "$c") { return "$c`:" }
    }
    return $null
}

Write-Output "wrapper under test: $wrapper"
Write-Output ''

# --- case 1: the opt-out actually opts out ----------------------------------
Write-Output 'case 1: -MapDrive "" skips the mapping step entirely'
$key = New-Fixture
$rc = Invoke-Wrapper -KeyFile $key -MapDrive '' -MapTarget '\\no-such-host-zzz\share'
Assert-That 'runs to completion despite an unreachable MapTarget' ($rc -eq 0) "got $rc"
Assert-That 'no mapping line in the log' `
    (-not ((Get-ChildItem (Join-Path $root 'logs') -Filter '*.log' | ForEach-Object { Get-Content $_.FullName -Raw }) -join '' -match 'verified ->'))

# --- case 2: a target that cannot be established aborts, and does NOT scan ---
Write-Output ''
Write-Output 'case 2: an unestablishable target aborts with 16 before scanning'
$free = Get-FreeDriveLetter
if (-not $free) {
    Write-Output '    [SKIP] no free drive letter available on this machine'
} else {
    $key = New-Fixture
    $rc = Invoke-Wrapper -KeyFile $key -MapDrive $free -MapTarget '\\no-such-host-zzz\share'
    Assert-That 'exits 16 (its own code, not a generic failure)' ($rc -eq 16) "got $rc"
    $log = (Get-ChildItem (Join-Path $root 'logs') -Filter '*.log' -ErrorAction SilentlyContinue |
            ForEach-Object { Get-Content $_.FullName -Raw }) -join ''
    Assert-That 'the detector was NOT run' (-not ($log -match 'stub detector ran')) `
        'a drive it could not establish must stop the run before any scanning'
}

# --- case 3: a drive pointing ELSEWHERE aborts and is never remapped ---------
#
# THE DANGEROUS CASE, and the one this suite exists for. It needs a drive letter
# that is genuinely mapped to a known share, so it is SKIPPED where none exists
# -- notably on CI runners. That is a real coverage gap and is stated rather than
# papered over: on a machine with the production Y: mapping this case runs and is
# meaningful; on a clean runner it does not.
Write-Output ''
Write-Output 'case 3: a drive mapped elsewhere aborts with 15 and is not remapped'
$mapped = Get-SmbMapping -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $mapped) {
    Write-Output '    [SKIP] no mapped network drive on this machine (expected on CI)'
} else {
    $key    = New-Fixture
    $letter = $mapped.LocalPath
    $before = $mapped.RemotePath
    $rc = Invoke-Wrapper -KeyFile $key -MapDrive $letter -MapTarget '\\definitely-not-this\share'
    Assert-That "exits 15 for $letter mapped elsewhere" ($rc -eq 15) "got $rc"
    $after = (Get-SmbMapping -LocalPath $letter -ErrorAction SilentlyContinue).RemotePath
    Assert-That 'the existing mapping was left untouched' ($after -eq $before) `
        "was '$before', now '$after' -- the wrapper must never remap"
    $log = (Get-ChildItem (Join-Path $root 'logs') -Filter '*.log' -ErrorAction SilentlyContinue |
            ForEach-Object { Get-Content $_.FullName -Raw }) -join ''
    Assert-That 'the detector was NOT run' (-not ($log -match 'stub detector ran'))
}

Write-Output ''
if ($script:Failures -gt 0) { Write-Output "FAILURES: $script:Failures"; exit 1 }
Write-Output 'all cases passed'
exit 0
