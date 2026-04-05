@echo off
:loop
echo [%date% %time%] Starting Fleet Notifier...
python D:\CommandCenter\notifier.py
echo [%date% %time%] Notifier exited with code %ERRORLEVEL%, restarting in 10s...
timeout /t 10 /nobreak >nul
goto loop
