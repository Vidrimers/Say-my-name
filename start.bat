@echo off
chcp 65001 >nul
title SAY MY NAME
cd /d "%~dp0"

REM --- required files ---
if not exist "app\asr_ui.py" goto :missing
if not exist "app\requirements.txt" goto :missing
if not exist "start.vbs" goto :missing
if not exist "make_lnk.ps1" goto :missing
goto :files_ok

:missing
echo.
echo ERROR: some application files are missing.
echo Download the latest zip from:
echo   https://github.com/Vidrimers/Say-my-name/releases
echo and extract it into this folder.
echo.
pause
exit /b 1

:files_ok
REM --- find Python ---
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

set "REQ=app\requirements.txt"
if not exist "venv\Scripts\python.exe" goto :make_venv

echo Checking packages...
venv\Scripts\python.exe -m pip install -r "%REQ%" >nul 2>nul
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
venv\Scripts\python.exe -m pip install -r "%REQ%"
if errorlevel 1 (
    echo Dependency install failed.
    pause
    exit /b 1
)

:deps_done
powershell -NoProfile -ExecutionPolicy Bypass -File make_lnk.ps1
start "" venv\Scripts\pythonw.exe app\asr_ui.py
exit /b 0
