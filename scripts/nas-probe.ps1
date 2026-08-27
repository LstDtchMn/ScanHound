<#
.SYNOPSIS
    Storage-identity probes, shared between the deploy engine and anything else
    that needs to prove WHERE a container's bind mounts actually resolve.

.DESCRIPTION
    SR3-1, 2026-08-26. The reviewer's finding was that scripts/deploy-core.ps1
    contained zero references to mountpoint, 9p or /library/tv, and that

        "Compose exit 0 proves a container started, not that its bind mounts
         resolve to the intended shares."

    docker-compose.yml binds the WSL2 path /mnt/nas/nas-tv-blackbeard to
    /library/tv READ-WRITE. That is the TV download / extract / rename
    destination. If the WSL2 mount is absent when the container is created, the
    bind silently resolves to an ordinary directory inside the VM and the
    application writes TV files somewhere Plex will never see. The comment
    block at the top of docker-compose.yml says so, and
    scripts/mount-nas-shares.ps1 exists because it already happened on
    2026-07-26 -- with the Scheduled Task reporting LastTaskResult 0.

    Every runtime check the deploy engine had -- image id, running state, port,
    env, /health, log volume -- passes in that state.

    WHY EXTRACTION RATHER THAN A SECOND COPY OF THE RULE
    ---------------------------------------------------
    scripts/mount-nas-shares.ps1 is a live Scheduled Task (Boot + Logon +
    288x/day, RunLevel Highest). Its behaviour is deliberately NOT changed by
    this file. But re-typing its identity rule here would create the exact
    defect class this review sequence has already found twice: two copies of a
    safety rule that drift apart silently.

    So nothing is re-typed. This module PARSES mount-nas-shares.ps1 and lifts
    the `$probeScript` here-string out of it verbatim, then applies three
    explicitly listed parameterising substitutions so a test fixture can point
    the same rule at its own targets. Each substitution ANCHOR must match
    exactly once or the module throws. That is what makes drift impossible
    rather than merely unlikely:

      * the anchors contain "/library/tv", "9p" and
        ";path=UNC\\TURTLELANDSRV2\\$share;" -- so if any of those three
        production constants is edited in mount-nas-shares.ps1, the anchor stops
        matching and this module fails LOUDLY instead of probing for a stale
        rule;
      * every other line of the rule is used byte-for-byte as written there, so
        it cannot drift at all.

    tests/test_nas_probe_pin.ps1 proves both halves: that the extraction
    succeeds against the real file, and that it FAILS when the rule text is
    changed.

    WHAT "READ-ONLY" MEANS HERE, STATED EXACTLY
    ------------------------------------------
    Nothing in this module mounts, unmounts, recreates, stops or starts the
    ScanHound container, and nothing writes to a host filesystem.

    There is exactly ONE write, and it is the point of the exercise: the lifted
    probe script writes and then deletes a single file
    (.scanhound-mount-probe.<pid>) at the critical read-write target. That is
    the same probe file mount-nas-shares.ps1 already writes to the same share
    every twelve minutes, it is gated on the identity check having PASSED (so
    it can never write into an unverified local directory), and it is deleted
    and its absence re-checked before the probe reports success. A destination
    that cannot be written and cleaned up is not a proven destination, and
    "mounted" is not the same claim as "writable".

.NOTES
    Dot-source this file. It defines functions and no top-level side effects.
#>

# ---------------------------------------------------------------------------
# The parameterising substitutions
# ---------------------------------------------------------------------------
# Applied to the VERBATIM text lifted from mount-nas-shares.ps1's $probeScript.
# Each `Old` must appear exactly once; anything else is a hard error.
#
# Why these three and no others:
#   critical target  the fixture's read-write destination is not /library/tv
#   fstype           a fixture cannot create a 9p share; it proves the SHAPE
#   origin           production's origin is a UNC path in the 9p superblock
#                    options; a fixture's is whatever its own mount reports
#
# After substitution the script's second data column carries the whole expected
# ORIGIN string instead of a bare share name, and the critical target and
# expected filesystem type arrive as positional arguments $4 and $5.
$script:NasProbeSubstitutions = @(
    @{  What = 'critical read-write target'
        Old  = 'CRITICAL_TARGET="/library/tv"'
        New  = 'CRITICAL_TARGET="$4"' }
    @{  What = 'expected filesystem type'
        Old  = '    if [ "$fstype" != "9p" ]; then'
        New  = '    if [ "$fstype" != "$5" ]; then' }
    @{  What = 'expected mount origin'
        Old  = '    expected=";path=UNC\\TURTLELANDSRV2\\$share;"'
        New  = '    expected="$share"' }
)

