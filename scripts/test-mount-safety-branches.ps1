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
    param($Name, $WslRc, $Ps, $ExecRc, $StopRc, $PsAfter, $ExpectExit, $ExpectText,
          $WslRunning = 'docker-desktop', $DockerServerRc = 0)

    Remove-Item "$env:TEMP\sh_stub_stopped.flag" -Force -ErrorAction SilentlyContinue
    # Fake wsl: must answer BOTH the passive readiness probe
    # (`--list --running --quiet`) and the mount invocation. Writing only an
    # unconditional `exit /b` made every readiness check look like a failure.
    $wsl = @(
        '@echo off'
        'if "%1"=="--list" ('
        "    if not `"$WslRunning`"==`"`" echo $WslRunning"
        '    exit /b 0'
        ')'
        "exit /b $WslRc"
    ) -join "`r`n"
    Set-Content "$stub\wsl.bat" $wsl -Encoding ascii
    $env:SH_STUB_DOCKER_SERVER_RC = $DockerServerRc

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

Write-Output "=== readiness gate (the 2026-07-26 boot failure) ===`n"

# The distro is not running. The probe must report NOT READY and must not
# attempt any mount -- and must NOT launch the distro to find out.
Run-Case -Name "R1. distro not running -> not-ready (exit 8), no mount attempted" `
         -WslRc 0 -Ps "scanhound" -ExecRc 0 -StopRc 0 -PsAfter "" `
         -WslRunning "" `
         -ExpectExit 8 -ExpectText "No mount was attempted"

# Distro up but the ENGINE is dead: a healthy client must not pass the gate.
Run-Case -Name "R2. distro up but docker server dead -> not-ready, not mount-failed" `
         -WslRc 0 -Ps "scanhound" -ExecRc 0 -StopRc 0 -PsAfter "" `
         -DockerServerRc 1 `
         -ExpectExit 8 -ExpectText "docker-server-unreachable"

# A deterministic wrong-share condition must fail fast, not sleep through the
# 15s/30s backoff ladder.
Run-Case -Name "R3. wrong share (deterministic) -> not retried" `
         -WslRc 3 -Ps "scanhound" -ExecRc 0 -StopRc 0 -PsAfter "" `
         -ExpectExit 1 -ExpectText "deterministic failure (exit 3) -- not retrying"

Remove-Item "$stub\wsl.bat" -Force -ErrorAction SilentlyContinue
Remove-Item "$env:TEMP\sh_stub_stopped.flag" -Force -ErrorAction SilentlyContinue

