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

  ==========================================================================
  LIMITS -- what the handler-coverage check does and does not guarantee
  ==========================================================================

  Round 5's version of this docstring said an arm "counts because it EXISTS,
  not because somebody described it". That was false when it was written, and
  it is what let the next hole through. So, plainly:

  IT GUARANTEES, and each of these fails the suite when broken:

    * Write-CommitFailureReport contains no switch, no loop of any kind, and no
      trap. Not "none was found" -- none is PERMITTED (Get-HandlerProhibitedShapes).
    * No assignment to $verdict and no call to Die exists anywhere in that
      function except as a top-level statement of a verdict arm, or of the
      function itself. Again a prohibition, not a search.
    * Every top-level if/elseif/else block of that function that reaches a
      verdict carries exactly one "# BRANCH:" marker; the markers are all
      distinct; the number of arms EQUALS the number of markers in the script;
      that set is exactly the ValidateSet of Invoke-FailureHandlerBranch; and
      every one of them was driven by a case in this file.

  IT DOES NOT GUARANTEE:

    * That the six verdicts are the RIGHT six, or that their wording is right.
      Only the individual branch cases speak to that, and only for the text
      they quote.
    * Anything about any other function in the script. The rules above are
      scoped to Write-CommitFailureReport by name, on purpose -- they would be
      wrong applied to a script that legitimately loops.
    * That a verdict cannot be reached through a shape nobody has thought of.
      The prohibition list is finite and was written on 2026-08-26 against the
      four shapes an adversarial reviewer actually used. A verdict reached from
      inside a function CALLED by the handler, or through a dot-sourced file,
      or by invoking a string, is outside every rule here. The claim being made
      is bounded: the function may not contain the shapes that could hide one,
      NOT that no hidden one exists.
    * That every line of the script is defended. It is not. See PROBE B, D and
      E in tests/mutate_claude_permissions.py, which are DECLARED gaps, and the
      random-line kill rate that the same file publishes as a number. That rate
      is well under 100% and is reported as it comes rather than tuned.

  ==========================================================================

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
    <#
      Every verdict branch of Write-CommitFailureReport carries a marker
      comment on its own line. This returns EVERY marker occurrence in the
      script, in file order, WITH DUPLICATES.

      R1, 2026-08-26: it used to end in "| Sort-Object -Unique", and so did the
      caller. MEASURED: a seventh reachable verdict arm was inserted into the
      same elseif chain carrying a COPY of an existing arm's marker, and the
      suite stayed at 56 passed / 0 failed -- 6 unique markers compared against
      6 unique markers, with the only count assertion written as "-lt 6", which
      can see an arm REMOVED and never one ADDED. De-duplicating is what threw
      the measurement away, so it does not happen here or at the call site.
    #>
    $src = [System.IO.File]::ReadAllText($SCRIPT)
    return @([regex]::Matches($src, '(?m)^\s*#\s*BRANCH:([a-z\-]+)\s*$') |
             ForEach-Object { $_.Groups[1].Value })
}

