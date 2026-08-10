# Prove run-dv-scan.ps1 streams the detector's output to its log WHILE the scan
# runs, and that making it do so broke none of the exit-code contract.
#
# WHY THIS EXISTS. The wrapper used to fold the detector's output in only after
# the process exited, so the 2026-08-09 five-hour run logged nothing but its own
# preflight lines and was indistinguishable from a hang. Reasoning from that
# absence produced a throughput figure 12x too low and a review that had to be
# retracted.
#
# THE ASSERTION THAT MATTERS is case 1's mid-run check: detector lines must be
# in the log while the wrapper is STILL RUNNING. A `Start-Process -ArgumentList
# '/c', $inner` attempt passed every end-of-run check and failed exactly here --
# it reported success in 14 s having captured nothing, because the redirection
# did not survive the argument array and the child exited immediately. An
# end-state-only test cannot tell that apart from a working stream.
#
# The stub is real python, not a .bat, because python's buffering is part of the
# behaviour under test: logging->stderr is line-buffered and streams, but stdout
# redirected to a file is block-buffered and would arrive only at exit.
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-dv-scan-streaming.ps1

#
# -Wrapper exists so the NEGATIVE CONTROL stays reproducible: point it at the
# pre-streaming wrapper and case 1's mid-run assertion must FAIL. A test that
# has never been shown to fail is not evidence.

param([string]$Wrapper)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$wrapper = if ($Wrapper) { $Wrapper } else { Join-Path $PSScriptRoot 'run-dv-scan.ps1' }
$root    = Join-Path $env:TEMP 'dv-scan-stream-test'
$script:Failures = 0

# --- helpers ---------------------------------------------------------------

function Read-LogSafe {
    # The wrapper is appending to this file as we read it, so open with a share
    # mode that tolerates the writer instead of racing Get-Content's default.
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        $fs = New-Object System.IO.FileStream(
                $Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
                ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        $sr = New-Object System.IO.StreamReader($fs)
        $text = $sr.ReadToEnd()
        $sr.Dispose()
        return $text
    } catch { return '' }
}

function Assert-That {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) {
        Write-Output "    [PASS] $Name"
    } else {
        $script:Failures++
        Write-Output "    [FAIL] $Name"
        if ($Detail) { Write-Output "           $Detail" }
    }
}

function New-Fixture {
    # A throwaway repo layout the wrapper will accept: detector, dovi_tool and
    # config where its layout checks expect them, plus real reachable roots.
    param([int]$RootCount = 2, [switch]$WithBadRoot)

    if (Test-Path -LiteralPath $root) {
        Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    }
    $det = Join-Path $root 'scripts\host-detector'
    $dat = Join-Path $root 'data'
    $log = Join-Path $root 'logs'
    foreach ($d in @($det, $dat, $log)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

    # dovi_tool only has to exist -- the wrapper Test-Paths it and the stub
    # detector never calls it.
    Set-Content -LiteralPath (Join-Path $det 'dovi_tool.exe') -Value 'stub' -Encoding ascii

    $roots = @()
    for ($i = 0; $i -lt $RootCount; $i++) {
        $r = Join-Path $root ("lib{0}" -f $i)
        New-Item -ItemType Directory -Force -Path $r | Out-Null
        Set-Content -LiteralPath (Join-Path $r "movie$i.mkv") -Value 'x' -Encoding ascii
        $roots += ($r -replace '\\', '/')
    }
    if ($WithBadRoot) { $roots += 'Q:/definitely-not-mounted' }

    # The wrapper reads dv_library_roots as a semicolon-separated string with
    # forward slashes, exactly as production writes it.
    $cfg = @{ dv_library_roots = ($roots -join ';'); dv_detection = $true }
    $cfg | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dat 'dv_host.json') -Encoding ascii

    # The stub detector. Emits on a cadence so a mid-run observation is possible,
    # records its working directory so we can prove the wrapper still launches it
    # from the repo root, and touches a marker so "never invoked" is checkable.
    $stub = @'
import logging, os, sys, time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stub")

marker = os.environ.get("DV_STUB_MARKER")
if marker:
    with open(marker, "w") as fh:
        fh.write(os.getcwd())

count = int(os.environ.get("DV_STUB_LINES", "8"))
delay = float(os.environ.get("DV_STUB_DELAY", "3"))
rc    = int(os.environ.get("DV_STUB_RC", "0"))

for i in range(count):
    log.info("stub progress line %d", i)
    time.sleep(delay)

sys.stderr.write("stub stderr trailer\n")
if os.environ.get("DV_STUB_NONL"):
    # No trailing newline: exercises the -Final flush of a partial last line.
    sys.stdout.write("stub unterminated tail")
else:
    print("stub stdout trailer")
sys.exit(rc)
'@
    Set-Content -LiteralPath (Join-Path $det 'dv_host_scan.py') -Value $stub -Encoding ascii
    return $log
}

