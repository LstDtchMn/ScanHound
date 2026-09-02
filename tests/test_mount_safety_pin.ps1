<#
  PINS the mount-safety behaviour of scripts/mount-nas-shares.ps1.

  WHY THIS EXISTS, AND WHY IT EXISTS FIRST.

  mount-nas-shares.ps1 is a LIVE Scheduled Task: At Startup, At Logon, and
  every twelve minutes, RunLevel Highest. It is the only thing standing between
  a failed WSL mount and ScanHound writing TV files into a VM directory where
  Plex will never see them. Every brief in this review sequence forbade
  touching it, and the one that finally authorised an edit made this file the
  condition: pin the CURRENT behaviour first, prove the pin passes BEFORE the
  edit, and require the same pin to pass after. Without that, the edit is a
  change to a safety-critical live task made on assertion alone.

  WHAT IS PINNED. The whole decision table, end to end, as OUTCOMES -- process
  exit code, whether a recreate was issued, whether a stop was issued -- not as
  a restatement of the code:

    the host mount stage's verdict     0 / 1 / 2 and anything else
      x the container's own answer     probed 0, probed nonzero, not-running,
                                       copy-failed, timeout, docker-unavailable
      x what happens next              leave it, recreate it, stop it, refuse

  HOW IT RUNS THE REAL SCRIPT. Through tests/mount-recovery-harness.ps1, which
  makes a throwaway copy under a closed set of declared, anchored, exactly-once
  substitutions -- executables, staging root, compose recipe, mutex name, log
  path -- and PROVES that the decision region is byte-identical to the live
  file. Nothing is stubbed inside that region. Section 0 below asserts exactly
  that before any case runs, so a substitution that ever reached into the
  decision logic makes this suite refuse rather than quietly stop describing
  production.

  AND EVERY PIN IS SHOWN TO FAIL. Section 7 reintroduces the defects this
  script was hardened against -- the healthy-container sacrifice, the narrow
  timeout allow-list, the missing critical-failure stop, the working-tree
  compose fallback -- and requires the case written for each one to break, with
  the changed OUTCOME asserted rather than "it failed". A pin that cannot fail
  is not a pin.

  One of those controls is deliberately split in two, and the reason is a
  finding rather than a test artefact: the healthy-container property is
  defended in TWO places, and the second of them is unreachable as the file
  stands. See the two CONTROL cases there.

  NOTHING PRODUCTION IS TOUCHED. The live script is only ever READ. The copy
  calls shims, not docker.exe or wsl.exe; it takes a per-run mutex name, never
  Global\ScanHound-MountNASShares; it stages under the test's own TEMP
  directory, not C:\ProgramData\ScanHound\run; and it recreates from a fixture
  compose file in TEMP. No container, image, mount or share is touched.

  Run:  powershell -ExecutionPolicy Bypass -File tests\test_mount_safety_pin.ps1
  Needs: nothing. No Docker daemon, no WSL, no NAS.
#>

$ErrorActionPreference = 'Stop'
$REPO  = Split-Path -Parent $PSScriptRoot
$MOUNT = Join-Path $REPO 'scripts\mount-nas-shares.ps1'
. (Join-Path $PSScriptRoot 'mount-recovery-harness.ps1')

$PASS = 0; $FAIL = 0; $FAILED = @()
function Check([string]$CaseName, [scriptblock]$body) {
    Write-Host ""
    Write-Host ("-- {0}" -f $CaseName) -ForegroundColor Cyan
    try {
        & $body
        $script:PASS++
        Write-Host ("  PASS  {0}" -f $CaseName) -ForegroundColor Green
    } catch {
        $script:FAIL++
        $script:FAILED += $CaseName
        Write-Host ("  FAIL  {0}`n          {1}" -f $CaseName, $_.Exception.Message) -ForegroundColor Red
    }
}
function Assert([bool]$cond, [string]$msg) { if (-not $cond) { throw $msg } }

$SUFFIX = [guid]::NewGuid().ToString('N').Substring(0, 8)
$FX     = Join-Path $env:TEMP "mountpin-$SUFFIX"
$RUNROOT = Join-Path $FX 'run'
$SHIMDIR = Join-Path $FX 'shims'
$PINDIR  = Join-Path $FX 'pinned'
$COMPOSEDIR = Join-Path $FX 'deploy'
$COMPOSE = Join-Path $COMPOSEDIR 'docker-compose.yml'
$MOUNTLOG = Join-Path $FX 'mount.log'
# Never Global\ScanHound-MountNASShares. A copy that took the live name would
# either suppress a real recovery pass or exit 0 as "another instance is
# already running" and pin nothing.
$MUTEX  = "Global\mountpin-$SUFFIX"

New-Item -ItemType Directory -Force -Path $FX, $RUNROOT, $SHIMDIR, $PINDIR, $COMPOSEDIR | Out-Null
Set-Content -LiteralPath $COMPOSE -Value "name: mountpin`nservices:`n  app:`n    image: mountpin:latest`n" -Encoding ASCII

$SHIMS = New-MountPinShims -Dir $SHIMDIR -Transcript (Join-Path $FX 'transcript.txt')

