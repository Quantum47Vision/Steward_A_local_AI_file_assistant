@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Steward

echo.
echo   Steward
echo   Local file assistant  -  D: only  -  cannot delete
echo   ------------------------------------------------------------
echo.

REM ---- find python -------------------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (
  echo   Python was not found on this machine.
  echo   Install Python 3.10 or newer from python.org and tick
  echo   "Add Python to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

REM ---- first run: build the venv ------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo   First run. Building a private environment in .venv
  echo   This happens once and takes a few minutes.
  echo.
  %PY% -m venv .venv
  if errorlevel 1 (
    echo   Could not create the virtual environment.
    pause
    exit /b 1
  )
  set "FRESH=1"
)

set "VPY=.venv\Scripts\python.exe"

REM ---- install deps if missing --------------------------------------
"%VPY%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 set "FRESH=1"

if defined FRESH (
  echo   Installing dependencies...
  echo.
  "%VPY%" -m pip install --upgrade pip --quiet
  "%VPY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   Dependency install failed. Scroll up for the reason.
    pause
    exit /b 1
  )
  echo.
  echo   Done. Torch is a big download - that part is over now.
  echo.
)

REM ---- go ------------------------------------------------------------
echo   Starting. Your browser will open in a moment.
echo   Close this window to stop Steward.
echo.
"%VPY%" server.py

echo.
echo   Steward stopped.
pause
