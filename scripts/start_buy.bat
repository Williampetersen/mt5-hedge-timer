@echo off
rem Start the BUY app. Extra arguments are passed on, e.g.:
rem   scripts\start_buy.bat --check
rem   scripts\start_buy.bat --test-in 60
setlocal
cd /d "%~dp0.."
set RC=1
if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run first:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  goto end
)
".venv\Scripts\python.exe" -m hedgebot --config config\buy.yaml %*
set RC=%ERRORLEVEL%
:end
if not defined HEDGEBOT_NOPAUSE pause
exit /b %RC%