function New-Pin {
    param([string]$MountScriptPath = $MOUNT)
    return New-PinnedMountScript -MountScriptPath $MountScriptPath -OutDir $PINDIR `
        -WslExe $SHIMS.Wsl -DockerExe $SHIMS.Docker -RunRoot $RUNROOT `
        -ComposeFile $COMPOSE -ProjectDir $FX -MutexName $MUTEX -MountLog $MOUNTLOG `
        -ImageTag "mountpin-$($SUFFIX):latest" -ProbeTimeoutSec 6
}

$script:BUILD = New-Pin

function Invoke-Case {
    <#
      One scenario. Every SH_PIN_* variable is set on every run, so no case can
      inherit another's state -- a leaked EXEC_RC would make a pin pass for the
      wrong reason, which is the exact failure this suite exists to prevent.
    #>
    param(
        [hashtable]$Env = @{},
        [string]$ScriptPath = $null
    )
    $id    = [guid]::NewGuid().ToString('N').Substring(0, 6)
    $tr    = Join-Path $FX "transcript-$id.txt"
    $stopF = Join-Path $FX "stopped-$id.flag"
    $recF  = Join-Path $FX "recreated-$id.flag"
    Set-Content -LiteralPath $tr -Value '' -Encoding ASCII
    $e = @{
        SH_PIN_TRANSCRIPT     = $tr
        SH_PIN_STOPFLAG       = $stopF
        SH_PIN_RECREATEFLAG   = $recF
        SH_PIN_CTR            = "mountpin-$SUFFIX"
        SH_PIN_DOCKER_MODE    = 'fake'
        SH_PIN_REAL_DOCKER    = ''
        SH_PIN_WSL_RC         = 0
        SH_PIN_PS             = "mountpin-$SUFFIX"
        SH_PIN_PS_AFTER       = ''
        SH_PIN_PS_RECREATED   = ''
        SH_PIN_PS_RC          = 0
        SH_PIN_CP_RC          = 0
        SH_PIN_CP_RC_AFTER    = ''
        SH_PIN_EXEC_RC        = 0
        SH_PIN_EXEC_RC_AFTER  = ''
        SH_PIN_EXEC_OUT       = 'OK    /library/tv'
        SH_PIN_EXEC_OUT_AFTER = ''
        SH_PIN_EXEC_HANG      = 0
        SH_PIN_EXEC_HANG_AFTER = 0
        SH_PIN_STOP_RC        = 0
        SH_PIN_COMPOSE_RC     = 0
        SH_PIN_TAGFILE        = ''
        SH_PIN_TAG_RC         = 0
        SH_PIN_IMAGE_OUT      = ''
        SH_PIN_IMAGE_RC       = 0
        # The write-guard probe (2026-09-01). ABSENT unless a case says so,
        # which is exactly what an older image answers.
        SH_PIN_GUARD_RC       = 1
        SH_PIN_GUARD_OUT      = ''
        SH_PIN_GUARD_HANG     = 0
    }
    foreach ($k in $Env.Keys) { $e[$k] = $Env[$k] }
    $path = $(if ($ScriptPath) { $ScriptPath } else { $script:BUILD.Path })
    $r = Invoke-PinnedMountScript -Path $path -Env $e
    $lines = @()
    # Blank lines dropped: the transcript is seeded with one so the file exists
    # before the run, and counting it made "nothing was touched" read as
    # "something was touched".
    if (Test-Path -LiteralPath $tr) { $lines = @(Get-Content -LiteralPath $tr | Where-Object { "$_".Trim() -ne '' }) }
    return [pscustomobject]@{
        ExitCode   = $r.ExitCode
        Text       = $r.Text
        Transcript = @($lines)
        Recreated  = [bool](@($lines) | Where-Object { $_ -match '^docker\[\w+\] :: compose ' })
        Stopped    = [bool](@($lines) | Where-Object { $_ -match '^docker\[\w+\] :: stop ' })
        WslCalled  = [bool](@($lines) | Where-Object { $_ -match '^wsl ' })
    }
}

function Assert-Outcome {
    param($R, [int]$Exit, [string]$Says, [nullable[bool]]$Recreated = $null, [nullable[bool]]$Stopped = $null)
    if ($R.ExitCode -ne $Exit) {
        throw ("exit $($R.ExitCode), expected $Exit.`n  ---- output ----`n" +
               (($R.Text -split "`n" | Select-Object -Last 12) -join "`n"))
    }
    if ($Says -and ($R.Text -notmatch [regex]::Escape($Says))) {
        throw ("the output does not say '$Says'.`n  ---- output ----`n" +
               (($R.Text -split "`n" | Select-Object -Last 12) -join "`n"))
    }
    if ($null -ne $Recreated -and $R.Recreated -ne $Recreated) {
        throw "recreate issued = $($R.Recreated), expected $Recreated. transcript:`n  $(($R.Transcript) -join "`n  ")"
    }
    if ($null -ne $Stopped -and $R.Stopped -ne $Stopped) {
        throw "stop issued = $($R.Stopped), expected $Stopped. transcript:`n  $(($R.Transcript) -join "`n  ")"
    }
}

Write-Host ""
Write-Host "== mount-nas-shares.ps1 -- mount-safety behaviour PIN" -ForegroundColor Cyan
Write-Host "   fixture $FX"

try {

# ===========================================================================
# 0. The harness itself
# ===========================================================================

Check "PIN-0: the pinned copy differs from the live script ONLY outside the decision region" {
    $b = $script:BUILD
    Assert ($b.OriginalLines -eq $b.PinnedLines) `
        "the pinned copy has a different line count ($($b.OriginalLines) -> $($b.PinnedLines)); a substitution changed the shape of the file"
    # The size of the divergence, pinned against a number DERIVED from the
    # declared anchors rather than typed here. An undeclared edit -- or a
    # declared one that quietly grew -- fails this.
    Assert ($b.ChangedLines -eq $b.DeclaredChangedLines) `
        ("the copy differs from the live script on $($b.ChangedLines) line(s), but the declared " +
         "substitutions account for $($b.DeclaredChangedLines). Something was changed that is not declared.")
    Write-Host "        $($b.OriginalLines) lines, $($b.ChangedLines) changed by $(@($script:MountPinSubstitutions).Count) declared substitutions"
    # The load-bearing claim. Everything these cases exercise lives from the
    # mount invocation to the end of the file, and none of it is substituted.
    Assert (Assert-MountPinDecisionRegionIntact -Build $b) "the decision region is not intact"
    $region = Get-MountPinDecisionRegion -Text $b.Original
    # A region that had shrunk to nothing would make the assertion above
    # trivially true, so its size is pinned too.
    Assert (($region -split "`n").Count -gt 150) "the decision region is only $(($region -split "`n").Count) lines; the anchor has moved"
    Write-Host "        decision region: $(($region -split "`n").Count) lines, byte-identical"
}

Check "PIN-0b: the harness reads the script the same way whether the checkout is CRLF or LF" {
    # core.autocrlf is true in this repository: a fresh worktree is CRLF and
    # the primary checkout is LF. A harness whose anchors matched raw bytes
    # would produce a verdict that moved with the checkout, and a suite whose
    # verdict moves with the checkout is not a suite. Both shapes are built
    # here and required to produce the same copy.
    $lf   = Get-MountPinText -Path $MOUNT
    $crlf = Join-Path $FX 'mount-crlf.ps1'
    [IO.File]::WriteAllText($crlf, ($lf -replace "`n", "`r`n"), (New-Object Text.UTF8Encoding($false)))
    $lfp  = Join-Path $FX 'mount-lf.ps1'
    [IO.File]::WriteAllText($lfp, $lf, (New-Object Text.UTF8Encoding($false)))
    Assert (([IO.File]::ReadAllText($crlf)).Contains("`r`n")) "the CRLF copy is not actually CRLF; this control would prove nothing"
    Assert (-not ([IO.File]::ReadAllText($lfp)).Contains("`r")) "the LF copy is not actually LF; this control would prove nothing"
    $a = New-Pin -MountScriptPath $crlf
    $b = New-Pin -MountScriptPath $lfp
    Assert ($a.Pinned -ceq $b.Pinned) "the harness produced DIFFERENT copies from the CRLF and LF checkouts"
    Assert ($a.ChangedLines -eq $script:BUILD.ChangedLines) "the CRLF build changed a different number of lines"

    # The OTHER half, and the one that actually bit: the ANCHORS the controls
    # below are written with live in a .ps1 too, so they carry whatever line
    # ending the checkout gave THIS file. A multi-line anchor in CRLF matches
    # zero times against an LF copy, and a control that matches nothing reports
    # "this would prove nothing" instead of reproducing its defect. Shown here
    # both ways rather than assumed.
    # Normalised to LF FIRST and then re-expanded, so this control means the
    # same thing whichever way THIS file happens to be stored.
    $lfAnchor = Get-MountPinAnchor @'
        } else {
            Write-Host "Container already sees all nine verified mounts -- leaving it running."
        }
