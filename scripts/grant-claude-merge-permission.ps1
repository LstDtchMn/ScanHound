<#
.SYNOPSIS
    Add the Bash permission rules Claude needs to merge PRs (and, optionally,
    to deploy) without prompting.

.DESCRIPTION
    Written 2026-08-25 at Jesse's request, after `gh pr merge` was refused with:

        Permission for this action was denied by the Claude Code auto mode
        classifier. ... To allow this type of action in the future, the user
        can add a Bash permission rule to their settings.

    WHAT IS ALREADY ALLOWED. `Bash(docker compose:*)` is in the allow list
    today, so `docker compose up -d --build` is very likely permitted already --
    it has simply never been attempted in this session. Only the merge is a
    known refusal. That is why -IncludeDeploy is opt-in and off by default:
    adding a rule for something already permitted is noise, and noise in an
    allow list is how the list stops being read.

    WHAT THIS ACTUALLY GRANTS, stated plainly because an allow list is easy to
    add to and hard to remember:

        Bash(gh pr merge:*)   Claude can land ANY pull request in ANY repo it
                              can reach, into any branch, without asking. It is
                              not scoped to ScanHound, to main, or to PRs
                              Claude opened. The rule syntax has no way to
                              express those limits.

    That is a real widening. Project memory has recorded merge and deploy as
    Jesse-only decisions across many sessions; this reverses that for merges. It
    is Jesse's call to make and this script does not argue with it -- it just
    makes sure the scope is visible before it is granted, and reversible after.

    SAFETY
      * a timestamped backup is written before any change;
      * rules already present are not duplicated;
      * -WhatIf shows the exact change and writes nothing;
      * -Revoke removes the rules again.

.PARAMETER IncludeDeploy
    Also add explicit docker rules for the deploy path. Usually unnecessary --
    see above.

.PARAMETER Revoke
    Remove the rules this script adds, and stop.

.PARAMETER WhatIf
    Show what would change; write nothing.

.EXAMPLE
    .\scripts\grant-claude-merge-permission.ps1 -WhatIf
    .\scripts\grant-claude-merge-permission.ps1
    .\scripts\grant-claude-merge-permission.ps1 -Revoke
#>

[CmdletBinding()]
param(
    [switch]$IncludeDeploy,
    [switch]$Revoke,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

$SettingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'

# The known refusal, and nothing more. Read-only gh commands (view, checks,
# create, run view) already work and do not need rules.
$MergeRules = @(
    'Bash(gh pr merge:*)'
)

# Opt-in only. docker compose:* already covers the deploy.
$DeployRules = @(
    'Bash(docker compose up:*)',
    'Bash(docker compose build:*)',
    'Bash(docker restart:*)'
)

function Write-JsonNoBom {
    <#
      Write JSON as UTF-8 WITHOUT a byte-order mark.

      PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM, and JSON
      parsers reject one. This script did exactly that on 2026-08-25: the rule
      landed correctly, but settings.json went from `{` to `ï»¿{` and
      stopped being strictly-valid JSON -- which risks breaking EVERY setting in
      the file, not just the one being added.

      [System.IO.File]::WriteAllText with a UTF8Encoding constructed as
      ($false) is the only reliable no-BOM write on 5.1.
    #>
    param([string]$Path, [string]$Json)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Json, $utf8NoBom)
}

function Say([string]$m)  { Write-Host "  $m" }
function Good([string]$m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  WARN $m" -ForegroundColor Yellow }
function Die([string]$m)  {
    Write-Host "  STOP $m" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $SettingsPath)) { Die "no settings file at $SettingsPath" }

$raw = Get-Content -Raw -Path $SettingsPath -Encoding UTF8
try { $settings = $raw | ConvertFrom-Json } catch { Die "settings.json is not valid JSON: $_" }

if (-not $settings.permissions) { Die "settings.json has no 'permissions' section" }
$existing = @($settings.permissions.allow)
Say ("current allow list: {0} rule(s)" -f $existing.Count)

