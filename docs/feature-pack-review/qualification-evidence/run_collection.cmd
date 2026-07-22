@echo off
REM Recurring RSS-shadow evidence collection for the ScanHound qualification window.
REM Registered as a Windows Scheduled Task; run manually any time to collect on demand.
"C:\Users\NLSur\AppData\Local\Programs\Python\Python312\python.exe" "X:\Docker Apps\scanhound-qualification-evidence\collect_shadow_evidence.py"
exit /b %ERRORLEVEL%
