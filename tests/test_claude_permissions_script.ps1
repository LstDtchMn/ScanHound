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
    <#
      A settings file shaped like the real one: an allow list plus unrelated
      top-level keys that must survive every operation.

      -Shape EXISTS BECAUSE THE FACTORY WAS THE BLIND SPOT. Until 2026-08-26
      this function ALWAYS emitted a three-element allow list, so all 23
      assertions were built on the one input shape in which the missing-allow-key
      defect cannot appear. That is passes-by-construction on the INPUT axis:
      no assertion, however sharp, could have seen it. The shapes are therefore
      part of the factory's contract, not of individual tests:

        List           an allow list with entries (the original, still default)
        EmptyAllow     "allow": []            -- always worked; the control
        NoAllowKey     a deny list, NO allow  -- the fresh-user shape, the defect
        NullAllow      "allow": null          -- @($null).Count is 1, same lie
        UnrelatedOnly  a permissions section with neither allow nor deny
    #>
    param(
        [string[]]$Allow = @('Bash(dir:*)', 'Bash(git add:*)', 'Bash(docker compose:*)'),
        [ValidateSet('List', 'EmptyAllow', 'NoAllowKey', 'NullAllow', 'UnrelatedOnly')]
        [string]$Shape = 'List'
    )
    $p = Join-Path $env:TEMP ("perm-fixture-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,8))
    switch ($Shape) {
        'List'          { $perms = [ordered]@{ allow = $Allow; additionalDirectories = @('C:\somewhere') } }
        'EmptyAllow'    { $perms = [ordered]@{ allow = @();    additionalDirectories = @('C:\somewhere') } }
        'NoAllowKey'    { $perms = [ordered]@{ deny  = @('Bash(rm:*)'); additionalDirectories = @('C:\somewhere') } }
        'NullAllow'     { $perms = [ordered]@{ allow = $null;  additionalDirectories = @('C:\somewhere') } }
        'UnrelatedOnly' { $perms = [ordered]@{ additionalDirectories = @('C:\somewhere') } }
    }
    $obj = [ordered]@{
        permissions = $perms
        model       = 'claude-opus-5'
        theme       = 'dark'
    }
    [System.IO.File]::WriteAllText($p, ($obj | ConvertTo-Json -Depth 20),
        (New-Object System.Text.UTF8Encoding($false)))
    return $p
}

function Remove-Fixture {
    <# Fixtures may carry ReadOnly (the attribute case), which Remove-Item
       -Force handles but [IO.File]::Delete does not. #>
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        (Get-Item -LiteralPath $Path -Force).Attributes = 'Normal'
        Remove-Item -LiteralPath $Path -Force
    }
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
    foreach ($suffix in @('.bak-*', '.replaced-*', '.candidate-*')) {
        Get-ChildItem -Path (Split-Path $p) -Filter ((Split-Path $p -Leaf) + $suffix) `
            -ErrorAction SilentlyContinue | Remove-Item -Force
    }
}
function Get-Litter([string]$p, [string]$suffix) {
    @(Get-ChildItem -Path (Split-Path $p) -Filter ((Split-Path $p -Leaf) + $suffix) `
        -ErrorAction SilentlyContinue)
}

