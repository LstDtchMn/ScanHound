<#
  Failure-injection tests for scripts/claude-permissions.ps1.

  The review's verdict was: keep this unused until its destructive/undo paths
  have failure-injection tests. This is that. Every case runs against a
  DISPOSABLE fixture; the user's real settings.json is never touched.

  What these exist to catch, all of which the previous version got wrong:

    * a BOM written into the live file (the original production incident);
    * -Revoke announcing success without proving the rules are gone;
    * plain -Revoke leaving -IncludeDeploy rules behind while the help said
      it removed "the rules this script adds";
    * a bad candidate being committed and only then discovered.

  Run:  powershell -ExecutionPolicy Bypass -File tests\test_claude_permissions_script.ps1
#>

$ErrorActionPreference = 'Stop'
$SCRIPT = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\claude-permissions.ps1'
if (-not (Test-Path $SCRIPT)) { throw "cannot find $SCRIPT" }

$PASS = 0; $FAIL = 0
function Check([string]$name, [scriptblock]$body) {
    try {
        & $body
        $script:PASS++
        Write-Host ("  PASS  {0}" -f $name) -ForegroundColor Green
    } catch {
        $script:FAIL++
        Write-Host ("  FAIL  {0}`n          {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
    }
}

function New-Fixture {
    <# A settings file shaped like the real one: an allow list plus unrelated
       top-level keys that must survive every operation. #>
    param([string[]]$Allow = @('Bash(dir:*)', 'Bash(git add:*)', 'Bash(docker compose:*)'))
    $p = Join-Path $env:TEMP ("perm-fixture-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,8))
    $obj = [ordered]@{
        permissions = [ordered]@{ allow = $Allow; additionalDirectories = @('C:\somewhere') }
        model       = 'claude-opus-5'
        theme       = 'dark'
    }
    [System.IO.File]::WriteAllText($p, ($obj | ConvertTo-Json -Depth 20),
        (New-Object System.Text.UTF8Encoding($false)))
    return $p
}

function Invoke-Script { param([string[]]$ScriptArgs)
    & powershell -NoProfile -ExecutionPolicy Bypass -File $SCRIPT @ScriptArgs 2>&1 | Out-String
}
function Get-Allow([string]$p) {
    @(([System.IO.File]::ReadAllText($p) | ConvertFrom-Json).permissions.allow)
}
function Test-HasBom([string]$p) {
    $b = [System.IO.File]::ReadAllBytes($p)
    return ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)
}

Write-Host ""
Write-Host "== claude-permissions.ps1 -- failure injection" -ForegroundColor Cyan

# ---------------------------------------------------------------- grant ----
Check "grant adds the merge rule" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    if ((Get-Allow $f) -notcontains 'Bash(gh pr merge:*)') { throw "rule not added" }
    Remove-Item $f -Force
}

Check "grant writes NO BOM (the original production incident)" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    if (Test-HasBom $f) { throw "a BOM was written -- the exact defect this replaces" }
    Remove-Item $f -Force
}

Check "grant leaves the file strictly parseable" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    $null = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    Remove-Item $f -Force
}

Check "grant preserves unrelated settings" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    $o = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    if ($o.model -ne 'claude-opus-5') { throw "lost 'model'" }
    if ($o.theme -ne 'dark')          { throw "lost 'theme'" }
    if (-not $o.permissions.additionalDirectories) { throw "lost additionalDirectories" }
    Remove-Item $f -Force
}

Check "grant preserves pre-existing rules" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    foreach ($r in @('Bash(dir:*)', 'Bash(git add:*)', 'Bash(docker compose:*)')) {
        if ((Get-Allow $f) -notcontains $r) { throw "lost $r" }
    }
    Remove-Item $f -Force
}

Check "grant is idempotent" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    $n1 = (Get-Allow $f).Count
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    if ((Get-Allow $f).Count -ne $n1) { throw "second run changed the list" }
    Remove-Item $f -Force
}

Check "-WhatIf writes nothing" {
    $f = New-Fixture
    $before = [System.IO.File]::ReadAllBytes($f)
    Invoke-Script @('-SettingsPath', $f, '-WhatIf') | Out-Null
    $after = [System.IO.File]::ReadAllBytes($f)
    if (Compare-Object $before $after) { throw "-WhatIf modified the file" }
    Remove-Item $f -Force
}

# --------------------------------------------------------------- revoke ----
Check "revoke removes the merge rule" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    Invoke-Script @('-SettingsPath', $f, '-Revoke') | Out-Null
    if ((Get-Allow $f) -contains 'Bash(gh pr merge:*)') { throw "rule survived revoke" }
    Remove-Item $f -Force
}

Check "PLAIN revoke also removes -IncludeDeploy rules (OPS-6)" {
    # The old script only removed deploy rules if you repeated -IncludeDeploy,
    # while its help said -Revoke removed "the rules this script adds". A
    # standing authorization survived an undo the user believed was complete.
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f, '-IncludeDeploy') | Out-Null
    $granted = Get-Allow $f
    if ($granted -notcontains 'Bash(docker restart:*)') { throw "fixture setup: deploy rules not granted" }
    Invoke-Script @('-SettingsPath', $f, '-Revoke') | Out-Null
    $left = Get-Allow $f
    foreach ($r in @('Bash(gh pr merge:*)', 'Bash(docker compose up:*)',
                     'Bash(docker compose build:*)', 'Bash(docker restart:*)')) {
        if ($left -contains $r) { throw "plain -Revoke left $r behind" }
    }
    Remove-Item $f -Force
}

