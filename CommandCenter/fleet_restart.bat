@echo off
REM ─────────────────────────────────────────────────────────────
REM  FLEET RESTART — reap orphans, restart cleanly, open dashboard
REM  Target of the desktop shortcut. Keeps the console open so the
REM  progress report and any failure stay readable.
REM ─────────────────────────────────────────────────────────────
title Fleet Restart
cd /d "D:\CommandCenter"

python fleet_restart.py %*
set RC=%ERRORLEVEL%

echo.
if %RC% NEQ 0 (
    echo   Restart reported a problem ^(exit %RC%^).
    echo   Check D:\CommandCenter\logs\fleet_launch.log
    echo.
)
echo   Press any key to close...
pause >nul