'@
    $crlfAnchor = ($lfAnchor -replace "`n", "`r`n")
    Assert ($crlfAnchor.Contains("`r`n")) "the CRLF anchor is not CRLF; this control would prove nothing"
    Assert ((Get-MountPinSubstringCount $script:BUILD.Pinned $crlfAnchor) -eq 0) `
        "a raw CRLF anchor matched the LF copy, so this control cannot show the normalisation is doing anything"
    Assert ((Get-MountPinSubstringCount $script:BUILD.Pinned (Get-MountPinAnchor $crlfAnchor)) -eq 1) `
        "Get-MountPinAnchor did not make the CRLF anchor match the LF copy exactly once"
    Write-Host "        a CRLF anchor matches 0 times raw and exactly once after normalisation"
}

Check "PIN-0c: the HARNESS file's own line endings do not change the copy it builds" {
    # PIN-0b varies the endings of the script under test and holds the harness
    # -- the file the anchors are WRITTEN in -- at whatever this checkout gave
    # it. That is the wrong axis. The anchors are here-strings, so a CRLF
    # checkout of tests/mount-recovery-harness.ps1 made every multi-line anchor
    # CRLF while the copy is LF; New-PinnedMountScript threw in setup and this
    # whole suite reported nothing. Measured 2026-09-01 in a fresh worktree:
    # 0 tests run, against a claimed 28/0. So the harness is loaded from a
    # CRLF copy and from an LF copy, each in its own process, and both builds
    # must succeed and produce the same bytes.
    $harness = Join-Path $PSScriptRoot 'mount-recovery-harness.ps1'
    $lfText  = ([IO.File]::ReadAllText($harness) -replace "`r`n", "`n")
    $hc = Join-Path $FX 'harness-crlf.ps1'
    $hl = Join-Path $FX 'harness-lf.ps1'
    [IO.File]::WriteAllText($hc, ($lfText -replace "`n", "`r`n"), (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText($hl, $lfText, (New-Object Text.UTF8Encoding($false)))
    Assert (([IO.File]::ReadAllText($hc)).Contains("`r`n")) "the CRLF harness copy is not CRLF; this control would prove nothing"
    Assert (-not ([IO.File]::ReadAllText($hl)).Contains("`r")) "the LF harness copy is not LF; this control would prove nothing"

    $pinArgs = @{
        MountScriptPath = $MOUNT; OutDir = (Join-Path $FX 'pin0c'); WslExe = $SHIMS.Wsl; DockerExe = $SHIMS.Docker
        RunRoot = $RUNROOT; ComposeFile = $COMPOSE; ProjectDir = $FX; MutexName = $MUTEX; MountLog = $MOUNTLOG
        ImageTag = "mountpin-$($SUFFIX):latest"; ProbeTimeoutSec = 6
    }
    $argFile = Join-Path $FX 'pin0c-args.json'
    ($pinArgs | ConvertTo-Json -Depth 3) | Set-Content -LiteralPath $argFile -Encoding UTF8
    $child = Join-Path $FX 'pin0c-child.ps1'
    # The child dot-sources ONE copy of the harness and builds the pin with it.
    # A separate process per copy: dot-sourcing a second copy into this session
    # would silently redefine the functions the rest of this suite runs on.
    [IO.File]::WriteAllText($child, @'
param([string]$Harness, [string]$ArgFile)
$ErrorActionPreference = 'Stop'
. $Harness
$a = Get-Content -LiteralPath $ArgFile -Raw | ConvertFrom-Json
$b = New-PinnedMountScript -MountScriptPath $a.MountScriptPath -OutDir $a.OutDir -WslExe $a.WslExe -DockerExe $a.DockerExe `
        -RunRoot $a.RunRoot -ComposeFile $a.ComposeFile -ProjectDir $a.ProjectDir -MutexName $a.MutexName -MountLog $a.MountLog `
        -ImageTag $a.ImageTag -ProbeTimeoutSec ([int]$a.ProbeTimeoutSec)
$sha = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($b.Pinned))).Replace('-', '')
Write-Output ("PIN0C {0} {1}" -f $sha, $b.ChangedLines)
'@, (New-Object Text.UTF8Encoding($false)))

    $results = @{}
    foreach ($copy in @(@{ Name = 'CRLF'; Path = $hc }, @{ Name = 'LF'; Path = $hl })) {
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            $out  = & powershell -NoProfile -ExecutionPolicy Bypass -File $child -Harness $copy.Path -ArgFile $argFile 2>&1 | ForEach-Object { "$_" }
            $code = $LASTEXITCODE
        } finally { $ErrorActionPreference = $prev }
        $line = @($out | Where-Object { $_ -like 'PIN0C *' })
        Assert ($code -eq 0 -and $line.Count -eq 1) ("the harness loaded from a $($copy.Name) copy could not build the pin (exit $code):`n" + ((@($out) | Select-Object -Last 6) -join "`n"))
        $results[$copy.Name] = $line[0]
        Write-Host "        harness stored $($copy.Name): $($line[0])"
    }
    Assert ($results['CRLF'] -ceq $results['LF']) "the harness built DIFFERENT copies depending on how the harness file itself is stored"
    # And it is the same build this session made, so the children are not
    # agreeing on some trivially-equal empty copy.
    $mine = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($script:BUILD.Pinned))).Replace('-', '')
    Assert ($results['LF'] -eq ("PIN0C {0} {1}" -f $mine, $script:BUILD.ChangedLines)) "the child build does not match this session's build; the control is measuring something else"
}

