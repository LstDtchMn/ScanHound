# Prove run-dv-scan.ps1 hands the DV ingest key to the detector, and refuses to
# scan at all when it cannot.
#
# WHY THIS EXISTS. The wrapper never set SCANHOUND_DV_INGEST_KEY. dv_host_scan.py
# reads exactly that variable and sends it as X-DV-Ingest-Key; the server rejects
# anything else with 401. So every run walked all four roots correctly, scanned
# for an hour, and had every upload refused -- 670 rows accumulated in dv_host.db
# that the container never received, behind a generic LastTaskResult of 1 that
# looked like any ordinary detector failure.
#
# THE ASSERTION THAT MATTERS is case 3's: the detector must actually RECEIVE the
# key. Asserting only "the wrapper exits 0" would have passed throughout the
# entire outage. The stub records the value it was given, so the test observes
# the credential arriving at the process that needs it rather than inferring it
# from the wrapper's own exit code.
#
# The second assertion that matters is case 1's: with no key, the detector must
# NOT be invoked at all. Scanning first and discovering the 401 an hour later is
# the failure mode this whole wrapper exists to prevent -- work that cannot land,
# reported like work that did.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-dv-scan-ingest-key.ps1
#
# -Wrapper exists so the NEGATIVE CONTROL stays reproducible: point it at the
# pre-fix wrapper (git show origin/main:scripts/run-dv-scan.ps1) and case 3 MUST
# fail. A test that has never been shown to fail is not evidence.

param([string]$Wrapper)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$wrapper = if ($Wrapper) { $Wrapper } else { Join-Path $PSScriptRoot 'run-dv-scan.ps1' }
$root    = Join-Path $env:TEMP 'dv-scan-key-test'
$script:Failures = 0

$py = @(
    'C:\Users\NLSur\AppData\Local\Programs\Python\Python312\python.exe',
    'C:\Program Files\Python312\python.exe'
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Output 'FATAL: no python found; this suite needs the real interpreter.'; exit 1 }

function Assert-That {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Output "    [PASS] $Name" }
    else {
        $script:Failures++
        Write-Output "    [FAIL] $Name"
        if ($Detail) { Write-Output "           $Detail" }
    }
}

function New-Fixture {
    # A throwaway repo layout the wrapper accepts: stub detector, a dovi_tool it
    # only Test-Paths, config, and one real reachable root.
    if (Test-Path -LiteralPath $root) {
        Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    }
    $det = Join-Path $root 'scripts\host-detector'
    $dat = Join-Path $root 'data'
    New-Item -ItemType Directory -Force -Path $det, $dat, (Join-Path $root 'logs') | Out-Null
    Set-Content -LiteralPath (Join-Path $det 'dovi_tool.exe') -Value 'stub' -Encoding ascii

    $lib = Join-Path $root 'lib0'
    New-Item -ItemType Directory -Force -Path $lib | Out-Null
    Set-Content -LiteralPath (Join-Path $lib 'movie0.mkv') -Value 'x' -Encoding ascii
    @{ dv_library_roots = ($lib -replace '\\', '/'); dv_detection = $true } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dat 'dv_host.json') -Encoding ascii

    # The stub records the credential it was handed. This is the observation the
    # whole suite turns on: the key reaching the DETECTOR, not the wrapper
    # believing it sent one.
    $stub = @'
import logging, os
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
marker = os.environ.get("DV_KEY_MARKER")
if marker:
    with open(marker, "w") as fh:
        fh.write(os.environ.get("SCANHOUND_DV_INGEST_KEY", "<ABSENT>"))
logging.getLogger("stub").info("stub detector ran")
'@
    Set-Content -LiteralPath (Join-Path $det 'dv_host_scan.py') -Value $stub -Encoding utf8
}

