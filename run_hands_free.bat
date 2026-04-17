@echo off
rem Launcher for Windows (cmd / PowerShell both OK).
setlocal
cd /d "%~dp0"
python hands_free_voice.py %*
endlocal
