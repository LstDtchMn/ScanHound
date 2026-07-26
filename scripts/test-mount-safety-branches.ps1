# Exercise mount-nas-shares.ps1's safety-critical decision branches with a
# stubbed docker, per peer review: these paths must be tested without breaking
# the live read-write TV mount.
#
# The stub controls `docker ps/exec/stop` outcomes; the host mount stage is
# short-circuited by pointing the script at a fake `wsl` that returns the
# desired exit code (2 = critical share failed, 0 = all good).

$ErrorActionPreference = "Continue"
$stub   = Join-Path $PSScriptRoot "test-stub"
$script = Join-Path $PSScriptRoot "mount-nas-shares.ps1"

function Run-Case {
    param($Name, $WslRc, $Ps, $ExecRc, $StopRc, $PsAfter, $ExpectExit, $ExpectText)

    Remove-Item "$env:TEMP\sh_stub_stopped.flag" -Force -ErrorAction SilentlyContinue
    # fake wsl: returns the host-stage exit code we want to simulate
    Set-Content "$stub\wsl.bat" "@echo off`r`nexit /b $WslRc" -Encoding ascii

    $env:SH_STUB_PS       = $Ps
    $env:SH_STUB_EXEC_RC  = $ExecRc
    $env:SH_STUB_STOP_RC  = $StopRc
    $env:SH_STUB_PS_AFTER = $PsAfter

    $out = & powershell -NoProfile -ExecutionPolicy Bypass -Command "
        `$env:Path = '$stub;' + `$env:Path
        & '$script' 2>&1 | Out-String
        exit `$LASTEXITCODE" 2>&1 | Out-String
    $code = $LASTEXITCODE

    $okExit = ($code -eq $ExpectExit)
    $okText = ($out -match [regex]::Escape($ExpectText))
    $verdict = if ($okExit -and $okText) { "PASS" } else { "FAIL" }
    Write-Output "[$verdict] $Name"
    Write-Output "        exit=$code (expected $ExpectExit)"
    Write-Output "        expected text present: $okText  ('$ExpectText')"
    if ($verdict -eq "FAIL") {
        Write-Output "        ---- output ----"
        ($out -split "`n" | Select-Object -Last 8) | ForEach-Object { Write-Output "        $_" }
    }
    Write-Output ""
}

Write-Output "=== safety-critical branches (stubbed docker/wsl) ===`n"

# Case 6: critical TV share fails host verification AND the container is blind.
# Must stop the container, verified, and exit 2.
Run-Case -Name "6. critical host failure + blind container -> stop, verified" `
         -WslRc 2 -Ps "scanhound" -ExecRc 2 -StopRc 0 -PsAfter "" `
         -ExpectExit 2 -ExpectText "has been STOPPED (verified not running)"

# Same, but the container is provably healthy: must NOT be stopped.
Run-Case -Name "6b. critical host failure + healthy container -> leave running" `
         -WslRc 2 -Ps "scanhound" -ExecRc 0 -StopRc 0 -PsAfter "" `
         -ExpectExit 2 -ExpectText "Left running; NOT recreated"

# Case 9/3: critical failure, container blind, and `docker stop` FAILS.
# Must NOT claim it was stopped; must exit 7.
Run-Case -Name "9. critical failure + stop FAILS -> honest report, exit 7" `
         -WslRc 2 -Ps "scanhound" -ExecRc 2 -StopRc 1 -PsAfter "scanhound" `
         -ExpectExit 7 -ExpectText "could not be confirmed stopped"

# Stop returns 0 but the container is still running (lying daemon).
Run-Case -Name "9b. stop returns 0 but container still up -> stop-failed, exit 7" `
         -WslRc 2 -Ps "scanhound" -ExecRc 2 -StopRc 0 -PsAfter "scanhound" `
         -ExpectExit 7 -ExpectText "could not be confirmed stopped"

# Critical failure while the container is not running at all: nothing to stop.
Run-Case -Name "6c. critical host failure + container not running -> no start" `
         -WslRc 2 -Ps "" -ExecRc 0 -StopRc 0 -PsAfter "" `
         -ExpectExit 2 -ExpectText "was NOT started"

Remove-Item "$stub\wsl.bat" -Force -ErrorAction SilentlyContinue
Remove-Item "$env:TEMP\sh_stub_stopped.flag" -Force -ErrorAction SilentlyContinue

