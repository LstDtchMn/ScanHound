@echo off
REM Stub docker for exercising mount-nas-shares.ps1 safety branches without
REM touching production. Behaviour is driven by SH_STUB env vars:
REM   SH_STUB_PS       = names printed by `docker ps` (empty => not running)
REM   SH_STUB_EXEC_RC  = exit code for `docker exec` (the container probe)
REM   SH_STUB_STOP_RC  = exit code for `docker stop`
REM   SH_STUB_PS_AFTER = names printed by `docker ps` AFTER a stop was issued
REM                      (lets us simulate "stop returned 0 but it's still up")
if "%1"=="ps" (
    if exist "%TEMP%\sh_stub_stopped.flag" (
        if not "%SH_STUB_PS_AFTER%"=="" echo %SH_STUB_PS_AFTER%
    ) else (
        if not "%SH_STUB_PS%"=="" echo %SH_STUB_PS%
    )
    exit /b 0
)
if "%1"=="cp"   exit /b 0
if "%1"=="exec" exit /b %SH_STUB_EXEC_RC%
if "%1"=="stop" (
    if "%SH_STUB_STOP_RC%"=="0" echo. > "%TEMP%\sh_stub_stopped.flag"
    exit /b %SH_STUB_STOP_RC%
)
exit /b 0