function Invoke-ScriptVanishingDestination {
    <#
      FAILURE INJECTION FOR THE COMMIT HANDLER'S CLAIM, NOT JUST ITS EXIT CODE.

      The locked-destination case cannot discriminate the two handlers: there
      the live file really IS unchanged, so an unconditional "settings.json is
      UNCHANGED" happens to be true. This induces the case where it is FALSE.

      The child is started, then this process watches the settings directory
      and DELETES the destination the instant the candidate appears -- which is
      after the candidate is written and flushed but before ReplaceFile runs.
      ReplaceFile then fails with the destination genuinely absent: the same
      end state ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 is documented to leave
      behind, reached by a route this machine can actually produce.

      The pre-fix handler printed "settings.json is UNCHANGED and the candidate
      was discarded" here and then deleted the candidate -- a false claim plus
      the destruction of the only other copy of the intended content.

      Induced is reported, never assumed: if the race is lost the caller
      retries, and a case that could never induce it FAILS rather than passing
      on a scenario that did not happen.
    #>
    param([string]$SettingsFile)
    $dir  = Split-Path $SettingsFile
    $leaf = Split-Path $SettingsFile -Leaf
    $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $SCRIPT, '-SettingsPath', $SettingsFile)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = 'powershell.exe'
    $psi.Arguments              = ($argv | ForEach-Object {
                                     if ($_ -match '[\s"]') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    # BOTH streams async: the poll loop below owns this thread, so neither pipe
    # may be left to fill.
    $errTask = $p.StandardError.ReadToEndAsync()
    $outTask = $p.StandardOutput.ReadToEndAsync()

    $deleted = $false
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while (-not $deleted -and -not $p.HasExited -and $sw.Elapsed.TotalSeconds -lt 60) {
        if ([System.IO.Directory]::GetFiles($dir, ($leaf + '.candidate-*')).Length -gt 0) {
            try { [System.IO.File]::Delete($SettingsFile); $deleted = $true } catch { }
        }
    }
    $p.WaitForExit()
    $code = $p.ExitCode
    $p.Dispose()
    return [pscustomobject]@{
        Output   = ($outTask.Result + $errTask.Result)
        ExitCode = $code
        Deleted  = $deleted
    }
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

# ------------------------------- D1: the handler may not claim what it has
#                                      not measured -----------------------
Check "a failed commit REPORTS MEASURED state, and does not claim UNCHANGED when the destination is GONE" {
    <#
      The charge against the pre-fix handler was not that it lost data in the
      locked case -- it did not -- but that it asserted "settings.json is
      UNCHANGED and the candidate was discarded" with no Test-Path, no re-read
      and no comparison behind it, and then deleted the candidate. Induce the
      state in which that sentence is false and see what the handler says.
    #>
    $f = $null
    $r = $null
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        if ($f) { Remove-Backups $f; Remove-Fixture $f }
        $f = New-Fixture
        $r = Invoke-ScriptVanishingDestination -SettingsFile $f
        if ($r.Deleted -and $r.Output -match 'the commit FAILED') { break }
        $r = $null
    }
    if (-not $r) {
        throw "could not induce a commit failure with the destination absent in 6 attempts -- the case proves nothing, so it fails"
    }
    Assert-ExitNonZero $r 'commit whose destination vanished'

    # 1. It must not claim the file is unchanged. It is not there at all.
    if ($r.Output -match 'is UNCHANGED') {
        throw "the handler claimed 'is UNCHANGED' while the destination did not exist. Output:`n$($r.Output)"
    }
    # 2. It must report the state it actually measured.
    if ($r.Output -notmatch 'MEASURED state') {
        throw "the handler did not report a MEASURED state. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'NO LONGER EXISTS') {
        throw "the handler did not report that the destination is absent. Output:`n$($r.Output)"
    }
    # 3. It must not have deleted the only remaining copy of the new content.
    if ((Get-Litter $f '.candidate-*').Count -eq 0) {
        throw "the handler DELETED the candidate in a partial failure -- the intended content is now gone"
    }
    # 4. And it must have left the operator something to recover from.
    if ((Get-Litter $f '.bak-*').Count -eq 0) { throw "no backup left to recover from" }
    if ($r.Output -notmatch 'recover with') {
        throw "the handler printed no recovery command. Output:`n$($r.Output)"
    }
    Remove-Backups $f; Remove-Fixture $f
}

Check "a SUCCESSFUL commit leaves no ReplaceFile backup copy behind" {
    # The commit now passes a REAL lpBackupFileName so partial failures stay
    # recoverable. That must not turn a clean run into a litter generator.
    $f = New-Fixture
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f)) 'grant'
    $left = Get-Litter $f '.replaced-*'
    if ($left.Count -gt 0) { throw "$($left.Count) ReplaceFile backup copy/copies left behind after a verified commit" }
    Remove-Backups $f; Remove-Fixture $f
}

