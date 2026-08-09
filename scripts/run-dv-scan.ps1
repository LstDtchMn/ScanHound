# Scheduled-task wrapper for the DV host detector.
#
# WHY A WRAPPER AND NOT A BARE `python dv_host_scan.py` ACTION.
#
# The detector's library roots include Y:, which is a MAPPED NETWORK DRIVE
# (\\TURTLELANDSRV2\4K HDR Geronimo, DriveType=4 -- verified 2026-08-09). Drive
# mappings are per-logon-session, so a scheduled task that runs without the
# user's interactive session sees no Y: at all. The detector would then walk
# zero roots, detect zero files, POST an empty import and EXIT 0 -- a silent
# no-op indistinguishable from "nothing new to scan".
#
# That is the exact shape that cost us the TV share on the NAS mount work: a
# script printed success and returned 0 while having quietly skipped its most
# important input. So this wrapper's real job is to make an unreachable root a
# LOUD, NONZERO failure that shows up in LastTaskResult, before the detector is
# allowed to draw any conclusion from an empty walk.
#
# It also writes a dated log, because the detector's own stdout otherwise goes
# nowhere under Task Scheduler and a stalled pipeline would again be invisible.
#
# Exit codes (distinct on purpose -- LastTaskResult is the only signal Task
# Scheduler preserves):
#   0  detector ran and succeeded
#   10 repo/script/config layout is wrong
#   11 a configured library root is unreachable (the mapped-drive case)
#   12 python not found
#   13 dovi_tool.exe not found
#   1  the detector itself failed; see the log

[CmdletBinding()]
param(
    [string]$RepoRoot = 'X:\Docker Apps\ScanHound',
    [string]$LogDir   = 'X:\Docker Apps\ScanHound\data\dv-scan-logs',
    [int]$KeepLogs    = 30
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} {1} {2}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Output $line
    if ($script:LogFile) { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding utf8 }
}

# --- log file first, so even an early failure is recorded -------------------

$script:LogFile = $null
try {
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    }
    $script:LogFile = Join-Path $LogDir ("dv-scan-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
} catch {
    Write-Output "WARNING: could not create log directory '$LogDir': $($_.Exception.Message)"
}

Write-Log "=== DV host scan starting (wrapper) ==="
Write-Log "session user: $env:USERNAME   interactive: $([Environment]::UserInteractive)"

# --- layout ----------------------------------------------------------------

$detector = Join-Path $RepoRoot 'scripts\host-detector\dv_host_scan.py'
$config   = Join-Path $RepoRoot 'data\dv_host.json'
$doviDir  = Join-Path $RepoRoot 'scripts\host-detector'
$doviExe  = Join-Path $doviDir  'dovi_tool.exe'

foreach ($required in @($detector, $config)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        Write-Log "Required file missing: $required" 'ERROR'
        exit 10
    }
}
if (-not (Test-Path -LiteralPath $doviExe -PathType Leaf)) {
    Write-Log "dovi_tool.exe not found at $doviExe -- every detection would fail." 'ERROR'
    exit 13
}

# --- python ----------------------------------------------------------------
#
# Pinned candidates before PATH. A scheduled task inherits a PATH this script
# does not control, and picking up some other interpreter would fail in
# confusing ways (the detector imports backend.rename.dv_detect).
$python = @(
    'C:\Users\NLSur\AppData\Local\Programs\Python\Python312\python.exe',
    'C:\Program Files\Python312\python.exe'
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1

if (-not $python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source }
}
if (-not $python) {
    Write-Log "python.exe not found at any pinned location or on PATH." 'ERROR'
    exit 12
}
Write-Log "python: $python"

# --- THE LOAD-BEARING CHECK: are the library roots actually reachable? ------
#
# Read the roots from the SAME file the detector reads, so this can never drift
# from what is about to be scanned. Semicolon-separated, forward slashes.
try {
    $cfg = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
} catch {
    Write-Log "Could not parse $config : $($_.Exception.Message)" 'ERROR'
    exit 10
}

# PowerShell 5.1 ConvertFrom-Json yields a PSCustomObject; a missing property
# under StrictMode throws, so test for it explicitly rather than assuming.
$rootsRaw = $null
if ($cfg.PSObject.Properties.Name -contains 'dv_library_roots') {
    $rootsRaw = $cfg.dv_library_roots
}
if ([string]::IsNullOrWhiteSpace($rootsRaw)) {
    Write-Log "dv_library_roots is empty in $config -- nothing to scan." 'ERROR'
    exit 10
}

$roots = $rootsRaw -split ';' | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() }
Write-Log "configured roots: $($roots.Count)"

$unreachable = @()
foreach ($r in $roots) {
    # The config uses forward slashes for readability; Windows APIs want
    # backslashes. Test both the UNC and drive-letter forms as written.
    $win = $r -replace '/', '\'
    if (Test-Path -LiteralPath $win) {
        $n = @(Get-ChildItem -LiteralPath $win -Recurse -File -Include *.mkv,*.mp4 `
                             -ErrorAction SilentlyContinue).Count
        Write-Log ("  OK          {0}  ({1} video files)" -f $r, $n)
    } else {
        Write-Log ("  UNREACHABLE {0}" -f $r) 'ERROR'
        $unreachable += $r
    }
}

if ($unreachable.Count -gt 0) {
    Write-Log "" 'ERROR'
    Write-Log "$($unreachable.Count) of $($roots.Count) library root(s) unreachable. ABORTING." 'ERROR'
    Write-Log "Refusing to run the detector: it would walk the reachable roots only," 'ERROR'
    Write-Log "detect nothing for the rest, and exit 0 -- reporting a clean scan while" 'ERROR'
    Write-Log "silently skipping libraries. A drive-letter root (Y:) is a per-session" 'ERROR'
    Write-Log "mapping, so the usual cause is that this task ran without the user's" 'ERROR'
    Write-Log "interactive session, or the NAS is down." 'ERROR'
    exit 11
}

# --- run the detector ------------------------------------------------------
#
# From the repo root, because the detector's --config/--db defaults are
# repo-relative, and with dovi_tool's directory FIRST on PATH (its docstring
# requires it to be resolvable).
Push-Location -LiteralPath $RepoRoot
$savedPath = $env:PATH
try {
    $env:PATH = "$doviDir;$env:PATH"
    Write-Log "running: $python scripts\host-detector\dv_host_scan.py"

    # Native stderr is NOT redirected into the PowerShell pipeline here: in 5.1
    # that wraps each line in an ErrorRecord and flips $? to false even on a
    # clean exit 0, which would make every successful scan look like a failure.
    & $python 'scripts\host-detector\dv_host_scan.py' 2>&1 | ForEach-Object {
        $text = "$_"
        Write-Output $text
        if ($script:LogFile) { Add-Content -LiteralPath $script:LogFile -Value $text -Encoding utf8 }
    }
    $code = $LASTEXITCODE
} finally {
    $env:PATH = $savedPath
    Pop-Location
}

if ($code -ne 0) {
    Write-Log "detector exited $code" 'ERROR'
    exit 1
}

Write-Log "=== DV host scan finished OK ==="

# --- prune old logs --------------------------------------------------------
try {
    Get-ChildItem -LiteralPath $LogDir -Filter 'dv-scan-*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $KeepLogs |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { }

exit 0