# ===========================================================================
# 1. All shares verified (host stage exit 0)
# ===========================================================================

Check "PIN-1: all shares verified and the container proves all nine targets -- nothing is touched" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 0; SH_PIN_EXEC_RC = 0 }
    Assert-Outcome $r 0 "leaving it running" -Recreated $false -Stopped $false
}

Check "PIN-2: all shares verified but the container is NOT RUNNING -- recreate, then verify" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 0; SH_PIN_PS = ''; SH_PIN_PS_RECREATED = "mountpin-$SUFFIX"; SH_PIN_EXEC_RC_AFTER = 0 }
    Assert-Outcome $r 0 "is not running -- starting it" -Recreated $true -Stopped $false
    Assert ($r.Text -match 'Post-recreate verification passed') "the post-recreate gate did not report a pass"
}

Check "PIN-3: all shares verified but the container cannot see them -- recreate" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 0; SH_PIN_EXEC_RC = 1; SH_PIN_EXEC_RC_AFTER = 0 }
    Assert-Outcome $r 0 "recreate required" -Recreated $true -Stopped $false
}

Check "PIN-4: a probe that TIMES OUT is a recreate, not a pass" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 0; SH_PIN_EXEC_HANG = 30; SH_PIN_EXEC_HANG_AFTER = 0; SH_PIN_EXEC_RC_AFTER = 0 }
    Assert-Outcome $r 0 "Probe timed out" -Recreated $true
}

