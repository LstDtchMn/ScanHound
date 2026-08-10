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
$repo    = Split-Path -Parent $PSScriptRoot
$script:Failures = 0

$py = @(
    'C:\Users\NLSur\AppData\Local\Programs\Python\Python312\python.exe',
    'C:\Program Files\Python312\python.exe'
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Output 'FATAL: no python found; this suite needs the real interpreter.'; exit 1 }

function Invoke-PyFile {
    # Run a python script through cmd's `>` -- the same OS-level redirection the
    # wrapper uses -- and return the captured text. Never pipes native stderr
    # through PowerShell, so a traceback cannot become a terminating error here.
    param([string]$ScriptBody, [string[]]$Arguments = @())
    $f   = Join-Path $env:TEMP ("dvtest-{0}.py"  -f [System.Guid]::NewGuid().ToString('N'))
    $out = Join-Path $env:TEMP ("dvtest-{0}.out" -f [System.Guid]::NewGuid().ToString('N'))
    Set-Content -LiteralPath $f -Value $ScriptBody -Encoding utf8
    try {
        $argStr = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
        & cmd /c "`"$py`" `"$f`" $argStr > `"$out`" 2>&1"
        $rc = $LASTEXITCODE
        $text = if (Test-Path -LiteralPath $out) { (Get-Content -LiteralPath $out -Raw) } else { '' }
        return [pscustomobject]@{ Code = $rc; Text = ($text -replace "`r", '') }
    } finally {
        Remove-Item -LiteralPath $f, $out -Force -ErrorAction SilentlyContinue
    }
}

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
    param([int]$RootCount = 2, [switch]$WithBadRoot, [int]$DbRows = 3)

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

# A title outside the cp1252 code page. The wrapper pins PYTHONIOENCODING=utf-8
# and reads the capture as UTF-8; if those two ever disagree this line arrives
# as mojibake, which is how the old UTF-16 "p y t h o n . e x e" bug looked.
# Written as escapes, not literal characters: this file is emitted as ASCII and
# PowerShell 5.1 parses an unmarked .ps1 as ANSI, so a literal would be mangled
# before python ever saw it.
log.info("stub unicode title \u6771\u4eac Story (1953).mkv")
sys.stderr.write("stub stderr trailer\n")
if os.environ.get("DV_STUB_NONL"):
    # No trailing newline: exercises the -Final flush of a partial last line.
    sys.stdout.write("stub unterminated tail")
else:
    print("stub stdout trailer")
sys.exit(rc)
'@
    Set-Content -LiteralPath (Join-Path $det 'dv_host_scan.py') -Value $stub -Encoding ascii

    # A real dv_host.db so the heartbeat's progress query has something to read.
    # Same table name the detector creates, because the wrapper queries it by
    # name -- a fixture that invented its own would test nothing.
    if ($DbRows -gt 0) {
        $mk = @'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("CREATE TABLE IF NOT EXISTS dv_host (path TEXT PRIMARY KEY, dv_layer TEXT,"
          " sig_mtime REAL, sig_size INTEGER, title TEXT,"
          " scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
for i in range(int(sys.argv[2])):
    c.execute("INSERT OR IGNORE INTO dv_host (path, dv_layer) VALUES (?,?)", ("f%d.mkv" % i, "fel"))
c.commit()
'@
        Invoke-PyFile -ScriptBody $mk -Arguments @((Join-Path $dat 'dv_host.db'), "$DbRows") | Out-Null
    }
    return $log
}