function Get-HandlerVerdictArms {
    <#
      THE STRUCTURAL READING OF THE HANDLER'S DECISION SURFACE (V1).

      Get-HandlerBranchMarkers above counts "# BRANCH:" comments, which is a
      list of the branches that VOLUNTEERED. A branch that carries no comment is
      invisible to it, so a coverage case built on it can only ever see
      self-declared branches -- MEASURED: a real, reachable seventh verdict arm
      inserted into Write-CommitFailureReport without a marker left the suite at
      49 passed / 0 failed, because 6 markers were compared against 6 modes.

      This reads the arms from the PARSE TREE instead. An arm is a clause or
      else-clause of an if statement that is a DIRECT statement of the function
      body, and it counts as a verdict arm when it reaches a verdict: an
      assignment to $verdict, or a call to Die. Nested ifs inside an arm (the
      $bakExists / $keptCopy sub-cases) are not arms of the decision surface and
      are not counted; the two measurement blocks at the top of the function are
      not counted either, because neither assigns a verdict nor dies.

      Returns one object per verdict arm: its start line in the script AS IT IS
      NOW, and the markers found inside it. Line numbers are computed here and
      never written down, so they cannot go stale.

      WHAT THIS FUNCTION DOES NOT DO, stated here because round 5's version of
      this comment claimed reach it did not have. It reads IfStatementAst and
      nothing else, and it only looks at if statements that are DIRECT
      statements of the function body. A verdict reached from a switch, a
      foreach, a while, a do, a trap, or an if nested inside an arm is invisible
      to it -- all four were demonstrated against round 5 and all four passed at
      56/0. Those shapes are not detected here; they are PROHIBITED outright by
      Get-HandlerProhibitedShapes below, which is a bounded check rather than a
      search. Read the LIMITS section at the top of this file before trusting
      either one further than that.
    #>
    $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($SCRIPT, [ref]$null, [ref]$errs)
    if ($errs -and $errs.Count -gt 0) { throw "the script under test does not parse: $($errs[0].Message)" }
    $fns = @($ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $n.Name -eq 'Write-CommitFailureReport' }, $true))
    if ($fns.Count -ne 1) {
        throw "expected exactly one definition of Write-CommitFailureReport, found $($fns.Count)"
    }
    $arms = @()
    foreach ($st in $fns[0].Body.EndBlock.Statements) {
        if ($st -isnot [System.Management.Automation.Language.IfStatementAst]) { continue }
        $blocks = @()
        foreach ($c in $st.Clauses) { $blocks += ,$c.Item2 }
        if ($null -ne $st.ElseClause) { $blocks += ,$st.ElseClause }
        foreach ($b in $blocks) {
            $hits = @($b.FindAll({ param($n)
                ($n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                 $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
                 $n.Left.VariablePath.UserPath -eq 'verdict') -or
                ($n -is [System.Management.Automation.Language.CommandAst] -and
                 $n.GetCommandName() -eq 'Die') }, $true))
            if ($hits.Count -eq 0) { continue }
            $arms += [pscustomobject]@{
                Line    = $b.Extent.StartLineNumber
                Markers = @([regex]::Matches($b.Extent.Text, '(?m)^\s*#\s*BRANCH:([a-z\-]+)\s*$') |
                            ForEach-Object { $_.Groups[1].Value })
            }
        }
    }
    return $arms
}

