@echo off
rem Validate both configs as a pair, then start the BUY and SELL apps together.
rem Extra arguments are passed on, e.g.:
rem   scripts\start_both.bat --check
rem   scripts\start_both.bat --test-in 60
rem   scripts\start_both.bat --loop
setlocal
cd /d "%~dp0.."
set RC=1
if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. Run first:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  goto end
)
".venv\Scripts\python.exe" run_pair.py --buy config\buy.yaml --sell config\sell.yaml %*
set RC=%ERRORLEVEL%
:end
if not defined HEDGEBOT_NOPAUSE pause
exit /b %RC%