# Anchors used to lift production constants out of mount-nas-shares.ps1 without
# re-typing them. Same rule: exactly one match or throw.
$script:NasHostPrefixAnchor = '_target="/mnt/nas/$1"'
$script:NasPlexPrefixAnchor = '"/library/plex-source/$key"'

function Get-NasSubstringCount {
    param([string]$Haystack, [string]$Needle)
    if (-not $Needle) { return 0 }
    $n = 0; $i = 0
    while ($true) {
        $i = $Haystack.IndexOf($Needle, $i, [StringComparison]::Ordinal)
        if ($i -lt 0) { break }
        $n++; $i += $Needle.Length
    }
    return $n
}

# ---------------------------------------------------------------------------
# Reading mount-nas-shares.ps1 WITHOUT executing it
# ---------------------------------------------------------------------------
# Dot-sourcing it would run an elevated-Scheduled-Task script that mounts
# shares and can recreate the container. The parser reads the same text and
# runs none of it.

function Get-NasMountScriptAst {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "the recovery script is not at '$Path'; the storage identity rule cannot be read."
    }
    $full = (Resolve-Path -LiteralPath $Path).ProviderPath
    $toks = $null; $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($full, [ref]$toks, [ref]$errs)
    if ($errs -and @($errs).Count -gt 0) {
        throw "'$Path' does not parse ($(@($errs).Count) error(s)); refusing to guess at the storage identity rule."
    }
    return $ast
}

function Resolve-NasConstantAst {
    <# Unwrap pipeline/command wrappers down to a constant expression, or $null. #>
    param($Node)
    $n = $Node
    if ($n -is [System.Management.Automation.Language.PipelineAst]) {
        if (@($n.PipelineElements).Count -ne 1) { return $null }
        $n = $n.PipelineElements[0]
    }
    if ($n -is [System.Management.Automation.Language.CommandExpressionAst]) { $n = $n.Expression }
    if ($n -is [System.Management.Automation.Language.ParenExpressionAst])   { return (Resolve-NasConstantAst $n.Pipeline) }
    return $n
}

function Get-NasScriptAssignment {
    param($Ast, [Parameter(Mandatory)][string]$Variable, [string]$Path)
    $hits = @($Ast.FindAll({
        param($n)
        $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $n.Left.VariablePath.UserPath -eq $Variable
    }, $true))
    if ($hits.Count -ne 1) {
        throw ("'$Path' assigns `$$Variable $($hits.Count) time(s); expected exactly one. " +
               "The storage identity rule cannot be lifted from an ambiguous source.")
    }
    return $hits[0]
}

function Get-NasScriptString {
    <# The literal value of a single-assignment string variable. #>
    param($Ast, [Parameter(Mandatory)][string]$Variable, [string]$Path)
    $e = Resolve-NasConstantAst (Get-NasScriptAssignment -Ast $Ast -Variable $Variable -Path $Path).Right
    if ($e -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) {
        throw "'$Path' does not assign `$$Variable a literal string; it cannot be lifted."
    }
    return $e.Value
}