function Get-HandlerProhibitedShapes {
    <#
      CLOSURE BY PROHIBITION, NOT BY DETECTION (R2, R3).

      Five rounds of this file have been a detector chasing shapes, and the
      detector lost every round. Round 5's walker reads IfStatementAst and only
      at the function body's top level. Three shapes were then demonstrated
      against it, each a REACHABLE verdict the walker could not see, each
      leaving the suite at 56 passed / 0 failed:

        * a switch ($true) { { $keptCopy -and -not $verifiedIdentical } { ... Die } }
          at the top level of the function body, carrying no marker;
        * an if nested INSIDE the already-marked BRANCH:identity-unknown arm,
          with its own Warn, its own $verdict and its own Die -- the arm still
          carried exactly one marker, so all four marker rules passed;
        * (R1, fixed above) a seventh top-level arm carrying a DUPLICATE of
          another arm's marker.

      Writing a wider walker would invite a sixth round. This function does the
      other thing instead. Write-CommitFailureReport is a small report function:
      it measures, prints a table, picks one of six verdicts and dies. It has no
      legitimate need for a loop, a switch or a trap, and no legitimate need for
      a verdict assignment or a Die anywhere except the top level of a verdict
      arm or the top level of the function itself. So that is ASSERTED, and the
      claim made is the honest one: not "no hidden verdict path exists" -- which
      is a search -- but "the function does not contain the shapes a hidden
      verdict path would have to use", which is a bounded, decidable check.

      An author who genuinely needs one of these shapes gets a failing test
      telling them to restructure the function or to extend this rule
      deliberately. That is the intended cost.

      Returns a list of human-readable violations; empty means clean.
    #>
    $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($SCRIPT, [ref]$null, [ref]$errs)
    if ($errs -and $errs.Count -gt 0) { throw "the script under test does not parse: $($errs[0].Message)" }
    $fns = @($ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $n.Name -eq 'Write-CommitFailureReport' }, $true))
    if ($fns.Count -ne 1) {
        throw "expected exactly one definition of Write-CommitFailureReport, found $($fns.Count)"
    }
    $body = $fns[0].Body
    $violations = @()

    # RULE A -- branching constructs the function may not contain AT ALL.
    # LoopStatementAst is the common base of foreach, for, while, do/while and
    # do/until, so all five are covered by one test and a sixth loop form added
    # to the language would be covered too.
    foreach ($h in @($body.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.SwitchStatementAst] -or
            $n -is [System.Management.Automation.Language.LoopStatementAst]   -or
            $n -is [System.Management.Automation.Language.TrapStatementAst] }, $true))) {
        $violations += ("line {0}: a {1} -- Write-CommitFailureReport may not contain a switch, a loop or a trap" `
                        -f $h.Extent.StartLineNumber, $h.GetType().Name)
    }

    # RULE B -- a verdict may only be reached from the top level of a verdict
    # arm, or from the top level of the function. Anything deeper is a verdict
    # the marker rules cannot name and no test drives.
    $allowed = New-Object System.Collections.ArrayList
    $null = $allowed.Add($body.EndBlock)
    foreach ($st in $body.EndBlock.Statements) {
        if ($st -isnot [System.Management.Automation.Language.IfStatementAst]) { continue }
        foreach ($c in $st.Clauses) { $null = $allowed.Add($c.Item2) }
        if ($null -ne $st.ElseClause) { $null = $allowed.Add($st.ElseClause) }
    }
    foreach ($n in @($body.FindAll({ param($n)
            ($n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
             $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
             $n.Left.VariablePath.UserPath -eq 'verdict') -or
            ($n -is [System.Management.Automation.Language.CommandAst] -and
             $n.GetCommandName() -eq 'Die') }, $true))) {
        $isDie = $n -is [System.Management.Automation.Language.CommandAst]
        # A command's own parent is its pipeline; the pipeline's parent is the
        # block it is a statement of.
        $container = $(if ($isDie) { $n.Parent.Parent } else { $n.Parent })
        $ok = $false
        foreach ($a in $allowed) { if ([object]::ReferenceEquals($a, $container)) { $ok = $true; break } }
        if (-not $ok) {
            $what = $(if ($isDie) { 'a call to Die' } else { 'an assignment to $verdict' })
            $violations += ("line {0}: {1} that is NOT a top-level statement of a verdict arm or of the function itself" `
                            -f $n.Extent.StartLineNumber, $what)
        }
    }
    return @($violations)
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
        [switch]$NoBackup,
        # V2. Hold the candidate open with FileShare::Read -- readable and
        # hashable, but NOT deletable, because deletion needs FileShare.Delete.
        # The handler's Remove-Item then fails silently (-ErrorAction
        # SilentlyContinue) and the question is what it says afterwards.
        [switch]$HoldCandidate
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
    $candHold = $null
    if ($HoldCandidate) {
        $candHold = New-Object System.IO.FileStream($cand,
            [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
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
    } finally {
        if ($hold)     { $hold.Dispose() }
        if ($candHold) { $candHold.Dispose() }
    }

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
    # THE NEGATIVE DIRECTION (F5's rule, applied to the case added this round).
    # "the output mentions BOM" is satisfied by a message that says a BOM is NOT
    # what it found, so the two warnings the read gate now chooses between have
    # to be told apart in BOTH directions -- this one, and the SHORTER-THAN-THREE
    # -BYTES case below. Without this, anything that always chose the length
    # message would pass here.
    if ($r.Output -notmatch 'already has a BOM') {
        throw "a real BOM was not reported as a BOM. Output:`n$($r.Output)"
    }
    if ($r.Output -match 'SHORTER THAN 3 BYTES') {
        throw "a BOM-ed file was reported as too short. Output:`n$($r.Output)"
    }
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
    # V2. The row must be true when it is PRINTED, not merely true when it was
    # taken. The discard used to happen inside the branch, AFTER this table, so
    # the operator read "candidate still present .... <path>" about a file that
    # was already gone by the time the sentence below it was written.
    if ($u.Output -notmatch 'candidate still present \.+ no') {
        throw ("the MEASURED table lists the candidate as still present while the run went on to " +
               "discard it -- the row was printed before the deletion. Output:`n$($u.Output)")
    }
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
    # V6. The branch exists so an ACCESS result is never reported as a CONTENT
    # finding; it then named ONE cause as fact -- "Something holds it open
    # exclusively, or denied access." A read can also fail because the file went
    # away between the Test-Path and the read, or on an I/O or path-length
    # error. Nothing here observed which.
    if ($r.Output -notmatch 'did NOT determine which') {
        throw "the branch states a cause for the access failure that it never observed. Output:`n$($r.Output)"
    }
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


# ===== V2: a branch may not state an outcome it did not check ==============
Check "BRANCH:unchanged does not claim a discard it could not perform (V2)" {
    <#
      The branch ran

          if ($candLeft) { Remove-Item $Candidate -Force -ErrorAction SilentlyContinue }

      and then printed "The candidate was discarded." unconditionally -- no
      Test-Path, no re-check. -ErrorAction SilentlyContinue is precisely the
      switch that makes a failed deletion silent, so the sentence was an
      assertion about disk state taken from an attempt, which is D1's finding
      inside the function this round rewrote.

      Hold the candidate with FileShare::Read: readable and hashable, so the
      branch still reaches 'unchanged', but not deletable.
    #>
    $r = Invoke-FailureHandlerBranch -Branch unchanged -HoldCandidate
    if ($r.ExitCode -eq 99) { throw "the handler RETURNED; it is documented never to return" }
    if (-not (Test-Path -LiteralPath $r.Candidate)) {
        throw "fixture check: the candidate WAS deleted, so this case is not testing what it claims"
    }
    if ($r.Output -match 'The candidate was discarded') {
        throw ("the handler said the candidate was discarded while it is still on disk at " +
               "$($r.Candidate). Output:`n$($r.Output)")
    }
    if ($r.Output -notmatch 'could NOT be discarded') {
        throw "the handler did not report that the discard failed. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'candidate still present \.+ .+\.candidate-') {
        throw "the MEASURED table did not show the candidate as still present. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'is UNCHANGED') {
        throw "the destination IS byte-identical; that verdict must not change. Output:`n$($r.Output)"
    }
    Remove-BranchFiles $r
}

# ===== V3: a probe that turned into a defect ===============================
Check "a settings file SHORTER THAN THREE BYTES is reported as SHORT, not as a BOM (V3)" {
    <#
      Found by mutating a line no finding had named: Test-NoBom's

          if ($n -lt 3) { return $false }

      Flipping it to $true left the suite at 49 passed / 0 failed. Looking at
      what the guard actually feeds, the read gate turned BOTH of Test-NoBom's
      two meanings into one sentence: measured against a two-byte file
      containing {} -- which has no BOM at all -- the script printed

          WARN the CURRENT settings.json already has a BOM; it is not strict JSON.

      A cause stated without being observed, in a script whose last three rounds
      were about exactly that. The gate now measures the length and says which,
      and this case is what makes the length guard defended rather than merely
      present.

      R4, 2026-08-26: THIS CASE WAS ITSELF AN UNDEFENDED-LINE FACTORY. The V3
      fix added four Warn lines to that gate and this case asserted on the text
      of ONE of them, so deleting any of the other three left the suite at 56
      passed / 0 failed. Round 5 published undefended lines as an INHERITED
      class while newly creating three, which the disclosure did not say. Each
      of the four sentences now has an assertion of its own, keyed to text only
      that line carries.
    #>
    $p = Join-Path $env:TEMP ("perm-short-{0}.json" -f [guid]::NewGuid().ToString('N').Substring(0,8))
    [System.IO.File]::WriteAllText($p, '{}', (New-Object System.Text.UTF8Encoding($false)))
    if ((New-Object System.IO.FileInfo $p).Length -ge 3) { throw "fixture check: the file is not shorter than 3 bytes" }
    $before = [System.IO.File]::ReadAllBytes($p)
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-ExitNonZero $r 'a two-byte settings file'
    if ($r.Output -match 'already has a BOM') {
        throw ("the script reported a BOM in a file with no BOM in it -- there are not three bytes " +
               "here to be one. Output:`n$($r.Output)")
    }
    if ($r.Output -notmatch 'SHORTER THAN 3 BYTES') {
        throw "the script did not report the length it actually measured. Output:`n$($r.Output)"
    }
    # R4: one assertion per sentence the gate prints, so no line of it can be
    # deleted without a case saying so.
    if ($r.Output -notmatch 'not a finding that a BOM is present') {
        throw ("the gate reported the LENGTH but never said that this is not a BOM finding, which " +
               "is the whole distinction V3 exists to draw. Output:`n$($r.Output)")
    }
    if ($r.Output -notmatch 'cannot hold a permissions') {
        throw ("the gate did not say why a file this short fails the checks below, so the operator " +
               "is left to guess at the refusal that follows. Output:`n$($r.Output)")
    }
    if ($r.Output -notmatch 'the checks below will refuse it') {
        throw ("the gate did not tell the operator that a refusal is coming. Output:`n$($r.Output)")
    }
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "it modified the file" }
    Remove-Fixture $p
}

# ===== V5: a refusal may not block the undo ================================
Check "-Revoke over an allow list with a NULL entry still removes the owned rules (V5)" {
    <#
      MEASURED against the F4 guard as first written: with
      ["Bash(gh pr merge:*)","Bash(docker restart:*)",null] and -Revoke it exited
      1, wrote nothing, and left BOTH standing authorizations in place. The
      .DESCRIPTION of the same script rejects a provenance sidecar because a lost
      sidecar means "a revoke that leaves a standing authorization in place", and
      OPS-6 calls that worse than a failed grant -- so the guard produced the
      outcome the design argument forbids, from the other side.

      A grant is still refused (the case below this one); an undo is not.
    #>
    $p = New-RawFixture '{"permissions":{"allow":["Bash(gh pr merge:*)","Bash(docker restart:*)",null,"Bash(dir:*)"]},"model":"x"}'
    $r = Invoke-Script @('-SettingsPath', $p, '-Revoke')
    Assert-Exit0 $r 'revoke over a list containing a null'
    $o = [System.IO.File]::ReadAllText($p) | ConvertFrom-Json
    $allow = @($o.permissions.allow)
    foreach ($rule in @('Bash(gh pr merge:*)', 'Bash(docker restart:*)')) {
        if ($allow -contains $rule) { throw "the undo left the standing authorization $rule in place" }
    }
    if ($allow -notcontains 'Bash(dir:*)') { throw "the undo removed an unrelated rule" }
    # The null is not ours to delete. It was there before; it is there after.
    if ($allow.Count -ne 2) { throw "expected 2 entries (the null and Bash(dir:*)), got $($allow.Count): [$($allow -join ', ')]" }
    if ($null -ne $allow[0]) { throw "the null entry was not preserved at its own index; entry 0 is '$($allow[0])'" }
    # And it is not counted as a rule.
    if ($r.Output -notmatch 'current allow list: 3 rule') {
        throw "the null was counted as a rule, or the count is wrong. Output:`n$($r.Output)"
    }
    if ($r.Output -notmatch 'not a string') {
        throw "the run did not tell the operator about the entry it preserved. Output:`n$($r.Output)"
    }
    Remove-Backups $p; Remove-Fixture $p
}

Check "-WhatIf over a malformed allow writes nothing AND reports success (V5)" {
    <#
      .PARAMETER WhatIf says "Show the change; write nothing", and the contract
      at the top of this file says a WhatIf run exits 0 with the bytes
      unchanged. The F4 guard exited 1 for both malformed shapes, which is a
      preview reporting a failure it did not have. Neither shape was tested.
    #>
    foreach ($json in @('{"permissions":{"allow":[null,"Bash(dir:*)"]},"model":"x"}',
                        '{"permissions":{"allow":{"a":1}},"model":"x"}')) {
        $p = New-RawFixture $json
        $before = [System.IO.File]::ReadAllBytes($p)
        $r = Invoke-Script @('-SettingsPath', $p, '-WhatIf')
        Assert-Exit0 $r "-WhatIf over $json"
        if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "-WhatIf modified $json" }
        if ($r.Output -notmatch 'would REFUSE') {
            throw "-WhatIf did not say a real run would refuse. Output:`n$($r.Output)"
        }
        if ($r.Output -notmatch 'nothing written') {
            throw "-WhatIf did not say it wrote nothing. Output:`n$($r.Output)"
        }
        Remove-Fixture $p
    }
}

