"""One-shot: write tools/release.bat with CRLF (not committed)."""
from pathlib import Path

out = Path(__file__).resolve().parent / "release.bat"

lines = r"""
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

echo.
echo === SAY MY NAME - release ===
echo.

if not exist "app\version.py" (
    echo version.py not found
    pause
    exit /b 1
)

for /f "usebackq tokens=2 delims==" %%A in (`type "app\version.py" ^| findstr /C:"__version__ ="`) do set "RAW=%%A"
set "RAW=%RAW:"=%"
set "RAW=%RAW: =%"
for /f "tokens=1-3 delims=." %%A in ("%RAW%") do (
    set "Y=%%A"
    set "M=%%B"
    set "N=%%C"
)
set /a NEXT_N=N+1
set "NEXT=%Y%.%M%.%NEXT_N%"

echo Current version: %RAW%
echo Next version:    %NEXT%
echo.

set "USE=%NEXT%"
set /p "OK=Use %NEXT%? [Y/n] "
if /i "%OK%"=="n" set /p "USE=Enter version: "

echo.
set "MSG=Release %USE%"
set /p "MSG=Commit message [%MSG%]: "

echo.
echo Bumping version to %USE%...
python tools/bump_version.py %USE%

echo Committing...
git add -A
git commit -m "%MSG%"

echo Pushing...
git push

echo Tagging %USE%...
git tag %USE%
git push origin %USE%

echo.
echo Done. GitHub Actions will build zip and create the release.
echo.
pause
""".lstrip("\n")

out.write_bytes(lines.replace("\n", "\r\n").encode("utf-8"))
print("wrote", out)