function Invoke-Wrapper {
    param([string]$KeyFile, [string]$Marker, [string]$EnvKey)
    $logDir = Join-Path $root 'logs'
    $prev = $env:SCANHOUND_DV_INGEST_KEY
    $env:DV_KEY_MARKER = $Marker
    if ($null -ne $EnvKey) { $env:SCANHOUND_DV_INGEST_KEY = $EnvKey }
    else { Remove-Item -Path 'env:SCANHOUND_DV_INGEST_KEY' -ErrorAction SilentlyContinue }
    try {
        # -IngestKeyFile only when the wrapper under test declares it. The
        # NEGATIVE CONTROL runs this same suite against the PRE-FIX wrapper,
        # which has no such parameter -- passing it there aborts on a binding
        # error, and a control that cannot execute proves nothing. Omitting it
        # lets the old wrapper run its real behaviour, which is what must fail.
        $supportsKeyFile = (Get-Command $wrapper).Parameters.ContainsKey('IngestKeyFile')
        if ($supportsKeyFile) {
            & $wrapper -RepoRoot $root -LogDir $logDir -HeartbeatMinutes 0.05 `
                       -IngestKeyFile $KeyFile *> $null
        } else {
            & $wrapper -RepoRoot $root -LogDir $logDir -HeartbeatMinutes 0.05 *> $null
        }
        return $LASTEXITCODE
    } finally {
        if ($null -ne $prev) { $env:SCANHOUND_DV_INGEST_KEY = $prev }
        else { Remove-Item -Path 'env:SCANHOUND_DV_INGEST_KEY' -ErrorAction SilentlyContinue }
        Remove-Item -Path 'env:DV_KEY_MARKER' -ErrorAction SilentlyContinue
    }
}

Write-Output "wrapper under test: $wrapper"
Write-Output ''

# --- case 1: no key at all -------------------------------------------------
Write-Output 'case 1: no key file and no environment key'
New-Fixture
$marker  = Join-Path $root 'key-seen.txt'
$missing = Join-Path $root 'does-not-exist.secret'
$rc = Invoke-Wrapper -KeyFile $missing -Marker $marker -EnvKey $null
Assert-That 'exits 14 (its own code, not a generic 1)' ($rc -eq 14) "got $rc"
Assert-That 'the detector was NOT invoked' (-not (Test-Path -LiteralPath $marker)) `
    'the marker exists, so an hour of unusable scanning would have happened first'

# --- case 2: present but empty ---------------------------------------------
Write-Output ''
Write-Output 'case 2: key file exists but is empty/whitespace'
New-Fixture
$marker  = Join-Path $root 'key-seen.txt'
$empty   = Join-Path $root 'empty.secret'
Set-Content -LiteralPath $empty -Value "   `r`n" -Encoding ascii
$rc = Invoke-Wrapper -KeyFile $empty -Marker $marker -EnvKey $null
Assert-That 'exits 14' ($rc -eq 14) "got $rc"
Assert-That 'the detector was NOT invoked' (-not (Test-Path -LiteralPath $marker))

# --- case 3: THE ONE THAT MATTERS ------------------------------------------
Write-Output ''
Write-Output 'case 3: key file present -> the DETECTOR receives it'
New-Fixture
$marker = Join-Path $root 'key-seen.txt'
$keyF   = Join-Path $root 'good.secret'
# Trailing newline on purpose: the file on the server has one, and a key sent
# with it attached is rejected exactly like a missing one.
Set-Content -LiteralPath $keyF -Value "s3cr3t-from-file`r`n" -Encoding ascii
$rc = Invoke-Wrapper -KeyFile $keyF -Marker $marker -EnvKey $null
$seen = if (Test-Path -LiteralPath $marker) { (Get-Content -LiteralPath $marker -Raw) } else { '<no marker>' }
Assert-That 'the wrapper succeeded' ($rc -eq 0) "got $rc"
Assert-That 'the detector received the key' ($seen -eq 's3cr3t-from-file') "detector saw: '$seen'"

# --- case 4: environment wins ----------------------------------------------
Write-Output ''
Write-Output 'case 4: an environment key overrides the file'
New-Fixture
$marker = Join-Path $root 'key-seen.txt'
$keyF   = Join-Path $root 'good.secret'
Set-Content -LiteralPath $keyF -Value 'from-file' -Encoding ascii
$rc = Invoke-Wrapper -KeyFile $keyF -Marker $marker -EnvKey 'from-environment'
$seen = if (Test-Path -LiteralPath $marker) { (Get-Content -LiteralPath $marker -Raw) } else { '<no marker>' }
Assert-That 'the wrapper succeeded' ($rc -eq 0) "got $rc"
Assert-That 'the detector received the ENVIRONMENT key' ($seen -eq 'from-environment') "detector saw: '$seen'"

# --- case 5: the secret is never written to the log ------------------------
Write-Output ''
Write-Output 'case 5: the key never appears in the log'
$logs = Get-ChildItem -LiteralPath (Join-Path $root 'logs') -Filter '*.log' -ErrorAction SilentlyContinue
$logText = ($logs | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw }) -join "`n"
Assert-That 'no key value in any log line' `
    (($logText -notmatch 'from-environment') -and ($logText -notmatch 'from-file')) `
    'a credential reached the log file'

Write-Output ''
if ($script:Failures -gt 0) { Write-Output "FAILURES: $script:Failures"; exit 1 }
Write-Output 'all cases passed'
exit 0
