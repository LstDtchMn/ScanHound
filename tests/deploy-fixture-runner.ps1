<#
  Runs scripts/deploy-core.ps1 against a fixture config in a CHILD process, so
  the test suite gets a real process exit code as well as the ledger.

  Why a child process and not an in-process call: the reviewer's cases are
  stated as "deploy exits nonzero". An in-process call can only report a
  verdict string, and a verdict string is exactly the kind of thing that can be
  right while the operator-facing contract is wrong. Both are checked.

  Hooks are named here rather than passed in, so the test seam is a closed set
  visible in one place instead of arbitrary code reaching into the engine.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [Parameter(Mandatory)][string]$ResultPath,
    [string]$Hook = '',
    # Explicit, rather than an ambient environment variable. The first run of
    # case C passed the old image through $env: and it arrived empty, which
    # turned "docker run --entrypoint sleep <image> 600" into a request for an
    # image literally called 600.
    [string]$HookArg = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\deploy-core.ps1')

function ConvertTo-PlainValue {
    <#
      ConvertFrom-Json hands back PSCustomObject, not Hashtable, at every
      level. The engine's NasMounts entries are read with .Target/.Origin, so a
      PSCustomObject would work there by accident -- but Get-NasSpec and
      ConvertTo-NasProbeData index by key, and a config that is a Hashtable at
      the top level and PSCustomObject underneath is exactly the kind of
      difference that makes a fixture stop resembling production. Convert the
      whole tree once.
    #>
    param($V)
    if ($null -eq $V) { return $null }
    if ($V -is [string]) { return $V }
    if ($V -is [System.Management.Automation.PSCustomObject]) {
        $h = @{}
        foreach ($p in $V.PSObject.Properties) { $h[$p.Name] = ConvertTo-PlainValue $p.Value }
        return $h
    }
    if ($V -is [System.Collections.IEnumerable]) {
        return @(@($V) | ForEach-Object { ConvertTo-PlainValue $_ })
    }
    return $V
}

$raw = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$over = @{}
foreach ($p in $raw.PSObject.Properties) { $over[$p.Name] = ConvertTo-PlainValue $p.Value }

# ---- test seams -----------------------------------------------------------
# Case C. Replace the just-activated container with one running a DIFFERENT
# image, under the same name. That is a genuine wrong-running-image state; the
# test does not reimplement the identity check it is trying to qualify.
if ($Hook -eq 'SwapToOldImage') {
    if (-not $HookArg) { throw "SwapToOldImage needs -HookArg <image>" }
    $swapTo = $HookArg
    $over['OnAfterActivate'] = {
        param($c)
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & docker rm -f $c.Container 2>&1 | Out-Null
            & docker run -d --name $c.Container --entrypoint sleep $swapTo 600 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "the case C hook could not start $swapTo" }
        } finally { $ErrorActionPreference = $prev }
    }.GetNewClosure()
}

# SR3-2. Break the container AFTER the post-promotion reconcile, so the case
# that qualifies the final runtime checks creates a genuinely bad final
# container instead of reimplementing the checks it is trying to qualify.
if ($Hook -eq 'StopAfterReconcile') {
    $over['OnAfterReconcile'] = {
        param($c)
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & docker stop -t 1 $c.Container 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "the StopAfterReconcile hook could not stop $($c.Container)" }
        } finally { $ErrorActionPreference = $prev }
    }
}

