@echo off
chcp 65001 >nul
title SAY MY NAME
cd /d "%~dp0"

REM --- find a real Python (not the Windows Store stub) ---
set "PY="
where python >nul 2>nul && for /f "delims=" %%I in ('where python') do (
    "%%I" -c "import sys" >nul 2>nul && if not defined PY set "PY=%%I"
)
where py >nul 2>nul && if not defined PY (
    py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo Python was not found.
    echo Install Python 3.10+ from https://python.org
    echo During install, enable "Add python.exe to PATH" and keep pip enabled.
    pause
    exit /b 1
)

echo Using: %PY%

REM --- venv + dependencies (first run) ---
if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv venv
    if errorlevel 1 (
        echo Failed to create venv.
        pause
        exit /b 1
    )
    echo Installing packages (first time, may take a few minutes)...
    venv\Scripts\python.exe -m pip install --upgrade pip
    venv\Scripts\python.exe -m pip install -r app\requirements.txt
    if errorlevel 1 (
        echo Dependency install failed.
        pause
        exit /b 1
    )
    echo Done.
) else (
    venv\Scripts\python.exe -m pip install -r app\requirements.txt >nul 2>nul
)

REM --- launch UI ---
start "" venv\Scripts\pythonw.exe app\asr_ui.py
if errorlevel 1 (
    echo Launch failed, retrying with console...
    venv\Scripts\python.exe app\asr_ui.py
    pause
)
