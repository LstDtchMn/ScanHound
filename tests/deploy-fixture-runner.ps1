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

$raw = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$over = @{}
foreach ($p in $raw.PSObject.Properties) {
    $v = $p.Value
    if ($v -is [System.Collections.IEnumerable] -and $v -isnot [string]) { $v = @($v) }
    $over[$p.Name] = $v
}

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

$cfg = New-DeployConfig $over
$result = Invoke-DeployCore -Config $cfg

$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultPath -Encoding UTF8
if ($result.Verdict -eq 'VERIFIED') { exit 0 } else { exit 1 }