$target = @($MergeRules)
if ($IncludeDeploy) { $target += $DeployRules }

# ---------------------------------------------------------------- revoke ----
if ($Revoke) {
    $toRemove = @($existing | Where-Object { $target -contains $_ })
    if ($toRemove.Count -eq 0) { Good "none of these rules are present; nothing to do"; exit 0 }
    Write-Host ""
    Say "would REMOVE:"
    foreach ($r in $toRemove) { Write-Host "      - $r" -ForegroundColor Yellow }
    if ($WhatIf) { Write-Host ""; Warn "-WhatIf: nothing written"; exit 0 }

    $backup = "$SettingsPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $SettingsPath $backup
    Good "backup written to $backup"

    $settings.permissions.allow = @($existing | Where-Object { $target -notcontains $_ })
    Write-JsonNoBom -Path $SettingsPath -Json ($settings | ConvertTo-Json -Depth 20)
    Good ("removed {0} rule(s); allow list is now {1}" -f $toRemove.Count, $settings.permissions.allow.Count)
    Write-Host ""
    Say "Restart Claude Code for this to take effect."
    exit 0
}

# ----------------------------------------------------------------- grant ----
$toAdd = @($target | Where-Object { $existing -notcontains $_ })

Write-Host ""
Write-Host "  This grants Claude the following, WITHOUT prompting:" -ForegroundColor Yellow
Write-Host ""
Write-Host "    gh pr merge   -- land ANY pull request, in ANY repo it can reach," -ForegroundColor Yellow
Write-Host "                     into ANY branch, including main. Not scoped to" -ForegroundColor Yellow
Write-Host "                     ScanHound and not scoped to PRs Claude opened." -ForegroundColor Yellow
if ($IncludeDeploy) {
Write-Host "    docker compose up / build, docker restart" -ForegroundColor Yellow
Write-Host "                  -- recreate running containers." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  Reverse it any time with:  .\scripts\grant-claude-merge-permission.ps1 -Revoke" -ForegroundColor Yellow
Write-Host ""

if ($toAdd.Count -eq 0) {
    Good "every rule is already present; no change needed"
    if (-not $IncludeDeploy) {
        Say "note: 'Bash(docker compose:*)' already covers the deploy path."
    }
    exit 0
}

Say "would ADD:"
foreach ($r in $toAdd) { Write-Host "      + $r" -ForegroundColor Green }
$alreadyThere = @($target | Where-Object { $existing -contains $_ })
foreach ($r in $alreadyThere) { Write-Host "      = $r (already present)" }

if ($WhatIf) { Write-Host ""; Warn "-WhatIf: nothing written"; exit 0 }

$backup = "$SettingsPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item $SettingsPath $backup
Good "backup written to $backup"

$settings.permissions.allow = @($existing + $toAdd)
Write-JsonNoBom -Path $SettingsPath -Json ($settings | ConvertTo-Json -Depth 20)

# Re-read and prove it, rather than trusting the write.
$firstBytes = [System.IO.File]::ReadAllBytes($SettingsPath)[0..2]
if ($firstBytes[0] -eq 0xEF -and $firstBytes[1] -eq 0xBB -and $firstBytes[2] -eq 0xBF) {
    Die "the write left a UTF-8 BOM; settings.json is no longer strict JSON. Restore from $backup"
}
Good "written without a BOM"
$check = (Get-Content -Raw -Path $SettingsPath -Encoding UTF8) | ConvertFrom-Json
$missing = @($toAdd | Where-Object { @($check.permissions.allow) -notcontains $_ })
if ($missing.Count -gt 0) {
    Die ("the write did not take: " + ($missing -join ', ') + " -- restore from $backup")
}
Good ("added {0} rule(s); allow list is now {1}" -f $toAdd.Count, @($check.permissions.allow).Count)

Write-Host ""
Say "Restart Claude Code for this to take effect."
Say "If 'gh pr merge' is still refused afterwards, the block is the auto-mode"
Say "classifier rather than the allow list, and this rule cannot lift it --"
Say "tell Claude and it will stop rather than work around it."