Check "PIN-5: a probe that could not be COPIED in is a recreate, not a pass" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 0; SH_PIN_CP_RC = 1; SH_PIN_CP_RC_AFTER = 0; SH_PIN_EXEC_RC_AFTER = 0 }
    Assert-Outcome $r 0 "Could not copy the probe in" -Recreated $true
}

Check "PIN-6: docker unavailable is a refusal (exit 3), not a recreate" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 0; SH_PIN_PS_RC = 1 }
    Assert-Outcome $r 3 "Docker is not available" -Recreated $false -Stopped $false
}

# ===========================================================================
# 2. Partial failure (host stage exit 1: read-only shares only)
# ===========================================================================

Check "PIN-7: a HEALTHY container is never sacrificed for a read-only share failure" {
    # The most easily lost property in the file, and the reason $needsRecreate
    # is re-evaluated after the switch.
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 1; SH_PIN_EXEC_RC = 0 }
    Assert-Outcome $r 1 "read-only shares are still unavailable" -Recreated $false -Stopped $false
}

Check "PIN-8: a read-only share failure with a BLIND container still recreates, and still exits 1" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 1; SH_PIN_PS = ''; SH_PIN_PS_RECREATED = "mountpin-$SUFFIX"; SH_PIN_EXEC_RC_AFTER = 0 }
    Assert-Outcome $r 1 "read-only shares are still unavailable" -Recreated $true
}

# ===========================================================================
# 3. Critical failure (host stage exit 2)
# ===========================================================================

Check "PIN-9: a critical-share failure NEVER recreates, and leaves a provably good container running" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 0 }
    Assert-Outcome $r 2 "Left running; NOT recreated" -Recreated $false -Stopped $false
}

Check "PIN-10: a critical-share failure with an unprovable container STOPS it, verified" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 2; SH_PIN_STOP_RC = 0; SH_PIN_PS_AFTER = '' }
    Assert-Outcome $r 2 "has been STOPPED (verified not running)" -Recreated $false -Stopped $true
}

Check "PIN-11: a stop that returns 0 while the container is still up is NOT reported as stopped (exit 7)" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 2; SH_PIN_STOP_RC = 0; SH_PIN_PS_AFTER = "mountpin-$SUFFIX" }
    Assert-Outcome $r 7 "could not be confirmed stopped" -Recreated $false -Stopped $true
}

Check "PIN-12: a critical-share failure with no container does NOT start one" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_PS = '' }
    Assert-Outcome $r 2 "was NOT started" -Recreated $false -Stopped $false
}

# ---------------------------------------------------------------------------
# 2026-09-01: a container that refuses unverified TV writes ITSELF is left
# running through a share outage. Everything below is the same shape as
# PIN-10 -- critical share failed, the nine-share probe says the container is
# not provably safe -- with only the write-guard answer varied. PIN-10 is the
# absent-guard control and is unchanged.
# ---------------------------------------------------------------------------

Check "PIN-12b: an unprovable container that carries the write guard is LEFT RUNNING, not stopped" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 2; SH_PIN_GUARD_RC = 0; SH_PIN_GUARD_OUT = '1' }
    Assert-Outcome $r 2 "LEFT RUNNING in DEGRADED mode" -Recreated $false -Stopped $false
    Assert ($r.Text -match 'write-guard version 1') "the outcome does not name the guard version it relied on"
}

Check "PIN-12c: a guard probe that FAILS (older image) keeps the stop -- absence is the default" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 2; SH_PIN_GUARD_RC = 1; SH_PIN_GUARD_OUT = "ModuleNotFoundError: No module named 'backend.share_identity'"; SH_PIN_STOP_RC = 0; SH_PIN_PS_AFTER = '' }
    Assert-Outcome $r 2 "has been STOPPED (verified not running)" -Recreated $false -Stopped $true
    Assert ($r.Text -match 'reports no write guard') "the stop is not attributed to the missing guard"
}

Check "PIN-12d: a guard probe that exits 0 with something that is not a version keeps the stop" {
    # A stubbed or chatty exec must never be READ as a guard. The nine-share
    # stub prints a sentence; a real guard prints an integer.
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 2; SH_PIN_GUARD_RC = 0; SH_PIN_GUARD_OUT = 'STUBBED in-container probe'; SH_PIN_STOP_RC = 0; SH_PIN_PS_AFTER = '' }
    Assert-Outcome $r 2 "has been STOPPED (verified not running)" -Recreated $false -Stopped $true
    Assert ($r.Text -match 'unparseable') "the stop is not attributed to an unparseable guard answer"
}

Check "PIN-12e: guard version 0 is no guard" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 2; SH_PIN_GUARD_RC = 0; SH_PIN_GUARD_OUT = '0'; SH_PIN_STOP_RC = 0; SH_PIN_PS_AFTER = '' }
    Assert-Outcome $r 2 "has been STOPPED (verified not running)" -Recreated $false -Stopped $true
}

Check "PIN-12f: a guard probe that HANGS is absent, not a guard -- the stop still happens" {
    # The probe timeout is substituted to 6 s (declared); the shim sleeps
    # longer. A wedged daemon must not turn into "left running".
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 2; SH_PIN_GUARD_RC = 0; SH_PIN_GUARD_OUT = '1'; SH_PIN_GUARD_HANG = 9; SH_PIN_STOP_RC = 0; SH_PIN_PS_AFTER = '' }
    Assert-Outcome $r 2 "has been STOPPED (verified not running)" -Recreated $false -Stopped $true
    Assert ($r.Text -match 'timeout') "the stop is not attributed to the guard probe timing out"
}

