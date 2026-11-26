@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting local Whisper ASR...
python asr.py
pause