# R4-101-2. The promotion JOURNAL has to be observed while the transaction is
# still OPEN, and by then the deploy is halfway through a child process the test
# cannot see into. So this hook reads the journal at the one seam that sits
# between the tag move and the revert -- OnAfterReconcile -- writes what it
# found where the test can read it, and THEN breaks the container so the revert
# actually happens. One run therefore shows the journal open, and the file's
# absence afterwards shows it closed.
#
# Reading it here rather than asserting here is deliberate: the hook records a
# fact, the CASE decides what it means. A hook that threw would report as an
# engine failure.
if ($Hook -eq 'ProbeJournalThenStop') {
    if (-not $HookArg) { throw "ProbeJournalThenStop needs -HookArg <path>" }
    $journalOut = $HookArg
    $over['OnAfterReconcile'] = {
        param($c)
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $line = "MISSING"
            try {
                if (Test-Path -LiteralPath $c.PromotionJournal) {
                    $txt = (Get-Content -LiteralPath $c.PromotionJournal -Raw) -replace '\s+', ' '
                    $line = "PRESENT $txt"
                }
            } catch { $line = "UNREADABLE $($_.Exception.Message)" }
            Set-Content -LiteralPath $journalOut -Value $line -Encoding UTF8
            & docker stop -t 1 $c.Container 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "the ProbeJournalThenStop hook could not stop $($c.Container)" }
        } finally { $ErrorActionPreference = $prev }
    }.GetNewClosure()
}

# SR3-5, the RELEASE half. The refusal case holds the deploy lock from the TEST
# side, so all it can see is that the engine ASKS for it; when the engine LETS
# GO is invisible from there, and a mutant that released the lock on the line
# after taking it survived the whole suite because of that.
#
# This hook is how a case sees the other half. It is spawned from INSIDE a run
# that is still in progress and asks a SEPARATE PROCESS to take the same named
# mutex. A separate process is not incidental: a named mutex is re-entrant for
# the thread that already owns it, so a WaitOne issued in THIS process would
# succeed while the lock is held and the case would assert the opposite of what
# it means.
if ($Hook -eq 'ProbeDeployLock') {
    if (-not $HookArg) { throw "ProbeDeployLock needs -HookArg <path>" }
    $probeOut    = $HookArg
    $probeScript = "$HookArg.probe.ps1"
    # Written as a FILE and invoked with -File. The same thing built as a
    # -Command string would put the mutex name through a second round of
    # PowerShell quoting, and the name carries a backslash.
    Set-Content -LiteralPath $probeScript -Encoding ASCII -Value @'
param([Parameter(Mandatory)][string]$Name)
$m = New-Object System.Threading.Mutex($false, $Name)
$got = $false
try { $got = $m.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $got = $true }
if ($got) { try { $m.ReleaseMutex() } catch { } }
$m.Dispose()
if ($got) { Write-Output 'ACQUIRED' } else { Write-Output 'BLOCKED' }
'@
    $probeNow = {
        param($c, $phase)
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $probeScript -Name $c.DeployMutexName 2>&1 |
                   ForEach-Object { $_.ToString() }
            # NO-ANSWER is written rather than swallowed, so a probe that never
            # ran cannot be read as a lock that was held.
            $answer = @($out) | Where-Object { $_ -eq 'ACQUIRED' -or $_ -eq 'BLOCKED' } | Select-Object -Last 1
            if (-not $answer) { $answer = "NO-ANSWER[$(@($out) -join ' / ')]" }
            Add-Content -LiteralPath $probeOut -Value "$phase=$answer"
        } finally { $ErrorActionPreference = $prev }
    }.GetNewClosure()
    # Both seams, because one answer at one moment says nothing about the span.
    $over['OnAfterActivate']  = { param($c) & $probeNow $c 'activate'  }.GetNewClosure()
    $over['OnAfterReconcile'] = { param($c) & $probeNow $c 'reconcile' }.GetNewClosure()
}

$cfg = New-DeployConfig $over
$result = Invoke-DeployCore -Config $cfg

$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultPath -Encoding UTF8

# The same mapping scripts/merge-and-deploy.ps1 uses, so the exit code the
# suite asserts on is the operator-facing one. 'plan only' is a SUCCESS: SR3-7
# is about -WhatIf being production-safe, and a dry run that exits nonzero
# would be reporting a failure that did not happen.
if ($result.Verdict -eq 'VERIFIED' -or $result.Verdict -eq 'plan only') { exit 0 } else { exit 1 }
