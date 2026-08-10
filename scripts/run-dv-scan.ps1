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
    [int]$KeepLogs    = 30,
    # How often to log a "still running" line while the detector is silent. The
    # detector can legitimately say nothing for ~20 minutes (one 90 GB file at
    # the measured 79 MB/s), so streaming alone still leaves long gaps; this is
    # what separates "working" from "hung" in the log. Test fixtures set it low.
    [double]$HeartbeatMinutes = 5
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} {1} {2}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Output $line
    if ($script:LogFile) { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding utf8 }
}

# --- live tail of the detector's captured output ----------------------------
#
# State is script-scoped because Read-DetectorTail is called repeatedly from the
# poll loop and has to remember where it left off.
$script:DetReader    = $null
$script:DetPending   = ''
$script:DetLineCount = 0

function Write-DetectorLine {
    # Detector lines carry python's own timestamp, so they are indented rather
    # than re-stamped -- and written as utf8 like every other line this script
    # emits, so the log has ONE encoding throughout.
    param([string]$Line)
    $script:DetLineCount++
    Write-Output $Line
    if ($script:LogFile) {
        Add-Content -LiteralPath $script:LogFile -Value "    $Line" -Encoding utf8
    }
}

function Read-DetectorTail {
    # Emit every COMPLETE line appended to the capture file since the last call.
    #
    # COMPLETE is the load-bearing word. A poll can land in the middle of a
    # write, and emitting a half-written line would split one detector line into
    # two -- breaking "every line appears exactly once" just as surely as
    # dropping one would. So a partial tail is held in $script:DetPending until
    # its newline arrives. -Final flushes whatever is left once the process has
    # exited, which is how an unterminated last line still reaches the log.
    param([Parameter(Mandatory = $true)][string]$Path, [switch]$Final)

    if ($null -eq $script:DetReader) {
        if (-not (Test-Path -LiteralPath $Path)) { return }
        try {
            # FileShare::ReadWrite is required -- cmd still holds this file open
            # for writing. Verified 2026-08-09 that cmd's `>` permits a
            # concurrent reader; without that share flag the open throws.
            $fs = New-Object System.IO.FileStream(
                    $Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
                    ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
            # UTF-8, matched to the PYTHONIOENCODING the launch pins below.
            #
            # This replaces Get-Content's ANSI default, which was latently wrong:
            # measured 2026-08-09, python's redirected streams were UTF-8 here
            # only because an ambient PYTHONIOENCODING=utf-8:surrogateescape
            # happened to be set, while the locale code page is cp1252. So the
            # detector's encoding depended on who launched it -- a shell and
            # Task Scheduler could disagree. It never showed because the detector
            # emitted pure ASCII; now that it logs filenames, it would.
            $script:DetReader = New-Object System.IO.StreamReader(
                    $fs, (New-Object System.Text.UTF8Encoding($false)), $true)
        } catch {
            return  # not readable yet; the next poll tries again
        }
    }

    try {
        $chunk = $script:DetReader.ReadToEnd()
    } catch {
        return
    }
    if ($chunk) { $script:DetPending += $chunk }

    $nl = $script:DetPending.LastIndexOf("`n")
    if ($nl -ge 0) {
        $complete          = $script:DetPending.Substring(0, $nl)
        $script:DetPending = $script:DetPending.Substring($nl + 1)
        # Split on "`n" and trim the CR, rather than splitting on "`r`n": the
        # last element of a CRLF split would otherwise keep a trailing CR.
        foreach ($line in ($complete -split "`n")) {
            Write-DetectorLine ($line.TrimEnd([char]13))
        }
    }
    if ($Final -and $script:DetPending.Length -gt 0) {
        Write-DetectorLine ($script:DetPending.TrimEnd([char]13))
        $script:DetPending = ''
    }
}

function Close-DetectorTail {
    if ($null -ne $script:DetReader) {
        try { $script:DetReader.Dispose() } catch { }
        $script:DetReader = $null
    }
}

function Get-DvHostRowCount {
    # Progress, read from the ARTIFACT rather than inferred from the process
    # list. dv_host.db held the true rate the whole time on 2026-08-09 while I
    # was deriving a 12x-wrong one from two `ps` snapshots; one COUNT(*) would
    # have settled it. So the heartbeat reports it.
    #
    # Routed through cmd's `>` for the same reason the detector is: no native
    # stderr may touch the PowerShell pipeline, or a broken query would become a
    # terminating error under EAP='Stop' and take the whole run down to log a
    # number. Returns $null whenever anything at all goes wrong.
    param([string]$Python, [string]$DbPath)

    if (-not (Test-Path -LiteralPath $DbPath)) { return $null }
    $tmp = Join-Path $LogDir ("dv-count-{0}.tmp" -f [System.Guid]::NewGuid().ToString('N'))
    try {
        # mode=ro: this connection cannot create or modify the database. It is
        # NOT lock-free -- a WAL reader still participates in normal read
        # locking, and a long-lived one can delay checkpoint progress. What makes
        # it safe here is that it is bounded: one COUNT(*) in a short-lived child
        # process that then exits, which normally coexists with the writer.
        # Verified 2026-08-09 against the live database while a scan held it open
        # in WAL mode. (Claim corrected after peer review; the design was fine,
        # the comment overstated it.)
        # Single quotes only inside the -c payload -- cmd owns the double ones.
        $q = 'import sys,sqlite3,pathlib;u=pathlib.Path(sys.argv[1]).as_uri()+' +
             "'?mode=ro';" +
             'print(sqlite3.connect(u,uri=True,timeout=2.0).execute(' +
             "'SELECT COUNT(*) FROM dv_host').fetchone()[0])"
        $inner = '"{0}" -c "{1}" "{2}" > "{3}" 2>&1' -f $Python, $q, $DbPath, $tmp
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName         = $env:ComSpec
        $psi.Arguments        = '/c "' + $inner + '"'
        $psi.UseShellExecute  = $false
        $psi.CreateNoWindow   = $true
        $psi.WorkingDirectory = $RepoRoot
        $p = [System.Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit(15000)) {
            try { $p.Kill() } catch { }
            return $null
        }
        $first = @(Get-Content -LiteralPath $tmp -ErrorAction SilentlyContinue)
        if ($first.Count -gt 0 -and $first[0] -match '^\s*(\d+)\s*$') {
            return [int]$Matches[1]
        }
        return $null
    } catch {
        return $null
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
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

# @(...) IS LOAD-BEARING. A pipeline yielding ONE item returns a scalar string,
# and under Set-StrictMode `.Count` on a string is a terminating error -- so a
# config with a single library root crashed the wrapper before it probed
# anything. Neither the original tests (2 roots) nor production (4 roots) hit it;
# it took a deliberate one-root fixture to surface. Same reason $unreachable is
# initialised as @() below.
$roots = @($rootsRaw -split ';' | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() })
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
    Write-Log "running: $python -u scripts\host-detector\dv_host_scan.py"

    # DO NOT PIPE NATIVE STDERR THROUGH THE POWERSHELL PIPELINE.
    #
    # The previous version's comment said exactly this and the very next line did
    # `2>&1 | ForEach-Object` anyway. In PS 5.1 that wraps each stderr line in a
    # NativeCommandError ErrorRecord, and with $ErrorActionPreference='Stop' the
    # FIRST such line is a terminating error. Measured on the 2026-08-09 11:00
    # run: the wrapper died immediately after logging "running:", never reached
    # its own "detector exited" line, reported exit 1, and the detector did
    # nothing at all -- dv_host.db untouched, zero new dv_scan rows.
    #
    # REDIRECT AT THE OS LEVEL, via cmd, so PowerShell never handles the streams.
    # Nothing below relaxes $ErrorActionPreference any more: the detector is no
    # longer invoked as a native command by PowerShell, so there is no pipeline
    # left to decorate an ErrorRecord onto and nothing to guard against.
    #
    # Two earlier attempts both failed, each differently, and both are worth
    # naming because the fix is not obvious:
    #
    #   `2>&1 | ForEach-Object`  -- PS 5.1 wraps every native stderr line in a
    #     NativeCommandError ErrorRecord; with EAP='Stop' the first one is
    #     TERMINATING. Measured 2026-08-09 11:00: the wrapper died right after
    #     logging "running:", reported exit 1, and the detector did nothing.
    #   `*>> $LogFile`           -- stops the crash only because EAP was relaxed,
    #     NOT because redirection avoids the wrapping: the ErrorRecord decoration
    #     ("FullyQualifiedErrorId : NativeCommandError") still landed IN the log.
    #     It also writes UTF-16 while this script's own lines are UTF-8, so the
    #     detector's output rendered as "p y t h o n . e x e".
    #
    # cmd's `>` is plain OS file redirection: no ErrorRecord, no PowerShell
    # encoding decision, and cmd propagates the child's exit code. The captured
    # bytes are then appended to the log as UTF-8 like every other line, so the
    # file has ONE encoding throughout.
    #
    # WHY THE OUTPUT IS NOW TAILED LIVE INSTEAD OF FOLDED IN AFTER EXIT.
    #
    # The previous version read $detOut only once the process had exited, so a
    # five-hour run wrote a log containing nothing but the preflight lines above
    # -- indistinguishable from a hung one. On 2026-08-09 that absence is what
    # made me infer throughput from two `ps` snapshots and get 6.7 MB/s when the
    # real figure, sitting in data/dv_host.db the whole time, was 79 MB/s. The
    # 12x error produced a review claiming the design "cannot finish" that then
    # had to be retracted in full. Live output is the cheapest defence against
    # inferring what you could have read.
    #
    # The launch is System.Diagnostics.Process rather than `& cmd /c` ONLY so
    # there is a handle to poll; the command line handed to cmd is byte-for-byte
    # what `& cmd /c "..."` produced, so every property above still holds. Note
    # what does NOT work here:
    #
    #   Start-Process -ArgumentList '/c', $inner  -- the redirection does not
    #     survive being split across an argument ARRAY. Measured against a stub
    #     printing for 24 s: the wrapper reported "finished OK" in 14 s having
    #     captured nothing, because the child exited immediately. ProcessStartInfo
    #     takes the raw command line as ONE string, which is why it works.
    #
    # `-u` is deliberate. Probed 2026-08-09: python's stderr (where `logging`
    # writes, and the detector logs nothing else) is line-buffered and streams
    # on its own, but stdout redirected to a file is BLOCK-buffered -- a stub's
    # print() lines all appeared at exit, not live. The detector uses no print()
    # today, so -u changes nothing now; it is here so that adding one later
    # cannot silently un-fix this.
    $detOut = Join-Path $LogDir ("detector-{0}.out" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

    $inner = '"{0}" -u scripts\host-detector\dv_host_scan.py > "{1}" 2>&1' -f $python, $detOut
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName        = $env:ComSpec
    $psi.Arguments       = '/c "' + $inner + '"'
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow  = $true
    # PIN the child's stream encoding instead of inheriting whatever the caller
    # happens to have. Measured 2026-08-09: python reported utf-8 streams under
    # a cp1252 locale purely because an ambient PYTHONIOENCODING was set in the
    # launching shell -- so the same script could write UTF-8 interactively and
    # cp1252 under Task Scheduler, and the reader above can only be right about
    # one of them. UTF-8 also has no unencodable character, so a title in any
    # script logs cleanly.
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    # MUST be set explicitly. PowerShell sets a native command's working
    # directory from the current location, so Push-Location above was enough for
    # `& cmd /c`; .NET does not, and would inherit the process-wide current
    # directory instead. The detector's --config/--db defaults are repo-relative,
    # so getting this wrong would point it at the wrong database.
    $psi.WorkingDirectory = $RepoRoot

    # Baseline BEFORE the detector starts, so the heartbeat's "+N this run"
    # counts only what this run produced rather than the standing total.
    $dbPath   = Join-Path $RepoRoot 'data\dv_host.db'
    $baseRows = Get-DvHostRowCount -Python $python -DbPath $dbPath
    if ($null -ne $baseRows) { Write-Log "dv_host.db at start: $baseRows rows" }

    $code = $null
    $proc = $null
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        Write-Log "could not start the detector: $($_.Exception.Message)" 'ERROR'
        $code = 1
    }

    if ($null -ne $proc) {
        $started  = Get-Date
        $nextBeat = $started.AddMinutes($HeartbeatMinutes)
        # WaitForExit(ms) returns $true the moment the process ends, so this
        # neither busy-waits nor delays the finish by a full poll interval.
        while (-not $proc.WaitForExit(1000)) {
            Read-DetectorTail -Path $detOut
            $now = Get-Date
            if ($now -ge $nextBeat) {
                $el = New-TimeSpan -Start $started -End $now
                $rows = Get-DvHostRowCount -Python $python -DbPath $dbPath
                # ABSOLUTE COUNT ONLY -- deliberately no "+N this run".
                #
                # An earlier version reported the delta against a pre-launch
                # baseline and called it this run's work. It is not, in three
                # separate ways: another host process writes this same database;
                # an UPSERT of an existing path does real scanning work while
                # leaving COUNT(*) unchanged, so the delta UNDER-counts; and the
                # primary key is the raw path string, so the same file counts
                # twice under two spellings. Peer review (ChatGPT, 2026-08-09)
                # called it correctly: this branch exists because a proxy got
                # promoted into a stronger claim, and that is what the delta was.
                # Per-run progress comes from the detector's own [N] lines, which
                # actually know what they scanned.
                if ($null -eq $rows) {
                    $prog = 'dv_host.db unavailable'
                } else {
                    $prog = "dv_host.db $rows rows"
                }
                # Formatted from TotalHours, not 'hh', so a run past 24 h does
                # not silently wrap its hour count back to zero.
                Write-Log ("  ... still running: {0:00}:{1:00}:{2:00} elapsed, {3} detector line(s), {4}" -f `
                           [int]$el.TotalHours, $el.Minutes, $el.Seconds, $script:DetLineCount, $prog)
                $nextBeat = $now.AddMinutes($HeartbeatMinutes)
            }
        }
        Read-DetectorTail -Path $detOut -Final
        $code = $proc.ExitCode
        $proc.Dispose()
    }
    # FAIL CLOSED. There are exactly two legitimate outcomes: the start threw and
    # $code is already 1, or a process exists and gave us its ExitCode. The old
    # `if ($null -eq $code) { $code = 0 }` turned every other state into SUCCESS
    # -- including .NET's documented case where Process::Start returns $null
    # without throwing because no process resource was started. A wrapper whose
    # entire purpose is to make "the detector did nothing" loud must never
    # report 0 for "we never observed a result". (ChatGPT, 2026-08-09.)
    if ($null -eq $code) {
        Write-Log "detector produced neither a process nor an exit code -- treating as failure." 'ERROR'
        $code = 1
    }
    Close-DetectorTail

    # SAFETY NET, not redundancy. If the live tail never read a single line the
    # run must not be LESS diagnosable than it was before this change, so fall
    # back to the old post-exit read. Gated on a zero count precisely because
    # zero is the only state in which a re-read cannot duplicate anything.
    if ($script:DetLineCount -eq 0 -and (Test-Path -LiteralPath $detOut)) {
        # @(...) is load-bearing, same as for $roots above: a single-line file
        # would otherwise make .Count a terminating error under StrictMode.
        $residue = @(Get-Content -LiteralPath $detOut -Encoding UTF8 -ErrorAction SilentlyContinue)
        if ($residue.Count -gt 0) {
            Write-Log "live tail captured nothing; fell back to a post-exit read." 'WARNING'
            foreach ($line in $residue) { Write-DetectorLine $line }
        }
    }
    Remove-Item -LiteralPath $detOut -Force -ErrorAction SilentlyContinue
} finally {
    Close-DetectorTail
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