Check "PIN-12g: the guard is consulted only AFTER the nine-share probe fails -- a provably good container never needs it" {
    # PIN-9's shape with a guard that would be READ as absent-and-broken if it
    # were consulted: the outcome must be PIN-9's, untouched.
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 0; SH_PIN_GUARD_RC = 0; SH_PIN_GUARD_OUT = 'garbage' }
    Assert-Outcome $r 2 "Left running; NOT recreated" -Recreated $false -Stopped $false
    Assert (-not ($r.Text -match 'write-guard probe')) "the guard was consulted although the nine-share probe already proved the container"
}

# ===========================================================================
# 4. Indeterminate host stage (anything outside the 0/1/2 verdict space)
# ===========================================================================

Check "PIN-13: a TIMED-OUT mount stage (15) is indeterminate -- no recreate from an unknown result" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 15; SH_PIN_EXEC_RC = 0 }
    Assert-Outcome $r 8 "Left running; NOT recreated" -Recreated $false -Stopped $false
    Assert ($r.Text -match 'INDETERMINATE') "the run did not classify the mount stage as indeterminate"
}

Check "PIN-14: an UNRECOGNISED mount-stage code (137) is indeterminate too, not ordinary handling" {
    # The allow-list, not a special case for the one code that was measured.
    # Under the pre-fix code 137 fell through to `$criticalHostFailure =
    # ($mountExit -eq 2)`, which is false, and a blind container could be
    # recreated on the strength of a result that never described the mounts.
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 137; SH_PIN_PS = ''; SH_PIN_PS_RECREATED = "mountpin-$SUFFIX" }
    Assert-Outcome $r 8 "not one of its defined verdicts" -Recreated $false
}

Check "PIN-15: indeterminate + an unprovable container -> stopped, exit 8" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 15; SH_PIN_EXEC_RC = 2; SH_PIN_STOP_RC = 0; SH_PIN_PS_AFTER = '' }
    Assert-Outcome $r 8 "nothing can write into a non-NAS directory" -Recreated $false -Stopped $true
}

Check "PIN-16: indeterminate + an unprovable container that will not stop -> exit 7, manual intervention" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 15; SH_PIN_EXEC_RC = 2; SH_PIN_STOP_RC = 1; SH_PIN_PS_AFTER = "mountpin-$SUFFIX" }
    Assert-Outcome $r 7 "MANUAL INTERVENTION REQUIRED" -Recreated $false -Stopped $true
}

# ===========================================================================
# 5. The recreate itself, and what gates it
# ===========================================================================

Check "PIN-17: a missing pinned recovery recipe REFUSES the recreate (exit 4) rather than using the working tree" {
    Rename-Item -LiteralPath $COMPOSE -NewName 'docker-compose.yml.hidden'
    try {
        $r = Invoke-Case @{ SH_PIN_WSL_RC = 0; SH_PIN_PS = '' }
        Assert-Outcome $r 4 "refusing to recreate from the mutable working tree" -Recreated $false
    } finally {
        Rename-Item -LiteralPath (Join-Path $COMPOSEDIR 'docker-compose.yml.hidden') -NewName 'docker-compose.yml'
    }
}

Check "PIN-18: a recreate whose compose call FAILS is exit 4, and never claims success" {
    $r = Invoke-Case @{ SH_PIN_WSL_RC = 0; SH_PIN_PS = ''; SH_PIN_COMPOSE_RC = 1 }
    Assert-Outcome $r 4 "recovery never builds from the working tree" -Recreated $true
}

Check "PIN-19: a recreate that leaves the CRITICAL target unproven stops the container (exit 5)" {
    $r = Invoke-Case @{
        SH_PIN_WSL_RC = 0; SH_PIN_PS = ''; SH_PIN_PS_RECREATED = "mountpin-$SUFFIX"
        SH_PIN_EXEC_RC_AFTER = 2; SH_PIN_STOP_RC = 0; SH_PIN_PS_AFTER = ''
    }
    Assert-Outcome $r 5 "Post-recreate verification failed for /library/tv" -Recreated $true -Stopped $true
}

Check "PIN-20: a recreate that leaves only READ-ONLY sources unproven is exit 6, and does not stop the container" {
    $r = Invoke-Case @{
        SH_PIN_WSL_RC = 0; SH_PIN_PS = ''; SH_PIN_PS_RECREATED = "mountpin-$SUFFIX"
        SH_PIN_EXEC_RC_AFTER = 1
    }
    Assert-Outcome $r 6 "one or more read-only sources" -Recreated $true -Stopped $false
}

# ===========================================================================
# 6. Single instance
# ===========================================================================