function Start-Wrapper {
    # Launch the wrapper the way Task Scheduler does -- as its own powershell
    # process -- so the exit code we read is the same LastTaskResult would show.
    param([string]$LogDir, [double]$Heartbeat = 5)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName        = (Get-Command powershell.exe).Source
    $psi.Arguments       = ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -RepoRoot "{1}" -LogDir "{2}" -HeartbeatMinutes {3}' `
                            -f $wrapper, $root, $LogDir, $Heartbeat)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow  = $true
    return [System.Diagnostics.Process]::Start($psi)
}

function Wait-ForLog {
    param([string]$LogDir, [int]$TimeoutSec = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $f = @(Get-ChildItem -LiteralPath $LogDir -Filter 'dv-scan-*.log' -ErrorAction SilentlyContinue |
               Sort-Object LastWriteTime -Descending)
        if ($f.Count -gt 0) { return $f[0].FullName }
        Start-Sleep -Milliseconds 200
    }
    return $null
}

function Count-Occurrences {
    param([string]$Text, [string]$Pattern)
    return ([regex]::Matches($Text, $Pattern)).Count
}

# ===========================================================================
Write-Output ''
Write-Output '=== 1. streaming: detector lines reach the log DURING the run ==='

$logDir = New-Fixture
$env:DV_STUB_LINES  = '8'
$env:DV_STUB_DELAY  = '3'      # 8 x 3 s = ~24 s of running
$env:DV_STUB_RC     = '0'
$env:DV_STUB_NONL   = ''
$env:DV_STUB_MARKER = Join-Path $root 'invoked.txt'

$proc = Start-Wrapper -LogDir $logDir -Heartbeat 0.15   # ~9 s between heartbeats
$logFile = Wait-ForLog -LogDir $logDir

# THE MID-RUN ASSERTION. Poll only while the process is alive; if it exits
# before detector lines appear, $sawMidRun stays false and the case fails --
# which is precisely how the Start-Process attempt was caught.
$sawMidRun   = $false
$midRunCount = 0
$midRunAt    = $null
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while (-not $proc.HasExited) {
    $now = Read-LogSafe -Path $logFile
    $c = Count-Occurrences -Text $now -Pattern 'stub progress line \d+'
    if ($c -ge 2) { $sawMidRun = $true; $midRunCount = $c; $midRunAt = $sw.Elapsed.TotalSeconds; break }
    Start-Sleep -Milliseconds 400
}
$stillRunningWhenSeen = (-not $proc.HasExited)

$proc.WaitForExit()
$exit = $proc.ExitCode
$final = Read-LogSafe -Path $logFile

