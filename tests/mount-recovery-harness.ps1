<#
  Runs scripts/mount-nas-shares.ps1 -- THE LIVE SCHEDULED TASK -- in a fixture,
  without touching production.

  WHY A HARNESS AND NOT A REWRITE OF THE SCRIPT.

  mount-nas-shares.ps1 is registered Boot + Logon + every 12 minutes at
  RunLevel Highest, and it is the only thing standing between a failed WSL
  mount and ScanHound writing TV files into a VM directory. Two things follow:

    * it must be TESTED, because "changing a safety-critical live task on
      assertion alone" is what this review sequence keeps refusing; and
    * it must not grow test seams. Every parameter added so a test can steer it
      is a parameter an attacker or a mistake can steer it with, and the script
      spends 130 lines pinning its own executables precisely to remove that
      class of steering.

  So nothing here is injected into the script. This harness produces a
  THROWAWAY COPY with a closed, declared set of anchored substitutions, the
  same idiom scripts/nas-probe.ps1 uses to lift the storage rule and
  tests/test_nas_probe_pin.ps1 uses for its negative controls:

    * every anchor must occur EXACTLY ONCE in the real file, or the harness
      refuses. A substitution that silently matched nothing would produce a
      copy that still ran the production paths -- against production;
    * every substitution is confined to the PLUMBING: which executables to
      call, where to stage payloads, which compose recipe and project
      directory to recreate from, which mutex to take, where to log. None of
      them touch a mount check, a probe verdict, or a recreate decision;
    * and Assert-MountPinDecisionRegionIntact proves that: the decision region
      -- from the mount invocation to the end of the file, which is every
      branch this pins -- is BYTE-IDENTICAL between the real script and the
      copy.

  Normalised to LF before anything is matched or compared. core.autocrlf is
  true in this repository, so a fresh worktree is CRLF and the primary checkout
  is LF; a harness that matched multi-line anchors against raw bytes would
  produce a verdict that moved with the checkout, which is not a verdict.
#>

# Deliberately NO Set-StrictMode here. This file is dot-sourced into
# tests/test_deploy_core_docker.ps1, and a mode set in a dot-sourced file
# applies to the scope it is loaded INTO -- so it would silently change how
# every unrelated case in that suite behaves.