# ------------------------------- D2: the ReadOnly destination -------------
Check "a READ-ONLY destination is refused with an ACTIONABLE message and leaves no litter" {
    <#
      MEASURED behaviour change, now deliberate. With ReadOnly set on the
      destination, [System.IO.File]::Replace throws "Access to the path is
      denied." while the pre-OPS-7 Move-Item -Force SUCCEEDED -- and cleared
      the attribute as a side effect. Failing closed is right; failing closed
      with a message that names neither the cause nor the fix, after having
      already written a backup, is not.
    #>
    $f = New-Fixture
    $before = [System.IO.File]::ReadAllBytes($f)
    (Get-Item -LiteralPath $f -Force).Attributes = 'ReadOnly'
    try {
        $r = Invoke-Script @('-SettingsPath', $f)
        Assert-ExitNonZero $r 'grant onto a read-only destination'
        if ($r.Output -notmatch 'READ-ONLY') {
            throw "the refusal did not name the read-only attribute as the cause. Output:`n$($r.Output)"
        }
        if ($r.Output -notmatch 'attrib -R') {
            throw "the refusal gave the operator no command to clear it. Output:`n$($r.Output)"
        }
        # Refused BEFORE writing anything, so a refusal costs the user nothing
        # to clean up. The unguarded build reaches the commit and leaves a
        # timestamped backup behind.
        if ((Get-Litter $f '.bak-*').Count -gt 0) {
            throw "the refusal still wrote a backup -- it did not refuse before touching the disk"
        }
        if ((Get-Litter $f '.candidate-*').Count -gt 0) { throw "the refusal left a candidate behind" }
        if (Compare-Object $before ([System.IO.File]::ReadAllBytes($f))) { throw "the refusal modified the file" }
        # The protection the user set must survive. Move-Item -Force silently
        # cleared it; this script must not.
        if (((Get-Item -LiteralPath $f -Force).Attributes -band [System.IO.FileAttributes]::ReadOnly) -eq 0) {
            throw "the script cleared the user's READ-ONLY attribute"
        }
    } finally { Remove-Backups $f; Remove-Fixture $f }
}

Check "-WhatIf over a READ-ONLY destination still previews, warns, and exits 0" {
    $f = New-Fixture
    (Get-Item -LiteralPath $f -Force).Attributes = 'ReadOnly'
    try {
        $r = Invoke-Script @('-SettingsPath', $f, '-WhatIf')
        Assert-Exit0 $r '-WhatIf over a read-only file'
        if ($r.Output -notmatch 'would ADD') { throw "-WhatIf stopped previewing" }
        if ($r.Output -notmatch 'READ-ONLY') { throw "-WhatIf did not warn that a real run would refuse" }
    } finally { Remove-Fixture $f }
}

# ------------------------------- D3: the absent allow key -----------------
Check "a permissions section with NO allow key counts 0 rules, not 1" {
    # @($null) has Count 1, so the un-normalised read printed "1 rule(s)" for
    # ZERO rules: a confident wrong number, which is worse than a crash.
    $f = New-Fixture -Shape NoAllowKey
    $r = Invoke-Script @('-SettingsPath', $f, '-WhatIf')
    Assert-Exit0 $r '-WhatIf over a file with no allow key'
    if ($r.Output -notmatch 'current allow list: 0 rule') {
        throw "reported the wrong count for an absent allow key. Output:`n$($r.Output)"
    }
    Remove-Fixture $f
}