Assert-That -Name 'detector lines present mid-run (process still alive)' `
            -Condition ($sawMidRun -and $stillRunningWhenSeen) `
            -Detail ("sawMidRun=$sawMidRun stillRunning=$stillRunningWhenSeen count=$midRunCount at={0:N1}s" -f `
                     $(if ($null -ne $midRunAt) { $midRunAt } else { 0 }))
if ($sawMidRun) {
    Write-Output ("           (saw $midRunCount line(s) at {0:N1}s, run is ~24s)" -f $midRunAt)
}

# Every line exactly once: not merely 8 total, but each index present once.
$dupes = @()
for ($i = 0; $i -lt 8; $i++) {
    $n = Count-Occurrences -Text $final -Pattern ("stub progress line $i\b")
    if ($n -ne 1) { $dupes += "line ${i}: seen $n time(s)" }
}
Assert-That -Name 'all 8 detector lines appear exactly once' -Condition ($dupes.Count -eq 0) `
            -Detail ($dupes -join '; ')
Assert-That -Name 'stderr trailer appears exactly once' `
            -Condition ((Count-Occurrences -Text $final -Pattern 'stub stderr trailer') -eq 1)
Assert-That -Name 'stdout trailer appears exactly once' `
            -Condition ((Count-Occurrences -Text $final -Pattern 'stub stdout trailer') -eq 1)
Assert-That -Name 'exit code is 0' -Condition ($exit -eq 0) -Detail "got $exit"
Assert-That -Name 'log says finished OK' -Condition ($final -match 'DV host scan finished OK')

# The fallback firing would mean the live tail read nothing and the old
# post-exit path supplied the lines -- end-state assertions cannot tell the
# difference, so check it explicitly.
Assert-That -Name 'post-exit fallback did NOT fire (lines came from the live tail)' `
            -Condition ($final -notmatch 'fell back to a post-exit read')
Assert-That -Name 'heartbeat lines present while detector was silent' `
            -Condition ((Count-Occurrences -Text $final -Pattern 'still running:') -ge 1) `
            -Detail ("count=" + (Count-Occurrences -Text $final -Pattern 'still running:'))

# .NET does not inherit PowerShell's location the way `& cmd` did, so the
# wrapper must set WorkingDirectory itself or the detector reads the wrong DB.
$cwd = if (Test-Path -LiteralPath $env:DV_STUB_MARKER) { (Get-Content -LiteralPath $env:DV_STUB_MARKER -Raw).Trim() } else { '<not invoked>' }
Assert-That -Name 'detector ran with the repo root as its working directory' `
            -Condition ($cwd -eq $root) -Detail "cwd=$cwd expected=$root"

# ===========================================================================
Write-Output ''
Write-Output '=== 2. nonzero detector exit -> wrapper exit 1, code logged ==='

$logDir = New-Fixture
$env:DV_STUB_LINES  = '3'
$env:DV_STUB_DELAY  = '1'
$env:DV_STUB_RC     = '7'
$env:DV_STUB_MARKER = Join-Path $root 'invoked.txt'

$proc = Start-Wrapper -LogDir $logDir
$logFile = Wait-ForLog -LogDir $logDir
$proc.WaitForExit()
$exit = $proc.ExitCode
$final = Read-LogSafe -Path $logFile

Assert-That -Name 'wrapper exit is 1' -Condition ($exit -eq 1) -Detail "got $exit"
Assert-That -Name 'log records "detector exited 7"' -Condition ($final -match 'detector exited 7')
Assert-That -Name 'the 3 detector lines survived the failure path' `
            -Condition ((Count-Occurrences -Text $final -Pattern 'stub progress line \d+') -eq 3) `
            -Detail ("count=" + (Count-Occurrences -Text $final -Pattern 'stub progress line \d+'))

# ===========================================================================
Write-Output ''
Write-Output '=== 3. control: unreachable root -> exit 11, detector NEVER invoked ==='

$logDir = New-Fixture -RootCount 1 -WithBadRoot
$env:DV_STUB_LINES  = '2'
$env:DV_STUB_DELAY  = '1'
$env:DV_STUB_RC     = '0'
$env:DV_STUB_MARKER = Join-Path $root 'invoked.txt'

$proc = Start-Wrapper -LogDir $logDir
$logFile = Wait-ForLog -LogDir $logDir
$proc.WaitForExit()
$exit = $proc.ExitCode
$final = Read-LogSafe -Path $logFile

Assert-That -Name 'exit is 11' -Condition ($exit -eq 11) -Detail "got $exit"
Assert-That -Name 'log flags the unreachable root' -Condition ($final -match 'UNREACHABLE')
Assert-That -Name 'detector was never invoked (no marker file)' `
            -Condition (-not (Test-Path -LiteralPath $env:DV_STUB_MARKER))

# ===========================================================================
Write-Output ''
Write-Output '=== 4. control: single-root config (the scalar .Count case) ==='

$logDir = New-Fixture -RootCount 1
$env:DV_STUB_LINES  = '2'
$env:DV_STUB_DELAY  = '1'
$env:DV_STUB_RC     = '0'
$env:DV_STUB_MARKER = Join-Path $root 'invoked.txt'

$proc = Start-Wrapper -LogDir $logDir
$logFile = Wait-ForLog -LogDir $logDir
$proc.WaitForExit()
$exit = $proc.ExitCode
$final = Read-LogSafe -Path $logFile

Assert-That -Name 'exit is 0 (no StrictMode .Count crash)' -Condition ($exit -eq 0) -Detail "got $exit"
Assert-That -Name 'log reports exactly 1 configured root' -Condition ($final -match 'configured roots: 1')
Assert-That -Name 'detector lines still streamed' `
            -Condition ((Count-Occurrences -Text $final -Pattern 'stub progress line \d+') -eq 2)

# ===========================================================================
Write-Output ''
Write-Output '=== 5. edge: final line with no trailing newline is not dropped ==='

$logDir = New-Fixture
$env:DV_STUB_LINES  = '2'
$env:DV_STUB_DELAY  = '1'
$env:DV_STUB_RC     = '0'
$env:DV_STUB_NONL   = '1'
$env:DV_STUB_MARKER = Join-Path $root 'invoked.txt'

$proc = Start-Wrapper -LogDir $logDir
$logFile = Wait-ForLog -LogDir $logDir
$proc.WaitForExit()
$exit = $proc.ExitCode
$final = Read-LogSafe -Path $logFile

Assert-That -Name 'unterminated last line appears exactly once' `
            -Condition ((Count-Occurrences -Text $final -Pattern 'stub unterminated tail') -eq 1) `
            -Detail ("count=" + (Count-Occurrences -Text $final -Pattern 'stub unterminated tail'))
Assert-That -Name 'exit code is 0' -Condition ($exit -eq 0) -Detail "got $exit"

# ===========================================================================
$env:DV_STUB_NONL = ''
Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ''
if ($script:Failures -eq 0) {
    Write-Output '=== ALL CASES PASSED ==='
    exit 0
} else {
    Write-Output ("=== {0} ASSERTION(S) FAILED ===" -f $script:Failures)
    exit 1
}
