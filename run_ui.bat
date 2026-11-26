@echo off
REM Prefer "SAY MY NAME.lnk" or start.vbs — no ConEmu tab / no "Press Enter".
start "" wscript.exe "%~dp0start.vbs"
exit /b 0