function Get-NasScriptOrderedMap {
    <# The literal key->value pairs of a single-assignment hashtable variable,
       in source order. #>
    param($Ast, [Parameter(Mandatory)][string]$Variable, [string]$Path)
    $e = Resolve-NasConstantAst (Get-NasScriptAssignment -Ast $Ast -Variable $Variable -Path $Path).Right
    if ($e -is [System.Management.Automation.Language.ConvertExpressionAst]) { $e = $e.Child }
    if ($e -isnot [System.Management.Automation.Language.HashtableAst]) {
        throw "'$Path' does not assign `$$Variable a hashtable literal; it cannot be lifted."
    }
    $out = [ordered]@{}
    foreach ($kv in $e.KeyValuePairs) {
        $k = Resolve-NasConstantAst $kv.Item1
        $v = Resolve-NasConstantAst $kv.Item2
        if ($k -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or
            $v -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) {
            throw "'$Path': `$$Variable contains a non-literal entry; it cannot be lifted."
        }
        $out[$k.Value] = $v.Value
    }
    if ($out.Count -eq 0) { throw "'$Path': `$$Variable is empty." }
    return $out
}

function Get-NasScriptFunctionText {
    param($Ast, [Parameter(Mandatory)][string]$Name, [string]$Path)
    $hits = @($Ast.FindAll({
        param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name
    }, $true))
    if ($hits.Count -ne 1) { throw "'$Path' defines $Name $($hits.Count) time(s); expected exactly one." }
    return $hits[0].Extent.Text
}

# ---------------------------------------------------------------------------
# The probe script
# ---------------------------------------------------------------------------

function Get-NasProbeScript {
    <#
      The container probe, lifted verbatim from mount-nas-shares.ps1 and
      parameterised by exactly the three substitutions listed at the top of
      this file.

      Returns the sh source. Its contract after substitution:

          sh probe.sh <dataFile> <expectedCount> <expectedTargets> \
                      <criticalTarget> <expectedFsType>

      dataFile lines are  <containerTarget>TAB<expectedOriginSubstring>
      exit 0 = every target identity-verified and the critical target proved
               writable and deletable
      exit 1 = a non-critical target failed
      exit 2 = the critical target failed, or coverage failed
    #>
    param([Parameter(Mandatory)][string]$MountScriptPath)

    $ast  = Get-NasMountScriptAst -Path $MountScriptPath
    $text = Get-NasScriptString -Ast $ast -Variable 'probeScript' -Path $MountScriptPath

    foreach ($s in $script:NasProbeSubstitutions) {
        $n = Get-NasSubstringCount $text $s.Old
        if ($n -ne 1) {
            throw ("the $($s.What) anchor matched $n time(s) in '$MountScriptPath'. " +
                   "The recovery script's identity rule has changed shape; this module " +
                   "will not probe with a stale copy of it. Expected exactly one line: " +
                   $s.Old)
        }
        $text = $text.Replace($s.Old, $s.New)
    }
    # LF only: this is copied into a Linux container and run by sh.
    return ($text -replace "`r`n", "`n")
}