function Start-Wrapper {
    # Launch the wrapper the way Task Scheduler does -- as its own powershell
    # process -- so the exit code we read is the same LastTaskResult would show.
    param([string]$LogDir, [double]$Heartbeat = 5)
    # Capture the wrapper's own console through cmd's `>` so a crash before it
    # opens its log file is still diagnosable. Without this a failure looks
    # identical to "produced no output", which cost real time to chase.
    $script:WrapperConsole = Join-Path $env:TEMP ("dv-wrapper-console-{0}.txt" -f [System.Guid]::NewGuid().ToString('N'))
    $ps  = (Get-Command powershell.exe).Source
    $cmd = ('"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}" -RepoRoot "{2}" -LogDir "{3}" -HeartbeatMinutes {4} > "{5}" 2>&1' `
            -f $ps, $wrapper, $root, $LogDir, $Heartbeat, $script:WrapperConsole)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName        = $env:ComSpec
    $psi.Arguments       = '/c "' + $cmd + '"'
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

# THE MID-RUN ASSERTION, and the DEADLINE is what gives it teeth.
#
# "Lines appeared before the process exited" is NOT enough, and assuming it was
# nearly shipped a vacuous test. The old wrapper folds its whole capture in
# after the detector exits and then quits ~100 ms later; a 400 ms poll can land
# inside that window, so the control passed by luck on one run and failed on
# another. Timing is the honest discriminator: the stub emits for ~24 s, so the
# old wrapper cannot put a single line in the log before ~24 s, while streaming
# puts one there at ~1 s. Requiring 2 lines within MID_RUN_DEADLINE seconds is
# unreachable for the former and has ~3x margin for the latter.
$MID_RUN_DEADLINE = 12.0
$sawMidRun   = $false
$midRunCount = 0
$midRunAt    = $null
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while (-not $proc.HasExited) {
    $now = Read-LogSafe -Path $logFile
    $c = Count-Occurrences -Text $now -Pattern 'stub progress line \d+'
    if ($c -ge 2) { $sawMidRun = $true; $midRunCount = $c; $midRunAt = $sw.Elapsed.TotalSeconds; break }
    Start-Sleep -Milliseconds 250
}
$stillRunningWhenSeen = (-not $proc.HasExited)
$inTime = ($null -ne $midRunAt -and $midRunAt -lt $MID_RUN_DEADLINE)

$proc.WaitForExit()
$exit = $proc.ExitCode
$final = Read-LogSafe -Path $logFile

Assert-That -Name "detector lines in the log within ${MID_RUN_DEADLINE}s, process still alive" `
            -Condition ($sawMidRun -and $stillRunningWhenSeen -and $inTime) `
            -Detail ("sawMidRun=$sawMidRun stillRunning=$stillRunningWhenSeen inTime=$inTime count=$midRunCount at={0:N1}s (deadline ${MID_RUN_DEADLINE}s; a post-exit fold cannot beat ~24s)" -f `
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

# -join, not '+': adding two [char] values in PowerShell is INTEGER addition.
$cjk = -join @([char]0x6771, [char]0x4EAC)
Assert-That -Name 'non-ASCII title survives the capture -> log round-trip (no mojibake)' `
            -Condition ($final -match [regex]::Escape("stub unicode title $cjk Story (1953).mkv")) `
            -Detail 'PYTHONIOENCODING and the tail reader disagreeing shows up here as mojibake'
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

# The heartbeat must report the DATABASE row count, not just elapsed time --
# that number is the one whose absence caused the 12x error.
Assert-That -Name 'baseline row count logged before the detector starts' `
            -Condition ($final -match 'dv_host\.db at start: 3 rows')
Assert-That -Name 'heartbeat reports the dv_host.db row count and delta' `
            -Condition ($final -match 'dv_host\.db 3 rows \(\+0 this run\)') `
            -Detail 'expected "dv_host.db 3 rows (+0 this run)" in a heartbeat line'

# .NET does not inherit PowerShell's location the way `& cmd` did, so the
# wrapper must set WorkingDirectory itself or the detector reads the wrong DB.
$cwd = if (Test-Path -LiteralPath $env:DV_STUB_MARKER) { (Get-Content -LiteralPath $env:DV_STUB_MARKER -Raw).Trim() } else { '<not invoked>' }
Assert-That -Name 'detector ran with the repo root as its working directory' `
            -Condition ($cwd -eq $root) -Detail "cwd=$cwd expected=$root"

if ($script:Failures -gt 0 -and (Test-Path -LiteralPath $script:WrapperConsole)) {
    Write-Output '           ---- wrapper console ----'
    Get-Content -LiteralPath $script:WrapperConsole | Select-Object -Last 25 |
        ForEach-Object { Write-Output "           $_" }
}

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
Write-Output ''
Write-Output '=== 6. the REAL detector: per-file logging, incl. an unencodable title ==='
#
# Cases 1-5 drive a stub. This one drives scripts\host-detector\dv_host_scan.py
# itself, because the streaming fix is worthless if the detector says nothing --
# and because logging a FILENAME is a new hazard: the wrapper captures output
# under the ANSI code page, where an unencodable character would raise
# UnicodeEncodeError and kill a multi-hour scan over a log line.
#
# dovi_tool is stubbed to exit 0 leaving the RPU empty, which dv_detect reads as
# an authoritative "none" -- enough to exercise the logging without reading a
# real 45 GB title. --api points at the discard port so the run cannot POST a
# dv-import at the live container.

$c6      = Join-Path $env:TEMP 'dv-detector-logtest'
$c6media = Join-Path $c6 'media'
$c6stub  = Join-Path $c6 'stubbin'
Remove-Item -LiteralPath $c6 -Recurse -Force -ErrorAction SilentlyContinue
foreach ($d in @($c6, $c6media, $c6stub)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

Set-Content -LiteralPath (Join-Path $c6stub 'dovi_tool.bat') -Value "@echo off`r`nexit /b 0" -Encoding ascii

# One plain title and one whose characters the ANSI code page cannot represent.
$plain  = 'Plain Movie (2001).mkv'
# -join, not '+': PowerShell adds two [char] values as INTEGERS.
$exotic = (-join @([char]0x6771, [char]0x4EAC)) + ' Story (1953).mkv'   # CJK, not in cp1252
foreach ($n in @($plain, $exotic)) {
    [System.IO.File]::WriteAllBytes((Join-Path $c6media $n), (New-Object byte[] 2048))
}

$c6cfg = Join-Path $c6 'cfg.json'
@{ dv_library_roots = ($c6media -replace '\\', '/'); dv_detection = $true } |
    ConvertTo-Json | Set-Content -LiteralPath $c6cfg -Encoding ascii
$c6db  = Join-Path $c6 'test_dv_host.db'
$c6out = Join-Path $c6 'detector.out'

# Run the detector exactly as the wrapper does: cmd's `>`, from the repo root,
# with the stub dovi_tool first on PATH.
$savedPath = $env:PATH
$savedEnc  = $env:PYTHONIOENCODING
$env:PATH = "$c6stub;$env:PATH"
# Pin the encoding exactly as the wrapper does, so this case tests the shipped
# configuration rather than whatever the launching shell happened to export.
$env:PYTHONIOENCODING = 'utf-8'
Push-Location -LiteralPath $repo
try {
    $detArgs = '--config "{0}" --db "{1}" --api http://127.0.0.1:9' -f $c6cfg, $c6db
    & cmd /c "`"$py`" -u scripts\host-detector\dv_host_scan.py $detArgs > `"$c6out`" 2>&1"
    $c6code = $LASTEXITCODE
} finally {
    Pop-Location
    $env:PATH = $savedPath
    $env:PYTHONIOENCODING = $savedEnc
}
$c6text = if (Test-Path -LiteralPath $c6out) { Get-Content -LiteralPath $c6out -Encoding UTF8 -Raw } else { '' }

Assert-That -Name 'detector exits 0' -Condition ($c6code -eq 0) -Detail "got $c6code"
Assert-That -Name 'no UnicodeEncodeError / traceback on the unencodable title' `
            -Condition ($c6text -notmatch 'UnicodeEncodeError' -and $c6text -notmatch 'Traceback') `
            -Detail (($c6text -split "`n" | Select-Object -Last 4) -join ' | ')
Assert-That -Name 'logs a "scanning <file> (N GB)" line BEFORE reading' `
            -Condition ((Count-Occurrences -Text $c6text -Pattern 'scanning .*\(\d+\.\d GB\)') -eq 2) `
            -Detail ("count=" + (Count-Occurrences -Text $c6text -Pattern 'scanning .*\(\d+\.\d GB\)'))
Assert-That -Name 'logs a per-file result with elapsed time and MB/s' `
            -Condition ((Count-Occurrences -Text $c6text -Pattern '-> \w+ in \d+s \(\d+ MB/s\)') -eq 2) `
            -Detail ("count=" + (Count-Occurrences -Text $c6text -Pattern '-> \w+ in \d+s \(\d+ MB/s\)'))
Assert-That -Name 'the plain title is logged by name' `
            -Condition ($c6text -match [regex]::Escape('Plain Movie (2001).mkv'))
Assert-That -Name 'the unencodable title is logged intact, not mangled' `
            -Condition ($c6text -match [regex]::Escape($exotic)) `
            -Detail "expected '$exotic' in the detector's captured output"
Assert-That -Name 'both files were counted and indexed [1]/[2]' `
            -Condition (($c6text -match '\[1\] scanning') -and ($c6text -match '\[2\] scanning'))
Assert-That -Name 'reports the final scanned count' `
            -Condition ($c6text -match 'scanned 2 file\(s\)')

Remove-Item -LiteralPath $c6 -Recurse -Force -ErrorAction SilentlyContinue

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