# ---------------------------------------------------------------------------
# The declared substitutions
# ---------------------------------------------------------------------------
# @TOKEN@ placeholders are filled from New-PinnedMountScript's parameters.
# Order matters only in that each Old must still be present when its turn
# comes; they are disjoint by construction and each is verified exactly-once
# immediately before it is applied.
$script:MountPinSubstitutions = @(
    @{
        What = 'the pinned wsl.exe'
        Old  = "`$WslExe    = Join-Path `$env:SystemRoot 'System32\wsl.exe'"
        New  = "`$WslExe    = '@WSLEXE@'"
    }
    @{
        What = 'the pinned docker.exe'
        Old  = "`$DockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'"
        New  = "`$DockerExe = '@DOCKEREXE@'"
    }
    @{
        # The fixture's shims live under the user's TEMP and are owned by the
        # user, which is exactly what this assertion exists to reject. It is
        # not what these cases are about, and neutering it is stated rather
        # than worked around.
        What = 'the pinned-executable ACL assertion'
        Old  = "function Assert-PinnedExe([string]`$path, [string]`$label) {"
        New  = "function Assert-PinnedExe([string]`$path, [string]`$label) { return"
    }
    @{
        What = 'the staging-root/staged-file ACL assertion'
        Old  = "function Assert-AdminOwnedNoUserWrite([string]`$path, [string]`$label) {"
        New  = "function Assert-AdminOwnedNoUserWrite([string]`$path, [string]`$label) { return"
    }
    @{
        What = 'the staging root'
        Old  = "`$RunRoot = Join-Path `$env:ProgramData 'ScanHound\run'"
        New  = "`$RunRoot = '@RUNROOT@'"
    }
    @{
        # icacls /setowner BUILTIN\Administrators fails for an unelevated
        # token, and these tests deliberately do not run elevated.
        What = 'the staged-file hardening'
        Old  = @"
    foreach (`$a in @(@('/setowner','BUILTIN\Administrators'), @('/inheritance:r'),
                     @('/grant','BUILTIN\Administrators:(F)'),
                     @('/grant','*S-1-5-18:(F)'))) {
        & icacls.exe `$path @a /q | Out-Null
        if (`$LASTEXITCODE -ne 0) { throw "icacls failed hardening staged file `$path (`$(`$a -join ' '))." }
    }
"@
        # Padded to the SAME number of lines as the anchor it replaces, so the
        # copy and the real script stay line-for-line aligned and a changed-line
        # count is a meaningful number rather than an offset artefact.
        New  = @"
    # pinned copy: the staged-file hardening below is not applied, because
    # icacls /setowner BUILTIN\Administrators requires an elevated token and
    # these cases deliberately do not run elevated. Six lines, matching the
    # anchor, so the copy stays line-aligned with the real script.
    #
    #
"@
    }
    @{
        What = 'the staging-directory hardening'
        Old  = @"
        foreach (`$a in @(@('/setowner','BUILTIN\Administrators'), @('/inheritance:r'),
                         @('/grant','BUILTIN\Administrators:(OI)(CI)(F)'),
                         @('/grant','*S-1-5-18:(OI)(CI)(F)'))) {
            & icacls.exe `$dir @a /q | Out-Null
            if (`$LASTEXITCODE -ne 0) { throw "icacls failed hardening `$dir (`$(`$a -join ' '))." }
        }
"@
        New  = @"
        # pinned copy: the staging-directory hardening below is not applied,
        # for the same reason as the staged-file hardening above -- icacls
        # /setowner BUILTIN\Administrators needs an elevated token. Six lines,
        # matching the anchor, so the copy stays line-aligned.
        #
        #
"@
    }
    @{
        What = 'the pinned recovery recipe'
        Old  = "`$ComposeFile       = `"C:\ProgramData\ScanHound\deploy\docker-compose.yml`""
        New  = "`$ComposeFile       = '@COMPOSEFILE@'"
    }
    @{
        What = 'the compose project directory'
        Old  = "`$ComposeProjectDir = `"X:\Docker Apps\ScanHound`""
        New  = "`$ComposeProjectDir = '@PROJECTDIR@'"
    }
    @{
        # THE most important substitution. Global\ScanHound-MountNASShares is
        # held by the live Scheduled Task. A copy that took the real name would
        # either block a real recovery pass or -- far worse -- exit 0 as "another
        # instance is already running" and pin nothing at all.
        What = 'the recovery mutex name'
        Old  = "`$mutex = New-Object System.Threading.Mutex(`$false, `"Global\ScanHound-MountNASShares`")"
        New  = "`$mutex = New-Object System.Threading.Mutex(`$false, '@MUTEX@')"
    }
    @{
        What = 'the run log'
        Old  = "`$MountLog = 'C:\ProgramData\ScanHound\logs\mount-nas-shares.log'"
        New  = "`$MountLog = '@MOUNTLOG@'"
    }
    @{
        # R5-101-1. The only tag the recovery task will ever move. A fixture
        # cannot be allowed to name scanhound:latest, so this is substituted
        # rather than steered by the journal -- which is the point of the
        # constant existing in the first place.
        What = 'the recovery image tag'
        Old  = "`$RecoveryImageTag = 'scanhound:latest'"
        New  = "`$RecoveryImageTag = '@IMAGETAG@'"
    }
    @{
        # 90 s is right in production and would make the one case that pins the
        # TIMEOUT branch take a minute and a half. The branch is what is being
        # pinned, not the number.
        What = 'the container-probe timeout default'
        Old  = "function Invoke-ContainerProbe([int]`$TimeoutSec = 90) {"
        New  = "function Invoke-ContainerProbe([int]`$TimeoutSec = @PROBETIMEOUT@) {"
    }
    @{
        # Same reason, for the write-guard probe added 2026-09-01: the HANG
        # case would otherwise take a minute and a half per run.
        What = 'the guard-probe timeout default'
        Old  = "function Invoke-ContainerGuardProbe([int]`$TimeoutSec = 90) {"
        New  = "function Invoke-ContainerGuardProbe([int]`$TimeoutSec = @PROBETIMEOUT@) {"
    }
)

function Get-MountPinText {
    param([Parameter(Mandatory)][string]$Path)
    return ([IO.File]::ReadAllText($Path) -replace "`r`n", "`n")
}

function Get-MountPinAnchor {
    <#
      Normalise a multi-line anchor to LF before it is matched against a copy.

      MEASURED, not defensive. core.autocrlf is true in this repository, so a
      here-string written inside a CRLF-checked-out .ps1 carries CRLF, while
      New-PinnedMountScript normalises the script it copies to LF. A multi-line
      anchor then matches ZERO times, and a control that exists to reproduce a
      defect reports "this would prove nothing" instead of reproducing it.

      It happened, and it gave two different verdicts for the same code purely
      from how each file was stored: the controls in tests/test_mount_safety_pin.ps1
      (a new file, LF) passed, while the identical mechanism in
      tests/test_deploy_core_docker.ps1 (tracked, therefore CRLF) silently
      matched nothing. Every anchor goes through here so the verdict cannot
      move with the checkout.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    return ($Text -replace "`r`n", "`n")
}

function Get-MountPinSubstringCount {
    param([string]$Text, [string]$Needle)
    $n = 0; $i = 0
    while ($true) {
        $j = $Text.IndexOf($Needle, $i, [StringComparison]::Ordinal)
        if ($j -lt 0) { break }
        $n++; $i = $j + 1
    }
    return $n
}

# The first line of the region every pinned case is about. Everything from here
# to the end of the file is the mount invocation, the verdict allow-list, the
# indeterminate branch, the critical-failure branch, the recreate decision, the
# recreate itself and the post-recreate gate.
$script:MountPinDecisionAnchor = 'Write-Host "Mounting NAS shares inside the docker-desktop WSL2 distro..."'

function Get-MountPinDecisionRegion {
    param([Parameter(Mandatory)][string]$Text)
    $i = $Text.IndexOf($script:MountPinDecisionAnchor, [StringComparison]::Ordinal)
    if ($i -lt 0) { throw "the decision-region anchor is not in the script: $($script:MountPinDecisionAnchor)" }
    return $Text.Substring($i)
}

function New-PinnedMountScript {
    <#
      A throwaway copy of the recovery script with exactly the declared
      substitutions applied, and nothing else.

      Returns a PSCustomObject with the copy's Path, the number of LINES that
      differ from the real script, and both texts, so a caller can assert on
      the size and the location of the divergence rather than take it on trust.
    #>
    param(
        [Parameter(Mandatory)][string]$MountScriptPath,
        [Parameter(Mandatory)][string]$OutDir,
        [Parameter(Mandatory)][string]$WslExe,
        [Parameter(Mandatory)][string]$DockerExe,
        [Parameter(Mandatory)][string]$RunRoot,
        [Parameter(Mandatory)][string]$ComposeFile,
        [Parameter(Mandatory)][string]$ProjectDir,
        [Parameter(Mandatory)][string]$MutexName,
        [Parameter(Mandatory)][string]$MountLog,
        [Parameter(Mandatory)][string]$ImageTag,
        [int]$ProbeTimeoutSec = 8
    )
    if (-not (Test-Path -LiteralPath $MountScriptPath)) {
        throw "the recovery script is missing at $MountScriptPath"
    }
    $orig = Get-MountPinText -Path $MountScriptPath
    $text = $orig
    $tokens = @{
        '@WSLEXE@'       = $WslExe
        '@DOCKEREXE@'    = $DockerExe
        '@RUNROOT@'      = $RunRoot
        '@COMPOSEFILE@'  = $ComposeFile
        '@PROJECTDIR@'   = $ProjectDir
        '@MUTEX@'        = $MutexName
        '@MOUNTLOG@'     = $MountLog
        '@IMAGETAG@'     = $ImageTag
        '@PROBETIMEOUT@' = "$ProbeTimeoutSec"
    }
    foreach ($s in $script:MountPinSubstitutions) {
        # Round-6 verifier D2. $s.Old and $s.New are here-strings written in
        # THIS file, so they carry whatever line ending the checkout gave this
        # file -- CRLF on any fresh worktree -- while $text was normalised to
        # LF by Get-MountPinText. Matched raw, a multi-line anchor occurred 0
        # times, the throw below fired in every fresh checkout and never on the
        # author's LF tree, and tests/test_mount_safety_pin.ps1 died in setup
        # with 0 tests run while 28/0 was being reported from an LF tree. One
        # layer below the control that claimed every anchor already went
        # through Get-MountPinAnchor: now every one does, on both sides.
        $old = Get-MountPinAnchor $s.Old
        $n = Get-MountPinSubstringCount $text $old
        if ($n -ne 1) {
            throw ("the anchor for '$($s.What)' occurs $n time(s) in $MountScriptPath. " +
                   "A harness that silently matched nothing would run the PRODUCTION path " +
                   "against production; the anchor has to be repaired before these cases mean anything.")
        }
        $new = Get-MountPinAnchor $s.New
        foreach ($k in $tokens.Keys) { $new = $new.Replace($k, $tokens[$k]) }
        if ($new -match "@[A-Z]+@") { throw "an unresolved placeholder is left in the substitution for '$($s.What)': $new" }
        $text = $text.Replace($old, $new)
    }
    if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
    $p = Join-Path $OutDir ("mount-pinned-" + [guid]::NewGuid().ToString('N').Substring(0, 8) + ".ps1")
    [IO.File]::WriteAllText($p, $text, (New-Object Text.UTF8Encoding($false)))

    $a = $orig -split "`n"; $b = $text -split "`n"
    $changed = 0
    $max = [Math]::Max($a.Count, $b.Count)
    for ($i = 0; $i -lt $max; $i++) {
        $x = $(if ($i -lt $a.Count) { $a[$i] } else { $null })
        $y = $(if ($i -lt $b.Count) { $b[$i] } else { $null })
        if ($x -cne $y) { $changed++ }
    }
    # How many lines the DECLARED substitutions can account for. Derived from
    # the anchors themselves rather than typed as a constant: a hard-coded
    # number is satisfied by coincidence the moment the set changes, and this
    # one has to fail loudly when it does. Every New is written to differ on
    # every line of its Old, so the two numbers must be equal.
    $declared = 0
    foreach ($s in $script:MountPinSubstitutions) {
        $declared += @(($s.Old -replace "`n$", '') -split "`n").Count
    }
    return [pscustomobject]@{
        Path         = $p
        Original     = $orig
        Pinned       = $text
        ChangedLines = $changed
        DeclaredChangedLines = $declared
        OriginalLines = $a.Count
        PinnedLines   = $b.Count
    }
}

function Assert-MountPinDecisionRegionIntact {
    <#
      The claim the whole harness rests on: nothing in the decision region was
      substituted. If a future substitution ever reaches into it, every case
      built on this harness stops describing the live script -- silently --
      and this is what says so.
    #>
    param([Parameter(Mandatory)]$Build)
    $o = Get-MountPinDecisionRegion -Text $Build.Original
    $p = Get-MountPinDecisionRegion -Text $Build.Pinned
    if ($o -cne $p) {
        $oa = $o -split "`n"; $pa = $p -split "`n"
        $first = 'unknown'
        for ($i = 0; $i -lt [Math]::Max($oa.Count, $pa.Count); $i++) {
            $x = $(if ($i -lt $oa.Count) { $oa[$i] } else { '<end>' })
            $y = $(if ($i -lt $pa.Count) { $pa[$i] } else { '<end>' })
            if ($x -cne $y) { $first = "line $($i + 1) of the region:`n    real:   $x`n    pinned: $y"; break }
        }
        throw ("a substitution reached into the DECISION REGION, so these cases no longer " +
               "describe the live recovery task. First difference at $first")
    }
    return $true
}

# ---------------------------------------------------------------------------
# The shims
# ---------------------------------------------------------------------------

function New-MountPinShims {
    <#
      Writes the two executables the pinned copy calls.

      wsl shim   -- ignores its arguments and exits with SH_PIN_WSL_RC, which
                    is how a case chooses the host mount stage's verdict
                    (0 all verified / 1 read-only share failed / 2 critical or
                    coverage failure / anything else = indeterminate).

      docker shim -- two modes.
        fake: answers ps / cp / exec / stop / compose / tag / image inspect
              from environment variables. No Docker daemon is involved, so the
              decision table can be pinned exhaustively and in seconds.
        real: TRANSLATES the container name and forwards to the real
              docker.exe. The recovery script names its container literally, so
              this is what lets the C1-C5 cases run the real consumer against a
              real fixture container without ever naming the production one. A
              request that still names 'scanhound' after translation is
              REFUSED, not forwarded -- the one accident this whole file must
              not have.

      Both append every invocation to a transcript, so a case asserts on what
      was actually asked of Docker rather than on what it assumes happened.
    #>
    param(
        [Parameter(Mandatory)][string]$Dir,
        [Parameter(Mandatory)][string]$Transcript,
        [string]$RealDocker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
    )
    if (-not (Test-Path -LiteralPath $Dir)) { New-Item -ItemType Directory -Force -Path $Dir | Out-Null }

    $wsl = Join-Path $Dir 'wsl-shim.ps1'
    [IO.File]::WriteAllText($wsl, @'
$rc = 0
if ($env:SH_PIN_WSL_RC) { $rc = [int]$env:SH_PIN_WSL_RC }
try { Add-Content -LiteralPath $env:SH_PIN_TRANSCRIPT -Value ("wsl rc=$rc :: " + ($args -join ' ')) } catch { }
Write-Output "RESULT: pinned wsl shim returning $rc"
exit $rc
'@, (New-Object Text.UTF8Encoding($false)))

    $docker = Join-Path $Dir 'docker-shim.ps1'
    [IO.File]::WriteAllText($docker, @'
# Fixture stand-in for docker.exe. See New-MountPinShims in
# tests/mount-recovery-harness.ps1 for why this exists.
$ErrorActionPreference = 'Continue'
$ctr  = $env:SH_PIN_CTR
$mode = $env:SH_PIN_DOCKER_MODE
$flag = $env:SH_PIN_STOPFLAG

# The container name the recovery script uses is a LITERAL. Translate it here,
# and refuse anything that still names it afterwards.
$mapped = @()
foreach ($a in $args) {
    $s = "$a"
    if ($s -eq 'scanhound')          { $mapped += $ctr; continue }
    if ($s -like 'scanhound:*')      { $mapped += ($ctr + $s.Substring(9)); continue }
    if ($s -eq 'name=^scanhound$')   { $mapped += ("name=^" + $ctr + '$'); continue }
    $mapped += $s
}
try { Add-Content -LiteralPath $env:SH_PIN_TRANSCRIPT -Value ("docker[$mode] :: " + ($mapped -join ' ')) } catch { }
foreach ($m in $mapped) {
    if ($m -eq 'scanhound' -or $m -like 'scanhound:*' -or $m -eq 'name=^scanhound$') {
        try { Add-Content -LiteralPath $env:SH_PIN_TRANSCRIPT -Value "REFUSED: an argument still names the PRODUCTION container" } catch { }
        Write-Error "the pinned docker shim refused to forward a command naming the production container"
        exit 125
    }
}

function Get-Rc([string]$name, [int]$dflt) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($null -eq $v -or $v -eq '') { return $dflt }
    return [int]$v
}

if ($mode -eq 'real') {
    # The ONE thing real Docker cannot answer here: the in-container nine-share
    # probe. Its rule asks for 9p mounts whose superblock options carry
    # path=UNC\TURTLELANDSRV2\<share>, and a test fixture has no NAS and no WSL
    # distro to mount one in. So `docker exec` -- and only `docker exec` -- is
    # answered locally when SH_PIN_EXEC_STUB_RC is set. The same substitution
    # tests/test_deploy_core_docker.ps1 already makes for SR3-1, where ext4
    # named volumes stand in for 9p shares.
    #
    # Everything else is REAL: the images, the tag, the container, the compose
    # recreate, docker cp into a live container. The cases built on this are
    # about the promotion transaction, and every part of THAT is real.
    if ($mapped[0] -eq 'exec' -and (($mapped -join ' ') -match 'share_identity')) {
        # The write-guard probe (2026-09-01). Absent unless a case says
        # otherwise: rc 1 and no version, which the task reads as "no guard".
        $ghang = Get-Rc 'SH_PIN_GUARD_HANG' 0
        if ($ghang -gt 0) { Start-Sleep -Seconds $ghang }
        if ($env:SH_PIN_GUARD_OUT) { Write-Output $env:SH_PIN_GUARD_OUT }
        exit (Get-Rc 'SH_PIN_GUARD_RC' 1)
    }
    if ($mapped[0] -eq 'exec' -and "$($env:SH_PIN_EXEC_STUB_RC)" -ne '') {
        Write-Output "STUBBED in-container probe: this fixture has no 9p NAS shares"
        exit ([int]$env:SH_PIN_EXEC_STUB_RC)
    }
    $real = $env:SH_PIN_REAL_DOCKER
    & $real @mapped
    exit $LASTEXITCODE
}

$verb = "$($mapped[0])"
switch ($verb) {
    'ps' {
        # STOPPED is checked before RECREATED: a stop issued after a recreate
        # must be visible, or a post-recreate stop would read as "still up".
        # `compose` clears the stop flag, so a recreate genuinely undoes it.
        $names = $env:SH_PIN_PS
        if ($flag -and (Test-Path -LiteralPath $flag)) { $names = $env:SH_PIN_PS_AFTER }
        elseif ($env:SH_PIN_RECREATEFLAG -and (Test-Path -LiteralPath $env:SH_PIN_RECREATEFLAG) -and $env:SH_PIN_PS_RECREATED) {
            $names = $env:SH_PIN_PS_RECREATED
        }
        if ($names) { Write-Output $names }
        exit (Get-Rc 'SH_PIN_PS_RC' 0)
    }
    'cp'   {
        $recreated = ($env:SH_PIN_RECREATEFLAG -and (Test-Path -LiteralPath $env:SH_PIN_RECREATEFLAG))
        if ($recreated) { exit (Get-Rc 'SH_PIN_CP_RC_AFTER' (Get-Rc 'SH_PIN_CP_RC' 0)) }
        exit (Get-Rc 'SH_PIN_CP_RC' 0)
    }
    'exec' {
        if (($mapped -join ' ') -match 'share_identity') {
            # The write-guard probe (2026-09-01), answered separately from the
            # nine-share probe so a case can give the two different answers.
            # Absent by default: rc 1, no output.
            $ghang = Get-Rc 'SH_PIN_GUARD_HANG' 0
            if ($ghang -gt 0) { Start-Sleep -Seconds $ghang }
            if ($env:SH_PIN_GUARD_OUT) { Write-Output $env:SH_PIN_GUARD_OUT }
            exit (Get-Rc 'SH_PIN_GUARD_RC' 1)
        }
        $recreated = ($env:SH_PIN_RECREATEFLAG -and (Test-Path -LiteralPath $env:SH_PIN_RECREATEFLAG))
        $hang = $(if ($recreated) { Get-Rc 'SH_PIN_EXEC_HANG_AFTER' 0 } else { Get-Rc 'SH_PIN_EXEC_HANG' 0 })
        if ($hang -gt 0) { Start-Sleep -Seconds $hang }
        if ($recreated -and $env:SH_PIN_EXEC_OUT_AFTER) { Write-Output $env:SH_PIN_EXEC_OUT_AFTER }
        elseif ($env:SH_PIN_EXEC_OUT)                   { Write-Output $env:SH_PIN_EXEC_OUT }
        if ($recreated) { exit (Get-Rc 'SH_PIN_EXEC_RC_AFTER' (Get-Rc 'SH_PIN_EXEC_RC' 0)) }
        exit (Get-Rc 'SH_PIN_EXEC_RC' 0)
    }
    'stop' {
        $rc = Get-Rc 'SH_PIN_STOP_RC' 0
        if ($rc -eq 0 -and $flag) { Set-Content -LiteralPath $flag -Value 'stopped' }
        exit $rc
    }
    'compose' {
        if ($env:SH_PIN_RECREATEFLAG) { Set-Content -LiteralPath $env:SH_PIN_RECREATEFLAG -Value 'recreated' }
        if ($flag -and (Test-Path -LiteralPath $flag)) { Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue }
        exit (Get-Rc 'SH_PIN_COMPOSE_RC' 0)
    }
    'tag' {
        if ($env:SH_PIN_TAGFILE) { Add-Content -LiteralPath $env:SH_PIN_TAGFILE -Value ($mapped -join ' ') }
        exit (Get-Rc 'SH_PIN_TAG_RC' 0)
    }
    'image' {
        if ($env:SH_PIN_IMAGE_OUT) { Write-Output $env:SH_PIN_IMAGE_OUT }
        exit (Get-Rc 'SH_PIN_IMAGE_RC' 0)
    }
}
exit 0
'@, (New-Object Text.UTF8Encoding($false)))

    return [pscustomobject]@{ Wsl = $wsl; Docker = $docker; RealDocker = $RealDocker; Transcript = $Transcript }
}

function Invoke-PinnedMountScript {
    <#
      Runs the pinned copy in a CHILD process, so what a case observes is a
      real process exit code -- the same thing Task Scheduler records as
      LastTaskResult -- and not a variable.
    #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [hashtable]$Env = @{},
        [int]$TimeoutSec = 240
    )
    $prev = @{}
    foreach ($k in $Env.Keys) {
        $prev[$k] = [Environment]::GetEnvironmentVariable($k)
        [Environment]::SetEnvironmentVariable($k, "$($Env[$k])")
    }
    try {
        $ea = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $global:LASTEXITCODE = 0
            $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $Path 2>&1 |
                   ForEach-Object { $_.ToString() }
            $code = $LASTEXITCODE
        } finally { $ErrorActionPreference = $ea }
    } finally {
        foreach ($k in $prev.Keys) { [Environment]::SetEnvironmentVariable($k, $prev[$k]) }
    }
    return [pscustomobject]@{ ExitCode = $code; Output = @($out); Text = (@($out) -join "`n") }
}
