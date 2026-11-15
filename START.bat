@echo off
rem ================================================================
rem  Trade2026 - double-click to start BOTH apps (BUY + SELL).
rem  They wait, then open at the scheduled time in config\buy.yaml
rem  and config\sell.yaml (default 14:29:45 Copenhagen, Mon-Fri).
rem  Keep this window open until the trade has opened.
rem ================================================================
title Trade2026 - BUY + SELL
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setup has not been run yet. Run this once, then start again:
  echo   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  echo.
  pause
  exit /b 1
)
echo Starting BUY and SELL. Both wait for the scheduled time, then open.
echo Keep this window open. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" run_pair.py --buy config\buy.yaml --sell config\sell.yaml %*
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (echo Finished OK.) else (echo Finished with exit code %RC% - see the messages above and the logs folder.)
pause
exit /b %RC%