Check "revoke preserves unrelated rules and settings" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f, '-IncludeDeploy') | Out-Null
    Invoke-Script @('-SettingsPath', $f, '-Revoke') | Out-Null
    $o = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    foreach ($r in @('Bash(dir:*)', 'Bash(git add:*)', 'Bash(docker compose:*)')) {
        if (@($o.permissions.allow) -notcontains $r) { throw "revoke removed unrelated rule $r" }
    }
    if ($o.model -ne 'claude-opus-5') { throw "revoke lost 'model'" }
    Remove-Item $f -Force
}

Check "revoke writes NO BOM and leaves valid JSON" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    Invoke-Script @('-SettingsPath', $f, '-Revoke') | Out-Null
    if (Test-HasBom $f) { throw "revoke wrote a BOM" }
    $null = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    Remove-Item $f -Force
}

Check "revoke on a clean file is a no-op, not an error" {
    $f = New-Fixture
    $before = [System.IO.File]::ReadAllBytes($f)
    Invoke-Script @('-SettingsPath', $f, '-Revoke') | Out-Null
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($f))) { throw "modified a file with nothing to remove" }
    Remove-Item $f -Force
}

Check "grant -> revoke -> grant round-trips" {
    $f = New-Fixture
    $start = (Get-Allow $f).Count
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    Invoke-Script @('-SettingsPath', $f, '-Revoke') | Out-Null
    if ((Get-Allow $f).Count -ne $start) { throw "revoke did not restore the original count" }
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    if ((Get-Allow $f) -notcontains 'Bash(gh pr merge:*)') { throw "re-grant failed" }
    Remove-Item $f -Force
}

# ------------------------------------------------------- refusal paths -----
Check "a missing settings file is refused, not created" {
    $p = Join-Path $env:TEMP ("perm-absent-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,6))
    Invoke-Script @('-SettingsPath', $p) | Out-Null
    if (Test-Path $p) { throw "the script CREATED a settings file that did not exist" }
}

Check "unparseable JSON is refused and left untouched" {
    $p = Join-Path $env:TEMP ("perm-bad-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,6))
    [System.IO.File]::WriteAllText($p, "{ this is not json", (New-Object System.Text.UTF8Encoding($false)))
    $before = [System.IO.File]::ReadAllBytes($p)
    Invoke-Script @('-SettingsPath', $p) | Out-Null
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "modified a file it could not parse" }
    Remove-Item $p -Force
}

Check "a file with no permissions section is refused" {
    $p = Join-Path $env:TEMP ("perm-noperm-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,6))
    [System.IO.File]::WriteAllText($p, '{"model":"x"}', (New-Object System.Text.UTF8Encoding($false)))
    $before = [System.IO.File]::ReadAllBytes($p)
    Invoke-Script @('-SettingsPath', $p) | Out-Null
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "modified a file with no permissions section" }
    Remove-Item $p -Force
}

Check "an EXISTING BOM is detected and reported" {
    $f = New-Fixture
    $bytes = [System.IO.File]::ReadAllBytes($f)
    $withBom = ,([byte]0xEF) + ,([byte]0xBB) + ,([byte]0xBF) + $bytes
    [System.IO.File]::WriteAllBytes($f, $withBom)
    $out = Invoke-Script @('-SettingsPath', $f, '-WhatIf')
    if ($out -notmatch 'BOM') { throw "an existing BOM was not reported" }
    Remove-Item $f -Force
}

Check "a backup is written before any change" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    $baks = @(Get-ChildItem -Path (Split-Path $f) -Filter ((Split-Path $f -Leaf) + '.bak-*') -ErrorAction SilentlyContinue)
    if ($baks.Count -eq 0) { throw "no backup was written" }
    $baks | Remove-Item -Force
    Remove-Item $f -Force
}

Check "no candidate temp files are left behind" {
    $f = New-Fixture
    Invoke-Script @('-SettingsPath', $f) | Out-Null
    $leftovers = @(Get-ChildItem -Path (Split-Path $f) -Filter ((Split-Path $f -Leaf) + '.candidate-*') -ErrorAction SilentlyContinue)
    if ($leftovers.Count -gt 0) { throw "$($leftovers.Count) candidate file(s) left behind" }
    Get-ChildItem -Path (Split-Path $f) -Filter ((Split-Path $f -Leaf) + '.bak-*') -ErrorAction SilentlyContinue | Remove-Item -Force
    Remove-Item $f -Force
}

Write-Host ""
Write-Host ("  {0} passed, {1} failed" -f $PASS, $FAIL) -ForegroundColor $(if ($FAIL) { 'Red' } else { 'Green' })
if ($FAIL) { exit 1 }
exit 0
