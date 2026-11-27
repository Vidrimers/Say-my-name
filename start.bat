@echo off
chcp 65001 >nul
title SAY MY NAME
cd /d "%~dp0"

REM --- Python: use py launcher if present, else python ---
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :have_python
python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :have_python

echo Python was not found.
echo Install Python 3.10+ from https://python.org
echo Enable "Add python.exe to PATH" during install.
pause
exit /b 1

:have_python
echo Using: %PY%

if not exist "venv\Scripts\python.exe" goto :make_venv
goto :deps_done

:make_venv
echo Creating virtual environment...
%PY% -m venv venv
if errorlevel 1 (
    echo Failed to create venv.
    pause
    exit /b 1
)
echo Installing packages...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r app\requirements.txt
if errorlevel 1 (
    echo Dependency install failed.
    pause
    exit /b 1
)

:deps_done
powershell -NoProfile -ExecutionPolicy Bypass -File tools\make_lnk.ps1
start "" venv\Scripts\pythonw.exe app\asr_ui.py
exit /b 0