Check "the FRESH-USER shape (deny list, no allow key) grants end to end" {
    <#
      The primary case for a new user: somebody with a deny list who has never
      allowed anything is exactly who runs a script that adds a first allow
      rule. Un-normalised, $settings.permissions.allow = $wanted threw
      SetValueInvocationException from OUTSIDE any try/catch, so the script
      died with a raw .NET error instead of its own STOP message and left the
      .bak- copy behind uncollected.
    #>
    $f = New-Fixture -Shape NoAllowKey
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant on the fresh-user shape'
    if ($r.Output -match 'SetValueInvocationException') {
        throw "the script died with a raw .NET exception. Output:`n$($r.Output)"
    }
    $o = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    if (@($o.permissions.allow) -notcontains 'Bash(gh pr merge:*)') { throw "the first allow rule did not land" }
    if (@($o.permissions.allow).Count -ne 1) { throw "expected exactly 1 rule, got $(@($o.permissions.allow).Count)" }
    if (@($o.permissions.deny) -notcontains 'Bash(rm:*)') { throw "the deny list was lost" }
    if ($o.model -ne 'claude-opus-5') { throw "lost 'model'" }
    if (Test-HasBom $f) { throw "wrote a BOM" }
    Remove-Backups $f; Remove-Fixture $f
}

Check "a null allow key counts 0 rules and grants end to end" {
    $f = New-Fixture -Shape NullAllow
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant over "allow": null'
    if ($r.Output -notmatch 'current allow list: 0 rule') {
        throw "reported the wrong count for a null allow key. Output:`n$($r.Output)"
    }
    if ((Get-Allow $f) -notcontains 'Bash(gh pr merge:*)') { throw "the rule did not land" }
    Remove-Backups $f; Remove-Fixture $f
}

Check "an EMPTY allow list counts 0 rules and grants end to end (the control)" {
    # This shape always worked -- which is exactly why it is here. It is the
    # control that shows the defect was the ABSENT key, not empty-ness.
    $f = New-Fixture -Shape EmptyAllow
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant over an empty allow list'
    if ($r.Output -notmatch 'current allow list: 0 rule') {
        throw "reported the wrong count for an empty allow list. Output:`n$($r.Output)"
    }
    if ((Get-Allow $f) -notcontains 'Bash(gh pr merge:*)') { throw "the rule did not land" }
    Remove-Backups $f; Remove-Fixture $f
}

Check "a permissions section with unrelated subkeys ONLY grants end to end" {
    $f = New-Fixture -Shape UnrelatedOnly
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'grant over a permissions section with neither allow nor deny'
    if ($r.Output -notmatch 'current allow list: 0 rule') {
        throw "reported the wrong count. Output:`n$($r.Output)"
    }
    $o = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json
    if (@($o.permissions.allow) -notcontains 'Bash(gh pr merge:*)') { throw "the rule did not land" }
    if (-not $o.permissions.additionalDirectories) { throw "lost additionalDirectories" }
    Remove-Backups $f; Remove-Fixture $f
}

Check "revoke over a file with no allow key is a clean no-op" {
    $f = New-Fixture -Shape NoAllowKey
    $before = [System.IO.File]::ReadAllBytes($f)
    $r = Invoke-Script @('-SettingsPath', $f, '-Revoke')
    Assert-Exit0 $r 'revoke over a file with no allow key'
    if ($r.Output -notmatch 'current allow list: 0 rule') {
        throw "reported the wrong count. Output:`n$($r.Output)"
    }
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($f))) { throw "modified a file with nothing to remove" }
    if ((Get-Litter $f '.bak-*').Count -gt 0) { throw "a no-op revoke wrote a backup" }
    Remove-Fixture $f
}

Check "a permissions key that is not an object is refused, not crashed on" {
    $p = Join-Path $env:TEMP ("perm-scalar-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,6))
    [System.IO.File]::WriteAllText($p, '{"permissions":"nonsense","model":"x"}',
        (New-Object System.Text.UTF8Encoding($false)))
    $before = [System.IO.File]::ReadAllBytes($p)
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-ExitNonZero $r 'a scalar permissions key'
    if ($r.Output -notmatch 'STOP') { throw "died without the script's own refusal message. Output:`n$($r.Output)" }
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "modified the file" }
    Remove-Fixture $p
}

Write-Host ""
Write-Host ("  {0} passed, {1} failed" -f $PASS, $FAIL) -ForegroundColor $(if ($FAIL) { 'Red' } else { 'Green' })
if ($FAIL) { exit 1 }
exit 0