function Get-NasProductionFacts {
    <#
      The three production constants, derived from the substitution anchors
      themselves rather than re-typed. If mount-nas-shares.ps1 changes any of
      them, Get-NasProbeScript throws before this is ever reached.
    #>
    $crit = ($script:NasProbeSubstitutions | Where-Object { $_.What -like '*critical*' }).Old
    $fs   = ($script:NasProbeSubstitutions | Where-Object { $_.What -like '*filesystem*' }).Old
    $org  = ($script:NasProbeSubstitutions | Where-Object { $_.What -like '*origin*' }).Old

    $m = [regex]::Match($crit, '"([^"]+)"')
    if (-not $m.Success) { throw "the critical-target anchor is malformed." }
    $criticalTarget = $m.Groups[1].Value

    $m = [regex]::Match($fs, '!=\s*"([^"]+)"')
    if (-not $m.Success) { throw "the fstype anchor is malformed." }
    $fsType = $m.Groups[1].Value

    $m = [regex]::Match($org, '=\s*"(.+)"\s*$')
    if (-not $m.Success) { throw "the origin anchor is malformed." }
    # sh double quotes collapse \\ to \ at runtime, so the literal the script
    # actually compares against has single backslashes.
    $originTemplate = $m.Groups[1].Value.Replace('\\', '\')

    return @{ CriticalTarget = $criticalTarget; FsType = $fsType; OriginTemplate = $originTemplate }
}

function Get-NasSpec {
    <#
      The production storage spec, derived entirely from mount-nas-shares.ps1
      so the deploy engine and the recovery task can never disagree about which
      shares exist, where they come from, or which one is the critical
      read-write destination.

      Returns a hashtable:
        Mounts          @( @{ HostPath; Target; Origin; ReadOnly } ) in source order
        CriticalTarget  the read-write destination
        FsType          the filesystem type every source must report
    #>
    param([Parameter(Mandatory)][string]$MountScriptPath)

    $ast   = Get-NasMountScriptAst -Path $MountScriptPath
    $facts = Get-NasProductionFacts
    # Proves the probe script is liftable before any of the rest is trusted.
    Get-NasProbeScript -MountScriptPath $MountScriptPath | Out-Null

    $shares         = Get-NasScriptOrderedMap -Ast $ast -Variable 'shares'         -Path $MountScriptPath
    $criticalKey    = Get-NasScriptString     -Ast $ast -Variable 'CriticalKey'    -Path $MountScriptPath
    $criticalTarget = Get-NasScriptString     -Ast $ast -Variable 'CriticalTarget' -Path $MountScriptPath
    $hostScript     = Get-NasScriptString     -Ast $ast -Variable 'hostScript'     -Path $MountScriptPath
    $ctFn           = Get-NasScriptFunctionText -Ast $ast -Name 'Get-ContainerTarget' -Path $MountScriptPath

    if ($criticalTarget -ne $facts.CriticalTarget) {
        throw ("mount-nas-shares.ps1's `$CriticalTarget is '$criticalTarget' but its container " +
               "probe hard-codes '$($facts.CriticalTarget)'. Those two disagreeing is itself the bug.")
    }
    if (-not $shares.Contains($criticalKey)) {
        throw "mount-nas-shares.ps1's `$CriticalKey '$criticalKey' is not one of its own shares."
    }

    # The two path prefixes, pinned by anchors rather than re-typed.
    if ((Get-NasSubstringCount $hostScript $script:NasHostPrefixAnchor) -ne 1) {
        throw ("the host-path anchor did not match exactly once in mount-nas-shares.ps1's host " +
               "script. Expected: $($script:NasHostPrefixAnchor)")
    }
    if ((Get-NasSubstringCount $ctFn $script:NasPlexPrefixAnchor) -ne 1) {
        throw ("the read-only container-target anchor did not match exactly once in " +
               "Get-ContainerTarget. Expected: $($script:NasPlexPrefixAnchor)")
    }
    $hostPrefix = ([regex]::Match($script:NasHostPrefixAnchor, '"([^"$]+)\$1"')).Groups[1].Value
    $plexPrefix = ([regex]::Match($script:NasPlexPrefixAnchor, '"([^"$]+)\$key"')).Groups[1].Value
    if (-not $hostPrefix -or -not $plexPrefix) { throw "a path-prefix anchor is malformed." }

    $mounts = @()
    foreach ($key in $shares.Keys) {
        $isCritical = ($key -eq $criticalKey)
        $mounts += @{
            HostPath = "$hostPrefix$key"
            Target   = $(if ($isCritical) { $criticalTarget } else { "$plexPrefix$key" })
            Origin   = $facts.OriginTemplate.Replace('$share', $shares[$key])
            # Only the critical destination is bound read-write, exactly as
            # docker-compose.yml binds it. The probe's write test needs that,
            # and the other eight must NOT be writable from here.
            ReadOnly = (-not $isCritical)
        }
    }
    return @{ Mounts = $mounts; CriticalTarget = $criticalTarget; FsType = $facts.FsType }
}

# ---------------------------------------------------------------------------
# Running the probe
# ---------------------------------------------------------------------------

function ConvertTo-NasProbeData {
    param([Parameter(Mandatory)]$Mounts)
    $lines = @()
    foreach ($m in @($Mounts)) {
        if (-not $m.Target) { throw "a NAS mount entry has no Target." }
        if (-not $m.Origin) { throw "NAS mount '$($m.Target)' has no expected Origin." }
        if ($m.Target -match '\s') { throw "NAS target '$($m.Target)' contains whitespace; the probe's target list is space separated." }
        $lines += ("{0}`t{1}" -f $m.Target, $m.Origin)
    }
    # The trailing newline is load bearing: POSIX `read` returns non-zero on a
    # final line with no terminator and the consuming loop would drop the last
    # record. mount-nas-shares.ps1 carries the same note for the same reason.
    return (($lines -join "`n") + "`n")
}

function Invoke-NasProbeInContainer {
    <#
      Copy the probe into a RUNNING container and execute it there.

      Bounded with a job: a wedged container or daemon makes `docker exec` hang
      rather than return, and a deploy that hangs forever is not a deploy that
      failed safely.

      Returns @{ Reason; Code; Output }. Reason is one of
      probed / not-running / copy-failed / timeout / docker-unavailable.
      Only 'probed' carries a meaningful Code.

      Staging note, stated rather than dressed up: the script and data are
      written to a temp directory owned by the invoking user and then
      docker-cp'd in. mount-nas-shares.ps1 hardens its equivalents because
      wsl.exe executes them AS ROOT from a Windows path; here the payload is
      executed by `docker exec` under the same account that is already running
      the deploy, so a process able to swap the file could simply run docker
      itself. No new authority is granted.
    #>
    param(
        [Parameter(Mandatory)][string]$Container,
        [Parameter(Mandatory)][string]$ScriptText,
        [Parameter(Mandatory)][string]$DataText,
        [Parameter(Mandatory)][string]$CriticalTarget,
        [Parameter(Mandatory)][string]$FsType,
        [Parameter(Mandatory)][string[]]$Targets,
        [int]$TimeoutSec = 90,
        [string]$WorkRoot = $env:TEMP
    )
    $r = [pscustomobject]@{ Reason = 'docker-unavailable'; Code = -1; Output = '' }

    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $stage = $null
    try {
        $global:LASTEXITCODE = 0
        $running = & docker inspect -f '{{.State.Running}}' $Container 2>&1 | ForEach-Object { $_.ToString() }
        if ($LASTEXITCODE -ne 0) { $r.Reason = 'not-running'; return $r }
        if (@($running)[0].Trim() -ne 'true') { $r.Reason = 'not-running'; return $r }

        $stage = Join-Path $WorkRoot ("nasprobe-" + [guid]::NewGuid().ToString('N').Substring(0, 10))
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        $sp = Join-Path $stage 'probe-mounts.sh'
        $dp = Join-Path $stage 'probe-mounts.data'
        [IO.File]::WriteAllText($sp, ($ScriptText -replace "`r`n", "`n"), (New-Object Text.ASCIIEncoding))
        [IO.File]::WriteAllText($dp, ($DataText   -replace "`r`n", "`n"), (New-Object Text.ASCIIEncoding))

        & docker cp $sp "${Container}:/tmp/scanhound-deploy-probe.sh" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { $r.Reason = 'copy-failed'; return $r }
        & docker cp $dp "${Container}:/tmp/scanhound-deploy-probe.data" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { $r.Reason = 'copy-failed'; return $r }

        # -ArgumentList, because a Start-Job scriptblock inherits none of the
        # caller's scope: every one of these would silently be $null inside it,
        # and the probe's own coverage assertion would then compare against an
        # empty string and fail every run. mount-nas-shares.ps1 carries the same
        # note after the same bug.
        # The probe's stderr is merged into its stdout INSIDE the container,
        # not by PowerShell. Redirecting a native program's stderr in
        # PowerShell 5.1 wraps each line in an ErrorRecord, and the multi-line
        # decoration PowerShell then prints ("At line:3 char:20", the caret
        # marker, CategoryInfo, FullyQualifiedErrorId) pushed the probe's own
        # verdict line out of the report. Measured: the read-only-destination
        # case reported five lines of PowerShell decoration and no UNWRITABLE.
        # Merging in the container keeps the shell's own error next to the
        # verdict it caused, in order, with no decoration at all.
        foreach ($a in @($CriticalTarget, $FsType) + @($Targets)) {
            if ("$a".Contains("'")) { throw "a probe argument contains a single quote: $a" }
        }
        $job = Start-Job -ArgumentList $Container, ($Targets -join ' '), $CriticalTarget, $FsType, @($Targets).Count -ScriptBlock {
            param($c, $targetList, $critical, $fs, $count)
            $inner = "sh /tmp/scanhound-deploy-probe.sh /tmp/scanhound-deploy-probe.data " +
                     "'$count' '$targetList' '$critical' '$fs' 2>&1"
            $out = & docker exec $c sh -c $inner
            [pscustomobject]@{ Out = ($out | Out-String); Code = $LASTEXITCODE }
        }
        if (-not (Wait-Job $job -Timeout $TimeoutSec)) {
            Stop-Job $job -ErrorAction SilentlyContinue
            Remove-Job $job -Force -ErrorAction SilentlyContinue
            $r.Reason = 'timeout'
            return $r
        }
        $j = Receive-Job $job
        Remove-Job $job -Force -ErrorAction SilentlyContinue
        $r.Output = "$($j.Out)".TrimEnd()
        $r.Code   = $j.Code
        $r.Reason = 'probed'
        return $r
    } finally {
        $ErrorActionPreference = $prev
        if ($stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Invoke-NasHostSourceProbe {
    <#
      Prove the HOST sources before anything is activated against them.

      A throwaway container binds exactly the source->target set the real
      service uses, and the same probe runs inside it. Measured 2026-08-26 on
      this host: a container binding /mnt/nas/nas-tv-blackbeard reports

        ... - 9p \134\134TURTLELANDSRV2\134k rw,aname=drvfs;path=UNC\TURTLELANDSRV2\k;...

      i.e. the identical identity evidence the live container reports, so this
      measures what Docker will actually resolve at container-create time
      rather than a proxy for it.

      Creating and removing this throwaway container is the only state change,
      and it touches nothing named by the deploy: its name is derived from a
      fresh GUID.
    #>
    param(
        [Parameter(Mandatory)]$Mounts,
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)][string]$ScriptText,
        [Parameter(Mandatory)][string]$CriticalTarget,
        [Parameter(Mandatory)][string]$FsType,
        [int]$TimeoutSec = 90,
        [string]$WorkRoot = $env:TEMP
    )
    $name = "nas-host-probe-" + [guid]::NewGuid().ToString('N').Substring(0, 12)
    $argv = @('run', '-d', '--name', $name, '--pull', 'never', '--entrypoint', 'sleep')
    foreach ($m in @($Mounts)) {
        # An entry with no HostPath is bound by nothing on purpose: the target
        # is then an ordinary directory in the image and the probe must report
        # it BLIND rather than pass it.
        if (-not $m.HostPath) { continue }
        $spec = "$($m.HostPath):$($m.Target)"
        if ($m.ReadOnly) { $spec += ':ro' }
        $argv += @('-v', $spec)
    }
    $argv += @($Image, '600')

    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $out = & docker @argv 2>&1 | ForEach-Object { $_.ToString() }
        if ($LASTEXITCODE -ne 0) {
            return [pscustomobject]@{ Reason = 'host-container-failed'; Code = -1; Output = (@($out) -join "`n") }
        }
        return (Invoke-NasProbeInContainer -Container $name -ScriptText $ScriptText `
                    -DataText (ConvertTo-NasProbeData -Mounts $Mounts) `
                    -CriticalTarget $CriticalTarget -FsType $FsType `
                    -Targets @(@($Mounts) | ForEach-Object { $_.Target }) `
                    -TimeoutSec $TimeoutSec -WorkRoot $WorkRoot)
    } finally {
        & docker rm -f $name 2>&1 | Out-Null
        $ErrorActionPreference = $prev
    }
}
