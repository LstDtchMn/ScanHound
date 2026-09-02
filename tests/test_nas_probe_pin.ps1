<#
  Pins scripts/nas-probe.ps1 against scripts/mount-nas-shares.ps1.

  WHY THIS EXISTS. SR3-1 asked the deploy engine to prove the same storage
  identity the recovery task proves. The obvious way to do that -- re-type the
  rule in the deploy script -- is the defect this review sequence has already
  found twice: two copies of a safety rule that drift apart silently, and
  nobody notices until the stale one passes something the live one would have
  refused.

  So nas-probe.ps1 keeps NO copy. It parses mount-nas-shares.ps1 and lifts the
  `$probeScript` here-string out of it, then applies three declared
  parameterising substitutions. That makes most drift structurally impossible;
  what remains possible is that someone edits one of the three anchored lines
  and the substitution quietly stops applying, or edits a path prefix so the
  derived spec no longer describes the real mounts.

  This file proves the module catches exactly those cases. Every assertion
  below is a guard SHOWN TO FAIL: each negative control reintroduces one
  specific edit and requires the module to refuse. A guard written beside the
  code it checks passes by construction, so none of them are written that way.

  WHAT IS DELIBERATELY NOT ASSERTED. Ordinary edits to the rule -- tightening
  the mountpoint test, adding a verdict, changing a message -- are NOT caught
  and must not be: there is one copy of that text, so both consumers change
  together. Catching those would mean pinning a copy, which is the thing being
  avoided.

  mount-nas-shares.ps1 is NEVER written to. Negative controls are applied to
  throwaway copies under the user's TEMP.

  Run:  powershell -ExecutionPolicy Bypass -File tests\test_nas_probe_pin.ps1
        add -IncludeLiveNas to run the differential in section 4 (see there).
#>

[CmdletBinding()]
param(
    # Section 4 runs the ORIGINAL rule and the LIFTED rule side by side against
    # the real NAS shares. It is opt-in because it binds
    # \\TURTLELANDSRV2 shares into a throwaway container and, at the critical
    # read-write target only, writes and deletes one probe file -- the same
    # .scanhound-mount-probe.<pid> file mount-nas-shares.ps1 already writes to
    # the same share every twelve minutes. Nothing else is written, nothing is
    # mounted or unmounted, and the scanhound container is not touched.
    [switch]$IncludeLiveNas
)

$ErrorActionPreference = 'Stop'
$REPO   = Split-Path -Parent $PSScriptRoot
$MODULE = Join-Path $REPO 'scripts\nas-probe.ps1'
$MOUNT  = Join-Path $REPO 'scripts\mount-nas-shares.ps1'
$COMPOSE = Join-Path $REPO 'docker-compose.yml'
. $MODULE
# Dot-sourced HERE, at script scope, not inside a case body. `$script:` inside
# a dot-sourced function resolves against the scope it was dot-sourced INTO, so
# loading the engine inside a `&`-invoked case body would give Stop-Deploy a
# different (empty) ledger scope than the one a case can populate -- and its
# refusals would surface as null-property errors instead of their own messages.
. (Join-Path $REPO 'scripts\deploy-core.ps1')
# The engine's ledger. Only ever exists inside a run; section 2b calls
# Resolve-NasRuntimeSpec on its own, so it needs one to write a refusal into.
$script:D = [ordered]@{ stop_reason = $null }

$PASS = 0; $FAIL = 0; $FAILED = @(); $SKIP = 0
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

