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

  ROUND 4 (F1..F6). Two structural gaps, both of which had let a defect through
  while every assertion in the file passed:

    * NO NEGATIVE DIRECTIONS. Cases asserted that -IncludeDeploy DOES grant
      the deploy rules; none asserted that a plain grant does NOT. Same shape
      as SR2-3 -- a case proving one half of what its name implies -- so the
      round also re-read the file for others and found two: a backup case that
      checked only that a .bak- file EXISTS, and an idempotence case that
      compared COUNTS rather than the list.
    * NO FAILURE-MODE AXIS. -Shape gave the INPUT an axis in round 3; the
      FAILURE had none, so half of Write-CommitFailureReport's decision surface
      had no assertion, no mutant, and nothing able to say so. There are now
      two axes for it -- Invoke-ScriptWithCommitFailure for what can be induced
      end to end, Invoke-FailureHandlerBranch for the branches that structurally
      cannot be -- plus a coverage case that reads the handler's own branch
      markers and fails on any branch no test drives.

  Run:  powershell -ExecutionPolicy Bypass -File tests\test_claude_permissions_script.ps1
  And:  python tests\mutate_claude_permissions.py
#>

$ErrorActionPreference = 'Stop'
$SCRIPT = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\claude-permissions.ps1'
if (-not (Test-Path $SCRIPT)) { throw "cannot find $SCRIPT" }

# The rules a plain grant must NOT add. Spelled out here rather than imported,
# because a test that reads its expectation out of the script under test cannot
# see the script changing its mind (F5).
$DEPLOY_RULE_STRINGS = @('Bash(docker compose up:*)', 'Bash(docker compose build:*)', 'Bash(docker restart:*)')

