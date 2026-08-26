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

  SR2-3. Every case now asserts the child's EXIT CODE as well as the resulting
  file state. Before, Invoke-Script threw the exit code away, so a case named
  "revoke on a clean file is a no-op, not an error" proved only the first half:
  a script that exited 1 without writing passed it. Contract asserted here:

      success path   exit 0 AND the intended change is on disk
      no-op / WhatIf exit 0 AND the bytes are byte-identical
      refusal path   NONZERO exit AND the target file is untouched

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

function Invoke-Script {
    <#
      Returns BOTH the child's console text AND its exit code (SR2-3).

      Deliberately NOT '& powershell ... 2>&1 | Out-String'. On PS 5.1 that
      wraps every native stderr line in an ErrorRecord, and under
      $ErrorActionPreference='Stop' -- set at the top of this file -- the first
      such line terminates the TEST instead of being reported as child output.
      Driving System.Diagnostics.Process directly keeps the two streams as
      plain strings and makes ExitCode a first-class result.
    #>
    param([string[]]$ScriptArgs)
    $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $SCRIPT) + $ScriptArgs
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = 'powershell.exe'
    $psi.Arguments              = ($argv | ForEach-Object {
                                     if ($_ -match '[\s"]') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    # stderr read asynchronously: reading one stream to the end while the other
    # fills its pipe buffer deadlocks.
    $errTask = $p.StandardError.ReadToEndAsync()
    $out     = $p.StandardOutput.ReadToEnd()
    $p.WaitForExit()
    $code = $p.ExitCode
    $p.Dispose()
    return [pscustomobject]@{ Output = ($out + $errTask.Result); ExitCode = $code }
}

function Assert-Exit0($r, [string]$what) {
    if ($r.ExitCode -ne 0) {
        throw ("{0}: expected exit 0, got {1}. Child output:`n{2}" -f $what, $r.ExitCode, $r.Output)
    }
}
function Assert-ExitNonZero($r, [string]$what) {
    if ($r.ExitCode -eq 0) {
        throw ("{0}: expected a NONZERO exit, got 0. Child output:`n{1}" -f $what, $r.Output)
    }
}
function Get-Allow([string]$p) {
    @(([System.IO.File]::ReadAllText($p) | ConvertFrom-Json).permissions.allow)
}
function Test-HasBom([string]$p) {
    $b = [System.IO.File]::ReadAllBytes($p)
    return ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)
}
function Remove-Backups([string]$p) {
    Get-ChildItem -Path (Split-Path $p) -Filter ((Split-Path $p -Leaf) + '.bak-*') `
        -ErrorAction SilentlyContinue | Remove-Item -Force
}

Write-Host ""
Write-Host "== claude-permissions.ps1 -- failure injection" -ForegroundColor Cyan

# ---------------------------------------------------------------- grant ----
Check "grant adds the merge rule" {
    $f = New-Fixture
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant'
    if ((Get-Allow $f) -notcontains 'Bash(gh pr merge:*)') { throw "rule not added" }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "grant writes NO BOM (the original production incident)" {
    $f = New-Fixture
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant'
    if (Test-HasBom $f) { throw "a BOM was written -- the exact defect this replaces" }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "grant leaves the file strictly parseable" {
    $f = New-Fixture
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant'
    $null = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    Remove-Backups $f; Remove-Item $f -Force
}

Check "grant preserves unrelated settings" {
    $f = New-Fixture
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant'
    $o = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    if ($o.model -ne 'claude-opus-5') { throw "lost 'model'" }
    if ($o.theme -ne 'dark')          { throw "lost 'theme'" }
    if (-not $o.permissions.additionalDirectories) { throw "lost additionalDirectories" }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "grant preserves pre-existing rules" {
    $f = New-Fixture
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant'
    foreach ($r2 in @('Bash(dir:*)', 'Bash(git add:*)', 'Bash(docker compose:*)')) {
        if ((Get-Allow $f) -notcontains $r2) { throw "lost $r2" }
    }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "grant is idempotent, and the second run still exits 0" {
    $f = New-Fixture
    $r1 = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r1 'first grant'
    $n1 = (Get-Allow $f).Count
    $r2 = Invoke-Script @('-SettingsPath', $f)
    # "already present" is a SUCCESS, not a refusal: exit 0 is part of the
    # contract, and without this a nothing-to-do path could start failing.
    Assert-Exit0 $r2 'second grant (nothing to do)'
    if ((Get-Allow $f).Count -ne $n1) { throw "second run changed the list" }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "-WhatIf writes nothing AND reports success" {
    $f = New-Fixture
    $before = [System.IO.File]::ReadAllBytes($f)
    $r = Invoke-Script @('-SettingsPath', $f, '-WhatIf')
    # Previously this proved only that the bytes matched -- a -WhatIf that
    # errored out before reaching the preview would have passed identically.
    Assert-Exit0 $r '-WhatIf'
    $after = [System.IO.File]::ReadAllBytes($f)
    if (Compare-Object $before $after) { throw "-WhatIf modified the file" }
    Remove-Item $f -Force
}

# --------------------------------------------------------------- revoke ----
Check "revoke removes the merge rule" {
    $f = New-Fixture
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f)) 'setup grant'
    $r = Invoke-Script @('-SettingsPath', $f, '-Revoke')
    Assert-Exit0 $r 'revoke'
    if ((Get-Allow $f) -contains 'Bash(gh pr merge:*)') { throw "rule survived revoke" }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "PLAIN revoke also removes -IncludeDeploy rules (OPS-6)" {
    # The old script only removed deploy rules if you repeated -IncludeDeploy,
    # while its help said -Revoke removed "the rules this script adds". A
    # standing authorization survived an undo the user believed was complete.
    $f = New-Fixture
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f, '-IncludeDeploy')) 'setup grant -IncludeDeploy'
    $granted = Get-Allow $f
    if ($granted -notcontains 'Bash(docker restart:*)') { throw "fixture setup: deploy rules not granted" }
    $r = Invoke-Script @('-SettingsPath', $f, '-Revoke')
    Assert-Exit0 $r 'plain revoke'
    $left = Get-Allow $f
    foreach ($rule in @('Bash(gh pr merge:*)', 'Bash(docker compose up:*)',
                        'Bash(docker compose build:*)', 'Bash(docker restart:*)')) {
        if ($left -contains $rule) { throw "plain -Revoke left $rule behind" }
    }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "revoke preserves unrelated rules and settings" {
    $f = New-Fixture
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f, '-IncludeDeploy')) 'setup grant'
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f, '-Revoke')) 'revoke'
    $o = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    foreach ($rule in @('Bash(dir:*)', 'Bash(git add:*)', 'Bash(docker compose:*)')) {
        if (@($o.permissions.allow) -notcontains $rule) { throw "revoke removed unrelated rule $rule" }
    }
    if ($o.model -ne 'claude-opus-5') { throw "revoke lost 'model'" }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "revoke writes NO BOM and leaves valid JSON" {
    $f = New-Fixture
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f)) 'setup grant'
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f, '-Revoke')) 'revoke'
    if (Test-HasBom $f) { throw "revoke wrote a BOM" }
    $null = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    Remove-Backups $f; Remove-Item $f -Force
}

Check "revoke on a clean file is a no-op, not an error" {
    # The NAME claims two things. Byte-equality was the only one proved before:
    # a script that exited 1 without writing satisfied it. "not an error" is
    # the exit code, so assert the exit code.
    $f = New-Fixture
    $before = [System.IO.File]::ReadAllBytes($f)
    $r = Invoke-Script @('-SettingsPath', $f, '-Revoke')
    Assert-Exit0 $r 'revoke with nothing to remove'
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($f))) { throw "modified a file with nothing to remove" }
    Remove-Item $f -Force
}

Check "grant -> revoke -> grant round-trips" {
    $f = New-Fixture
    $start = (Get-Allow $f).Count
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f)) 'grant 1'
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f, '-Revoke')) 'revoke'
    if ((Get-Allow $f).Count -ne $start) { throw "revoke did not restore the original count" }
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f)) 'grant 2'
    if ((Get-Allow $f) -notcontains 'Bash(gh pr merge:*)') { throw "re-grant failed" }
    Remove-Backups $f; Remove-Item $f -Force
}

# ------------------------------------------------------- refusal paths -----
Check "a missing settings file is refused, not created" {
    $p = Join-Path $env:TEMP ("perm-absent-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,6))
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-ExitNonZero $r 'missing settings file'
    if (Test-Path $p) { throw "the script CREATED a settings file that did not exist" }
}

Check "unparseable JSON is refused and left untouched" {
    $p = Join-Path $env:TEMP ("perm-bad-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,6))
    [System.IO.File]::WriteAllText($p, "{ this is not json", (New-Object System.Text.UTF8Encoding($false)))
    $before = [System.IO.File]::ReadAllBytes($p)
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-ExitNonZero $r 'unparseable JSON'
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "modified a file it could not parse" }
    Remove-Item $p -Force
}

Check "a file with no permissions section is refused" {
    $p = Join-Path $env:TEMP ("perm-noperm-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,6))
    [System.IO.File]::WriteAllText($p, '{"model":"x"}', (New-Object System.Text.UTF8Encoding($false)))
    $before = [System.IO.File]::ReadAllBytes($p)
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-ExitNonZero $r 'no permissions section'
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "modified a file with no permissions section" }
    Remove-Item $p -Force
}

Check "an EXISTING BOM is detected and reported" {
    $f = New-Fixture
    $bytes = [System.IO.File]::ReadAllBytes($f)
    $withBom = ,([byte]0xEF) + ,([byte]0xBB) + ,([byte]0xBF) + $bytes
    [System.IO.File]::WriteAllBytes($f, $withBom)
    $r = Invoke-Script @('-SettingsPath', $f, '-WhatIf')
    # A warning is not a refusal: -WhatIf still succeeds.
    Assert-Exit0 $r '-WhatIf over a BOM-ed file'
    if ($r.Output -notmatch 'BOM') { throw "an existing BOM was not reported" }
    Remove-Item $f -Force
}

Check "a backup is written before any change" {
    $f = New-Fixture
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f)) 'grant'
    $baks = @(Get-ChildItem -Path (Split-Path $f) -Filter ((Split-Path $f -Leaf) + '.bak-*') -ErrorAction SilentlyContinue)
    if ($baks.Count -eq 0) { throw "no backup was written" }
    $baks | Remove-Item -Force
    Remove-Item $f -Force
}

Check "no candidate temp files are left behind" {
    $f = New-Fixture
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f)) 'grant'
    $leftovers = @(Get-ChildItem -Path (Split-Path $f) -Filter ((Split-Path $f -Leaf) + '.candidate-*') -ErrorAction SilentlyContinue)
    if ($leftovers.Count -gt 0) { throw "$($leftovers.Count) candidate file(s) left behind" }
    Remove-Backups $f; Remove-Item $f -Force
}

# ------------------------------------------- process contract (SR2-3) ------
Check "a CORRECT grant that exits nonzero is still a failure" {
    # The gap SR2-3 named: no previous case could see "state changed correctly,
    # then the script exited nonzero". Every state assertion here passes on
    # such a build; only the exit-code assertion catches it. This is the case
    # the mutation checker's 'exit-3-after-success' mutant must kill.
    $f = New-Fixture
    $r = Invoke-Script @('-SettingsPath', $f)
    $allow = Get-Allow $f
    if ($allow -notcontains 'Bash(gh pr merge:*)') { throw "state: rule not added" }
    if (Test-HasBom $f)                            { throw "state: BOM written" }
    foreach ($rule in @('Bash(dir:*)', 'Bash(git add:*)', 'Bash(docker compose:*)')) {
        if ($allow -notcontains $rule) { throw "state: lost $rule" }
    }
    # ... and only NOW the process contract.
    Assert-Exit0 $r 'a grant whose on-disk result is entirely correct'
    Remove-Backups $f; Remove-Item $f -Force
}

Check "a CORRECT revoke that exits nonzero is still a failure" {
    $f = New-Fixture
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f, '-IncludeDeploy')) 'setup grant'
    Remove-Backups $f
    $r = Invoke-Script @('-SettingsPath', $f, '-Revoke')
    $left = Get-Allow $f
    foreach ($rule in @('Bash(gh pr merge:*)', 'Bash(docker restart:*)')) {
        if ($left -contains $rule) { throw "state: $rule survived revoke" }
    }
    Assert-Exit0 $r 'a revoke whose on-disk result is entirely correct'
    Remove-Backups $f; Remove-Item $f -Force
}

# ---------------------------------------------- commit semantics (OPS-7) ---
Check "the commit PRESERVES the live file's ACL (File.Replace, not Move-Item)" {
    <#
      The discriminator between the two commit primitives.

      Move-Item -Force MOVES the candidate onto the destination path, so the
      surviving file carries the CANDIDATE's ACL -- whatever it inherited from
      the containing directory. Any explicit ACE on settings.json is silently
      dropped by the very script whose job is managing an authorization file.

      [System.IO.File]::Replace is ReplaceFile, which preserves the REPLACED
      file's security descriptor. Empirically, on this machine, in %TEMP%:
        after File.Replace     EveryoneACE=True
        after Move-Item -Force EveryoneACE=False
    #>
    $f = New-Fixture
    $acl = Get-Acl $f
    $acl.SetAccessRuleProtection($true, $true)   # stop inheritance so the ACE is ours alone
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        'Everyone', 'ReadData', 'Allow')))
    Set-Acl -Path $f -AclObject $acl
    if (-not @((Get-Acl $f).Access | Where-Object { $_.IdentityReference -like '*Everyone*' })) {
        throw "fixture setup: the marker ACE was not applied"
    }

    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant onto an ACL-marked file'
    if ((Get-Allow $f) -notcontains 'Bash(gh pr merge:*)') { throw "rule not added" }

    $survivors = @((Get-Acl $f).Access | Where-Object { $_.IdentityReference -like '*Everyone*' })
    if ($survivors.Count -eq 0) {
        throw "the commit DISCARDED the settings file's explicit ACL -- that is a moved temp file, not a replacement"
    }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "a commit that cannot complete leaves the live file UNTOUCHED and fails closed" {
    <#
      Failure injection for the commit itself: this test process holds the
      destination open with FileShare::Read, so the script can still read and
      back it up, but neither ReplaceFile nor a move can take it. The whole
      point of prepare -> validate -> commit is that this cannot corrupt
      anything: bytes identical, nonzero exit, candidate cleaned up, and a
      message that says the live file is unchanged.
    #>
    $f = New-Fixture
    $before = [System.IO.File]::ReadAllBytes($f)
    $hold = New-Object System.IO.FileStream($f,
        [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        $r = Invoke-Script @('-SettingsPath', $f)
        Assert-ExitNonZero $r 'commit against a locked destination'
        if ($r.Output -notmatch 'UNCHANGED') {
            throw "the failure message did not tell the operator the live file is unchanged. Output:`n$($r.Output)"
        }
    } finally { $hold.Dispose() }
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($f))) {
        throw "a failed commit MODIFIED the live file"
    }
    $leftovers = @(Get-ChildItem -Path (Split-Path $f) -Filter ((Split-Path $f -Leaf) + '.candidate-*') -ErrorAction SilentlyContinue)
    if ($leftovers.Count -gt 0) { throw "a failed commit left $($leftovers.Count) candidate file(s) behind" }
    Remove-Backups $f; Remove-Item $f -Force
}

Write-Host ""
Write-Host ("  {0} passed, {1} failed" -f $PASS, $FAIL) -ForegroundColor $(if ($FAIL) { 'Red' } else { 'Green' })
if ($FAIL) { exit 1 }
exit 0