Check "-Revoke over an allow value that is NOT an array refuses, and NAMES what is still in force (V5)" {
    <#
      The one direction that still refuses: there is no list to remove an
      element from, and making one out of a scalar is the coercion this script
      does not do. A refusal is defensible; a refusal that leaves the operator
      guessing what is still authorised is not.
    #>
    $p = New-RawFixture '{"permissions":{"allow":"Bash(gh pr merge:*)"},"model":"x"}'
    $before = [System.IO.File]::ReadAllBytes($p)
    $r = Invoke-Script @('-SettingsPath', $p, '-Revoke')
    Assert-ExitNonZero $r 'revoke over a scalar allow'
    if (Compare-Object $before ([System.IO.File]::ReadAllBytes($p))) { throw "it modified the file" }
    if ($r.Output -notmatch 'THIS UNDO CANNOT RUN') {
        throw "the refusal did not tell the operator the undo did not happen. Output:`n$($r.Output)"
    }
    # The message says the rule APPEARS IN THAT VALUE, not that it is in force:
    # a substring match on a shape this script cannot parse is not a finding
    # about what Claude Code will honour. Naming what was seen is the point.
    if ($r.Output -notmatch 'Bash\(gh pr merge:\*\)') {
        throw "the refusal did not name the rule it could see. Output:`n$($r.Output)"
    }
    Remove-Fixture $p
}