# Which verdict branches of Write-CommitFailureReport a test actually drove.
# The coverage case at the bottom of this file compares this against the
# markers in the script itself.
$script:BRANCHES_EXERCISED = @{}

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
        PreOwned       an allow list that ALREADY contains two of the rule
                       strings this script can grant, as if a human typed them
                       by hand months ago. Added for F6: every revoke case
                       before it only ever removed a rule the same test had
                       just granted, so no test could see -Revoke reaching a
                       rule this script never wrote.
    #>
    param(
        [string[]]$Allow = @('Bash(dir:*)', 'Bash(git add:*)', 'Bash(docker compose:*)'),
        [ValidateSet('List', 'EmptyAllow', 'NoAllowKey', 'NullAllow', 'UnrelatedOnly', 'PreOwned')]
        [string]$Shape = 'List'
    )
    $p = Join-Path $env:TEMP ("perm-fixture-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,8))
    switch ($Shape) {
        'List'          { $perms = [ordered]@{ allow = $Allow; additionalDirectories = @('C:\somewhere') } }
        'EmptyAllow'    { $perms = [ordered]@{ allow = @();    additionalDirectories = @('C:\somewhere') } }
        'NoAllowKey'    { $perms = [ordered]@{ deny  = @('Bash(rm:*)'); additionalDirectories = @('C:\somewhere') } }
        'NullAllow'     { $perms = [ordered]@{ allow = $null;  additionalDirectories = @('C:\somewhere') } }
        'UnrelatedOnly' { $perms = [ordered]@{ additionalDirectories = @('C:\somewhere') } }
        'PreOwned'      { $perms = [ordered]@{
                              allow = @('Bash(dir:*)', 'Bash(docker compose build:*)',
                                        'Bash(gh pr merge:*)', 'Bash(git add:*)')
                              additionalDirectories = @('C:\somewhere') } }
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

function Invoke-ScriptWithCommitFailure {
    <#
      FAILURE INJECTION FOR THE COMMIT HANDLER'S CLAIM, NOT JUST ITS EXIT CODE
      -- now on an AXIS (F2), not as a single hard-coded scenario.

      Both injections work the same way: start the child, watch the settings
      directory, and act the instant the candidate appears -- which is after
      the candidate has been written and flushed but before ReplaceFile runs.

        Vanish         DELETE the destination. ReplaceFile then fails with the
                       destination genuinely absent: the same end state
                       ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 is documented to
                       leave behind, reached by a route this machine can
                       actually produce. The 2026-08-26-morning handler printed
                       "settings.json is UNCHANGED and the candidate was
                       discarded" here and then deleted the candidate -- a
                       false claim plus the destruction of the only other copy
                       of the intended content.

        LockExclusive  HOLD the destination with FileShare::None. ReplaceFile
                       fails with a sharing violation while the destination is,
                       at that same instant, byte-identical and valid JSON.
                       This is F1: the rewritten handler turned "could not
                       READ it" into "EXISTS but does not parse" and "is NOT in
                       its pre-commit state", and offered a Copy-Item that
                       would have clobbered an intact file. Get-FileHashHex and
                       ReadAllText both ask for FileShare.Read, so they fail
                       TOGETHER -- which made that contradiction guaranteed
                       rather than an edge case.

      Induced is reported, never assumed: if the race is lost the caller
      retries, and a case that could never induce its scenario FAILS rather
      than passing on something that did not happen. For LockExclusive the
      handle is held until the child exits, so the child cannot possibly have
      read the file after the lock cleared.
    #>
    param(
        [string]$SettingsFile,
        [ValidateSet('Vanish', 'LockExclusive')]
        [string]$Injection
    )
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

    $induced = $false
    $hold    = $null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while (-not $induced -and -not $p.HasExited -and $sw.Elapsed.TotalSeconds -lt 60) {
        if ([System.IO.Directory]::GetFiles($dir, ($leaf + '.candidate-*')).Length -gt 0) {
            if ($Injection -eq 'Vanish') {
                try { [System.IO.File]::Delete($SettingsFile); $induced = $true } catch { }
            } else {
                try {
                    $hold = New-Object System.IO.FileStream($SettingsFile,
                        [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
                        [System.IO.FileShare]::None)
                    $induced = $true
                } catch { }
            }
        }
    }

    # GROUND TRUTH for LockExclusive, read through the very handle that is
    # denying the child: what the file actually WAS while the handler was
    # describing it.
    $groundTruthParses = $null
    $groundTruthBytes  = $null
    if ($hold) {
        try {
            $hold.Position = 0
            $buf = New-Object byte[] $hold.Length
            $null = $hold.Read($buf, 0, $buf.Length)
            $groundTruthBytes  = $buf
            $groundTruthParses = $false
            try { $null = ([System.Text.Encoding]::UTF8.GetString($buf) | ConvertFrom-Json)
                  $groundTruthParses = $true } catch { }
        } catch { }
    }

    $p.WaitForExit()
    $code = $p.ExitCode
    $p.Dispose()
    if ($hold) { $hold.Dispose() }
    return [pscustomobject]@{
        Output              = ($outTask.Result + $errTask.Result)
        ExitCode            = $code
        Induced             = $induced
        GroundTruthParses   = $groundTruthParses
        GroundTruthBytes    = $groundTruthBytes
    }
}

function Get-ScriptFunctionSource {
    <#
      Lift function definitions OUT of the script under test by parsing it,
      so the unit harness below drives the PRODUCTION text of
      Write-CommitFailureReport rather than a copy of it that can drift.
      Dot-sourcing is not an option: the script has a body that runs.
    #>
    param([string[]]$Name)
    $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($SCRIPT, [ref]$null, [ref]$errs)
    if ($errs -and $errs.Count -gt 0) { throw "the script under test does not parse: $($errs[0].Message)" }
    $found = @{}
    foreach ($fn in $ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        $found[$fn.Name] = $fn.Extent.Text
    }
    $sb = New-Object System.Text.StringBuilder
    foreach ($n in $Name) {
        if (-not $found.ContainsKey($n)) { throw "function '$n' is not defined in $SCRIPT" }
        $null = $sb.AppendLine($found[$n])
        $null = $sb.AppendLine()
    }
    return $sb.ToString()
}

function Get-HandlerBranchMarkers {
    <# Every verdict branch of Write-CommitFailureReport carries a marker
       comment on its own line. This is the list of branches the handler
       actually has, read from the handler. #>
    $src = [System.IO.File]::ReadAllText($SCRIPT)
    return @([regex]::Matches($src, '(?m)^\s*#\s*BRANCH:([a-z\-]+)\s*$') |
             ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
}

function Invoke-FailureHandlerBranch {
    <#
      THE FAILURE-MODE AXIS (F2).

      Four of Write-CommitFailureReport's branches cannot be reached by
      process-level injection at all, and the reason is structural rather than
      a matter of trying harder: the script takes $preCommitHash on the
      statement immediately before ReplaceFile, so anything this process does
      to the destination at candidate-time is baked INTO the pre-commit hash
      and comes back out as "unchanged". The window between the hash and the
      throw is not raceable.

      So those branches are driven directly, in a child process, against the
      handler's own source text lifted out of the script by the parser. The
      inputs are hand-built files plus a hand-chosen $PreCommitHash, which is
      the only way to say "these two hashes differ" or "there was no hash".

        unchanged         dest == pre-commit bytes, hash matches
        absent            dest deleted
        unreadable        dest held with FileShare::None by THIS process
        unparseable       dest read successfully and is not JSON
        identity-unknown  dest readable and valid, but $PreCommitHash is $null
        altered           dest readable, valid, and hashes differ

      The set above is checked against the markers in the handler by the
      coverage case at the bottom of this file, so adding a seventh branch
      makes the suite say so instead of leaving it silently untested.

      The driver ends in "exit 99", which the handler's contract says can never
      be reached -- every path ends in Die.
    #>
    param(
        [ValidateSet('unchanged', 'absent', 'unreadable', 'unparseable', 'identity-unknown', 'altered')]
        [string]$Branch,
        [switch]$NoBackup
    )
    $stem = Join-Path $env:TEMP ("perm-branch-{0}" -f [guid]::NewGuid().ToString('N').Substring(0,8))
    $dest = "$stem.json"
    $cand = "$stem.json.candidate-aaaaaaaa"
    $repl = "$stem.json.replaced-bbbbbbbb"
    $bak  = "$stem.json.bak-20260826-000000"

    $enc      = New-Object System.Text.UTF8Encoding($false)
    $original = '{"permissions":{"allow":["Bash(dir:*)"]},"model":"x"}'
    $changed  = '{"permissions":{"allow":["Bash(dir:*)","Bash(gh pr merge:*)"]},"model":"x"}'
    $garbage  = '{ this is not json'

    [System.IO.File]::WriteAllText($cand, $changed,  $enc)
    if (-not $NoBackup) { [System.IO.File]::WriteAllText($bak, $original, $enc) }

    function Local-Hash([string]$text) {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $b = (New-Object System.Text.UTF8Encoding($false)).GetBytes($text)
            return ([BitConverter]::ToString($sha.ComputeHash($b))).Replace('-', '')
        } finally { $sha.Dispose() }
    }

    switch ($Branch) {
        'unchanged'        { [System.IO.File]::WriteAllText($dest, $original, $enc); $hash = Local-Hash $original }
        'absent'           { $hash = Local-Hash $original }
        'unreadable'       { [System.IO.File]::WriteAllText($dest, $original, $enc); $hash = Local-Hash $original }
        'unparseable'      { [System.IO.File]::WriteAllText($dest, $garbage,  $enc); $hash = Local-Hash $original }
        'identity-unknown' { [System.IO.File]::WriteAllText($dest, $original, $enc); $hash = $null }
        'altered'          { [System.IO.File]::WriteAllText($dest, $changed,  $enc); $hash = Local-Hash $original }
    }
    $hashLit = $(if ($null -eq $hash) { '$null' } else { "'" + $hash + "'" })

    $funcs = Get-ScriptFunctionSource -Name @('Say', 'Good', 'Warn', 'Die',
                                              'Get-FileHashHex', 'Write-CommitFailureReport')
    $template = @'
$ErrorActionPreference = 'Stop'
{0}
Write-CommitFailureReport -Err 'injected: {1}' -Destination '{2}' -Candidate '{3}' -ReplacedCopy '{4}' -Backup '{5}' -PreCommitHash {6}
exit 99
'@
    $driver = $template -f $funcs, $Branch, $dest, $cand, $repl, $bak, $hashLit
    $driverPath = "$stem.driver.ps1"
    [System.IO.File]::WriteAllText($driverPath, $driver, $enc)

    $hold = $null
    if ($Branch -eq 'unreadable') {
        $hold = New-Object System.IO.FileStream($dest,
            [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::None)
    }
    try {
        $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $driverPath)
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName               = 'powershell.exe'
        $psi.Arguments              = ($argv | ForEach-Object {
                                         if ($_ -match '[\s"]') { '"' + $_ + '"' } else { $_ } }) -join ' '
        $psi.UseShellExecute        = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError  = $true
        $psi.CreateNoWindow         = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $errTask = $p.StandardError.ReadToEndAsync()
        $out     = $p.StandardOutput.ReadToEnd()
        $p.WaitForExit()
        $code = $p.ExitCode
        $p.Dispose()
    } finally { if ($hold) { $hold.Dispose() } }

    $script:BRANCHES_EXERCISED[$Branch] = $true
    return [pscustomobject]@{
        Output       = ($out + $errTask.Result)
        ExitCode     = $code
        Destination  = $dest
        Candidate    = $cand
        Backup       = $bak
        Stem         = $stem
    }
}

function Remove-BranchFiles($r) {
    Get-ChildItem -Path (Split-Path $r.Stem) -Filter ((Split-Path $r.Stem -Leaf) + '*') `
        -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
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
    $a1 = @(Get-Allow $f)
    $r2 = Invoke-Script @('-SettingsPath', $f)
    # "already present" is a SUCCESS, not a refusal: exit 0 is part of the
    # contract, and without this a nothing-to-do path could start failing.
    Assert-Exit0 $r2 'second grant (nothing to do)'
    # F5 sweep: this compared COUNTS. A second run that swapped one rule for
    # another -- or dropped a real rule and added a deploy rule -- kept the
    # count and passed. Compare the list itself.
    $a2 = @(Get-Allow $f)
    if (Compare-Object $a1 $a2) {
        throw ("second run changed the list: before [{0}] after [{1}]" -f ($a1 -join ', '), ($a2 -join ', '))
    }
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

Check "a backup is written before any change, and HOLDS THE PRE-CHANGE BYTES" {
    # F5 sweep. This asserted only that a .bak-* file EXISTS. A backup that is
    # an empty file, or a copy of the POST-change file, satisfied it perfectly
    # -- and every recovery instruction the failure handler prints is a
    # Copy-Item from this path. "A backup exists" is half of "there is
    # something to recover from"; the byte comparison is the other half.
    $f = New-Fixture
    $before = [System.IO.File]::ReadAllBytes($f)
    Assert-Exit0 (Invoke-Script @('-SettingsPath', $f)) 'grant'
    $baks = @(Get-ChildItem -Path (Split-Path $f) -Filter ((Split-Path $f -Leaf) + '.bak-*') -ErrorAction SilentlyContinue)
    if ($baks.Count -eq 0) { throw "no backup was written" }
    $bakBytes = [System.IO.File]::ReadAllBytes($baks[0].FullName)
    if ($bakBytes.Length -eq 0) { throw "the backup is EMPTY -- there is nothing to recover from" }
    if (Compare-Object $before $bakBytes) {
        throw "the backup is not a copy of the pre-change file, so restoring from it would not restore anything"
    }
    $after = [System.IO.File]::ReadAllBytes($f)
    if (-not (Compare-Object $before $after)) { throw "fixture check: the grant did not change the file at all" }
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
        $r = Invoke-ScriptWithCommitFailure -SettingsFile $f -Injection Vanish
        if ($r.Induced -and $r.Output -match 'the commit FAILED') { break }
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

# ===== F5: the NEGATIVE direction of the grant ============================
Check "a PLAIN grant does NOT add the deploy rules (no silent escalation)" {
    <#
      F5. The suite covered the positive direction only: -IncludeDeploy DOES
      produce Bash(docker restart:*). Nobody wrote the negative, so the line

          $grant = if ($IncludeDeploy) { $MERGE_RULES + $DEPLOY_RULES }
                   else { $MERGE_RULES }

      could be flattened to always grant everything and no case in the file was
      NAMED for it. (Measured: that mutation is in fact caught -- by the
      fresh-user allow-key case, which happens to assert "exactly 1 rule" and
      reports "got 4". An incidental kill by a test written for an unrelated
      axis is not coverage: rewrite that count assertion and the escalation
      goes invisible again.) This is the assertion that is ABOUT the thing.
    #>
    $f = New-Fixture
    $r = Invoke-Script @('-SettingsPath', $f)
    Assert-Exit0 $r 'plain grant'
    $allow = Get-Allow $f
    if ($allow -notcontains 'Bash(gh pr merge:*)') { throw "the merge rule did not land" }
    foreach ($rule in $DEPLOY_RULE_STRINGS) {
        if ($allow -contains $rule) {
            throw ("a PLAIN grant added the deploy rule $rule -- three standing " +
                   "container-lifecycle authorizations the operator never asked for")
        }
    }
    Remove-Backups $f; Remove-Item $f -Force
}

Check "a PLAIN grant does not PRINT the deploy authorizations either" {
    # The other half of the same escalation: under the flattened mutant
    # $IncludeDeploy is still $false, so the paragraph that explains what
    # docker restart authorises is skipped. The rules would arrive WITHOUT the
    # explanation. Assert the two halves move together.
    $f = New-Fixture
    $plain = Invoke-Script @('-SettingsPath', $f, '-WhatIf')
    Assert-Exit0 $plain 'plain -WhatIf'
    if ($plain.Output -match 'docker restart') {
        throw "a plain run offered docker restart. Output:`n$($plain.Output)"
    }
    $deploy = Invoke-Script @('-SettingsPath', $f, '-IncludeDeploy', '-WhatIf')
    Assert-Exit0 $deploy '-IncludeDeploy -WhatIf'
    if ($deploy.Output -notmatch 'docker restart') {
        throw "-IncludeDeploy did not name docker restart. Output:`n$($deploy.Output)"
    }
    if ($deploy.Output -notmatch 'recreate containers') {
        throw "-IncludeDeploy did not explain what it grants. Output:`n$($deploy.Output)"
    }
    Remove-Item $f -Force
}

# ===== F6: revoke reaches rules this script never granted ==================
Check "revoke removes OWNED rule strings a HUMAN added, and SAYS so (F6)" {
    <#
      Revoke matches by string VALUE against the owned vocabulary; nothing
      anywhere records who added a rule. Every previous revoke case granted the
      rules first, so no test could tell the two apart. PreOwned starts with
      two owned strings that this script never wrote.

      The behaviour is deliberate -- see WHAT -REVOKE ACTUALLY MATCHES: making
      revoke depend on a provenance file would mean a lost sidecar leaves a
      standing authorization in place while reporting success, which is the
      OPS-6 failure this script exists to avoid. So what is asserted here is
      that the SENTENCE is true: it must not claim it removes only what this
      script owns/added.
    #>
    $f = New-Fixture -Shape PreOwned
    $r = Invoke-Script @('-SettingsPath', $f, '-Revoke')
    Assert-Exit0 $r 'revoke over hand-added owned rules'
    $left = Get-Allow $f
    foreach ($rule in @('Bash(docker compose build:*)', 'Bash(gh pr merge:*)')) {
        if ($left -contains $rule) { throw "revoke left the hand-added $rule behind" }
    }
    foreach ($rule in @('Bash(dir:*)', 'Bash(git add:*)')) {
        if ($left -notcontains $rule) { throw "revoke removed the unrelated rule $rule" }
    }
    if ($r.Output -match 'removes only the entries THIS SCRIPT owns') {
        throw ("the message still claims it removes ONLY what this script owns, while it just " +
               "removed two rules this script never added. Output:`n$($r.Output)")
    }
    if ($r.Output -notmatch 'whether or not') {
        throw ("the message does not tell the operator that these rule strings go whether or " +
               "not this script added them. Output:`n$($r.Output)")
    }
    if ($r.Output -notmatch 'nothing\s+anywhere records who added a rule') {
        throw ("the message does not say that no provenance exists. Output:`n$($r.Output)")
    }
    Remove-Backups $f; Remove-Fixture $f
}

# ===== F4: the 'allow' type guard =========================================
function New-RawFixture([string]$json) {
    $p = Join-Path $env:TEMP ("perm-raw-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,8))
    [System.IO.File]::WriteAllText($p, $json, (New-Object System.Text.UTF8Encoding($false)))
    return $p
}

Check "an 'allow' that is a JSON OBJECT is refused, not counted and committed (F4)" {
    <#
      Measured before the guard: "1 rule(s)", exit 0, and the script COMMITTED
      an allow list whose first element was {"a":1}. D3's finding was a
      confident wrong number from an un-normalised allow key; this is the same
      key producing the same confident wrong number, plus a malformed allow
      list written into a security-relevant file.
    #>
    $p = New-RawFixture '{"permissions":{"allow":{"a":1}},"model":"x"}'
    $before = [System.IO.File]::ReadAllBytes($p)
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-ExitNonZero $r 'an allow key that is a JSON object'
    if ($r.Output -match 'rule\(s\)') { throw "it COUNTED a non-list allow. Output:`n$($r.Output)" }
    if ($r.Output -notmatch 'not a JSON array') { throw "the refusal did not name the cause. Output:`n$($r.Output)" }
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "it modified the file" }
    if ((Get-Litter $p '.bak-*').Count -gt 0) { throw "it took a backup for a run that could never commit" }
    Remove-Fixture $p
}

Check "an 'allow' that is a bare STRING is refused, not silently made a list (F4)" {
    # Measured before the guard: silently rewritten to ["Bash(x)","Bash(gh pr merge:*)"].
    # Coercion here is a silent rewrite of rules the operator never asked this
    # script to touch -- the same move as Move-Item quietly clearing READ-ONLY.
    $p = New-RawFixture '{"permissions":{"allow":"Bash(x)"},"model":"x"}'
    $before = [System.IO.File]::ReadAllBytes($p)
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-ExitNonZero $r 'a scalar allow key'
    if ($r.Output -notmatch 'not a JSON array') { throw "the refusal did not name the cause. Output:`n$($r.Output)" }
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "it rewrote a scalar into a list" }
    Remove-Fixture $p
}

Check "an 'allow' LIST containing a non-string is refused by INDEX (F4)" {
    # Measured before the guard: "2 rule(s)" for one rule, and the null was
    # preserved into the committed file.
    $p = New-RawFixture '{"permissions":{"allow":[null,"Bash(dir:*)"]},"model":"x"}'
    $before = [System.IO.File]::ReadAllBytes($p)
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-ExitNonZero $r 'a null inside the allow list'
    if ($r.Output -match 'current allow list: 2 rule') {
        throw "it counted the null as a rule. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'index 0') { throw "the refusal did not name the offending entry. Output:`n$($r.Output)" }
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "it modified the file" }
    Remove-Fixture $p
}

Check "an 'allow' list of plain strings is still accepted (the F4 control)" {
    # The guard must refuse malformed shapes without refusing the normal one.
    $p = New-RawFixture '{"permissions":{"allow":["Bash(dir:*)"]},"model":"x"}'
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-Exit0 $r 'a well-formed allow list'
    if ((Get-Allow $p) -notcontains 'Bash(gh pr merge:*)') { throw "the rule did not land" }
    Remove-Backups $p; Remove-Fixture $p
}

# ===== F1: an ACCESS failure is not a finding about CONTENT ================
Check "a commit failure on an UNREADABLE destination reports UNKNOWN, never 'does not parse' (F1)" {
    <#
      THE REGRESSION CASE. The destination is held with FileShare::None the
      instant the candidate appears. Get-FileHashHex and ReadAllText both ask
      for FileShare.Read, so both fail -- which is why the morning handler's
      "$postHash -eq $null" ALWAYS implied "$destParses -eq $false", and every
      unhashable-but-present run printed an honest "UNKNOWN" row and then
      contradicted it two lines later with

          WARN settings.json EXISTS but does not parse.
          STOP ... settings.json is NOT in its pre-commit state.

      Ground truth at that same instant, read through the handle doing the
      denying: byte-identical to pre-run, and valid JSON. The operator was told
      their settings file was corrupt and handed a Copy-Item that would have
      clobbered it.
    #>
    $f = $null
    $r = $null
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        if ($f) { Remove-Backups $f; Remove-Fixture $f }
        $f = New-Fixture
        $r = Invoke-ScriptWithCommitFailure -SettingsFile $f -Injection LockExclusive
        if ($r.Induced -and $r.Output -match 'the commit FAILED') { break }
        $r = $null
    }
    if (-not $r) {
        throw "could not induce a commit failure against an exclusively held destination in 6 attempts -- the case proves nothing, so it fails"
    }
    Assert-ExitNonZero $r 'commit against an exclusively held destination'

    # Ground truth, measured through the denying handle while the handler ran.
    if ($r.GroundTruthParses -ne $true) {
        throw "fixture check: the destination was NOT valid JSON at the moment of the failure, so this case is not testing what it claims"
    }

    # 1. It must not turn "could not read" into a claim about content.
    if ($r.Output -match 'does not parse') {
        throw "the handler claimed the file 'does not parse' when it could not read it at all. Output:`n$($r.Output)"
    }
    if ($r.Output -match 'is NOT in its pre-commit state') {
        throw "the handler asserted the file changed when it could not read or hash it. Output:`n$($r.Output)"
    }
    # 2. It must say so.
    if ($r.Output -notmatch 'could NOT BE READ') {
        throw "the handler did not report that the file could not be read. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'NOT MEASURED') {
        throw "the MEASURED table did not mark the unmeasured row as unmeasured. Output:`n$($r.Output)"
    }
    # 3. And it must NOT recommend overwriting an intact file on the strength
    #    of an access error.
    if ($r.Output -match "recover with:\s+Copy-Item") {
        throw "the handler offered a Copy-Item over a destination it could not read. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'DO NOT overwrite') {
        throw "the handler did not warn against overwriting an unread file. Output:`n$($r.Output)"
    }
    # 4. Nothing may be deleted when nothing is known.
    if ((Get-Litter $f '.candidate-*').Count -eq 0) {
        throw "the handler deleted the candidate on an undetermined outcome"
    }
    if ((Get-Litter $f '.bak-*').Count -eq 0) { throw "the handler deleted the backup" }

    # 5. And the file really was untouched.
    $post = [System.IO.File]::ReadAllBytes($f)
    if (Compare-Object $r.GroundTruthBytes $post) { throw "the destination changed after the run" }
    Remove-Backups $f; Remove-Fixture $f
}

# ===== F2: the branches nothing ever reached ==============================
Check "handler branch 'unparseable': read successfully and NOT JSON (F2)" {
    $r = Invoke-FailureHandlerBranch -Branch unparseable
    if ($r.ExitCode -eq 99) { throw "the handler RETURNED; it is documented never to return" }
    if ($r.ExitCode -eq 0)  { throw "the handler exited 0 on a failed commit" }
    if ($r.Output -notmatch 'was READ, and does NOT parse') {
        throw "did not report a read-and-invalid destination. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'readable \.+ yes') {
        throw "the MEASURED table did not record that the file WAS readable. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch "recover with:\s+Copy-Item") {
        throw "no recovery command for a destination that is present and invalid. Output:`n$($r.Output)"
    }
    if (-not (Test-Path -LiteralPath $r.Candidate)) { throw "it deleted the candidate on a partial failure" }
    Remove-BranchFiles $r
}

Check "handler branch 'altered': present, valid, and hashes DIFFER (F2)" {
    $r = Invoke-FailureHandlerBranch -Branch altered
    if ($r.ExitCode -eq 99) { throw "the handler RETURNED; it is documented never to return" }
    if ($r.ExitCode -eq 0)  { throw "the handler exited 0 on a failed commit" }
    if ($r.Output -match 'is UNCHANGED') { throw "claimed UNCHANGED about a file whose hash differs. Output:`n$($r.Output)" }
    if ($r.Output -notmatch 'NOT byte-identical to its pre-commit state') {
        throw "did not report the altered destination. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'byte-identical to pre-commit False') {
        throw "the MEASURED table did not record the comparison. Output:`n$($r.Output)"
    }
    # F3: the commit message claimed the non-identical branches print "a
    # recovery command". This one printed only "compare against: <path>".
    if ($r.Output -notmatch "recover with:\s+Copy-Item") {
        throw "the altered branch printed no recovery command, while the verdict says to recover using the paths listed above. Output:`n$($r.Output)"
    }
    if (-not (Test-Path -LiteralPath $r.Candidate)) { throw "it deleted the candidate on a partial failure" }
    Remove-BranchFiles $r
}

Check "handler branch 'identity-unknown': valid, but no pre-commit hash to compare (F2)" {
    # Readable and valid JSON, but one of the two hashes was never taken. The
    # handler may not call that "altered" any more than it may call an
    # unreadable file "invalid" -- same F1 rule, other input.
    $r = Invoke-FailureHandlerBranch -Branch 'identity-unknown'
    if ($r.ExitCode -eq 99) { throw "the handler RETURNED; it is documented never to return" }
    if ($r.Output -match 'NOT byte-identical') {
        throw "asserted the file differs when no comparison was possible. Output:`n$($r.Output)"
    }
    if ($r.Output -match 'is UNCHANGED') {
        throw "asserted the file is unchanged when no comparison was possible. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'NOT DETERMINED') {
        throw "did not report the comparison as undetermined. Output:`n$($r.Output)"
    }
    Remove-BranchFiles $r
}

Check "handler branch 'unchanged' and 'absent' via the same axis (F2)" {
    $u = Invoke-FailureHandlerBranch -Branch unchanged
    if ($u.Output -notmatch 'is UNCHANGED') { throw "byte-identical destination not reported UNCHANGED. Output:`n$($u.Output)" }
    if (Test-Path -LiteralPath $u.Candidate) { throw "the verified-unchanged branch did not discard the candidate" }
    Remove-BranchFiles $u

    $a = Invoke-FailureHandlerBranch -Branch absent
    if ($a.Output -notmatch 'NO LONGER EXISTS') { throw "absent destination not reported. Output:`n$($a.Output)" }
    if ($a.Output -match 'is UNCHANGED')        { throw "claimed UNCHANGED about a file that is gone. Output:`n$($a.Output)" }
    if (-not (Test-Path -LiteralPath $a.Candidate)) { throw "it deleted the candidate when the destination was gone" }
    Remove-BranchFiles $a
}

Check "handler branch 'unreadable' via the axis, driven directly (F2)" {
    $r = Invoke-FailureHandlerBranch -Branch unreadable
    if ($r.ExitCode -eq 99) { throw "the handler RETURNED; it is documented never to return" }
    if ($r.Output -match 'does not parse') { throw "content claim from an access failure. Output:`n$($r.Output)" }
    if ($r.Output -notmatch 'could NOT BE READ') { throw "did not report the access failure. Output:`n$($r.Output)" }
    Remove-BranchFiles $r
}

# ===== F3: a MEASURED table must not print unmeasured rows ================
Check "the 'timestamped backup' row is MEASURED, and a missing backup is never offered as a recovery path (F3)" {
    <#
      That row printed $Backup unconditionally, under a heading that says
      MEASURED, while the two rows immediately above it were Test-Path-guarded
      and printed 'not created'/'no'. Both recovery commands the handler emits
      are built from that path, so an unverified row means handing the operator
      a Copy-Item from a file that is not there.
    #>
    $r = Invoke-FailureHandlerBranch -Branch unparseable -NoBackup
    if ($r.ExitCode -eq 99) { throw "the handler RETURNED" }
    if ($r.Output -notmatch 'timestamped backup \.+ NOT PRESENT') {
        throw "the row printed a backup path that does not exist. Output:`n$($r.Output)"
    }
    if ($r.Output -match "recover with:\s+Copy-Item") {
        throw "it offered a Copy-Item from a backup that is not there. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'backup is NOT present') {
        throw "it did not say loudly that there is nothing to restore from. Output:`n$($r.Output)"
    }
    Remove-BranchFiles $r
}

# ===== F2: and the axis must be able to SEE an uncovered branch ============
Check "every verdict branch in the handler is named by the axis AND exercised by a test (F2)" {
    <#
      The finding behind this case was that half the handler's decision surface
      had no assertion and no mutant, and nothing in the suite could say so.
      This is what says so. Add a branch to Write-CommitFailureReport with its
      marker and this fails until the axis names it and a test drives it.

      It runs last on purpose: it reads what the cases above actually did.
    #>
    $markers = Get-HandlerBranchMarkers
    if ($markers.Count -lt 6) {
        throw "expected at least 6 branch markers in the handler, found $($markers.Count): $($markers -join ', ')"
    }
    $modes = @((Get-Command Invoke-FailureHandlerBranch).Parameters['Branch'].Attributes |
               Where-Object { $_ -is [System.Management.Automation.ValidateSetAttribute] } |
               ForEach-Object { $_.ValidValues })
    foreach ($m in $markers) {
        if ($modes -notcontains $m) {
            throw "the handler has a verdict branch '$m' that the failure-mode axis cannot even name -- it is untestable by construction"
        }
    }
    foreach ($m in $modes) {
        if ($markers -notcontains $m) {
            throw "the axis names a failure mode '$m' with no matching branch in the handler -- the axis has gone stale"
        }
    }
    foreach ($m in $markers) {
        if (-not $script:BRANCHES_EXERCISED.ContainsKey($m)) {
            throw "the handler branch '$m' was never exercised by any test in this file"
        }
    }
}

Write-Host ""
Write-Host ("  {0} passed, {1} failed" -f $PASS, $FAIL) -ForegroundColor $(if ($FAIL) { 'Red' } else { 'Green' })
if ($FAIL) { exit 1 }
exit 0