Check "PIN-21: a second instance exits 0 without calling wsl or docker at all" {
    # Held from a SEPARATE PROCESS: a named mutex is re-entrant for the thread
    # that already owns it, so a WaitOne issued here would succeed and the case
    # would assert the opposite of what it means.
    $holder = Join-Path $FX 'hold-mutex.ps1'
    Set-Content -LiteralPath $holder -Encoding ASCII -Value @'
param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Ready, [int]$Seconds = 60)
$m = New-Object System.Threading.Mutex($false, $Name)
$got = $false
try { $got = $m.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $got = $true }
Set-Content -LiteralPath $Ready -Value $(if ($got) { 'HELD' } else { 'NOT-HELD' })
Start-Sleep -Seconds $Seconds
if ($got) { try { $m.ReleaseMutex() } catch { } }
$m.Dispose()
'@
    $ready = Join-Path $FX 'holder-ready.txt'
    Remove-Item -LiteralPath $ready -Force -ErrorAction SilentlyContinue
    $p = Start-Process powershell -PassThru -WindowStyle Hidden -ArgumentList @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File',$holder,'-Name',$MUTEX,'-Ready',$ready,'-Seconds','60')
    try {
        $waited = 0
        while (-not (Test-Path -LiteralPath $ready) -and $waited -lt 30) { Start-Sleep -Milliseconds 200; $waited += 0.2 }
        Assert (Test-Path -LiteralPath $ready) "the mutex holder never started; this case would prove nothing"
        Assert ((Get-Content -LiteralPath $ready -Raw).Trim() -eq 'HELD') "the holder did not get the mutex; this case would prove nothing"
        $r = Invoke-Case @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 2 }
        Assert-Outcome $r 0 "Another instance is already running" -Recreated $false -Stopped $false
        Assert (-not $r.WslCalled) "the second instance ran the mount stage anyway: $(($r.Transcript) -join '; ')"
        Assert (@($r.Transcript).Count -eq 0) "the second instance touched something: $(($r.Transcript) -join '; ')"
    } finally {
        try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
}

# ===========================================================================
# 7. NEGATIVE CONTROLS -- every pin above shown to FAIL
# ===========================================================================
# A pin written beside the code it pins passes by construction. Each control
# reintroduces one specific defect into the throwaway copy and requires the
# case written for it to break -- and to break for the RIGHT reason, which is
# why each one asserts the changed OUTCOME rather than merely "it failed".

function New-DefectivePin {
    <# Edits are (Old, New) PAIRS and each Old must occur exactly once, so a
       control can never land on a second site or on nothing at all. Several
       pairs are allowed because some properties here are defended in more than
       one place, and a control that removed only one of them would report the
       pin as vacuous when it is merely redundant. #>
    param([Parameter(Mandatory)][string[][]]$Edits, [Parameter(Mandatory)][string]$Label)
    $text = $script:BUILD.Pinned
    foreach ($e in $Edits) {
        # Get-MountPinAnchor, always. These anchors are here-strings in THIS
        # file, so they carry whatever line ending the checkout gave it, while
        # the copy is LF. This file is currently LF and would therefore pass
        # without it -- until it is committed and checked out again under
        # core.autocrlf, at which point every control below would silently stop
        # reproducing its defect.
        $old = Get-MountPinAnchor $e[0]
        $new = Get-MountPinAnchor $e[1]
        $n = Get-MountPinSubstringCount $text $old
        if ($n -ne 1) { throw "the control '$Label' anchor matched $n time(s); it would prove nothing" }
        $text = $text.Replace($old, $new)
    }
    $p = Join-Path $PINDIR ("defect-" + [guid]::NewGuid().ToString('N').Substring(0, 8) + ".ps1")
    [IO.File]::WriteAllText($p, $text, (New-Object Text.UTF8Encoding($false)))
    return $p
}

$RECREATE_HEALTHY = @(
    ,@(@'
        } else {
            Write-Host "Container already sees all nine verified mounts -- leaving it running."
        }
'@, @'
        } else {
            $needsRecreate = $true
        }
