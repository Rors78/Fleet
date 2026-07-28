@echo off
REM Clean-restart GoldenEye. Optional: restart_goldeneye.bat -Mode paper
title GoldenEye - Clean Restart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\GoldenEye\restart_goldeneye.ps1" %*