# ===== V6: shapes that were accepted, recorded as decisions ================
Check "an EMPTY-STRING entry is preserved, not curated away (V6 control)" {
    <#
      MEASURED: an allow list containing "" is accepted and committed. That is
      recorded here as a DECISION rather than left for the next reviewer to
      re-derive. An empty string is a string, so the type guard has nothing to
      say about it; it is not in the owned vocabulary, so revoke has nothing to
      say about it either. This script adds and removes its own four rule
      strings -- it is not a linter for a file it does not own, and silently
      dropping an entry the operator put there is the coercion F4 refused.
    #>
    $p = New-RawFixture '{"permissions":{"allow":["","Bash(b:*)"]},"model":"x"}'
    $r = Invoke-Script @('-SettingsPath', $p)
    Assert-Exit0 $r 'grant over a list containing an empty string'
    $allow = Get-Allow $p
    if ($allow -notcontains 'Bash(gh pr merge:*)') { throw "the rule did not land" }
    if ($allow.Count -ne 3) { throw "expected 3 entries, got $($allow.Count): [$($allow -join '|')]" }
    if ($allow[0] -ne '') { throw "the empty-string entry was not preserved at its own index" }
    if ($r.Output -notmatch 'current allow list: 2 rule') {
        throw "an empty string is a string and is counted as an entry. Output:`n$($r.Output)"
    }
    Remove-Backups $p; Remove-Fixture $p
}