function Native {
    param([Parameter(Mandatory)][scriptblock]$Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $out  = & $Command 2>&1 | ForEach-Object { $_.ToString() }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
    [pscustomobject]@{ Output = @($out); ExitCode = $code; Text = (@($out) -join "`n") }
}

$TMP = Join-Path $env:TEMP ("nasprobe-pin-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $TMP | Out-Null

function New-MutatedMountScript {
    <#
      A throwaway copy of the recovery script with exactly one edit.

      Mutated by ANCHORED SUBSTRING, and the substring is required to occur
      exactly once, so a mutation can never silently land on a second site or
      on nothing at all. A negative control that did not actually change the
      file would "prove" the guard fires when nothing happened.
    #>
    param([string]$Old, [string]$New, [string]$Label)
    $text = [IO.File]::ReadAllText($MOUNT)
    $n = Get-NasSubstringCount $text $Old
    if ($n -ne 1) { throw "mutation '$Label' anchor matched $n time(s) in the real script; the control would prove nothing" }
    $p = Join-Path $TMP ("mount-" + [guid]::NewGuid().ToString('N').Substring(0, 8) + ".ps1")
    [IO.File]::WriteAllText($p, $text.Replace($Old, $New))
    return $p
}

function Assert-Refuses {
    <# The module must THROW, and the message must name the thing that changed
       -- an exception that says nothing useful is not much better than none. #>
    param([scriptblock]$Action, [string]$Expect, [string]$What)
    $threw = $false; $msg = ''
    try { & $Action } catch { $threw = $true; $msg = $_.Exception.Message }
    Assert $threw "$What -- the module did NOT refuse; it accepted a changed rule silently"
    Assert ($msg -like "*$Expect*") "$What -- refused, but the message does not name '$Expect': $msg"
    Write-Host "        refused with: $($msg.Substring(0, [Math]::Min(150, $msg.Length)))"
}

Write-Host ""
Write-Host "== nas-probe.ps1 pinned against mount-nas-shares.ps1" -ForegroundColor Cyan

try {
# ===========================================================================
# 1. The lift itself
# ===========================================================================

Check "the probe is lifted from the real script and differs ONLY by the declared substitutions" {
    $lifted   = Get-NasProbeScript -MountScriptPath $MOUNT
    $ast      = Get-NasMountScriptAst -Path $MOUNT
    $original = (Get-NasScriptString -Ast $ast -Variable 'probeScript' -Path $MOUNT) -replace "`r`n", "`n"

    # Re-apply the same substitutions to the original independently, and the
    # two must be byte-identical. That is the whole claim: nothing else about
    # the rule was touched.
    $expected = $original
    foreach ($s in $script:NasProbeSubstitutions) {
        Assert ((Get-NasSubstringCount $expected $s.Old) -eq 1) "the $($s.What) anchor is not present exactly once in the real script"
        $expected = $expected.Replace($s.Old, $s.New)
    }
    Assert ($lifted -ceq $expected) "the lifted probe is not the original with exactly the declared substitutions applied"

    # And the lift is not a no-op: it really did change three lines and nothing
    # more. Counting differing lines pins the SIZE of the divergence, so a
    # fourth substitution cannot be added without this failing.
    $a = $original -split "`n"; $b = $lifted -split "`n"
    Assert ($a.Count -eq $b.Count) "the lift changed the line count: $($a.Count) -> $($b.Count)"
    $diff = 0
    for ($i = 0; $i -lt $a.Count; $i++) { if ($a[$i] -cne $b[$i]) { $diff++ } }
    Assert ($diff -eq 3) "the lift changed $diff line(s); exactly 3 substitutions are declared"
    Write-Host "        $($a.Count) lines lifted, 3 changed, $($a.Count - 3) byte-identical"
}

Check "the production constants are read out of the anchors, not re-typed" {
    $f = Get-NasProductionFacts
    $raw = [IO.File]::ReadAllText($MOUNT)
    # Each derived constant must actually occur in the recovery script. If a
    # constant were mistyped here it would still be self-consistent, so it is
    # checked against the file rather than against itself.
    Assert ($f.CriticalTarget -eq '/library/tv') "critical target derived as '$($f.CriticalTarget)'"
    Assert ($f.FsType -eq '9p') "filesystem type derived as '$($f.FsType)'"
    Assert ($f.OriginTemplate -eq ';path=UNC\TURTLELANDSRV2\$share;') "origin template derived as '$($f.OriginTemplate)'"
    Assert ((Get-NasSubstringCount $raw 'CRITICAL_TARGET="/library/tv"') -eq 1) "the critical target is not in the recovery script as derived"
    Assert ((Get-NasSubstringCount $raw 'TURTLELANDSRV2') -ge 1) "the UNC host is not in the recovery script"
}

# ===========================================================================
# 2. The derived spec vs. an INDEPENDENT source of truth
# ===========================================================================
# docker-compose.yml is what actually creates the binds. mount-nas-shares.ps1
# is what proves them. If those two ever disagree the proof is about a set of
# mounts the container does not have -- so the spec derived from one is checked
# against the other.

Check "every derived mount matches a bind that docker-compose.yml actually declares" {
    $spec = Get-NasSpec -MountScriptPath $MOUNT
    $yaml = [IO.File]::ReadAllText($COMPOSE)
    Assert (@($spec.Mounts).Count -ge 1) "the derived spec has no mounts"
    foreach ($m in $spec.Mounts) {
        $bind = "$($m.HostPath):$($m.Target)"
        if ($m.ReadOnly) { $bind += ':ro' }
        Assert ((Get-NasSubstringCount $yaml $bind) -eq 1) `
            "docker-compose.yml does not declare the derived bind exactly once: $bind"
    }
    # The reverse direction too: compose must not bind a /mnt/nas source the
    # spec does not know about, or that share would be proven by nobody.
    $composeNas = @([regex]::Matches($yaml, '(?m)^\s*-\s*"(/mnt/nas/[^"]+)"') | ForEach-Object { $_.Groups[1].Value })
    Assert ($composeNas.Count -eq @($spec.Mounts).Count) `
        "docker-compose.yml declares $($composeNas.Count) /mnt/nas bind(s) but the derived spec has $(@($spec.Mounts).Count)"
    Write-Host "        $(@($spec.Mounts).Count) mounts agree between the recovery script and docker-compose.yml"
}

Check "the critical read-write destination is the one compose binds WITHOUT :ro" {
    $spec = Get-NasSpec -MountScriptPath $MOUNT
    $yaml = [IO.File]::ReadAllText($COMPOSE)
    $crit = @($spec.Mounts | Where-Object { $_.Target -eq $spec.CriticalTarget })
    Assert ($crit.Count -eq 1) "the spec has $($crit.Count) critical mounts; expected 1"
    Assert (-not $crit[0].ReadOnly) "the critical destination is marked read-only in the derived spec"
    Assert ((Get-NasSubstringCount $yaml "$($crit[0].HostPath):$($crit[0].Target):ro") -eq 0) `
        "docker-compose.yml binds the critical TV destination read-only, which contradicts the spec"
    foreach ($m in @($spec.Mounts | Where-Object { $_.Target -ne $spec.CriticalTarget })) {
        Assert ($m.ReadOnly) "non-critical mount $($m.Target) is not marked read-only"
    }
}

# ===========================================================================
# 2b. The path PRODUCTION actually takes through the engine
# ===========================================================================
# tests/test_deploy_core_docker.ps1 always supplies its own NasMounts, because
# it has no NAS. Production supplies none and the engine derives everything.
# That branch would otherwise be qualified by nothing, so it is executed here
# against the real repository.

Check "the engine's production branch derives exactly the spec the module does" {
    $cfg = New-DeployConfig @{
        Repo = $REPO; PinnedCompose = $COMPOSE; Container = 'unused'; Service = 'scanhound'
        ImageTag = 'unused:latest'; CandidatePrefix = 'unused:c-'; MutexName = 'Global\unused-pin-test'
        NasProbe = $true
    }
    Assert ($null -eq $cfg.NasMounts) "the production config is supposed to derive its mounts, not carry them"

    $viaEngine = Resolve-NasRuntimeSpec -Cfg $cfg -SourceDir $REPO
    $viaModule = Get-NasSpec -MountScriptPath $MOUNT

    Assert ($viaEngine.CriticalTarget -eq $viaModule.CriticalTarget) "critical target differs"
    Assert ($viaEngine.FsType -eq $viaModule.FsType) "filesystem type differs"
    $fmt = { param($m) ($m | ForEach-Object { "$($_.HostPath)|$($_.Target)|$($_.Origin)|$($_.ReadOnly)" }) -join "`n" }
    Assert ((& $fmt $viaEngine.Mounts) -ceq (& $fmt $viaModule.Mounts)) "the derived mount sets differ"
    Assert ($viaEngine.ProbeScript -ceq (Get-NasProbeScript -MountScriptPath $MOUNT)) "the engine is not using the lifted probe"
    Assert ($viaEngine.DataText -ceq (ConvertTo-NasProbeData -Mounts $viaModule.Mounts)) "the engine's probe data differs"
    Assert (@($viaEngine.Targets).Count -eq @($viaModule.Mounts).Count) "the engine's target list is the wrong size"
    Write-Host "        engine and module agree on $(@($viaEngine.Mounts).Count) mounts, fs $($viaEngine.FsType), critical $($viaEngine.CriticalTarget)"
}

Check "CONTROL: a target commit with no recovery script is a refusal, not a skipped proof" {
    $empty = Join-Path $TMP ('nosrc-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
    New-Item -ItemType Directory -Force -Path $empty | Out-Null
    $cfg = New-DeployConfig @{
        Repo = $REPO; PinnedCompose = $COMPOSE; Container = 'unused'; Service = 'scanhound'
        ImageTag = 'unused:latest'; CandidatePrefix = 'unused:c-'; MutexName = 'Global\unused-pin-test'
        NasProbe = $true
    }
    Assert-Refuses { Resolve-NasRuntimeSpec -Cfg $cfg -SourceDir $empty } 'will not invent one' 'the recovery script is missing from the target commit'
}

# ===========================================================================
# 3. Negative controls -- each guard shown to FAIL
# ===========================================================================

Check "CONTROL: changing the expected filesystem type stops the lift" {
    $p = New-MutatedMountScript '    if [ "$fstype" != "9p" ]; then' '    if [ "$fstype" != "virtiofs" ]; then' 'fstype'
    Assert-Refuses { Get-NasProbeScript -MountScriptPath $p } 'expected filesystem type' 'fstype changed'
}

Check "CONTROL: changing the critical target inside the probe stops the lift" {
    $p = New-MutatedMountScript 'CRITICAL_TARGET="/library/tv"' 'CRITICAL_TARGET="/library/tv-new"' 'critical'
    Assert-Refuses { Get-NasProbeScript -MountScriptPath $p } 'critical read-write target' 'critical target changed'
}

Check "CONTROL: changing the UNC host in the origin rule stops the lift" {
    $p = New-MutatedMountScript '    expected=";path=UNC\\TURTLELANDSRV2\\$share;"' '    expected=";path=UNC\\OTHERNAS\\$share;"' 'origin'
    Assert-Refuses { Get-NasProbeScript -MountScriptPath $p } 'expected mount origin' 'origin rule changed'
}

Check "CONTROL: changing the host mount prefix stops the spec being derived" {
    $p = New-MutatedMountScript '_target="/mnt/nas/$1"' '_target="/mnt/shares/$1"' 'hostprefix'
    Assert-Refuses { Get-NasSpec -MountScriptPath $p } 'host-path anchor' 'host mount prefix changed'
}

Check "CONTROL: changing the read-only container-target prefix stops the spec being derived" {
    $p = New-MutatedMountScript '"/library/plex-source/$key"' '"/library/plex/$key"' 'plexprefix'
    Assert-Refuses { Get-NasSpec -MountScriptPath $p } 'container-target anchor' 'plex-source prefix changed'
}

Check "CONTROL: the PowerShell critical target disagreeing with the shell one is refused" {
    # The two live in different languages in the same file and nothing but this
    # check makes them agree. If they diverge, the deploy would prove one path
    # while the recovery task protects another.
    $p = New-MutatedMountScript '$CriticalTarget = "/library/tv"' '$CriticalTarget = "/library/tv-ps"' 'criticalps'
    Assert-Refuses { Get-NasSpec -MountScriptPath $p } 'disagreeing' 'the two critical targets diverged'
}

Check "CONTROL: a second assignment to the probe script is refused rather than guessed at" {
    $text = [IO.File]::ReadAllText($MOUNT)
    $p = Join-Path $TMP ("mount-dup-" + [guid]::NewGuid().ToString('N').Substring(0, 6) + ".ps1")
    [IO.File]::WriteAllText($p, $text + "`n`$probeScript = 'echo not-the-real-rule'`n")
    Assert-Refuses { Get-NasProbeScript -MountScriptPath $p } 'probeScript 2 time(s)' 'the probe script is assigned twice'
}

Check "CONTROL: dropping a share from the recovery script breaks the compose cross-check" {
    # Proves section 2 is load bearing rather than trivially satisfiable.
    $p = New-MutatedMountScript `
        '    "nas-4k-magellan"              = "4K Magellan"' `
        '    # removed for the control' 'dropshare'
    $spec = Get-NasSpec -MountScriptPath $p
    $yaml = [IO.File]::ReadAllText($COMPOSE)
    $composeNas = @([regex]::Matches($yaml, '(?m)^\s*-\s*"(/mnt/nas/[^"]+)"') | ForEach-Object { $_.Groups[1].Value })
    Assert (@($spec.Mounts).Count -ne $composeNas.Count) `
        "a share was dropped and the counts still matched -- the cross-check is vacuous"
    Write-Host "        spec $(@($spec.Mounts).Count) vs compose $($composeNas.Count): the cross-check fires"
}

# ===========================================================================
# 4. Differential against the LIVE rule (opt-in)
# ===========================================================================

if (-not $IncludeLiveNas) {
    Write-Host ""
    Write-Host "-- SKIPPED: live-NAS differential (pass -IncludeLiveNas to run it)" -ForegroundColor Yellow
    $SKIP++
} else {
    Check "the lifted-and-parameterised probe agrees with the ORIGINAL, byte for byte, against the real shares" {
        # The one property the byte comparison in section 1 cannot establish:
        # that the three substituted lines still MEAN what they replaced. Run
        # both rules over the same nine real 9p mounts, in the same container,
        # and require identical verdicts and identical exit codes.
        $ast    = Get-NasMountScriptAst -Path $MOUNT
        $orig   = ((Get-NasScriptString -Ast $ast -Variable 'probeScript' -Path $MOUNT) -replace "`r`n", "`n")
        $shares = Get-NasScriptOrderedMap -Ast $ast -Variable 'shares' -Path $MOUNT
        $spec   = Get-NasSpec -MountScriptPath $MOUNT

        $name = "nas-pin-differential-" + [guid]::NewGuid().ToString('N').Substring(0, 8)
        $argv = @('run', '-d', '--name', $name, '--pull', 'never', '--entrypoint', 'sleep')
        foreach ($m in $spec.Mounts) {
            $b = "$($m.HostPath):$($m.Target)"
            if ($m.ReadOnly) { $b += ':ro' }
            $argv += @('-v', $b)
        }
        $argv += @('python:3.12-slim', '600')
        $c = Native { docker @argv }
        Assert ($c.ExitCode -eq 0) "could not start the differential container: $($c.Text)"

        try {
            $targets = ($spec.Mounts | ForEach-Object { $_.Target }) -join ' '
            $count   = @($spec.Mounts).Count

            # ORIGINAL data shape: target TAB share-name
            $origData = (( $spec.Mounts | ForEach-Object { $i = [array]::IndexOf(@($shares.Keys), (Split-Path $_.HostPath -Leaf)); "$($_.Target)`t$($shares[@($shares.Keys)[$i]])" }) -join "`n") + "`n"
            # LIFTED data shape: target TAB expected-origin
            $newData  = ConvertTo-NasProbeData -Mounts $spec.Mounts

            $dir = Join-Path $TMP 'diff'
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            [IO.File]::WriteAllText((Join-Path $dir 'o.sh'), $orig,     (New-Object Text.ASCIIEncoding))
            [IO.File]::WriteAllText((Join-Path $dir 'o.dat'), $origData, (New-Object Text.ASCIIEncoding))
            [IO.File]::WriteAllText((Join-Path $dir 'n.sh'), (Get-NasProbeScript -MountScriptPath $MOUNT), (New-Object Text.ASCIIEncoding))
            [IO.File]::WriteAllText((Join-Path $dir 'n.dat'), $newData,  (New-Object Text.ASCIIEncoding))
            foreach ($f in @('o.sh','o.dat','n.sh','n.dat')) {
                $cp = Native { docker cp (Join-Path $dir $f) "${name}:/tmp/$f" }
                Assert ($cp.ExitCode -eq 0) "could not copy $f in: $($cp.Text)"
            }

            $ro = Native { docker exec $name sh -c "sh /tmp/o.sh /tmp/o.dat '$count' '$targets' 2>&1" }
            $rn = Native { docker exec $name sh -c "sh /tmp/n.sh /tmp/n.dat '$count' '$targets' '$($spec.CriticalTarget)' '$($spec.FsType)' 2>&1" }

            Write-Host "        original  exit=$($ro.ExitCode)"
            Write-Host "        lifted    exit=$($rn.ExitCode)"
            foreach ($l in @($rn.Output)) { Write-Host "          $l" }
            Assert ($ro.ExitCode -eq $rn.ExitCode) "exit codes differ: original $($ro.ExitCode), lifted $($rn.ExitCode)"
            Assert ($ro.Text -ceq $rn.Text) "verdict lines differ.`n  original:`n$($ro.Text)`n  lifted:`n$($rn.Text)"
            # A differential in which BOTH sides fail everything would agree
            # vacuously. Require the real shares to have actually verified.
            Assert ($rn.ExitCode -eq 0) "both rules agreed, but on a FAILING system -- run mount-nas-shares.ps1 and retry; this control is vacuous otherwise"
        } finally {
            Native { docker rm -f $name } | Out-Null
        }
    }
}

}
finally {
    Remove-Item -LiteralPath $TMP -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host ("== {0} passed, {1} failed, {2} skipped" -f $PASS, $FAIL, $SKIP) -ForegroundColor $(if ($FAIL) { 'Red' } else { 'Green' })
foreach ($f in $FAILED) { Write-Host "   FAILED: $f" -ForegroundColor Red }
exit $(if ($FAIL) { 1 } else { 0 })