'@)
)
$DROP_HEALTHY_GUARD = @(
    ,@(@'
if ($needsRecreate -and $mountExit -ne 0 -and $probe.Reason -eq "probed" -and $probe.Code -eq 0) {
    $needsRecreate = $false
}
'@, '# control: the healthy-container guard is gone')
)

Check "CONTROL: recreating a container that already proves its mounts breaks PIN-1" {
    # The property: a container that can already see all nine verified targets
    # is LEFT ALONE. Put the recreate back into that branch and PIN-1 must move.
    $p = New-DefectivePin -Edits $RECREATE_HEALTHY -Label 'healthy'
    $r1 = Invoke-Case -ScriptPath $p @{ SH_PIN_WSL_RC = 0; SH_PIN_EXEC_RC = 0 }
    Assert ($r1.Recreated) `
        "PIN-1 requires a healthy container to be left alone. With the defect back it was still not recreated, so PIN-1 pins nothing."
    # MEASURED, and worth stating because it is the reason this control is
    # split in two: with only THIS edit, PIN-7 still passes. Its shape
    # (mountExit 1, probed, code 0) is caught by the second guard below, so a
    # single-edit control would have reported PIN-7 as vacuous when it is
    # merely defended twice.
    $r7 = Invoke-Case -ScriptPath $p @{ SH_PIN_WSL_RC = 1; SH_PIN_EXEC_RC = 0 }
    Assert (-not $r7.Recreated) "the second guard no longer catches the partial-failure shape; the control below is now mis-scoped"
    Write-Host "        PIN-1's shape recreates; PIN-7's is still saved by the second guard"
}

Check "CONTROL: PIN-7 survives a read-only failure only because TWO lines defend it -- remove both and it breaks" {
    # `if ($needsRecreate -and $mountExit -ne 0 -and probed -and code 0)` is
    # UNREACHABLE in the file as it stands: $needsRecreate is only ever set by
    # the not-running / timeout / copy-failed branches, and all three return a
    # probe whose Code is the initial -1, never 0. It becomes load bearing the
    # moment the branch above it recreates a healthy container -- which is
    # exactly the defect PIN-7 exists to catch -- so the honest control removes
    # both, and says so rather than crediting PIN-7 to a line that does no work
    # today.
    $p = New-DefectivePin -Edits ($RECREATE_HEALTHY + $DROP_HEALTHY_GUARD) -Label 'healthy+guard'
    $r7 = Invoke-Case -ScriptPath $p @{ SH_PIN_WSL_RC = 1; SH_PIN_EXEC_RC = 0 }
    Assert ($r7.Recreated) `
        "PIN-7 requires a healthy container to survive a read-only share failure. With both defences removed it was still not recreated, so PIN-7 pins nothing."
    Assert ($r7.ExitCode -eq 1) "the exit code moved as well, so PIN-7's recreate assertion is not the thing that fires"
    Write-Host "        with both defences gone: exit 1 as before, but a recreate PIN-7 forbids"
}

Check "CONTROL: narrowing the indeterminate allow-list back to 15 makes PIN-14 recreate on an unknown result" {
    $p = New-DefectivePin -Label 'allowlist' -Edits @(, @('if ($mountExit -notin @(0, 1, 2)) {', 'if ($mountExit -eq 15) {'))
    $r = Invoke-Case -ScriptPath $p @{ SH_PIN_WSL_RC = 137; SH_PIN_PS = ''; SH_PIN_PS_RECREATED = "mountpin-$SUFFIX"; SH_PIN_EXEC_RC_AFTER = 0 }
    Assert ($r.ExitCode -ne 8) "the defect did not change the verdict; PIN-14 would not have caught it"
    Assert ($r.Recreated) `
        ("PIN-14 exists because exit 137 used to fall through to ordinary handling and RECREATE the " +
         "container on a result that never described the mounts. With the allow-list narrowed it did not " +
         "recreate, so PIN-14 pins nothing.")
    Write-Host "        with the allow-list narrowed: exit $($r.ExitCode), recreate issued"
}

Check "CONTROL: removing the critical-failure stop makes PIN-10 leave a blind container running" {
    $p = New-DefectivePin -Label 'criticalstop' -Edits @(, @(@'
    Write-Host "Critical share unverified, the container is not provably safe and reports no write guard ($($guard.Reason)) -- stopping it."
    $stopState = Stop-ScanhoundVerified
'@, @'
    Write-Host "Critical share unverified, the container is not provably safe and reports no write guard ($($guard.Reason)) -- stopping it."
    $stopState = "stopped"
'@))
    $r = Invoke-Case -ScriptPath $p @{ SH_PIN_WSL_RC = 2; SH_PIN_EXEC_RC = 2; SH_PIN_STOP_RC = 0; SH_PIN_PS_AFTER = '' }
    Assert (-not $r.Stopped) `
        ("PIN-10 requires the container to be STOPPED when the critical share is unverified. With the " +
         "stop removed it was not stopped, and the run still exited $($r.ExitCode) claiming it had been.")
    Assert ($r.ExitCode -eq 2) "the control changed the exit code as well, so PIN-10's stop assertion is not what fires"
    Write-Host "        with the stop removed: exit 2 and the same message, but NO stop was issued -- only the transcript sees it"
}

Check "CONTROL: falling back to the working-tree recipe makes PIN-17 recreate from an unreviewed compose" {
    $p = New-DefectivePin -Label 'composefallback' -Edits @(, @(@'
    if (-not (Test-Path -LiteralPath $ComposeFile)) {
        Fail ("Deployed Compose recipe not found at $ComposeFile -- refusing to " +
              "recreate from the mutable working tree. Redeploy the bundle.") 4
    }
'@, '    # control: a missing pinned recipe no longer refuses'))
    Rename-Item -LiteralPath $COMPOSE -NewName 'docker-compose.yml.hidden'
    try {
        $r = Invoke-Case -ScriptPath $p @{ SH_PIN_WSL_RC = 0; SH_PIN_PS = ''; SH_PIN_PS_RECREATED = "mountpin-$SUFFIX"; SH_PIN_EXEC_RC_AFTER = 0 }
        Assert ($r.Recreated) `
            ("PIN-17 requires a missing pinned recipe to REFUSE. With the guard removed the run went " +
             "ahead and issued a recreate, which is what PIN-17 pins.")
        Assert ($r.ExitCode -ne 4) "the exit code is still 4, so PIN-17's assertion would not have moved"
    } finally {
        Rename-Item -LiteralPath (Join-Path $COMPOSEDIR 'docker-compose.yml.hidden') -NewName 'docker-compose.yml'
    }
}

}
finally {
    Remove-Item -LiteralPath $FX -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host ("== {0} passed, {1} failed" -f $PASS, $FAIL) -ForegroundColor $(if ($FAIL) { 'Red' } else { 'Green' })
foreach ($f in $FAILED) { Write-Host "   FAILED: $f" -ForegroundColor Red }
exit $(if ($FAIL) { 1 } else { 0 })