Check "duplicate owned rules: a grant is a no-op, and a revoke removes EVERY copy (V6 control)" {
    <#
      MEASURED: duplicates are counted as separate entries -- "3 rule(s)" for two
      distinct rules. Recorded as a decision, with the half that actually
      matters asserted: a revoke must remove EVERY copy. One surviving duplicate
      is a standing authorization that an operator was told had been revoked,
      which is the OPS-6 failure this script exists to prevent.
    #>
    $p = New-RawFixture '{"permissions":{"allow":["Bash(gh pr merge:*)","Bash(dir:*)","Bash(gh pr merge:*)"]},"model":"x"}'
    $g = Invoke-Script @('-SettingsPath', $p)
    Assert-Exit0 $g 'grant over duplicated owned rules'
    if ($g.Output -notmatch 'current allow list: 3 rule') {
        throw "duplicates are counted per entry; the count changed. Output:`n$($g.Output)"
    }
    if ($g.Output -notmatch 'already present') { throw "a grant of a rule already present is not a no-op. Output:`n$($g.Output)" }
    if ((Get-Allow $p).Count -ne 3) { throw "the grant changed the list" }
    $r = Invoke-Script @('-SettingsPath', $p, '-Revoke')
    Assert-Exit0 $r 'revoke over duplicated owned rules'
    $left = Get-Allow $p
    if ($left -contains 'Bash(gh pr merge:*)') {
        throw "a duplicate copy of the rule SURVIVED the undo: [$($left -join '|')]"
    }
    if ($left.Count -ne 1) { throw "expected 1 entry left, got $($left.Count): [$($left -join '|')]" }
    Remove-Backups $p; Remove-Fixture $p
}

