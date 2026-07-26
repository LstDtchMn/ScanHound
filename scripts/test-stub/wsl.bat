@echo off
if "%1"=="--list" (
    if not "docker-desktop"=="" echo docker-desktop
    exit /b 0
)
exit /b 2
