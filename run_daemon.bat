@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY=%CD%\.venv\Scripts\python.exe"
if exist "%PY%" (
  "%PY%" -c "import sys" >nul 2>nul
  if not errorlevel 1 goto :run
  echo [WARN] The virtual environment is broken, likely because this folder was moved.
)

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] No usable Python was found.
  echo Run setup_windows.bat to rebuild .venv.
  pause
  exit /b 1
)
set "PY=python"

:run
echo Starting finance bot with: %PY%
"%PY%" local_finance_bot.py daemon
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