# ===== R2/R3: the shapes that could hide a verdict are PROHIBITED ==========
Check "Write-CommitFailureReport contains no shape that could hide a verdict (R2, R3)" {
    <#
      Round 5 answered "is there an undeclared verdict path?" with a walker, and
      the walker was beaten three times running. This case answers a different,
      DECIDABLE question instead: does the function contain any of the shapes a
      hidden verdict path would have to use? See Get-HandlerProhibitedShapes for
      the two rules and for what they deliberately do not claim.

      This is a total closure, not a better detector. It will also reject
      perfectly reasonable code -- a foreach over the recovery paths, say. That
      is the intended cost: restructure the function, or extend the rule on
      purpose and say so here.
    #>
    $bad = Get-HandlerProhibitedShapes
    if ($bad.Count -gt 0) {
        throw ("Write-CommitFailureReport contains $($bad.Count) prohibited shape(s):`n          " +
               ($bad -join "`n          ") +
               "`n          Each of these can carry a verdict that the '# BRANCH:' marker rules " +
               "cannot name and no test drives. Restructure the function, or change the rule in " +
               "Get-HandlerProhibitedShapes deliberately and record why.")
    }
}

# ===== V1/R1: and the coverage case must SEE a branch nobody declared ======
Check "every verdict ARM of the handler is marked, named by the axis, and driven (V1, R1)" {
    <#
      V1. This case used to compare the failure-mode axis against
      Get-HandlerBranchMarkers -- a count of "# BRANCH:" comments. Its commit
      message said it "fails on any branch the axis cannot name or no test
      drives". It did not: it failed only on branches that VOLUNTEERED a marker.

      MEASURED: a real, reachable seventh verdict arm was inserted into
      Write-CommitFailureReport carrying no marker -- "} elseif ($keptCopy) {"
      with a Warn, a Move-Item recovery line and its own $verdict. Suite: 49
      passed, 0 failed, exit 0. Six markers were compared against six modes and
      nothing was looking at the seventh arm. The mutant F2 in
      tests/mutate_claude_permissions.py was only ever killed because it adds a
      marker BY HAND.

      R1, AND IT IS THE SAME MISTAKE ONE LEVEL DOWN. Round 5 moved the arms to
      the parse tree and then threw the measurement away again: both sides of
      the comparison ran through Sort-Object -Unique, and the only count
      assertion was "-lt 6". MEASURED: a SEVENTH reachable arm carrying a COPY
      of another arm's marker left the suite at 56 passed / 0 failed. Round 5's
      own proof used the one shape that survives a dedupe -- an UNMARKED arm --
      so the proof passed while the property did not hold.

      Nothing is de-duplicated now, and the counts are compared for EQUALITY,
      not for "at least". Five things are required:

        1. the arm count EQUALS the number of '# BRANCH:' markers in the script
           -- an added arm and a removed arm are both visible, in both
           directions;
        2. each arm carries exactly one marker -- an arm nothing can name is an
           arm nothing can be shown to test;
        3. the markers are all DISTINCT, so a copied marker cannot stand in for
           an arm that was never described;
        4. the marker set is exactly the axis's ValidateSet;
        5. every one was driven by a case above.

      It runs last on purpose: it reads what the cases above actually did.
    #>
    $arms   = Get-HandlerVerdictArms
    $inFile = Get-HandlerBranchMarkers
    if ($arms.Count -ne $inFile.Count) {
        throw ("Write-CommitFailureReport has $($arms.Count) verdict arm(s) but $($inFile.Count) " +
               "'# BRANCH:' marker(s) in the script. Arms are at line(s) " +
               (($arms | ForEach-Object { $_.Line }) -join ', ') +
               "; markers name [$($inFile -join ', ')]. One arm, one marker -- no more and no fewer.")
    }
    $unmarked = @($arms | Where-Object { $_.Markers.Count -ne 1 })
    if ($unmarked.Count -gt 0) {
        $where = (($unmarked | ForEach-Object { "line $($_.Line) has $($_.Markers.Count) marker(s)" }) -join '; ')
        throw ("Write-CommitFailureReport has $($unmarked.Count) verdict arm(s) that do not carry " +
               "exactly one '# BRANCH:' marker ($where). An arm the axis cannot name is an arm no " +
               "test can be shown to drive -- add the marker, add the mode, and drive it.")
    }
    $fromArms = @($arms | ForEach-Object { $_.Markers[0] })
    $dupes = @($fromArms | Group-Object | Where-Object { $_.Count -gt 1 })
    if ($dupes.Count -gt 0) {
        $which = (($dupes | ForEach-Object { "'$($_.Name)' x$($_.Count)" }) -join '; ')
        throw ("two or more verdict arms carry the SAME '# BRANCH:' marker ($which). A marker names " +
               "one arm; a duplicate lets a second arm hide behind the first one's coverage. Arms are " +
               "at line(s) " + (($arms | ForEach-Object { $_.Line }) -join ', ') + ".")
    }
    if (Compare-Object ($fromArms | Sort-Object) ($inFile | Sort-Object)) {
        throw ("markers inside verdict arms [$($fromArms -join ', ')] differ from markers anywhere in " +
               "the script [$($inFile -join ', ')] -- a marker outside a verdict arm names a branch " +
               "that does not exist.")
    }
    $modes = @((Get-Command Invoke-FailureHandlerBranch).Parameters['Branch'].Attributes |
               Where-Object { $_ -is [System.Management.Automation.ValidateSetAttribute] } |
               ForEach-Object { $_.ValidValues })
    foreach ($m in $fromArms) {
        if ($modes -notcontains $m) {
            throw "the handler has a verdict arm '$m' that the failure-mode axis cannot even name -- it is untestable by construction"
        }
    }
    foreach ($m in $modes) {
        if ($fromArms -notcontains $m) {
            throw "the axis names a failure mode '$m' with no matching arm in the handler -- the axis has gone stale"
        }
    }
    foreach ($m in $fromArms) {
        if (-not $script:BRANCHES_EXERCISED.ContainsKey($m)) {
            throw "the handler arm '$m' was never exercised by any test in this file"
        }
    }
}

Write-Host ""
Write-Host ("  {0} passed, {1} failed" -f $PASS, $FAIL) -ForegroundColor $(if ($FAIL) { 'Red' } else { 'Green' })
if ($FAIL) { exit 1 }
exit 0
