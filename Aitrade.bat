@echo off
rem ================================================================
rem  Aitrade - double-click to open the app window.
rem  First time on a new PC, run this once before:
rem    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
rem ================================================================
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Setup has not been run yet. Run this once, then start again:
  echo   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  echo.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m aitrade
