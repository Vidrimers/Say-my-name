@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo === Install PyTorch into venv ===
echo ~200-300 MB (CPU) or up to ~2.5 GB (CUDA).
echo.

if not exist "venv\Scripts\python.exe" (
    echo venv not found. Run start.bat first.
    pause
    exit /b 1
)

venv\Scripts\python.exe -m pip install torch

if errorlevel 1 (
    echo.
    echo Install failed. Check your internet and try again.
    pause
    exit /b 1
)

echo.
echo Done.
echo.
pause
