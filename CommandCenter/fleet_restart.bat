@echo off
REM ============================================================
REM   FLEET RESTART
REM   Reap orphans, restart the fleet cleanly, open the dashboard.
REM   Target of the "Fleet Restart" desktop shortcut.
REM
REM   NOTE: ASCII only in this file. cmd.exe parses REM lines with
REM   non-ASCII box-drawing characters as commands and errors on them.
REM ============================================================
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
