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

$cfg = New-DeployConfig $over
$result = Invoke-DeployCore -Config $cfg

$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultPath -Encoding UTF8
if ($result.Verdict -eq 'VERIFIED') { exit 0 } else { exit 1 }
