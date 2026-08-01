@echo off
cd /d "%~dp0"
title Steward - GPU torch
echo.
echo   Installing the CUDA build of PyTorch.
echo   Only run this if you have an NVIDIA card.
echo   The default install in start.bat is CPU-only.
echo.
if not exist ".venv\Scripts\python.exe" (
  echo   Run start.bat once first to create the environment.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip uninstall -y torch
.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu121
echo.
echo   Done. Start Steward normally with start.bat
pause
